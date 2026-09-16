#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触发范围与冷却 —— 纯逻辑层（不依赖 AstrBot 运行时，便于离线测试）。

职责：
- ``parse_id_set``：把配置里的「群号 / QQ 号」名单解析成集合
- ``normalize_target_mode`` / ``check_target``：群 / 用户两级白黑名单校验
- ``CooldownTracker``：冷却记录，防止自动回复过于频繁
- ``BurstLimiter``：滑动窗口限次，一段时间内最多回复 N 次
"""
from __future__ import annotations

import re
import time
from collections import deque

# 触发范围模式（群 / 用户两级通用）：
# off       = 不过滤（黑名单依旧生效，兼容旧配置）
# whitelist = 只有白名单里的群 / 人才触发
# blacklist = 黑名单里的群 / 人不触发
VALID_TARGET_MODES = ("off", "whitelist", "blacklist")


def parse_id_set(raw) -> set[str]:
    """把配置里的「群号 / QQ 号」名单解析成集合。

    兼容写法：
    - 列表：``["123", "456"]``
    - 字符串：``"123,456"``、``"123;456"``、``"123 456"``、换行分隔
    """
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = re.split(r"[,，;；、\s+#]+", str(raw))
    return {str(x).strip() for x in items if str(x).strip()}


def normalize_target_mode(value, default: str = "off") -> str:
    """把配置里的模式值规整成 VALID_TARGET_MODES 之一，非法值回落到默认。"""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_TARGET_MODES else default


def check_target(
    target_id: str,
    mode: str = "off",
    whitelist=None,
    blacklist=None,
) -> tuple[bool, str]:
    """判断某个群 / 用户是否允许触发回复，返回 ``(是否放行, 拦截原因)``。

    - ``off``：不做白名单校验，但黑名单依旧生效（兼容旧配置的 ``ignore_*_ids``）
    - ``whitelist``：只有白名单里的群 / 人才触发；白名单为空则全部不触发
    - ``blacklist``：黑名单里的群 / 人不触发

    黑名单优先级最高：同时出现在两份名单里按「不触发」处理。
    """
    tid = str(target_id or "").strip()
    mode = normalize_target_mode(mode)
    wl = parse_id_set(whitelist)
    bl = parse_id_set(blacklist)

    if tid and tid in bl:
        return False, "在黑名单"

    if mode == "whitelist":
        if not wl:
            return False, "白名单为空"
        if tid and tid in wl:
            return True, ""
        return False, "不在白名单"

    return True, ""


class CooldownTracker:
    """基于 ``time.monotonic`` 的冷却记录器（内存态，重启即清空）。"""

    def __init__(self) -> None:
        self._next_time: dict[str, float] = {}

    def remaining(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._next_time.get(key, 0.0) - now)

    def allow(self, key: str, cooldown: float, now: float | None = None) -> bool:
        if cooldown <= 0:
            return True
        return self.remaining(key, now) <= 0

    def touch(self, key: str, cooldown: float, now: float | None = None) -> None:
        if cooldown <= 0:
            return
        now = time.monotonic() if now is None else now
        self._next_time[key] = now + cooldown

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._next_time.clear()
        else:
            self._next_time.pop(key, None)

    def cleanup(self, now: float | None = None) -> None:
        """清理已经过期的记录，避免长期运行内存缓慢增长。"""
        now = time.monotonic() if now is None else now
        for k in [k for k, v in self._next_time.items() if v <= now]:
            self._next_time.pop(k, None)


class BurstLimiter:
    """滑动窗口限次：一段时间内最多自动回复 N 次（内存态，重启即清空）。

    与 ``CooldownTracker`` 互补：冷却管「两次回复之间至少隔多久」，
    限次管「一小段时间里一共最多回几次」，防止窗口一到就连续插话。
    """

    def __init__(self) -> None:
        self._marks: dict[str, deque] = {}

    def _prune(self, key: str, window: float, now: float) -> deque:
        q = self._marks.get(key)
        if q is None:
            q = deque()
            self._marks[key] = q
        while q and now - q[0] >= window:
            q.popleft()
        return q

    def count(self, key: str, window: float = 120.0,
              now: float | None = None) -> int:
        """窗口内已经回复过几次。"""
        if window <= 0:
            return 0
        now = time.monotonic() if now is None else now
        q = self._marks.get(key)
        if not q:
            return 0
        while q and now - q[0] >= window:
            q.popleft()
        return len(q)

    def allow(self, key: str, limit: int = 2, window: float = 120.0,
              now: float | None = None) -> bool:
        """还能不能再回一次；``limit <= 0`` 表示不限次。"""
        if not limit or int(limit) <= 0:
            return True
        return self.count(key, window=window, now=now) < int(limit)

    def remaining(self, key: str, limit: int = 2, window: float = 120.0,
                  now: float | None = None) -> float:
        """达到上限时，还要等多少秒才腾出配额；未达上限返回 0。"""
        if not limit or int(limit) <= 0 or window <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        q = self._marks.get(key)
        if not q:
            return 0.0
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) < int(limit):
            return 0.0
        oldest = q[len(q) - int(limit)]
        return max(0.0, window - (now - oldest))

    def note(self, key: str, window: float = 120.0,
             now: float | None = None) -> None:
        """记一次已经发出去的回复。"""
        if window <= 0:
            return
        now = time.monotonic() if now is None else now
        self._prune(key, window, now).append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._marks.clear()
        else:
            self._marks.pop(str(key or ""), None)

    def cleanup(self, now: float | None = None, window: float = 120.0) -> None:
        """清理窗口内已经没有记录的空队列，避免长期运行内存缓慢增长。"""
        now = time.monotonic() if now is None else now
        for k in list(self._marks):
            if not self._prune(k, window, now):
                self._marks.pop(k, None)
