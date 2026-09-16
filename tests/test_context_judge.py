#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context_judge 离线单元测试：python3 tests/test_context_judge.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context_judge import (COARSE_GRAY, COARSE_HIGH, COARSE_LOW,  # noqa: E402
                           BackoffTracker, FreqFilter, Verdict,
                           VerdictCache, build_judge_prompt,
                           cooldown_multiplier, format_messages,
                           parse_judge_reply, scaled_min_msgs, summarize)

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


T = 1000.0

print("[1] 频次粗筛：冷清 / 灰区 / 刷屏 / 被点名")
freq = FreqFilter()
check("没有消息 → 冷清", freq.coarse("g1", window=120, min_msgs=3, now=T)[0] == COARSE_LOW)
freq.observe("g1", "u1", "在吗", now=T)
freq.observe("g1", "u2", "在的", now=T)
kind, info = freq.coarse("g1", window=120, min_msgs=3, now=T)
check("2 条 < 门槛 3 → 冷清", kind == COARSE_LOW)
check("冷清原因带条数", "2" in info["reason"])
freq.observe("g1", "u3", "今天天气不错", now=T)
kind, info = freq.coarse("g1", window=120, min_msgs=3, now=T)
check("3 条 → 灰区", kind == COARSE_GRAY)
check("统计人数 = 3", info["senders"] == 3)
kind, info = freq.coarse("g1", window=120, min_msgs=3, spam_msgs=3, now=T)
check("达到刷屏阈值 → 冷清", kind == COARSE_LOW)
check("刷屏原因", "刷屏" in info["reason"])
kind, _ = freq.coarse("g1", window=120, min_msgs=99, addressed=True, now=T)
check("被点名 → 直接 HIGH", kind == COARSE_HIGH)
kind, info = freq.coarse("g1", window=10, min_msgs=3, now=T + 60)
check("窗口外的消息不算 → 冷清", kind == COARSE_LOW)
check("过期消息被窗口过滤", freq.stats("g1", window=10, now=T + 60)["msgs"] == 0)

print("[2] 频次粗筛：会话隔离与容量上限")
freq.observe("g2", "u9", "另一个群", now=T)
check("会话隔离：g2 只有 1 条", freq.stats("g2", window=120, now=T)["msgs"] == 1)
check("g1 不受影响", freq.stats("g1", window=120, now=T)["msgs"] == 3)
small = FreqFilter(max_per_session=3, max_sessions=2)
for i in range(5):
    small.observe("s1", "u1", f"m{i}", now=T)
check("单会话只留最近 3 条", small.stats("s1", window=120, now=T)["msgs"] == 3)
small.observe("s2", "u1", "x", now=T)
small.observe("s3", "u1", "y", now=T)
check("超过 max_sessions 淘汰最旧会话", small.stats("s1", window=120, now=T)["msgs"] == 0)
freq.forget("g1")
check("forget 清掉会话", freq.stats("g1", window=120, now=T)["msgs"] == 0)
freq.forget()
check("forget() 全清", freq.stats("g2", window=120, now=T)["msgs"] == 0)

print("[3] 缓存：同一串消息只问一次")
msgs = [("u1", "今天好热"), ("u2", "是啊，想吃冰")]
cache = VerdictCache(ttl=180, span=8)
check("首次未命中", cache.get("g1", msgs, now=T) is None)
cache.put("g1", msgs, Verdict(True, "那就来根冰棍吧", "llm"), now=T)
hit = cache.get("g1", msgs, now=T + 10)
check("同串复用命中", hit is not None and hit.should_reply is True)
check("复用的来源标记为 cache", hit.source == "cache")
check("复用的回复内容一致", hit.reply == "那就来根冰棍吧")
check("TTL 过期后未命中", cache.get("g1", msgs, now=T + 200) is None)
cache.put("g1", msgs, Verdict(False, "", "llm"), now=T)
check("别的会话不串味", cache.get("g2", msgs, now=T) is None)
check("消息变了就不算同串", cache.get("g1", msgs + [("u3", "我请")], now=T) is None)
check("空白差异不影响签名",
      cache.get("g1", [("u1", " 今天好热 "), ("u2", "是啊，想吃冰")], now=T) is not None)
cache.put("g1", msgs, Verdict(False, "", "llm"), now=T)
cache.cleanup(now=T + 1000)
check("cleanup 清掉过期项", len(cache) == 0)
cache2 = VerdictCache(ttl=180, max_entries=2)
cache2.put("g1", [("u1", "a")], Verdict(True, "x"), now=T)
cache2.put("g1", [("u1", "b")], Verdict(True, "y"), now=T)
cache2.put("g1", [("u1", "c")], Verdict(True, "z"), now=T)
check("条目数不超过上限", len(cache2) == 2)
cache2.forget("g1")
check("forget 清掉该会话缓存", len(cache2) == 0)

