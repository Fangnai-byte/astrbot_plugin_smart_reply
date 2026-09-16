# 更新日志

## [2.0.1] - 2026-09-16

### 修复

- 合并语义重复的私聊开关：此前 `enable_private` 卡入口、`private_enabled` 在判断前再卡一次，
  写两个开关才能关掉私聊。现在只保留 `enable_private` 一个，删掉 `private_enabled`。
- `enable_private` 默认值由 `true` 改为 `false`，与「私聊一般不主动插话」的原意一致；
  已有配置里写了 `true` 的照旧生效，想关私聊只改这一项即可。

## [2.0.0] - 2026-09-16

**破坏性变更**：彻底移除关键词体系，本插件从「关键词自动回复」重写为真正的「自动回复」——
不再靠词表触发，而是在合适的时机自己接一句话。旧的关键词规则配置不再被读取，升级前请先备份配置。

### 移除

- 删除全部关键词相关配置：`rules`、`match_type`、`cooldown_scope`、`cooldown_hint`、`image_dir`
  以及去重三项（`dedup_enabled` / `dedup_window` / `dedup_seconds` / `dedup_same_user_only`）。
- 删除 `reply_core.py` 与其单元测试：规则解析、三种匹配、图片解析、去重窗口随之去掉。
- 删除语境判断的开关项本身：自动回复即语境判断，不再需要两个体系并存。

### 新增

- **纯语境自动回复**：没有触发词，机器人按聊天记录判断此刻插话是否合适；判断与发送全在
  `main.py` 的 `_maybe_reply` 流程里，粗筛不通过就不花 token。
- **`guard.py`**：触发范围与冷却抽成独立纯逻辑层（`parse_id_set` / `normalize_target_mode` /
  `check_target` / `CooldownTracker`），与 `context_judge.py` 一样零 AstrBot 依赖，可离线测试。
- **被点名直通**：@机器人或唤醒词命中时判为 `COARSE_HIGH`，跳过粗筛直接交给模型。
- **总开关 `enabled`**：关闭后连消息记录都不统计。
- **兜底话术 `fallback_reply`**：模型只回 `YES` 没给内容时顶上，留空则不发送。
- **提示词可自定义**：`judge_prompt` 支持 `{messages}` / `{bot_name}` / `{session}` 占位符，
  `judge_system_prompt` 可整体替换内置系统提示词。

### 变更

- 配置项统一去掉 `ctx_` 前缀，改用扁平命名：`window_seconds`、`min_msgs`、`spam_msgs`、
  `probe_msgs`、`private_enabled`、`cache_ttl`、`cache_span`、`min_interval`、`cooldown_seconds`、
  `ack_seconds`、`ignore_limit`、`max_level`、`level_step`、`backoff_base`、`backoff_cap`、
  `reply_max_chars`、`bot_name`、`judge_prompt`、`judge_system_prompt`。
- `cooldown_seconds` 语义变化：由「关键词规则冷却」变为「两次自动回复之间的基础冷却」，并随降频档位放大。
- 触发范围、白黑名单、`no_quote`、`ignore_user_ids` / `ignore_group_ids` 保持兼容，旧写法照旧生效。
- 判断调用使用独立 session_id（`<会话>:smart-reply-judge`），不再污染正常对话记忆。

### 测试

- `tests/test_context_judge.py`：频次粗筛与会话隔离、缓存复用与过期、被无视降频与换算、
  单行结论解析、提示词组装。

### 说明

- 本仓库为独立插件，拥有独立版本线与独立配置。
- 仅支持 `aiocqhttp` 平台，AstrBot 版本要求 `>= v4.12.0`。

## [1.0.0] - 2026-09-16

首个版本：只在指定群生效的自动回复，外加一层「该不该接话」的语境判断。

### 新增

- **触发词自动回复**：规则写在配置里，一行一条「触发词，回复内容，图片，冷却秒数，生效群号，生效QQ号」，
  后四项可省略；也支持 JSON 数组写法，回复内容里可以带逗号。
- **三种匹配**：`contains` / `exact` / `regex`，默认包含匹配，正则在解析时编译，写错不会崩。
- **图片回复**：只写图名会自动在图片目录里补扩展名，也支持绝对路径与 http(s) 链接；图片找不到时日志告警且不发空消息。
- **触发范围（全局）**：群与用户各自一套 `off` / `whitelist` / `blacklist` 三态，
  白名单为空一律不触发，黑名单任何模式下都生效且优先级最高；旧配置里的 `ignore_user_ids`、`ignore_group_ids` 视为黑名单。
- **规则级范围**：每条规则可单独限定生效群号 / 生效QQ号，写了群号时私聊永远不触发该规则；两层范围都通过才会回复。
- **防刷屏与去重**：规则冷却可选按群 / 按人 / 全局，冷却期间最多提示一次；
  「上面已有这条触发词」的去重可整体开关，窗口与时间范围可调。
- **语境判断回复**（默认关闭）：频次粗筛三态、灰区才调模型、同串消息缓存复用、
  单行协议 `YES` / `NO` / `YES|回复内容`、被无视自动降频、备用话术。

> 1.0.0 的关键词体系已在 2.0.0 中整体移除，此条目仅作历史记录。
