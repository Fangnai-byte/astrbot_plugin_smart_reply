#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话接入与人格拼接离线测试：python3 tests/test_smart_reply_link.py"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m  # noqa: E402

N = chr(10)
FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


class FakeConv:
    def __init__(self, history, persona_id="p1"):
        self.history = json.dumps(history, ensure_ascii=False)
        self.persona_id = persona_id


class FakeCM:
    def __init__(self, conv):
        self.conv = conv
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return "cid-1"

    async def get_conversation(self, umo, cid):
        return self.conv

    async def add_message_pair(self, cid, user_message, assistant_message):
        self.pairs.append((cid, user_message, assistant_message))


class FakePM:
    async def resolve_selected_persona(self, umo=None, conversation_persona_id=None,
                                       platform_name=None):
        return ("p1", {"prompt": "【人格】白毛紫瞳魔女"}, "x")


class FakeCtx:
    def __init__(self, cm, pm):
        self.conversation_manager = cm
        self.persona_manager = pm


class FakeEvent:
    def __init__(self, text="哦"):
        self.unified_msg_origin = "umo-test"
        self.sent = []
        self._text = text

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "123"

    def get_message_str(self):
        return self._text

    async def send(self, chain):
        self.sent.append(chain)


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    def __init__(self, text="NO", boom=False):
        self.text = text
        self.boom = boom
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("provider 挂了")
        return FakeResp(self.text)


def build(cm, pm, cfg):
    o = m.SmartReplyPlugin.__new__(m.SmartReplyPlugin)
    o.context = FakeCtx(cm, pm)
    o.config = cfg
    o.cooldown = m.CooldownTracker()
    o.burst = m.BurstLimiter()
    o.freq = m.FreqFilter()
    o.verdicts = m.VerdictCache(ttl=o._cache_ttl(), span=o._cache_span())
    o.backoff = m.BackoffTracker(
        ack_seconds=o._ack_seconds(),
        miss_limit=o._ignore_limit(),
        max_level=o._max_level(),
    )
    o._last_llm_at = {}
    o._conv_ids = {}
    o._inflight = set()
    o._warn_at = {}
    return o


def history(n):
    return [{"role": "user", "content": f"m{i}"} for i in range(n)]