print("[4] 被无视降频")
back = BackoffTracker(ack_seconds=60, miss_limit=2, max_level=4)
check("初始档位 0", back.level("g1", now=T) == 0)
check("没人回过话 → observe 返回 False", back.observe("g1", now=T) is False)
back.note_reply("g1", now=T)
check("ack 窗口内还没被无视", back.stats("g1", now=T + 30)["level"] == 0)
check("超时没人接话 → 记一次被无视", back.stats("g1", now=T + 61)["misses"] == 1)
check("未达 miss_limit 档位仍为 0", back.level("g1", now=T + 61) == 0)
back.note_reply("g1", now=T + 61)
check("第二次被无视 → misses=2", back.stats("g1", now=T + 130)["misses"] == 2)
check("达到 miss_limit → 档位 1", back.level("g1", now=T + 130) == 1)
back.note_reply("g1", now=T + 130)
check("misses=3 仍为档位 1", back.level("g1", now=T + 200) == 1)
back.note_reply("g1", now=T + 200)
check("misses=4 → 档位 2", back.level("g1", now=T + 300) == 2)
back.observe("g1", now=T + 300)
check("窗口外冒头不算接话", back.stats("g1", now=T + 300)["misses"] == 4)
back.note_reply("g1", now=T + 300)
check("窗口内有人接话 → observe 返回 True", back.observe("g1", now=T + 330) is True)
check("有人接话 → 档位清零", back.level("g1", now=T + 330) == 0)
check("接话次数被记下", back.stats("g1", now=T + 330)["acks"] == 1)
check("待接话标记已清除", back.stats("g1", now=T + 330)["pending"] is False)
for _ in range(12):
    back.note_reply("g1", now=T)
    back.observe("g1", now=T + 61)
check("档位不超过上限 4", back.stats("g1", now=T + 61)["level"] == 4)
check("会话隔离：g2 仍为 0", back.level("g2", now=T) == 0)
back.reset("g1")
check("reset 清掉会话", back.level("g1", now=T) == 0)

print("[5] 降频换算")
check("档位 0 不抬高门槛", scaled_min_msgs(3, 0, 2) == 3)
check("每档抬高 step 条", scaled_min_msgs(3, 2, 2) == 7)
check("门槛至少为 1", scaled_min_msgs(0, 0, 0) == 1)
check("档位 0 冷却不放大", cooldown_multiplier(0, 2.0, 8.0) == 1.0)
check("档位 3 → 8 倍", cooldown_multiplier(3, 2.0, 8.0) == 8.0)
check("冷却受上限约束", cooldown_multiplier(9, 2.0, 8.0) == 8.0)

print("[6] 判断结果解析")
check("YES 无内容", parse_judge_reply("YES").should_reply is True)
check("YES 无内容时回复为空", parse_judge_reply("YES").reply == "")
check("YES|内容 取出回复", parse_judge_reply("YES|那就来根冰棍吧").reply == "那就来根冰棍吧")
check("中文「合适」也认", parse_judge_reply("合适|好耶").should_reply is True)
check("NO 不回复", parse_judge_reply("NO").should_reply is False)
check("中文「不回」不回复", parse_judge_reply("不回").should_reply is False)
check("小写 no 也认", parse_judge_reply("no").should_reply is False)
check("代码块包裹能剥掉", parse_judge_reply("```\nYES|好呀\n```").reply == "好呀")
check("带引号的回复去引号", parse_judge_reply('YES|"好呀"').reply == "好呀")
check("空输出不回复", parse_judge_reply("   ").should_reply is False)
check("空输出来源标记", parse_judge_reply("").source == "llm-empty")
check("乱输出保守不回", parse_judge_reply("嗯……让我想想").should_reply is False)
check("乱输出来源标记", parse_judge_reply("嗯……让我想想").source == "llm-unclear")
check("多行只看第一行", parse_judge_reply("NO\nYES|好呀").should_reply is False)
check("超长回复被截断", len(parse_judge_reply("YES|" + "字" * 300, max_reply_chars=20).reply) == 20)
check("YES 行的尾随解释不影响判定", parse_judge_reply("YES，可以插一句").should_reply is True)

print("[7] 提示词组装")
text = format_messages([("u1", "今天好热"), ("机器人", "来根冰棍？"), ("u2", "   ")],
                       bot_name="机器人")
check("跳掉空消息", "u2" not in text)
check("机器人被标注", "机器人(机器人)" in text)
prompt = build_judge_prompt(msgs, bot_name="宁宁", session="群聊")
check("记录与场景被填充", "群聊" in prompt and "今天好热" in prompt)
check("内置模板不依赖昵称配置", "机器人叫" not in prompt)
check("含 YES 约定", "YES" in prompt)
custom = build_judge_prompt(msgs, bot_name="宁宁", session="私聊",
                            template="{session}｜{bot_name}｜{messages}")
check("自定义模板生效", custom.startswith("私聊｜宁宁｜"))
check("模板占位符写错时回退内置模板",
      "判断标准" in build_judge_prompt(msgs, template="{oops}"))
long_text = format_messages([("u1", "字" * 5000)], max_chars=200)
check("超长记录被截断", len(long_text) <= 210)
check("日志摘要单行", "\n" not in summarize(msgs, bot_name="宁宁"))

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
