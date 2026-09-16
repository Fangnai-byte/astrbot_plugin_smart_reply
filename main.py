#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能自动回复（Smart Reply）—— AstrBot 插件

监测到指定内容，就发送指定内容，可附带一张图片，并带冷却防刷屏；
只有放行的群 / 人会触发，白名单没配好就不会乱回复。

配置里的规则写法（每条一行）::

    触发词，回复内容，图片，冷却秒数，生效群号，生效QQ号

例如::

    姆唔，姆唔，tu_1                   # 监测到「姆唔」→ 回复「姆唔」并附图片 tu_1
    早安，早安哦                      # 只发文字
    晚安，，tu_2，120                 # 只发图，冷却 120 秒
    签到，签到成功，tu_3，60，123456  # 只在 123456 这个群里触发
    摸头，好啦好啦，，0，123456+789   # 只在两个群里，且只对 789 这个人触发

图片只写图名即可，默认去 ``/root/AstrBot/data/workspaces/tu`` 里找，
自动补扩展名（tu_1 → tu_1.png）；也支持绝对路径和 http(s) 链接。

除关键词规则外，还可以打开「语境判断回复」：没命中关键词时，先按消息频次粗筛
（太冷清 / 刷屏都不打扰模型），只有灰区才问一次 LLM，同一串消息复用上次结论，
被无视则自动降频（详见 context_judge.py）。
"""
import os
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star

try:  # 兼容包 / 非包两种加载方式
    from .reply_core import (DEFAULT_IMAGE_DIR, CooldownTracker, RecentMessages,
                             appeared_above, check_target, match_keyword,
                             normalize_target_mode, parse_id_set, parse_rules,
                             resolve_image, rule_allows)
except ImportError:  # pragma: no cover
    from reply_core import (DEFAULT_IMAGE_DIR, CooldownTracker, RecentMessages,
                            appeared_above, check_target, match_keyword,
                            normalize_target_mode, parse_id_set, parse_rules,
                            resolve_image, rule_allows)

try:
    from .context_judge import (COARSE_HIGH, COARSE_LOW, JUDGE_SYSTEM_PROMPT,
                                BackoffTracker, FreqFilter, Verdict,
                                VerdictCache, build_judge_prompt,
                                cooldown_multiplier, parse_judge_reply,
                                scaled_min_msgs, summarize)
except ImportError:  # pragma: no cover
    from context_judge import (COARSE_HIGH, COARSE_LOW, JUDGE_SYSTEM_PROMPT,
                               BackoffTracker, FreqFilter, Verdict,
                               VerdictCache, build_judge_prompt,
                               cooldown_multiplier, parse_judge_reply,
                               scaled_min_msgs, summarize)

VALID_SCOPES = ("group", "user", "global")


class ReplyScopePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.cooldown = CooldownTracker()
        self.recent = RecentMessages()
        self.freq = FreqFilter()
        self.verdicts = VerdictCache(ttl=self._ctx_cache_ttl(),
                                     span=self._ctx_cache_span())
        self.backoff = BackoffTracker(ack_seconds=self._ctx_ack_seconds(),
                                      miss_limit=self._ctx_ignore_limit(),
                                      max_level=self._ctx_max_level())
        self._last_llm_at: dict = {}
        self._ctx_warned = False
        self.rules = []
        self._rules_sig = None
        self._load_rules()

    # ---------------- 配置读取 ----------------
    def _load_rules(self) -> None:
        raw = self.config.get("rules", []) or []
        self._rules_sig = repr(raw)
        rules, errors = parse_rules(raw, self._default_match_type())
        self.rules = rules
        for err in errors:
            logger.warning(f"[ReplyScope] {err}")
        logger.info(f"[ReplyScope] 已加载 {len(self.rules)} 条关键词规则")

    def _default_match_type(self) -> str:
        mt = str(self.config.get("match_type", "contains") or "contains").strip()
        return mt if mt in ("contains", "exact", "regex") else "contains"

    def _default_cooldown(self) -> float:
        try:
            return max(0.0, float(self.config.get("cooldown_seconds", 30) or 0))
        except (TypeError, ValueError):
            return 30.0

    def _cooldown_scope(self) -> str:
        scope = str(self.config.get("cooldown_scope", "group") or "group").strip()
        return scope if scope in VALID_SCOPES else "group"

    def _image_dir(self) -> str:
        return str(self.config.get("image_dir") or DEFAULT_IMAGE_DIR).strip() or DEFAULT_IMAGE_DIR

    def _bool(self, key: str, default: bool = True) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(val)

    def _no_quote(self) -> bool:
        """是否绕过框架的引用回复装饰（只影响本插件的消息）。"""
        return self._bool("no_quote", True)

    # -------- 「上面已有触发词就不发」的配置 --------
    def _dedup_enabled(self) -> bool:
        return self._bool("dedup_enabled", True)

    def _dedup_window(self) -> int:
        try:
            return max(1, int(self.config.get("dedup_window", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _dedup_seconds(self) -> float:
        try:
            return max(0.0, float(self.config.get("dedup_seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _dedup_same_user_only(self) -> bool:
        """默认 False：群里别人上面说过同样的触发词，也算「上面已有」。"""
        return self._bool("dedup_same_user_only", False)

    # -------- 语境判断（关键词都没命中时，判断此刻插话是否合适） --------
    def _ctx_enabled(self) -> bool:
        """总开关，默认关闭：打开后才会在没命中关键词时问模型。"""
        return self._bool("context_judge_enabled", False)

    def _ctx_num(self, key: str, default, low=0, high=None, cast=int):
        try:
            val = cast(self.config.get(key, default))
        except (TypeError, ValueError):
            val = cast(default)
        val = max(low, val)
        return min(high, val) if high is not None else val

    def _ctx_window(self) -> float:
        """粗筛看最近多少秒的消息。"""
        return self._ctx_num("ctx_window_seconds", 120, 5, 3600, float)

    def _ctx_min_msgs(self) -> int:
        """窗口内至少这么多条消息才考虑插话（太冷清就别打扰）？"""
        return self._ctx_num("ctx_min_msgs", 3, 1, 100)

    def _ctx_spam_msgs(self) -> int:
        """窗口内超过这么多条就当作刷屏，先不插话；0 表示不判断刷屏。"""
        return self._ctx_num("ctx_spam_msgs", 0, 0, 1000)

    def _ctx_level_step(self) -> int:
        """每升一档降频，粗筛下限抬高几条。"""
        return self._ctx_num("ctx_level_step", 2, 0, 20)

    def _ctx_probe_msgs(self) -> int:
        """发给模型看最近几条消息（越少越省钱）。"""
        return self._ctx_num("ctx_probe_msgs", 12, 2, 40)

    def _ctx_cache_ttl(self) -> float:
        return self._ctx_num("ctx_cache_ttl", 180, 1, 3600, float)

    def _ctx_cache_span(self) -> int:
        """算缓存签名时取最近几条消息。"""
        return self._ctx_num("ctx_cache_span", 8, 1, 40)

    def _ctx_min_interval(self) -> float:
        """同一个会话两次问模型的最小间隔秒数（0 表示不限制）。"""
        return self._ctx_num("ctx_min_interval", 20, 0, 3600, float)

    def _ctx_ack_seconds(self) -> float:
        """回复后多少秒内没人接话，算被无视。"""
        return self._ctx_num("ctx_ack_seconds", 60, 1, 3600, float)

    def _ctx_ignore_limit(self) -> int:
        """连续被无视几次开始降频。"""
        return self._ctx_num("ctx_ignore_limit", 2, 1, 20)

    def _ctx_max_level(self) -> int:
        return self._ctx_num("ctx_max_level", 4, 1, 10)

    def _ctx_backoff_base(self) -> float:
        return self._ctx_num("ctx_backoff_base", 2.0, 1.0, 10.0, float)

    def _ctx_backoff_cap(self) -> float:
        return self._ctx_num("ctx_backoff_cap", 8.0, 1.0, 100.0, float)

    def _ctx_cooldown(self) -> float:
        """语境回复的基础冷却秒数（会按降频档位放大）。"""
        return self._ctx_num("ctx_cooldown_seconds", 120, 0, 86400, float)

    def _ctx_max_reply_chars(self) -> int:
        return self._ctx_num("ctx_reply_max_chars", 120, 4, 500)

    def _ctx_bot_name(self) -> str:
        return str(self.config.get("ctx_bot_name", "") or "").strip() or "机器人"

    def _ctx_fallback_reply(self) -> str:
        """模型只说 YES 没给内容时的兜底话术；留空则不发送。"""
        return str(self.config.get("ctx_fallback_reply", "") or "").strip()

    def _ctx_prompt(self) -> str:
        return str(self.config.get("ctx_prompt", "") or "")

    def _ctx_system_prompt(self) -> str:
        return str(self.config.get("ctx_system_prompt", "") or "").strip() or JUDGE_SYSTEM_PROMPT

    def _session_key(self, event, group_id: str, sender_id: str) -> str:
        """会话标识：群聊按群、私聊按人；优先用框架给的 umo。"""
        return str(getattr(event, "unified_msg_origin", "") or group_id or sender_id)

    def _above_texts(self, session_key: str, sender_id: str) -> list:
        """取「上面」的消息文本，用于判断触发词是不是刚出现过。"""
        if not self._dedup_enabled():
            return []
        return self.recent.above(
            session_key,
            sender_id,
            window=self._dedup_window(),
            seconds=self._dedup_seconds(),
            same_user_only=self._dedup_same_user_only(),
        )

    def _id_list(self, key: str) -> set[str]:
        return parse_id_set(self.config.get(key, []) or [])

    # -------- 触发范围（白名单 / 黑名单） --------
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
                f"[ReplyScope] {scope} {target_id or '(未知)'} 不在触发范围：{reason}"
            )
        return ok

    def _ensure_rules(self) -> None:
        """配置在 WebUI 改动后，无需重载插件也能生效。"""
        if repr(self.config.get("rules", []) or []) != self._rules_sig:
            self._load_rules()

    def _cooldown_key(self, index: int, rule, group_id: str, sender_id: str) -> str:
        scope = self._cooldown_scope()
        if scope == "user":
            tail = sender_id
        elif scope == "global":
            tail = "global"
        else:
            tail = group_id or sender_id
        return f"{scope}:{index}:{rule.keyword}:{tail}"

    # -------- 语境判断：被点名就不用粗筛 --------
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

    async def _ask_llm(self, event, messages, umo: str):
        """问一次模型：现在插话合适吗。失败一律当作「不回复」。"""
        try:
            provider = self.context.get_using_provider(umo)
        except Exception as e:
            if not self._ctx_warned:
                self._ctx_warned = True
                logger.warning(f"[ReplyScope] 取 LLM 提供商出错：{e}")
            return Verdict(False, "", "no-provider")
        if provider is None:
            if not self._ctx_warned:
                self._ctx_warned = True
                logger.warning("[ReplyScope] 没有可用的 LLM 提供商，语境判断暂时跳过。")
            return Verdict(False, "", "no-provider")

        prompt = build_judge_prompt(
            messages,
            bot_name=self._ctx_bot_name(),
            session="群聊" if event.get_group_id() else "私聊",
            template=self._ctx_prompt(),
        )
        try:
            # 用独立的 session_id，别把判断过程和正常对话记到一起
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"{umo or 'reply-scope'}:replyscope-judge",
                system_prompt=self._ctx_system_prompt(),
            )
        except Exception as e:
            logger.warning(f"[ReplyScope] 语境判断调用失败：{e}")
            return Verdict(False, "", "llm-error")

        raw = str(getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            return Verdict(False, "", "llm-empty")
        verdict = parse_judge_reply(raw, max_reply_chars=self._ctx_max_reply_chars())
        logger.debug(
            f"[ReplyScope] 语境判断（{verdict.source}）→ "
            f"{summarize(messages, self._ctx_bot_name())}"
        )
        return verdict

    async def _context_reply(self, event, session_key: str, is_group: bool):
        """关键词都没命中时的兜底：频次粗筛 → 缓存 → 问模型 → 发送。"""
        now = time.monotonic()
        if not is_group and not self._bool("ctx_private_enabled", False):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or session_key)
        level = self.backoff.level(session_key, now=now)
        coarse, info = self.freq.coarse(
            session_key,
            window=self._ctx_window(),
            min_msgs=scaled_min_msgs(self._ctx_min_msgs(), level, self._ctx_level_step()),
            spam_msgs=self._ctx_spam_msgs(),
            addressed=self._is_addressed(event),
            now=now,
        )
        if coarse == COARSE_LOW:
            logger.debug(f"[ReplyScope] 语境判断跳过：{info.get('reason')}")
            return

        cd = self._ctx_cooldown() * cooldown_multiplier(
            level, self._ctx_backoff_base(), self._ctx_backoff_cap()
        )
        key = f"ctx:{session_key}"
        if not self.cooldown.allow(key, cd, now=now):
            logger.debug(
                f"[ReplyScope] 语境回复冷却中，剩余 {self.cooldown.remaining(key, now=now):.1f}s"
            )
            return

        window = [
            (s, t)
            for s, t, _ts in self.freq.recent(
                session_key, window=self._ctx_window(),
                limit=self._ctx_probe_msgs(), now=now,
            )
        ]
        if not window:
            return

        verdict = self.verdicts.get(session_key, window, now=now)
        if verdict is None:
            gap = self._ctx_min_interval()
            if gap > 0 and now - self._last_llm_at.get(session_key, 0.0) < gap:
                logger.debug("[ReplyScope] 本会话刚问过模型，先省着点")
                return
            self._last_llm_at[session_key] = now
            verdict = await self._ask_llm(event, window, umo)
            self.verdicts.put(session_key, window, verdict, now=now)

        if not verdict.should_reply:
            logger.debug(f"[ReplyScope] 语境判断不回复（{verdict.source}）")
            return

        reply = (verdict.reply or "").strip() or self._ctx_fallback_reply()
        if not reply:
            logger.debug("[ReplyScope] 语境判断说可以回，但没给内容，跳过")
            return

        self.cooldown.touch(key, cd, now=now)
        self.cooldown.cleanup(now=now)
        self.verdicts.cleanup(now=now)
        self.backoff.note_reply(session_key, now=now)
        logger.info(f"[ReplyScope] 语境回复已发送（{verdict.source}，降频档位 {level}）")
        chain = [Plain(reply)]
        if self._no_quote():
            await event.send(MessageChain(chain=chain))
        else:
            yield event.chain_result(chain)

    # ---------------- 消息监听 ----------------
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            self._ensure_rules()
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            sender_id = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            group_id = str(event.get_group_id() or "")
            is_group = bool(group_id)

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

            # 「上面」的消息先取出来（不含本条），再把本条记进去
            session_key = self._session_key(event, group_id, sender_id)
            above = self._above_texts(session_key, sender_id)
            self.recent.remember(session_key, sender_id, text)

            ctx_on = self._ctx_enabled()
            if ctx_on:
                # 频次统计与「有没有人接话」的判定，都要看到每一条消息
                self.freq.observe(session_key, sender_id, text)
                if self.backoff.observe(session_key):
                    logger.debug("[ReplyScope] 有人接话，降频档位已清零")

            for index, rule in enumerate(self.rules):
                if not rule.has_content or not match_keyword(text, rule):
                    continue

                # 规则自己的生效范围（群号 / QQ 号）也要放行
                allowed, reason = rule_allows(rule, group_id, sender_id)
                if not allowed:
                    logger.debug(f"[ReplyScope] 规则「{rule.keyword}」{reason}，跳过")
                    continue

                # 上面已经出现过同一个触发词，就不再重复回复
                if appeared_above(above, rule):
                    logger.debug(
                        f"[ReplyScope] 规则「{rule.keyword}」上面已出现过，跳过"
                    )
                    continue

                cd = rule.cooldown if rule.cooldown is not None else self._default_cooldown()
                key = self._cooldown_key(index, rule, group_id, sender_id)
                if not self.cooldown.allow(key, cd):
                    logger.debug(
                        f"[ReplyScope] 规则「{rule.keyword}」冷却中，"
                        f"剩余 {self.cooldown.remaining(key):.1f}s"
                    )
                    hint = str(self.config.get("cooldown_hint", "") or "").strip()
                    if hint and self.cooldown.allow(key + ":hint", cd):
                        self.cooldown.touch(key + ":hint", cd)
                        if self._no_quote():
                            await event.send(MessageChain(chain=[Plain(hint)]))
                        else:
                            yield event.plain_result(hint)
                    return

                chain = []
                if rule.reply:
                    chain.append(Plain(rule.reply))

                if rule.image:
                    resolved = resolve_image(rule.image, self._image_dir())
                    if resolved is None:
                        logger.warning(
                            f"[ReplyScope] 规则「{rule.keyword}」的图片找不到：{rule.image}"
                        )
                    elif resolved[0] == "url":
                        chain.append(Image.fromURL(resolved[1]))
                    else:
                        chain.append(Image.fromFileSystem(resolved[1]))

                if not chain:
                    return

                self.cooldown.touch(key, cd)
                self.cooldown.cleanup()
                logger.info(f"[ReplyScope] 命中规则「{rule.keyword}」→ 已发送")
                if self._no_quote():
                    # 直接发送，绕过框架 result_decorate 的「引用原消息」装饰
                    await event.send(MessageChain(chain=chain))
                else:
                    yield event.chain_result(chain)
                return  # 一条消息只触发第一条命中的规则

            # 关键词都没命中：按需判断此刻主动插话是否合适
            if ctx_on:
                async for result in self._context_reply(event, session_key, is_group):
                    yield result
        except Exception as e:
            logger.error(f"[ReplyScope] 处理消息异常: {e}")