async def run():
    print("[1] 人格拼接")
    base = "# 判定要求"
    merged = m.SmartReplyPlugin._compose_system_prompt(base, "【人格】魔女")
    check("人格在前", merged.startswith(N + "# Persona Instructions" + N + N))
    check("人格被完整保留", "【人格】魔女" in merged)
    check("任务在后", merged.index("# 插话任务") > merged.index("# Persona Instructions"))
    check("原提示词收尾", merged.endswith("# 判定要求"))
    check("无人格时原样返回", m.SmartReplyPlugin._compose_system_prompt(base, "") == base)

    print("[2] 会话与历史")
    cm = FakeCM(FakeConv(history(6)))
    o = build(cm, FakePM(), {"link_session": True, "link_context_max_msgs": 3})
    ctxs, persona, cid = await o._link_session(FakeEvent(), "aiocqhttp:GroupMessage:123")
    check("只取最近 3 条", [c["content"] for c in ctxs] == ["m3", "m4", "m5"])
    check("拿到人格", persona == "【人格】白毛紫瞳魔女")
    check("拿到会话 id", cid == "cid-1")

    o.config = {"link_session": True, "link_context_max_msgs": 0}
    ctxs, persona, _cid = await o._link_session(FakeEvent(), "umo2")
    check("上限 0 时仍带人格", persona and ctxs is None)

    o.config = {"link_session": False}
    check("关闭后完全降级", await o._link_session(FakeEvent(), "umo3") == (None, "", None))

    print("[3] 坏数据不抛异常")
    cm2 = FakeCM(FakeConv([]))
    cm2.conv.history = "{不是合法 json"
    o2 = build(cm2, FakePM(), {"link_session": True, "link_context_max_msgs": 10})
    ctxs, persona, cid = await o2._link_session(FakeEvent(), "umo4")
    check("坏 history 只丢上下文", ctxs is None and persona and cid == "cid-1")

    o3 = build(cm, FakePM(), {"link_session": True})
    o3.context.conversation_manager = None
    check("无会话管理器不炸", await o3._link_session(FakeEvent(), "umo5") == (None, "", None))

    print("[4] 写回会话历史")
    o.pairs_before = None
    await o._record_reply("cid-1", "最后一条群消息", "我觉得可以哦")
    check("写入一对", cm.pairs and cm.pairs[-1][0] == "cid-1")
    check("role 正确", cm.pairs[-1][1]["role"] == "user"
          and cm.pairs[-1][2]["role"] == "assistant")
    before = len(cm.pairs)
    await o._record_reply(None, "x", "y")
    await o._record_reply("cid-1", "x", "")
    check("缺 id 或空回复时跳过", len(cm.pairs) == before)

    print("[5] 缓存命中补会话 id")
    cm3 = FakeCM(FakeConv(history(1)))
    o4 = build(cm3, FakePM(), {"link_session": True, "link_context_max_msgs": 10})
    check("首次取到并缓存", await o4._ensure_conv_id(FakeEvent(), "umo9") == "cid-1")
    cm3.conv = None
    check("二次直接命中缓存", await o4._ensure_conv_id(FakeEvent(), "umo9") == "cid-1")
    o4.config = {"link_session": False}
    check("关闭后返回 None", await o4._ensure_conv_id(FakeEvent(), "umo10") is None)
    cm3.conv = FakeConv(history(1))
    o4.config = {"link_session": True, "link_context_max_msgs": 10}
    o4._conv_ids["umo9"] = ("old-cid", 0.0)
    check("缓存过期后重新取", await o4._ensure_conv_id(FakeEvent(), "umo9") == "cid-1")

    print("[6] 判定确实调用模型")
    prov = FakeProvider("NO")
    cm4 = FakeCM(FakeConv(history(5)))
    o5 = build(cm4, FakePM(), {"link_session": True, "link_context_max_msgs": 3})
    o5._warn_at = {}
    o5.context.get_using_provider = lambda umo: prov
    v = await o5._ask_llm(FakeEvent(), [("u1", "在聊作业")], "umo-prov")
    check("真的调了一次模型", len(prov.calls) == 1)
    kw = prov.calls[0] if prov.calls else {}
    check("复用当前会话 id", kw.get("session_id") == "umo-prov")
    check("历史真的传了进去",
          isinstance(kw.get("contexts"), list) and len(kw["contexts"]) == 3)
    check("人格进了 system_prompt",
          "# Persona Instructions" in str(kw.get("system_prompt"))
          and "【人格】白毛紫瞳魔女" in str(kw.get("system_prompt")))
    check("判定结果可用", v.should_reply is False)

    boom = FakeProvider(boom=True)
    o5.context.get_using_provider = lambda umo: boom
    v2 = await o5._ask_llm(FakeEvent(), [("u1", "x")], "umo-prov")
    check("调用失败当作不回复", v2.should_reply is False and v2.source == "llm-error")

    print("[7] 并发判定不重入")
    check("初始 in-flight 为空", not o5._inflight)
    await o5._record_reply("cid-1", "u", "a")
    check("写回仍正常", cm4.pairs and cm4.pairs[-1][0] == "cid-1")

    print("[8] 缓存命中路径也受 in-flight 保护")
    prov8 = FakeProvider("YES|缓存也能走通")
    cm8 = FakeCM(FakeConv(history(3)))
    o8 = build(cm8, FakePM(), {"cooldown_seconds": 0, "burst_limit": 0})
    o8._warn_at = {}
    o8.context.get_using_provider = lambda umo: prov8
    sk8 = "umo-conc"
    t8 = time.monotonic()
    for i in range(5):
        o8.freq.observe(sk8, "u", f"msg{i}", now=t8)
    ev1 = FakeEvent("哦")
    await o8._maybe_reply(ev1, sk8, False)
    check("第一次问了模型", len(prov8.calls) == 1)
    check("第一次发送成功", len(ev1.sent) == 1)
    ev2 = FakeEvent("哦")
    await o8._maybe_reply(ev2, sk8, False)
    check("第二次命中缓存没再问模型", len(prov8.calls) == 1)
    check("缓存路径也发送了", len(ev2.sent) == 1)
    o8._inflight.add(sk8)
    ev3 = FakeEvent("哦")
    await o8._maybe_reply(ev3, sk8, False)
    check("在飞时不重复发送", not ev3.sent)
    check("在飞标记没被误清", sk8 in o8._inflight)


asyncio.run(run())
print()
if FAILED:
    print(f"{len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
