#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能自动回复（Smart Reply）—— AstrBot 插件

它不是一个「关键词→回复」插件，而是让机器人在**合适的时候自己接一句话**：

1. 私聊一律跳过；群聊消息先过触发范围：群开关，用户与群的白名单、黑名单；
2. 再用消息**频次粗筛**：太冷清、正在刷屏都不打扰，也不花 token；
3. 只有落在灰区的消息串才问一次模型「现在插话合适吗」，合适就回一句；
4. 同一串消息判过就复用结论（见 ``context_judge.VerdictCache``）；
5. 回复后没人接话就算被无视，连续被无视自动降频，有人接话立刻恢复。

只想在特定群生效：把 ``group_target_mode`` 设为 ``whitelist``，
再把群号填进 ``group_whitelist``，其余群完全不参与。
"""
import inspect
import json
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

#: 这些 source 说明「这次根本没问到模型」，结论不能进缓存，否则会一路粘住
FAILED_SOURCES = frozenset({"no-provider", "llm-error", "llm-empty"})

#: 同一条告警最短重报间隔（秒），避免一次抖动就永久静音
WARN_INTERVAL = 600.0

#: 会话历史整段回传给模型的字符上限，防止超长上下文被接口直接拒掉
LINK_MAX_CHARS = 4000

#: 会话 id 缓存的保鲜期（秒）：会话被重置后旧 id 不会一直粘在 umo 上
CONV_ID_TTL = 60.0

#: 会话 id 缓存的条数上限，超过就按写入时间淘汰最旧的
CONV_ID_MAX = 512

try:  # 兼容包 / 非包两种加载方式
    from .guard import (BurstLimiter, CooldownTracker, check_target,
                        normalize_target_mode, parse_id_set)
except ImportError:  # pragma: no cover
    from guard import (BurstLimiter, CooldownTracker, check_target,
                       normalize_target_mode, parse_id_set)

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
        self.burst = BurstLimiter()
        self.freq = FreqFilter()
        self.verdicts = VerdictCache(ttl=self._cache_ttl(), span=self._cache_span())
        self.backoff = BackoffTracker(ack_seconds=self._ack_seconds(),
                                      miss_limit=self._ignore_limit(),
                                      max_level=self._max_level())
        self._last_llm_at: dict = {}
        self._conv_ids: dict = {}  # umo -> (conv_id, 写入时间)
        self._inflight: set = set()
        self._warn_at: dict = {}  # 告警来源 -> 上次播报时间（节流用）

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
        """保留的兼容开关：2.2.0 起发送一律走 event.send，恒为直接发送。"""
        return True

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

    def _burst_window(self) -> float:
        """限次窗口秒数：这段时间内最多回复 ``burst_limit`` 次。"""
        return self._num("burst_window_seconds", 120, 0, 86400, float)

    def _burst_limit(self) -> int:
        """限次窗口内最多自动回复几次（0 表示不限次）。"""
        return self._num("burst_limit", 2, 0, 100)

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

    def _fallback_reply(self) -> str:
        """模型只说 YES 没给内容时的兜底话术；留空则不发送。

        兜底文案是配置里写死的，照样按 ``reply_max_chars`` 收口，
        免得它比模型回复还长、把单次插话的字数上限撑破。
        """
        text = str(self.config.get("fallback_reply", "") or "").strip()
        if not text:
            return ""
        limit = self._reply_max_chars()
        return text if len(text) <= limit else text[:limit].rstrip()

    def _prompt(self) -> str:
        return str(self.config.get("judge_prompt", "") or "")

    def _system_prompt(self) -> str:
        return str(self.config.get("judge_system_prompt", "") or "").strip() or JUDGE_SYSTEM_PROMPT

    # -------- 挂在主会话上（不另开会话） --------
    def _link_session_enabled(self) -> bool:
        return self._bool("link_session", True)

    def _link_max_msgs(self) -> int:
        return self._num("link_context_max_msgs", 10, 0, 100)

    async def _link_session(self, event, umo: str):
        """取当前会话的历史与人格，返回 ``(contexts, persona_prompt, conv_id)``。

        跟主流程一样从 ``conversation_manager`` 取历史、从 ``persona_manager``
        取当前生效的人格，这样插话就在同一个会话、同一份人格里发生，
        而不是另起一段没有记忆的独立对话。
        """
        if not self._link_session_enabled() or not umo:
            return None, "", None
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return None, "", None
        try:
            conv_id = await cm.get_curr_conversation_id(umo)
        except Exception as e:
            logger.debug(f"[SmartReply] 取当前会话失败：{e}")
            return None, "", None
        if not conv_id:
            return None, "", None
        try:
            conv = await cm.get_conversation(umo, conv_id)
        except Exception as e:
            logger.debug(f"[SmartReply] 读取会话内容失败：{e}")
            return None, "", conv_id

        contexts = None
        if conv is not None:
            try:
                history = json.loads(getattr(conv, "history", "") or "[]")
                contexts = history if isinstance(history, list) else None
            except Exception:
                contexts = None
            limit = self._link_max_msgs()
            contexts = self._trim_history(contexts, limit)

        persona_prompt = await self._resolve_persona_prompt(event, umo, conv)
        return contexts or None, persona_prompt, conv_id

    async def _resolve_persona_prompt(self, event, umo: str, conv) -> str:
        """解析本会话生效的人格提示词，取不到返回空串。

        先走框架的 ``resolve_selected_persona``，但它在 ``acm.get_conf`` 那条
        路上会静默回落到全局配置，插件拿到的可能不是本会话路由对应的人格，
        所以再按 umo 路由自己补一次兜底；两次都空就发一条可观测告警，
        不再像以前那样只留 debug 一行、外面看着像「没人格」。
        """
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        platform_name = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            platform_name = str(getter() or "")
        persona = None
        persona_id = None
        try:
            persona_id, persona, *_rest = await pm.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=getattr(conv, "persona_id", None) if conv else None,
                platform_name=platform_name,
            )
        except Exception as e:
            logger.debug(f"[SmartReply] 框架解析人格出错：{e}")
        if not (persona and persona.get("prompt")):
            route_id, route_persona = await self._persona_by_umo_route(pm, umo)
            if route_id:
                persona_id = route_id
            if route_persona and route_persona.get("prompt"):
                persona = route_persona
                logger.debug(
                    f"[SmartReply] 人格改由 umo 路由兜底命中：{route_id}"
                )
        if persona and persona.get("prompt"):
            return str(persona["prompt"])
        self._warn_throttled(
            "persona",
            f"[SmartReply] 没能取到会话人格（umo={umo}，"
            f"解析结果={persona_id}），这次插话不带人格。",
        )
        return ""

    async def _persona_by_umo_route(self, pm, umo: str):
        """按 umo 路由配置直接解析人格，绕开框架的全局回落。"""
        pid = None
        try:
            from astrbot.api import sp

            session_cfg = await sp.get_async(
                scope="umo",
                scope_id=str(umo),
                key="session_service_config",
                default={},
            ) or {}
            pid = session_cfg.get("persona_id")
        except Exception as e:
            logger.debug(f"[SmartReply] 读会话人格配置失败：{e}")
        if not pid:
            acm = getattr(self.context, "astrbot_config_mgr", None)
            if acm is not None:
                try:
                    conf = acm.get_conf(umo) or {}
                    agent_runner = conf.get("agent_runner", {}) or {}
                    runner_config = agent_runner.get("config", {}) or {}
                    pid = (
                        runner_config.get("persona", {}).get("persona_id", "default")
                        if agent_runner.get("runner_type") == "local"
                        else runner_config.get("persona_id", "default")
                    )
                except Exception as e:
                    logger.debug(f"[SmartReply] 按 umo 取会话配置失败：{e}")
        if not pid:
            return None, None
        return pid, pm.get_persona_v3_by_id(pid)

    def _remember_conv_id(self, umo: str, conv_id: str) -> None:
        """记下 umo 当前对应的会话 id，顺手淘汰过期的旧记录。

        以前是「超过 512 条就整体 clear」，会把刚缓存的 id 一起抹掉，
        下一轮又得重新取一次；这里改成按时间淘汰最早的几条。
        """
        if not umo or not conv_id:
            return
        book = self._conv_ids
        book[umo] = (conv_id, time.monotonic())
        if len(book) <= CONV_ID_MAX:
            return
        deadline = time.monotonic() - CONV_ID_TTL
        for key in [k for k, (_cid, ts) in book.items() if ts < deadline]:
            book.pop(key, None)
        while len(book) > CONV_ID_MAX:
            oldest = min(book.items(), key=lambda kv: kv[1][1])[0]
            book.pop(oldest, None)

    async def _ensure_conv_id(self, event, umo: str):
        """拿这次插话要写回的会话 id，缓存命中跳过判定时也补一次。

        缓存只保鲜 ``CONV_ID_TTL`` 秒：会话被重置或换了新对话后，旧 id 不会
        一直粘在 umo 上，把插话写进已经废弃的会话里。
        """
        if not umo:
            _ctxs, _persona, cid = await self._link_session(event, umo)
            return cid
        cached = self._conv_ids.get(umo)
        if cached:
            cid, ts = cached
            if time.monotonic() - ts < CONV_ID_TTL:
                return cid
            self._conv_ids.pop(umo, None)
        _ctxs, _persona, cid = await self._link_session(event, umo)
        self._remember_conv_id(umo, cid)
        return cid

    async def _record_reply(self, conv_id: str | None, user_text: str, reply: str) -> None:
        """把这次插话写回会话历史，让主流程记得自己说过这句。"""
        if not conv_id or not reply:
            return
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return
        try:
            await cm.add_message_pair(
                conv_id,
                {"role": "user", "content": user_text or "(群里的话题)"},
                {"role": "assistant", "content": reply},
            )
        except Exception as e:
            logger.warning(
                f"[SmartReply] 写回会话历史失败，主流程可能不知道自己说过这句：{e}"
            )

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

    @staticmethod
    def _trim_history(contexts, limit: int, max_chars: int = LINK_MAX_CHARS):
        """按「回合」对齐历史切片，并限制回传的字符总量。

        - 只取最近 ``limit`` 条，``limit <= 0`` 表示这次不带历史；
        - Anthropic 系接口要求首条必须是 user，所以开头若是 assistant 就
          继续往后割，避免整段历史被接口直接判非法；
        - 再从后往前累计字符数，超过 ``max_chars`` 就少带几条。
        """
        if not isinstance(contexts, list) or not contexts:
            return None
        items = [c for c in contexts if isinstance(c, dict)]
        if not items:
            return None
        if limit <= 0:
            return None
        items = items[-limit:]
        while items and str(items[0].get("role") or "").lower() == "assistant":
            items = items[1:]
        total = 0
        kept = []
        for item in reversed(items):
            size = len(str(item.get("content") or ""))
            if kept and total + size > max_chars:
                break
            total += size
            kept.append(item)
        kept.reverse()
        return kept or None

    def _warn_throttled(self, source: str, message: str) -> None:
        """同一条告警按 ``WARN_INTERVAL`` 节流播报，避免一次抖动就永久静音。"""
        book = getattr(self, "_warn_at", None)
        if not isinstance(book, dict):
            book = {}
            self._warn_at = book
        now = time.monotonic()
        if now - float(book.get(source, 0.0)) < WARN_INTERVAL:
            return
        book[source] = now
        logger.warning(message)

    @staticmethod
    def _compose_system_prompt(base: str, persona_prompt: str) -> str:
        """人格在前、插话任务在后，跟主流程的拼法保持一致。

        取不到人格时直接返回任务提示词（结构上少了 Persona 段落），
        这是有意为之：宁可不带人格，也不要拿一段空标题糊弄模型。
        """
        if not persona_prompt:
            return base
        return ("\n# Persona Instructions\n\n" + persona_prompt
                + "\n\n" + "# 插话任务\n" + base)

    async def _get_provider(self, umo: str):
        """取当前会话生效的 LLM 提供商。

        ``get_using_provider`` 已标记废弃，优先走异步版；两版都可能返回
        协程或直接返回对象，所以这里统一 await 一次可等待对象。
        """
        getter = getattr(self.context, "get_using_provider_async", None)
        if not callable(getter):
            getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            return None
        result = getter(umo)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _ask_llm(self, event, messages, umo: str) -> Verdict:
        """问一次模型：现在插话合适吗。失败一律当作「不回复」。"""
        try:
            provider = await self._get_provider(umo)
        except Exception as e:
            self._warn_throttled(
                "no-provider", f"[SmartReply] 取 LLM 提供商出错：{e}"
            )
            return Verdict(False, "", "no-provider")
        if provider is None:
            self._warn_throttled(
                "no-provider", "[SmartReply] 没有可用的 LLM 提供商，自动回复暂时跳过。"
            )
            return Verdict(False, "", "no-provider")

        prompt = build_judge_prompt(
            messages,
            session="群聊" if event.get_group_id() else "私聊",
            template=self._prompt(),
        )
        contexts, persona_prompt, conv_id = await self._link_session(event, umo)
        self._remember_conv_id(umo, conv_id)
        system_prompt = self._system_prompt()
        system_prompt = self._compose_system_prompt(system_prompt, persona_prompt)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=umo or "smart-reply",
                contexts=contexts,
                system_prompt=system_prompt,
            )
        except Exception as e:
            self._warn_throttled("llm-error", f"[SmartReply] 判断调用失败：{e}")
            return Verdict(False, "", "llm-error")

        raw = str(getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            return Verdict(False, "", "llm-empty")
        verdict = parse_judge_reply(raw, max_reply_chars=self._reply_max_chars())
        logger.debug(
            f"[SmartReply] 判断结果（{verdict.source}）→ "
            f"{summarize(messages)}"
        )
        return verdict

    async def _maybe_reply(self, event, session_key: str, is_group: bool):
        """频次粗筛 → 冷却 / 限次 → 缓存 → 问模型 → 发送。"""
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

        key = f"reply:{session_key}"
        burst_window = self._burst_window()
        burst_limit = self._burst_limit()
        if not self.burst.allow(key, burst_limit, burst_window, now=now):
            wait = self.burst.remaining(key, burst_limit, burst_window, now=now)
            logger.debug(
                f"[SmartReply] 限次跳过：{burst_window:.0f} 秒内已回复 "
                f"{burst_limit} 次，再等 {wait:.0f} 秒"
            )
            return

        cd = self._cooldown() * cooldown_multiplier(
            level, self._backoff_base(), self._backoff_cap()
        )
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

        # 判定 + 发送整段串行：以前只护住「去问模型」那条分支，缓存命中时
        # 会直接往下走到发送，同一个会话可能被并发送出两条。
        if session_key in self._inflight:
            logger.debug("[SmartReply] 上一次判定还没回来，这次先跳过")
            return
        self._inflight.add(session_key)
        try:
            verdict = self.verdicts.get(session_key, window, now=now)
            if verdict is None:
                gap = self._min_interval()
                if gap > 0 and now - self._last_llm_at.get(session_key, 0.0) < gap:
                    logger.debug("[SmartReply] 本会话刚问过模型，先省着点")
                    return
                self._last_llm_at[session_key] = now
                verdict = await self._ask_llm(event, window, umo)
                # 取不到提供商 / 调用失败 / 回复为空都属于「这次没结论」，
                # 写进缓存会把失败状态钉住整个窗口，所以只在成功时落缓存。
                if verdict.source in FAILED_SOURCES:
                    logger.debug(f"[SmartReply] 失败结论（{verdict.source}）不进缓存")
                else:
                    self.verdicts.put(session_key, window, verdict, now=now)

            if not verdict.should_reply:
                logger.debug(f"[SmartReply] 判断为不回复（{verdict.source}）")
                return

            reply = (verdict.reply or "").strip() or self._fallback_reply()
            if not reply:
                logger.debug("[SmartReply] 判断为可以回，但没给内容，跳过")
                return

            # 统一走 event.send 单一出口：既能绕开 result_decorate 的引用装饰，
            # 也会置位 _has_send_oper，让主 Agent 不再重复回一遍。
            try:
                await event.send(MessageChain(chain=[Plain(reply)]))
            except Exception as e:
                logger.error(f"[SmartReply] 发送失败：{e}")
                return

            # 发送成功才落账，并且一律用「发送成功那一刻」的时间：
            # 判定期间可能已经等了几秒，用开头的 now 会让冷却/限次提前失效。
            sent_at = time.monotonic()
            user_text = (event.get_message_str() or "").strip() or (
                window[-1][1] if window else ""
            )
            await self._record_reply(
                await self._ensure_conv_id(event, umo), user_text, reply
            )
            self.cooldown.touch(key, cd, now=sent_at)
            self.burst.note(key, burst_window, now=sent_at)
            self.cooldown.cleanup(now=sent_at)
            self.burst.cleanup(now=sent_at, window=burst_window)
            self.verdicts.cleanup(now=sent_at)
            self.backoff.note_reply(session_key, now=sent_at)
            logger.info(
                f"[SmartReply] 已自动回复（{verdict.source}，降频档位 {level}，"
                f"{burst_window:.0f} 秒内第 {self.burst.count(key, burst_window, now=sent_at)} 次）"
            )
        finally:
            self._inflight.discard(session_key)

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

            # 只在群聊生效：私聊被框架判定为「被点名」，进来必然触发，
            # 接话会插在你和机器人的正常对话前，所以私聊一律不参与。
            if not is_group:
                return
            if not self._bool("enable_group", True):
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

            await self._maybe_reply(event, session_key, is_group)
        except Exception as e:
            logger.error(f"[SmartReply] 处理消息异常: {e}")
