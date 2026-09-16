#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回复范围控制 —— 纯逻辑层（不依赖 AstrBot 运行时，便于离线测试）。

职责：
- ``parse_rules``：把配置里的规则解析成 ``Rule`` 列表（含规则级群 / 人范围）
- ``match_keyword``：关键词匹配（包含 / 精确 / 正则）
- ``check_target``：全局群 / 用户白黑名单校验
- ``rule_allows``：单条规则的生效范围校验
- ``CooldownTracker``：冷却控制，防止刷屏
- ``RecentMessages``：记录「上面」的消息，判断触发词是不是刚出现过
- ``resolve_image``：把「图片名 / 路径 / 链接」解析成可直接发送的图片
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

# 规则字符串里的字段分隔符：中文逗号 / 英文逗号 / 竖线 / =>
SEPARATOR_RE = re.compile(r"\s*(?:，|,|\||=>|->)\s*")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 默认图片目录（宁宁的图都放这里，写图名即可）
DEFAULT_IMAGE_DIR = "/root/AstrBot/data/workspaces/tu"

VALID_MATCH_TYPES = ("contains", "exact", "regex")

# 触发范围模式（群 / 用户两级通用）：
# off       = 不过滤（黑名单依旧生效，兼容旧配置）
# whitelist = 只有白名单里的群 / 人才触发
# blacklist = 黑名单里的群 / 人不触发
VALID_TARGET_MODES = ("off", "whitelist", "blacklist")


@dataclass
class Rule:
    """一条「监测到 X → 发送 Y」规则。

    字段含义与配置里的字符串写法 ``触发词，回复内容，图片，冷却秒数`` 一一对应，
    其中后三项都可省略。
    """

    keyword: str
    reply: str = ""
    image: str = ""
    match_type: str = "contains"
    cooldown: float | None = None  # None 表示跟随全局默认冷却
    groups: tuple[str, ...] = ()  # 规则级生效群，空 = 不限
    users: tuple[str, ...] = ()  # 规则级生效用户，空 = 不限
    pattern: re.Pattern | None = field(default=None, repr=False)

    @property
    def has_content(self) -> bool:
        return bool(self.reply or self.image)


def normalize_text(text: str) -> str:
    """去掉首尾空白，统一大小写，方便匹配。"""
    return (text or "").strip().casefold()


def _to_rule(item, default_match_type: str = "contains") -> tuple[Rule | None, str]:
    """把单个配置项转成 Rule；返回 (rule, error_message)。"""
    match_type = default_match_type

    if isinstance(item, dict):
        keyword = str(item.get("keyword") or item.get("trigger") or "").strip()
        if not keyword:
            return None, "规则缺少 keyword 字段，已跳过"
        reply = str(item.get("reply") or item.get("text") or "")
        image = str(item.get("image") or item.get("pic") or "").strip()
        groups = parse_id_set(item.get("groups") or item.get("group_ids"))
        users = parse_id_set(item.get("users") or item.get("user_ids"))
        match_type = str(item.get("match_type") or default_match_type).strip() or "contains"
        cooldown = item.get("cooldown", None)
        try:
            cooldown = None if cooldown in (None, "") else float(cooldown)
        except (TypeError, ValueError):
            return None, f"规则「{keyword}」的 cooldown 不是数字，已改用全局冷却"
    elif isinstance(item, str):
        line = item.strip()
        if not line or line.startswith("#"):
            return None, ""
        parts = [p.strip() for p in SEPARATOR_RE.split(line)]
        parts += [""] * (6 - len(parts))
        keyword, reply, image, cooldown_raw = parts[0], parts[1], parts[2], parts[3]
        groups = parse_id_set(parts[4])
        users = parse_id_set(parts[5])
        if not keyword:
            return None, f"规则「{line}」缺少触发词，已跳过"
        cooldown = None
        if cooldown_raw:
            try:
                cooldown = float(cooldown_raw)
            except ValueError:
                return None, f"规则「{line}」的冷却不是数字，已改用全局冷却"
    else:
        return None, f"无法识别的规则类型：{type(item).__name__}，已跳过"

    if match_type not in VALID_MATCH_TYPES:
        return None, f"规则「{keyword}」的 match_type={match_type} 不支持，已跳过"

    pattern = None
    if match_type == "regex":
        try:
            pattern = re.compile(keyword, re.IGNORECASE)
        except re.error as e:
            return None, f"规则「{keyword}」正则不合法（{e}），已跳过"

    return Rule(
        keyword=keyword,
        reply=reply,
        image=image,
        match_type=match_type,
        cooldown=cooldown,
        groups=tuple(sorted(groups)),
        users=tuple(sorted(users)),
        pattern=pattern,
    ), ""


def parse_rules(raw, default_match_type: str = "contains") -> tuple[list[Rule], list[str]]:
    """解析规则配置。

    ``raw`` 支持三种写法：
    1. 字符串列表：``["姆唔，姆唔，tu_1", "早安，早安"]``
    2. 多行字符串（换行分隔）
    3. JSON 字符串 / JSON 列表：``[{"keyword": "姆唔", "reply": "姆唔", "image": "tu_1"}]``

    返回 ``(rules, errors)``。
    """
    errors: list[str] = []
    if raw is None:
        return [], errors

    items = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], errors
        if text.startswith("[") or text.startswith("{"):
            try:
                items = json.loads(text)
            except json.JSONDecodeError as e:
                return [], [f"rules JSON 解析失败：{e}"]
        else:
            items = text.splitlines()

    if isinstance(items, dict):
        items = items.get("rules", [])

    if not isinstance(items, (list, tuple)):
        return [], [f"rules 需要是列表，当前是 {type(items).__name__}"]

    rules: list[Rule] = []
    for item in items:
        rule, err = _to_rule(item, default_match_type)
        if rule is not None:
            rules.append(rule)
        if err:
            errors.append(err)
    return rules, errors


