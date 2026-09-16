#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard 离线单元测试：python3 tests/test_guard.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guard import (CooldownTracker, check_target, normalize_target_mode,  # noqa: E402
                   parse_id_set)

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


print("[1] 名单解析")
check("None → 空集合", parse_id_set(None) == set())
check("列表照收", parse_id_set(["123", "456"]) == {"123", "456"})
check("数字自动转字符串", parse_id_set([123, 456]) == {"123", "456"})
check("元组照收", parse_id_set(("1", "2")) == {"1", "2"})
check("逗号分隔", parse_id_set("1,2,3") == {"1", "2", "3"})
check("中英文分号与顿号", parse_id_set("1；2;3、4") == {"1", "2", "3", "4"})
check("空白与换行分隔", parse_id_set("1\n2 3") == {"1", "2", "3"})
check("加号分隔（兼容旧写法）", parse_id_set("1+2") == {"1", "2"})
check("井号分隔", parse_id_set("1#2") == {"1", "2"})
check("去重", parse_id_set("1,1,2") == {"1", "2"})
check("空串与多余空白", parse_id_set("  , 1 , ") == {"1"})

print("[2] 模式规整")
check("原样通过", normalize_target_mode("whitelist") == "whitelist")
check("大写转小写", normalize_target_mode("BLACKLIST") == "blacklist")
check("带空白", normalize_target_mode("  off  ") == "off")
check("非法值回落默认", normalize_target_mode("banana") == "off")
check("空值回落默认", normalize_target_mode("") == "off")
check("自定义默认值", normalize_target_mode(None, default="blacklist") == "blacklist")

print("[3] 触发范围校验：off")
check("off + 空名单放行", check_target("123", "off")[0] is True)
check("odd 模式名按 off 处理", check_target("123", "whatever")[0] is True)
check("off 下黑名单仍生效", check_target("123", "off", blacklist=["123"])[0] is False)
check("off 下黑名单原因", check_target("123", "off", blacklist=["123"])[1] == "在黑名单")
check("off 下名单外用字符串", check_target("123", "off", blacklist="999,123")[0] is False)
check("off 下名单外放行", check_target("123", "off", blacklist="999")[0] is True)

print("[4] 触发范围校验：whitelist / blacklist")
check("白名单命中放行", check_target("123", "whitelist", whitelist=["123"])[0] is True)
check("白名单未命中拦截", check_target("123", "whitelist", whitelist=["456"])[0] is False)
check("白名单未命中原因", check_target("123", "whitelist", whitelist=["456"])[1] == "不在白名单")
check("白名单为空一律拦截", check_target("123", "whitelist", whitelist=[])[0] is False)
check("白名单为空原因", check_target("123", "whitelist")[1] == "白名单为空")
check("黑名单命中拦截", check_target("123", "blacklist", blacklist=["123"])[0] is False)
check("黑名单外放行", check_target("123", "blacklist", blacklist=["456"])[0] is True)
check("黑名单优先于白名单", check_target("123", "whitelist", whitelist=["123"], blacklist=["123"])[0] is False)
check("黑名单优先原因", check_target("123", "whitelist", whitelist=["123"], blacklist=["123"])[1] == "在黑名单")
check("数字 ID 也能匹配字面名单", check_target(123, "whitelist", whitelist=["123"])[0] is True)
check("空白 ID 不算白名单命中", check_target("  ", "whitelist", whitelist=["123"])[0] is False)
check("空白 ID 在白名单模式下原因", check_target("", "whitelist", whitelist=["123"])[1] == "不在白名单")

print("[5] 冷却记录")
cd = CooldownTracker()
T = 5000.0
check("未记录时剩余 0", cd.remaining("g1", now=T) == 0.0)
check("冷却 0 直接放行", cd.allow("g1", 0, now=T) is True)
cd.touch("g1", 0, now=T)
check("冷却 0 不写记录", cd.remaining("g1", now=T) == 0.0)
check("未记录时允许", cd.allow("g1", 60, now=T) is True)
cd.touch("g1", 60, now=T)
check("记录后剩余时间正确", cd.remaining("g1", now=T) == 60.0)
check("冷却中不允许", cd.allow("g1", 60, now=T + 30) is False)
check("冷却中剩余递减", cd.remaining("g1", now=T + 30) == 30.0)
check("到期后允许", cd.allow("g1", 60, now=T + 60) is True)
check("超期剩余不出现负数", cd.remaining("g1", now=T + 999) == 0.0)
check("会话隔离：g2 不受影响", cd.allow("g2", 60, now=T + 30) is True)
cd.touch("g2", 60, now=T + 30)
check("会话隔离：g2 已记录", cd.remaining("g2", now=T + 30) == 60.0)
cd.reset("g1")
check("reset 单个键", cd.allow("g1", 60, now=T + 30) is True)
check("reset 不影响别的键", cd.allow("g2", 60, now=T + 30) is False)
cd.cleanup(now=T + 100)
check("cleanup 清掉过期项", cd.allow("g2", 60, now=T + 100) is True)
check("cleanup 后剩余为 0", cd.remaining("g2", now=T + 100) == 0.0)
cd.touch("g3", 60, now=T)
cd.touch("g4", 60, now=T)
cd.reset()
check("reset 全清", cd.remaining("g3", now=T) == 0.0)
check("reset 全清 2", cd.remaining("g4", now=T) == 0.0)

print("\n全部通过" if not FAILED else f"\n失败 {len(FAILED)} 项：{FAILED}")
sys.exit(1 if FAILED else 0)
