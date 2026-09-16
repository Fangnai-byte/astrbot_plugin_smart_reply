#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能自动回复 —— 语境判断层（纯逻辑，不依赖 AstrBot 运行时，便于离线测试）。

这一层负责回答「此刻机器人主动接一句话合不合适」，同时把调用成本压到最低：

1. ``FreqFilter``：先按「频次」粗筛。窗口内消息太少（冷清）或太多（刷屏）直接
   不回复，连 LLM 都不问；只有灰区才值得问模型。
2. ``VerdictCache``：同一串消息只问一次。用最近 N 条消息的签名做 key，
   同串消息再来（或被别的路径撞上）直接复用上次结论。
3. ``BackoffTracker``：回复后没人接话就算「被无视」，连续被无视就自动降频
   （粗筛更挑剔 + 冷却时间按倍数拉长），有人接话立刻恢复。

所有时间都走 ``time.monotonic``，并支持传入 ``now`` 便于测试。
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

# 频次粗筛结果
COARSE_LOW = "low"  # 不值得打扰模型（太冷清 / 太吵）
COARSE_GRAY = "gray"  # 灰区 —— 问 LLM
COARSE_HIGH = "high"  # 明显该回（被点名）—— 不问 LLM

JUDGE_SYSTEM_PROMPT = (
    "你是群聊机器人内部的「要不要插话」判断器。"
    "只根据给出的聊天记录判断此刻机器人主动回复是否合适，"
    "不要复述对话，不要解释，不要寒暄。"
)

DEFAULT_JUDGE_PROMPT = (
    "以下是{session}里最近的对话记录（按时间从早到晚）：\n"
    "{messages}\n"
    "机器人叫「{bot_name}」。判断机器人现在主动插一句话是否合适。\n"
    "判断标准：话题开放、有人在提问、气氛轻松时适合插话；"
    "两人私聊式对话、话题封闭、正在争执、刷屏时不适合。\n"
    "只输出一行：合适输出 YES，不合适输出 NO；"
    "如果合适并且你能给出自然的一句话回复，则输出 YES|回复内容。"
)

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
# 结论行的判定词：ASCII 词后面必须跟非字母，中文词直接前缀匹配
_YES_RE = re.compile(r"^(?:yes|y|是|回|可以|合适|应该)(?![A-Za-z])", re.IGNORECASE)
_NO_RE = re.compile(r"^(?:no|n|否|不回|不回复|不应该|别回)(?![A-Za-z])", re.IGNORECASE)


@dataclass
class Verdict:
    """一次语境判断的结论。"""

    should_reply: bool
    reply: str = ""
    source: str = ""  # coarse / cache / llm / llm-error / disabled ...


