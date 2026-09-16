#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插话人格解析兜底离线测试：python3 tests/test_persona_route.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m  # noqa: E402
from astrbot.api import sp  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


class FakePM:
    def __init__(self, framework_persona, route_persona="【人格】绫地宁宁"):
        self.framework_persona = framework_persona
        self.route_persona = route_persona
        self.framework_calls = 0
        self.route_calls = 0

    async def resolve_selected_persona(self, umo=None, conversation_persona_id=None,
                                       platform_name=None):
        self.framework_calls += 1
        if isinstance(self.framework_persona, Exception):
            raise self.framework_persona
        return ("x", self.framework_persona, None, False)

    def get_persona_v3_by_id(self, persona_id):
        self.route_calls += 1
        if persona_id == "绫地宁宁" and self.route_persona:
            return {"name": persona_id, "prompt": self.route_persona}
        return None


class FakeACM:
    def __init__(self, pid="绫地宁宁"):
        self.pid = pid
        self.calls = 0

    def get_conf(self, umo):
        self.calls += 1
        return {
            "agent_runner": {
                "runner_type": "local",
                "config": {"persona": {"persona_id": self.pid}},
            }
        }


class FakeCtx:
    def __init__(self, pm, acm):
        self.persona_manager = pm
        self.astrbot_config_mgr = acm


def fake_sp(payload):
    async def _get_async(**kwargs):
        return payload

    return _get_async


class FakeEvent:
    def get_platform_name(self):
        return "aiocqhttp"


def build(pm, acm=None):
    o = m.SmartReplyPlugin.__new__(m.SmartReplyPlugin)
    o.context = FakeCtx(pm, acm)
    o.config = {}
    o._warn_at = {}
    return o


async def run():
    real_get_async = sp.get_async

    try:
        print("[1] 框架解析成功时不走兜底")
        pm = FakePM({"prompt": "【人格】框架给的"})
        acm = FakeACM()
        o = build(pm, acm)
        sp.get_async = fake_sp({"persona_id": "别的"})
        got = await o._resolve_persona_prompt(FakeEvent(), "绫地宁宁:GroupMessage:1", None)
        check("用框架的人格", got == "【人格】框架给的")
        check("没碰路由", not pm.route_calls and not acm.calls)

        print("[2] 框架拿不到人格时按 umo 路由兜底")
        pm = FakePM(None)
        acm = FakeACM()
        o = build(pm, acm)
        sp.get_async = fake_sp({})
        got = await o._resolve_persona_prompt(FakeEvent(), "绫地宁宁:GroupMessage:1", None)
        check("兜底命中人格", got == "【人格】绫地宁宁")
        check("读的是本会话配置", acm.calls == 1)

        print("[3] 框架抛异常也照样兜底")
        pm = FakePM(RuntimeError("解析炸了"))
        acm = FakeACM()
        o = build(pm, acm)
        sp.get_async = fake_sp({})
        got = await o._resolve_persona_prompt(FakeEvent(), "绫地宁宁:GroupMessage:1", None)
        check("异常不吞掉兜底", got == "【人格】绫地宁宁")

        print("[4] 会话级配置优先于路由配置")
        pm = FakePM(None)
        acm = FakeACM(pid="不该被用")
        o = build(pm, acm)
        sp.get_async = fake_sp({"persona_id": "绫地宁宁"})
        got = await o._resolve_persona_prompt(FakeEvent(), "绫地宁宁:GroupMessage:1", None)
        check("用了会话级人格", got == "【人格】绫地宁宁")
        check("没回落 acm", acm.calls == 0)

        print("[5] 两路都拿不到就给可观测告警")
        pm = FakePM(None, route_persona="")
        acm = FakeACM(pid="查无此人")
        o = build(pm, acm)
        sp.get_async = fake_sp({})
        got = await o._resolve_persona_prompt(FakeEvent(), "绫地宁宁:GroupMessage:1", None)
        check("返回空串", got == "")
        check("留下告警痕迹", "persona" in o._warn_at)
    finally:
        sp.get_async = real_get_async

    print()
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
