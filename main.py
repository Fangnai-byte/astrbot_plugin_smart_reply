#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能自动回复（Smart Reply）—— AstrBot 插件

它不是一个「关键词→回复」插件，而是让机器人在**合适的时候自己接一句话**：

1. 每条消息先过触发范围：群聊 / 私聊开关，用户与群的白名单、黑名单；
2. 再用消息**频次粗筛**：太冷清、正在刷屏都不打扰，也不花 token；
3. 只有落在灰区的消息串才问一次模型「现在插话合适吗」，合适就回一句；
4. 同一串消息判过就复用结论（见 ``context_judge.VerdictCache``）；
5. 回复后没人接话就算被无视，连续被无视自动降频，有人接话立刻恢复。

只想在特定群生效：把 ``group_target_mode`` 设为 ``whitelist``，
再把群号填进 ``group_whitelist``，其余群完全不参与。
"""
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

try:  # 兼容包 / 非包两种加载方式
    from .guard import (CooldownTracker, check_target, normalize_target_mode,
                        parse_id_set)
except ImportError:  # pragma: no cover
    from guard import (CooldownTracker, check_target, normalize_target_mode,
                       parse_id_set)

try:
    from .context_judge import (COARSE_LOW, JUDGE_SYSTEM_PROMPT,
                                BackoffTracker, FreqFilter, Verdict,
                                VerdictCache, build_judge_prompt,
                                cooldown_multiplier, parse_judge_reply,
                                scaled_min_msgs, summarize)
except ImportError:  # pragma: no cover
    from context_judge import (COARSE_LOW, JUDGE_SYSTEM_PROMPT,
                               BackoffTracker, FreqFilter, Verdict,
                               VerdictCache, build_judge_prompt,
                               cooldown_multiplier, parse_judge_reply,
                               scaled_min_msgs, summarize)


class SmartReplyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.cooldown = CooldownTracker()
        self.freq = FreqFilter()
        self.verdicts = VerdictCache(ttl=self._cache_ttl(), span=self._cache_span())
        self.backoff = BackoffTracker(ack_seconds=self._ack_seconds(),
                                      miss_limit=self._ignore_limit(),
                                      max_level=self._max_level())
        self._last_llm_at: dict = {}
        self._warned = False

    # ---------------- 配置读取 ----------------
    def _bool(self, key: str, default: bool = True) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(val)

    def _num(self, key: str, default, low=0, high=None, cast=int):
        try:
            val = cast(self.config.get(key, default))
        except (TypeError, ValueError):
            val = cast(default)
        val = max(low, val)
        return min(high, val) if high is not None else val

    def _enabled(self) -> bool:
        """总开关：关闭后本插件完全不参与。"""
        return self._bool("enabled", True)

    def _no_quote(self) -> bool:
        """是否绕过框架的引用回复装饰（只影响本插件发出的消息）。"""
        return self._bool("no_quote", True)

    # -------- 频次粗筛 --------
    def _window(self) -> float:
        """粗筛看最近多少秒的消息。"""
        return self._num("window_seconds", 120, 5, 3600, float)

    def _min_msgs(self) -> int:
        """窗口内至少这么多条消息才考虑插话（太冷清就别打扰）。"""
        return self._num("min_msgs", 3, 1, 100)

    def _spam_msgs(self) -> int:
        """窗口内超过这么多条就当作刷屏，先不插话；0 表示不判断刷屏。"""
        return self._num("spam_msgs", 0, 0, 1000)

    def _probe_msgs(self) -> int:
        """发给模型看最近几条消息（越少越省钱）。"""
        return self._num("probe_msgs", 12, 2, 40)

    # -------- 复用与节流 --------
    def _cache_ttl(self) -> float:
        return self._num("cache_ttl", 180, 1, 3600, float)

    def _cache_span(self) -> int:
        """算缓存签名时取最近几条消息。"""
        return self._num("cache_span", 8, 1, 40)

    def _min_interval(self) -> float:
        """同一个会话两次问模型的最小间隔秒数（0 表示不限制）。"""
        return self._num("min_interval", 20, 0, 3600, float)

    def _cooldown(self) -> float:
        """自动回复的基础冷却秒数（会按降频档位放大）。"""
        return self._num("cooldown_seconds", 120, 0, 86400, float)

    # -------- 被无视降频 --------
    def _ack_seconds(self) -> float:
        """回复后多少秒内没人接话，算被无视。"""
        return self._num("ack_seconds", 60, 1, 3600, float)

    def _ignore_limit(self) -> int:
        """连续被无视几次开始降频。"""
        return self._num("ignore_limit", 2, 1, 20)

    def _max_level(self) -> int:
        return self._num("max_level", 4, 1, 10)

    def _level_step(self) -> int:
        """每升一档降频，粗筛下限抬高几条。"""
        return self._num("level_step", 2, 0, 20)

    def _backoff_base(self) -> float:
        return self._num("backoff_base", 2.0, 1.0, 10.0, float)

    def _backoff_cap(self) -> float:
        return self._num("backoff_cap", 8.0, 1.0, 100.0, float)

    # -------- 判断与回复 --------
    def _reply_max_chars(self) -> int:
        return self._num("reply_max_chars", 120, 4, 500)

    def _bot_name(self) -> str:
        return str(self.config.get("bot_name", "") or "").strip() or "机器人"

    def _fallback_reply(self) -> str:
        """模型只说 YES 没给内容时的兜底话术；留空则不发送。"""
        return str(self.config.get("fallback_reply", "") or "").strip()

    def _prompt(self) -> str:
        return str(self.config.get("judge_prompt", "") or "")

    def _system_prompt(self) -> str:
        return str(self.config.get("judge_system_prompt", "") or "").strip() or JUDGE_SYSTEM_PROMPT

    # -------- 触发范围（白名单 / 黑名单） --------
    def _id_list(self, key: str) -> set[str]:
        return parse_id_set(self.config.get(key, []) or [])

    def _target_mode(self, scope: str) -> str:
        return normalize_target_mode(self.config.get(f"{scope}_target_mode", "off"))

    def _check_target(self, scope: str, target_id: str) -> bool:
        """群 / 用户两级触发范围校验，返回是否放行。

        黑名单 = 新增的 ``{scope}_blacklist`` 与旧的 ``ignore_{scope}_ids`` 合并，
        旧配置照旧生效。
        """
        ok, reason = check_target(
            target_id,
            mode=self._target_mode(scope),
            whitelist=self._id_list(f"{scope}_whitelist"),
            blacklist=self._id_list(f"{scope}_blacklist") | self._id_list(f"ignore_{scope}_ids"),
        )
        if not ok:
            logger.debug(
                f"[SmartReply] {scope} {target_id or '(未知)'} 不在触发范围：{reason}"
            )
        return ok

    def _session_key(self, event, group_id: str, sender_id: str) -> str:
        """会话标识：群聊按群、私聊按人；优先用框架给的 umo。"""
        return str(getattr(event, "unified_msg_origin", "") or group_id or sender_id)

    # -------- 被点名就不用粗筛 --------
    def _is_addressed(self, event) -> bool:
        try:
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return True
        except Exception:
            pass
        try:
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(getattr(comp, "qq", "") or "") in (self_id, "all"):
                    return True
        except Exception:
            pass
        return False

    async def _ask_llm(self, event, messages, umo: str) -> Verdict:
        """问一次模型：现在插话合适吗。失败一律当作「不回复」。"""
        try:
            provider = self.context.get_using_provider(umo)
        except Exception as e:
            if not self._warned:
                self._warned = True
                logger.warning(f"[SmartReply] 取 LLM 提供商出错：{e}")
            return Verdict(False, "", "no-provider")
        if provider is None:
            if not self._warned:
                self._warned = True
                logger.warning("[SmartReply] 没有可用的 LLM 提供商，自动回复暂时跳过。")
            return Verdict(False, "", "no-provider")

        prompt = build_judge_prompt(
            messages,
            bot_name=self._bot_name(),
            session="群聊" if event.get_group_id() else "私聊",
            template=self._prompt(),
        )
        try:
            # 用独立的 session_id，别把判断过程和正常对话记到一起
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"{umo or 'smart-reply'}:smart-reply-judge",
                system_prompt=self._system_prompt(),
            )
        except Exception as e:
            logger.warning(f"[SmartReply] 判断调用失败：{e}")
            return Verdict(False, "", "llm-error")

        raw = str(getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            return Verdict(False, "", "llm-empty")
        verdict = parse_judge_reply(raw, max_reply_chars=self._reply_max_chars())
        logger.debug(
            f"[SmartReply] 判断结果（{verdict.source}）→ "
            f"{summarize(messages, self._bot_name())}"
        )
        return verdict

    async def _maybe_reply(self, event, session_key: str, is_group: bool):
        """频次粗筛 → 缓存 → 问模型 → 发送。"""
        now = time.monotonic()
        umo = str(getattr(event, "unified_msg_origin", "") or session_key)
        level = self.backoff.level(session_key, now=now)
        coarse, info = self.freq.coarse(
            session_key,
            window=self._window(),
            min_msgs=scaled_min_msgs(self._min_msgs(), level, self._level_step()),
            spam_msgs=self._spam_msgs(),
            addressed=self._is_addressed(event),
            now=now,
        )
        if coarse == COARSE_LOW:
            logger.debug(f"[SmartReply] 粗筛跳过：{info.get('reason')}")
            return

        cd = self._cooldown() * cooldown_multiplier(
            level, self._backoff_base(), self._backoff_cap()
        )
        key = f"reply:{session_key}"
        if not self.cooldown.allow(key, cd, now=now):
            logger.debug(
                f"[SmartReply] 冷却中，剩余 {self.cooldown.remaining(key, now=now):.1f}s"
            )
            return

        window = [
            (s, t)
            for s, t, _ts in self.freq.recent(
                session_key, window=self._window(),
                limit=self._probe_msgs(), now=now,
            )
        ]
        if not window:
            return

        verdict = self.verdicts.get(session_key, window, now=now)
        if verdict is None:
            gap = self._min_interval()
            if gap > 0 and now - self._last_llm_at.get(session_key, 0.0) < gap:
                logger.debug("[SmartReply] 本会话刚问过模型，先省着点")
                return
            self._last_llm_at[session_key] = now
            verdict = await self._ask_llm(event, window, umo)
            self.verdicts.put(session_key, window, verdict, now=now)

        if not verdict.should_reply:
            logger.debug(f"[SmartReply] 判断为不回复（{verdict.source}）")
            return

        reply = (verdict.reply or "").strip() or self._fallback_reply()
        if not reply:
            logger.debug("[SmartReply] 判断为可以回，但没给内容，跳过")
            return

        self.cooldown.touch(key, cd, now=now)
        self.cooldown.cleanup(now=now)
        self.verdicts.cleanup(now=now)
        self.backoff.note_reply(session_key, now=now)
        logger.info(f"[SmartReply] 已自动回复（{verdict.source}，降频档位 {level}）")
        chain = [Plain(reply)]
        if self._no_quote():
            # 直接发送，绕过框架 result_decorate 的「引用原消息」装饰
            await event.send(MessageChain(chain=chain))
        else:
            yield event.chain_result(chain)

    # ---------------- 消息监听 ----------------
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if not self._enabled():
                return
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            sender_id = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            group_id = str(event.get_group_id() or "")
            is_group = bool(group_id)

            # 群聊、私聊开关
            if is_group and not self._bool("enable_group", True):
                return
            if not is_group and not self._bool("enable_private", True):
                return

            # 机器人自己不发；用户 / 群按各自的触发范围过滤
            if self._bool("ignore_bot", True) and sender_id and sender_id == self_id:
                return
            if not self._check_target("user", sender_id):
                return
            if is_group and not self._check_target("group", group_id):
                return

            session_key = self._session_key(event, group_id, sender_id)
            # 频次统计与「有没有人接话」的判定，都要看到每一条消息
            self.freq.observe(session_key, sender_id, text)
            if self.backoff.observe(session_key):
                logger.debug("[SmartReply] 有人接话，降频档位已清零")

            async for result in self._maybe_reply(event, session_key, is_group):
                yield result
        except Exception as e:
            logger.error(f"[SmartReply] 处理消息异常: {e}")