def normalize_message(text: str, limit: int = 200) -> str:
    """压平空白，避免签名受排版影响。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit]


# --------------------------------------------------------------------------
# 1. 频次粗筛
# --------------------------------------------------------------------------
class FreqFilter:
    """按会话记录最近消息，用频次粗筛，决定值不值得动用 LLM。"""

    def __init__(self, max_per_session: int = 80, max_sessions: int = 512) -> None:
        self.max_per_session = max(1, int(max_per_session))
        self.max_sessions = max(1, int(max_sessions))
        self._data: OrderedDict[str, deque] = OrderedDict()

    def observe(self, session_id: str, sender_id: str, text: str,
                now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        key = str(session_id or "")
        bucket = self._data.get(key)
        if bucket is None:
            bucket = deque(maxlen=self.max_per_session)
            self._data[key] = bucket
            while len(self._data) > self.max_sessions:
                self._data.popitem(last=False)
        else:
            self._data.move_to_end(key)
        bucket.append((str(sender_id or ""), str(text or ""), now))

    def recent(self, session_id: str, window: float = 120.0, limit: int = 12,
               now: float | None = None) -> list[tuple[str, str, float]]:
        """取窗口内最近 ``limit`` 条，按时间从早到晚返回。"""
        now = time.monotonic() if now is None else now
        bucket = self._data.get(str(session_id or ""))
        if not bucket:
            return []
        window = float(window or 0)
        out: list[tuple[str, str, float]] = []
        for sender, text, ts in reversed(bucket):
            if window > 0 and now - ts > window:
                break
            out.append((sender, text, ts))
            if len(out) >= max(1, int(limit)):
                break
        out.reverse()
        return out

    def stats(self, session_id: str, window: float = 120.0,
              now: float | None = None) -> dict:
        recent = self.recent(session_id, window=window, limit=self.max_per_session, now=now)
        senders = {s for s, _t, _ts in recent if s}
        last = recent[-1] if recent else ("", "", 0.0)
        return {
            "msgs": len(recent),
            "senders": len(senders),
            "last_sender": last[0],
            "last_text": last[1],
            "last_ts": last[2],
        }

    def coarse(self, session_id: str, window: float = 120.0, min_msgs: int = 3,
               spam_msgs: int = 0, addressed: bool = False,
               now: float | None = None) -> tuple[str, dict]:
        """频次粗筛，返回 ``(COARSE_*, stats)``。

        - ``addressed``（被点名 / @）直接 HIGH，不必问模型；
        - 窗口内消息数少于 ``min_msgs`` → 太冷清，LOW；
        - ``spam_msgs > 0`` 且消息数不少于它 → 刷屏，先不插话，LOW；
        - 其余为灰区，交给 LLM。
        """
        info = self.stats(session_id, window=window, now=now)
        info["addressed"] = bool(addressed)
        if addressed:
            info["reason"] = "被点名"
            return COARSE_HIGH, info
        msgs = info["msgs"]
        if msgs <= 0:
            info["reason"] = "窗口内没有消息"
            return COARSE_LOW, info
        if msgs < max(1, int(min_msgs or 1)):
            info["reason"] = f"窗口内只有 {msgs} 条消息，太冷清"
            return COARSE_LOW, info
        if spam_msgs and msgs >= int(spam_msgs):
            info["reason"] = f"窗口内 {msgs} 条消息，正在刷屏"
            return COARSE_LOW, info
        info["reason"] = f"窗口内 {msgs} 条消息，属于灰区"
        return COARSE_GRAY, info

    def forget(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._data.clear()
        else:
            self._data.pop(str(session_id or ""), None)


# --------------------------------------------------------------------------
# 2. 同一串消息判过就复用
# --------------------------------------------------------------------------
class VerdictCache:
    """同一串消息只问一次模型，结论带 TTL 复用（LRU 上限，内存可控）。"""

    def __init__(self, ttl: float = 180.0, max_entries: int = 512,
                 span: int = 8) -> None:
        self.ttl = max(1.0, float(ttl or 1.0))
        self.max_entries = max(1, int(max_entries))
        self.span = max(1, int(span))
        self._data: OrderedDict[str, tuple[Verdict, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def signature(self, messages) -> str:
        """取最近 ``span`` 条消息的签名（发送人 + 文本）。"""
        items = list(messages or [])[-self.span:]
        raw = "\n".join(
            f"{normalize_message(m[0], 40)}>{normalize_message(m[1])}"
            if isinstance(m, (tuple, list)) and len(m) >= 2 else str(m)
            for m in items
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, session_id: str, messages, now: float | None = None) -> Verdict | None:
        now = time.monotonic() if now is None else now
        key = f"{session_id}:{self.signature(messages)}"
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        verdict, ts = item
        if now - ts > self.ttl:
            self._data.pop(key, None)
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return Verdict(verdict.should_reply, verdict.reply, "cache")

    def put(self, session_id: str, messages, verdict: Verdict,
            now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        key = f"{session_id}:{self.signature(messages)}"
        self._data[key] = (Verdict(bool(verdict.should_reply), verdict.reply or "",
                                   verdict.source or ""), now)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def cleanup(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for k in [k for k, (_v, ts) in self._data.items() if now - ts > self.ttl]:
            self._data.pop(k, None)

    def forget(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._data.clear()
            return
        prefix = f"{session_id}:"
        for k in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(k, None)

    def __len__(self) -> int:
        return len(self._data)


# --------------------------------------------------------------------------
# 3. 被无视就自动降频
# --------------------------------------------------------------------------
class BackoffTracker:
    """回复后没人接话算被无视；连续被无视则升级降频档位，有人接话即恢复。"""

    def __init__(self, ack_seconds: float = 60.0, miss_limit: int = 2,
                 max_level: int = 4, max_sessions: int = 512) -> None:
        self.ack_seconds = max(1.0, float(ack_seconds or 1.0))
        self.miss_limit = max(1, int(miss_limit or 1))
        self.max_level = max(1, int(max_level or 1))
        self.max_sessions = max(1, int(max_sessions))
        self._data: OrderedDict[str, dict] = OrderedDict()

    def _slot(self, session_id: str) -> dict:
        key = str(session_id or "")
        slot = self._data.get(key)
        if slot is None:
            slot = {"pending": 0.0, "misses": 0, "acks": 0, "replies": 0}
            self._data[key] = slot
            while len(self._data) > self.max_sessions:
                self._data.popitem(last=False)
        else:
            self._data.move_to_end(key)
        return slot

    def _settle(self, slot: dict, now: float) -> None:
        """回复后超过 ack 窗口还没人接话 → 记一次被无视。"""
        pending = slot.get("pending") or 0.0
        if pending and now - pending > self.ack_seconds:
            slot["pending"] = 0.0
            slot["misses"] = int(slot.get("misses", 0)) + 1

    def note_reply(self, session_id: str, now: float | None = None) -> None:
        """记下「我们刚回了话」，开始等有人接话。"""
        now = time.monotonic() if now is None else now
        slot = self._slot(session_id)
        self._settle(slot, now)
        slot["pending"] = now
        slot["replies"] = int(slot.get("replies", 0)) + 1

    def observe(self, session_id: str, senders: int = 1, now: float | None = None) -> bool:
        """有人说话 → 如果在 ack 窗口内，视为没被无视，清零降频。"""
        now = time.monotonic() if now is None else now
        slot = self._slot(session_id)
        self._settle(slot, now)  # 过期的等待先结算成一次被无视
        acked = False
        pending = slot.get("pending") or 0.0
        if pending and now - pending <= self.ack_seconds:
            acked = True
            slot["acks"] = int(slot.get("acks", 0)) + 1
            slot["misses"] = 0
        slot["pending"] = 0.0
        return acked

    def level(self, session_id: str, now: float | None = None) -> int:
        """当前降频档位：0=正常，越大越克制。"""
        now = time.monotonic() if now is None else now
        slot = self._slot(session_id)
        self._settle(slot, now)
        return self._level_of(slot)

    def _level_of(self, slot: dict) -> int:
        misses = int(slot.get("misses", 0))
        if misses < self.miss_limit:
            return 0
        level = (misses - self.miss_limit) // self.miss_limit + 1
        return min(self.max_level, level)

    def stats(self, session_id: str, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        slot = self._slot(session_id)
        self._settle(slot, now)
        return {
            "level": self._level_of(slot),
            "misses": int(slot.get("misses", 0)),
            "acks": int(slot.get("acks", 0)),
            "replies": int(slot.get("replies", 0)),
            "pending": bool(slot.get("pending") or 0.0),
        }

    def reset(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._data.clear()
        else:
            self._data.pop(str(session_id or ""), None)


def scaled_min_msgs(min_msgs: int, level: int, step: int = 2) -> int:
    """降频档位越高，粗筛下限越高（越不容易进入灰区问模型）。"""
    return max(1, int(min_msgs or 1) + max(0, int(level)) * max(0, int(step)))


def cooldown_multiplier(level: int, base: float = 2.0, cap: float = 8.0) -> float:
    """降频档位越高，语境回复的冷却时间越长。"""
    level = max(0, int(level))
    if level <= 0:
        return 1.0
    return min(float(cap), float(base) ** level)


# --------------------------------------------------------------------------
# 4. LLM 判断的提示词与结果解析
# --------------------------------------------------------------------------
def format_messages(messages, bot_name: str = "机器人", max_chars: int = 1200) -> str:
    """把 (发送人, 文本) 列表压成紧凑的对话记录，控制长度避免浪费 token。"""
    lines = []
    for item in messages or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            sender, text = item[0], item[1]
        else:
            sender, text = "", item
        sender = normalize_message(sender, 24) or "某人"
        text = normalize_message(text)
        if not text:
            continue
        if bot_name and sender == bot_name:
            sender = f"{bot_name}(机器人)"
        lines.append(f"{sender}: {text}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "…\n" + text[-max_chars:]
    return text


def build_judge_prompt(messages, bot_name: str = "机器人", session: str = "群聊",
                       template: str = "") -> str:
    """组装判断用的提示词；``template`` 为空时用内置模板。"""
    body = format_messages(messages, bot_name=bot_name)
    tpl = (template or "").strip() or DEFAULT_JUDGE_PROMPT
    try:
        return tpl.format(messages=body, bot_name=bot_name or "机器人", session=session)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_JUDGE_PROMPT.format(messages=body, bot_name=bot_name or "机器人",
                                           session=session)


def parse_judge_reply(raw: str, max_reply_chars: int = 120) -> Verdict:
    """解析模型的单行结论：YES / NO / YES|回复内容。

    解析不出来时保守处理 —— 当作「不回复」，宁可不打扰。
    """
    text = _CODE_FENCE_RE.sub("", str(raw or "").strip()).strip()
    if not text:
        return Verdict(False, "", "llm-empty")
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    head, _, tail = line.partition("|")
    head = head.strip()
    tail = tail.strip().strip('"').strip("'")
    if _NO_RE.match(head):
        return Verdict(False, "", "llm")
    if _YES_RE.match(head):
        return Verdict(True, tail[:max_reply_chars], "llm")
    return Verdict(False, "", "llm-unclear")


def summarize(messages, bot_name: str = "") -> str:
    """给日志用的一句话摘要。"""
    body = format_messages(messages, bot_name=bot_name, max_chars=120)
    return body.replace("\n", " ／ ")
