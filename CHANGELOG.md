## [2.2.0] - 2026-09-16

### 新增

- `link_session`（默认开）：插话接回当前会话，跟主流程同一个人格、同一份记忆。此前插件用独立 session_id
  （`<会话>:smart-reply-judge`）直调提供商，框架不会注入人格、也没有上下文，等于在空会话里另起一段对话。
  现在从 `conversation_manager` 取 `history` 当上下文，从 `persona_manager.resolve_selected_persona` 取人格，
  按主流程的拼法放进 `system_prompt`（`# Persona Instructions` 在前、插话任务在后），判定格式要求不会被盖掉。
- `link_context_max_msgs`（默认 10）：只带最近这么多条历史，`0` 表示只带人格不带记忆。
- 回复成功后用 `conversation_manager.add_message_pair` 把这次插话写回会话历史，主流程后续对话知道机器人说过这句。
- 缓存命中跳过判定时，补一次会话 id 查询，保证写回历史不漏。

### 修复

- `_ask_llm` 补回真实模型调用：此前只拼好提示词、取到会话上下文，却从未调用 `provider.text_chat`，
  未定义的 `resp` 抛出 `NameError` 被兜底 except 吞掉，表现为判定永远不回复、功能整体失效。
  现在以当前会话 `umo` 作 `session_id` 真正发起请求，`contexts` 传历史、`system_prompt` 传人格，
  调用失败仍按「不回复」降级。
- 会话 id 缓存加 60 秒保鲜期：会话重置或换新对话后，旧 id 不会一直粘在 umo 上把插话写进废弃会话；
  `umo` 为空时不再用 `smart-reply` 占位键污染缓存。
- 判定过程加 in-flight 标记：同一会话并发进来的消息不会同时问模型，避免重复计费与重复回复。
- `_compose_system_prompt` 独立成函数：人格拼接改用显式 `\n` 组装，避免跨行字符串被编辑写坏导致语法错误。
- 写回会话历史失败的日志从 debug 提到 warning，并说明主流程可能不知道自己说过这句。
- 插话人格解析加 umo 路由兜底：框架 `resolve_selected_persona` 在走 `astrbot_config_mgr` 分支时会静默回落全局配置
  （全局 `persona_id` 为 `default`，拿不到具体人格），插话侧于是拿不到人格却毫无提示。现在解析失败或人格为空时，
  改按 `umo` 依次读会话级 `session_service_config.persona_id` 与 `astrbot_config_mgr.get_conf(umo)` 里的 `persona_id`，
  命中即用，且会话级配置优先于路由配置；两条路都拿不到才输出一条节流 WARN 告警（此前只写 debug，静默失败外面看不见）。
- `_fallback_reply` 的兜底文案同样按 `reply_max_chars` 截断，不再出现兜底话术比模型回复还长、把单次字数上限撑破的情况。
- 会话 id 缓存补条数上限与按时间淘汰（`CONV_ID_TTL`）：超过上限先清掉早已过期的条目，长期运行也不会让 `_conv_ids` 无限增长。

### 测试

- 新增 `tests/test_smart_reply_link.py`：桩测 `_link_session` / `_ensure_conv_id` / `_record_reply` /
  `_ask_llm`，覆盖历史截断、上限为 0、人格拼接顺序、写回 role、`link_session=false` 降级、
  坏数据不抛异常、缓存过期重取，以及判定确实调用模型（断言 `session_id` / `contexts` / 人格三者都到位）
  与 provider 抛异常时降级为不回复。
- 新增 `tests/test_persona_route.py`：覆盖框架解析成功时不兜底、框架拿空人格时按 umo 路由兜底、框架抛异常仍能兜底、
  会话级配置优先于路由配置、两条路皆空时输出节流告警，共 5 组用例。

### 说明

- 兼容：`link_session=false` 退回旧行为（独立空会话、不读历史、不写回），群聊基本可用性不受影响。
- 兼容：`no_quote` 自 2.2.0 起不再生效——发送统一走 `event.send`，恒为直接发送、不引用原消息。
  该项仅为兼容旧配置保留，改动它不影响实际行为（README 与配置项描述已同步）。

# 更新日志

## [2.1.0] - 2026-09-16

### 变更

- 删除 `enable_private` 配置项，私聊一律不参与。私聊会被框架的 waking_check 判定为「被点名」
  （`is_at_or_wake_command` 恒为真，且本机 `friend_message_needs_wake_prefix` 为 `false`），
  进来就直通粗筛，只会抢先插在你和机器人的正常对话前，所以私聊直接跳过。
- 旧配置里残留的 `enable_private` 键会被忽略，不影响加载，也不影响群聊行为。

## [2.0.3] - 2026-09-16

### 变更

- 删除 `bot_name` 配置项：昵称统一由 `astrbot_plugin_full_prompt` 管理，本插件不再重复配置；
  内置判断提示词不再自称具体名字，改用「记录里带 (机器人) 标注的是机器人说过的话」描述。
- `build_judge_prompt` / `format_messages` 的 `bot_name` 参数保留且默认 `机器人`，
  旧的自定义模板里写了 `{bot_name}` 也不会报错，照常填成 `机器人`。

### 测试

- `tests/test_context_judge.py`：改为校验记录与场景占位符，并确认内置模板不依赖昵称配置。

## [2.0.2] - 2026-09-16

### 新增

- **滑动窗口限次**：`burst_window_seconds` 秒内自动回复最多 `burst_limit` 次，默认 120 秒 2 次，
  防止话头一多就一直插话；窗口是滑动的，最早那次回复出窗即释放额度，不用等整段窗口清零。
- 两项任一填 `0` 即关闭限次；手动指令触发与被点名直通不受限次影响。

### 变更

- **冷却与限次分层**：冷却决定「隔多久才能再说」，限次决定「这段时间里一共能说几次」，
  先过冷却再看限次，两者同时生效。
- 限次只统计真正发出去的自动回复，被点名回复也算在内（模型判为不回复的不占额度）。
- `guard.py` 新增 `BurstLimiter`（`allow` / `note` / `count` / `remaining` / `cleanup`），
  与 `CooldownTracker` 一样零 AstrBot 依赖，可离线测试。

### 测试

- `tests/test_guard.py`：窗口滑动释放、额度用尽拦下、`0` 关闭限次、超窗记录清理。

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