def match_keyword(text: str, rule: Rule) -> bool:
    """判断消息文本是否命中规则。"""
    haystack = normalize_text(text)
    if not haystack:
        return False
    needle = normalize_text(rule.keyword)
    if rule.match_type == "exact":
        return haystack == needle
    if rule.match_type == "regex":
        if rule.pattern is None:
            return False
        return bool(rule.pattern.search(text or ""))
    return needle in haystack


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


def rule_allows(rule: Rule, group_id: str = "", sender_id: str = "") -> tuple[bool, str]:
    """判断单条规则的「生效范围」是否覆盖当前会话，返回 ``(是否放行, 拦截原因)``。

    规则级范围是在全局白 / 黑名单之外的又一次收紧：

    - 规则写了群号 → 只有这些群放行，私聊（没有群号）一律不放行；
    - 规则写了 QQ 号 → 只有这些人放行；
    - 两项都不写 → 不限，完全跟随全局触发范围。
    """
    if rule.groups:
        gid = str(group_id or "").strip()
        if not gid or gid not in rule.groups:
            return False, "不在规则限定的群"
    if rule.users:
        uid = str(sender_id or "").strip()
        if not uid or uid not in rule.users:
            return False, "不在规则限定的人"
    return True, ""


def resolve_image(value: str, image_dir: str = DEFAULT_IMAGE_DIR) -> tuple[str, str] | None:
    """把图片配置解析成 ``("file", 绝对路径)`` 或 ``("url", 链接)``。

    依次尝试：网络链接 → 绝对路径 → 图片目录下的相对路径 →
    自动补扩展名 → 前缀模糊匹配（``tu_1`` → ``tu_1.png``）。
    """
    v = (value or "").strip().strip('"').strip("'")
    if not v:
        return None

    if v.startswith(("http://", "https://")):
        return ("url", v)

    v = os.path.expanduser(v)
    if os.path.isfile(v):
        return ("file", os.path.abspath(v))

    base_dir = os.path.expanduser(image_dir or DEFAULT_IMAGE_DIR)
    candidate = v if os.path.isabs(v) else os.path.join(base_dir, v)
    if os.path.isfile(candidate):
        return ("file", os.path.abspath(candidate))

    stem, ext = os.path.splitext(candidate)
    if not ext:
        for e in IMAGE_EXTS:
            if os.path.isfile(stem + e):
                return ("file", os.path.abspath(stem + e))
        for hit in sorted(glob.glob(glob.escape(stem) + ".*")):
            if os.path.splitext(hit)[1].lower() in IMAGE_EXTS:
                return ("file", os.path.abspath(hit))
    else:
        # 扩展名大小写不一致的情况（tu_1.PNG）
        for hit in sorted(glob.glob(glob.escape(stem) + ".*")):
            if os.path.splitext(hit)[1].lower() == ext.lower():
                return ("file", os.path.abspath(hit))
    return None


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


class RecentMessages:
    """按会话记录最近几条消息，用来判断「上面」有没有出现过触发词。

    例：A 已经说过「唔姆」并回复过了，紧接着 A 又说一次「唔姆」，
    因为「上面」有过，就不再发送。

    只存内存：每个会话一个定长队列，会话数量也有上限，长期运行不会无限增长。
    """

    def __init__(self, max_per_session: int = 60, max_sessions: int = 512) -> None:
        self.max_per_session = max(1, int(max_per_session))
        self.max_sessions = max(1, int(max_sessions))
        self._data: OrderedDict[str, deque] = OrderedDict()

    def remember(
        self,
        session_id: str,
        sender_id: str,
        text: str,
        now: float | None = None,
    ) -> None:
        """记下一条消息（(发送人, 文本, 时间)）。"""
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
        bucket.append((str(sender_id or ""), text or "", now))

    def above(
        self,
        session_id: str,
        sender_id: str = "",
        window: int = 1,
        seconds: float = 0.0,
        same_user_only: bool = False,
        now: float | None = None,
    ) -> list[str]:
        """取「上面」最近 window 条消息的文本。

        :param window: 往上找几条（1=只看紧邻的上一条）。
        :param seconds: 只看这么多秒以内的，<=0 表示不限时间。
        :param same_user_only: True 只看同一个发送人的消息；False（默认）群里别人说的也算。
        """
        now = time.monotonic() if now is None else now
        bucket = self._data.get(str(session_id or ""))
        if not bucket:
            return []
        window = max(1, int(window))
        limit = float(seconds or 0)
        sender = str(sender_id or "")
        out: list[str] = []
        for item_sender, item_text, ts in reversed(bucket):
            if limit > 0 and now - ts > limit:
                continue
            if same_user_only and item_sender != sender:
                continue
            out.append(item_text)
            if len(out) >= window:
                break
        return out

    def forget(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._data.clear()
        else:
            self._data.pop(str(session_id or ""), None)


def appeared_above(texts, rule: Rule) -> bool:
    """「上面」的消息里是否已经出现过该规则的触发词。"""
    return any(match_keyword(t, rule) for t in texts or [])
