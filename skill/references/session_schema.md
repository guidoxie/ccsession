# Claude Code 会话 JSONL 字段速查

供 skill 维护时参考。每条会话存储为 `~/.claude/projects/{encoded}/{sessionId}.jsonl`，每行一条 JSON。

## 项目路径编码

绝对路径中 `/`、`_`、`.` **三种字符全部**替换为 `-`。例：

- `/home/foo/bar` → `-home-foo-bar`
- `/home/foo/my_app` → `-home-foo-my-app`（`_` 也转）
- `/Users/foo/.claude` → `-Users-foo--claude`（双横线来自 `/` 和 `.` 各转一次）

**编码不可逆** —— `_` `.` `/` 都压成 `-` 后无法还原。`find_orphans.py` 判定孤儿时不要尝试反向解码项目目录，应该正向 `encode_project_path()` 进程 cwd 后查集合。

## 行类型（顶层 `type`）

| type | 含义 |
|---|---|
| `permission-mode` | 权限模式变更（会话首行常见） |
| `file-history-snapshot` | 文件历史快照 |
| `user` | 用户消息；`message.content` 为字符串时是**真实提问**，为数组且元素含 `tool_use_id` 时是**工具返回结果** |
| `assistant` | AI 回复；`message.content` 是数组，元素 `type` 可为 `text` / `thinking` / `tool_use` |
| `attachment` | 附件：`attachment.type` 可为 `skill_listing` / `plan_mode` / `deferred_tools_delta` 等 |
| `last-prompt` | Claude Code 主动落盘的"会话最后一条用户提示"——见下方"用户提示来源"段 |
| `system` | 系统事件；`subtype=="api_error"` 等表示 API 重试 |

注：**没有** `type=="summary"` 行——`raw_summary` 真源是 `type=="user"` + `isCompactSummary: true`，见下。

## 常见顶层字段

- `sessionId` — UUID，文件名即此
- `timestamp` — ISO8601 UTC（如 `2026-04-19T03:54:27.279Z`）
- `uuid` / `parentUuid` — 消息链
- `cwd` — 当时的工作目录（`fetch_commits_from_git()` 用它跑 `git log -C {cwd}`）
- `gitBranch` / `slug` / `version`
- `requestId` — assistant 一次推理的请求 ID（见"Token 去重"段）
- `apiErrorStatus` — 行级 API 错误状态码；存在即计入 `api_errors`

## 用户提问识别

- `type=="user"` 且 `message.content` 为字符串 → 真实用户输入
- `message.content` 为数组（含 `tool_use_id`）→ 工具结果回传，不计入对话轮次
- 系统注入仍要过滤（`<local-command-caveat>` `<bash-stdout>` `<bash-stdin>` 等），见 `is_real_question()`

## `isCompactSummary` —— `raw_summary` 真源

用户跑 `/compact` 时，Claude Code 把上一段会话压缩成长文本，写入**新会话**首条 user 行：

```json
{"type": "user", "isCompactSummary": true, "message": {"content": "This session is being continued from a previous conversation... Summary: ..."}}
```

`message.content` 是叙事性长文本——脚本据此抽取作 `raw_summary`。**它描述的是上一段会话**、不是当前会话；AI 综合摘要时只能当"开场背景"用，不能直接照抄。

## `last-prompt` 行 —— `lastPrompt` 字段

```json
{"type": "last-prompt", "lastPrompt": "帮我提交"}
```

是 Claude Code 主动落盘的"该会话最后一条用户提示"，比扫描尾部 user 行更准（不会被 `/compact` 等系统注入污染）。`fetch_commits_from_git` 区间用 `start..end`，`last_prompt` 用本行抽。

## 识别 AI 工具调用

`type=="assistant"` 的 `message.content` 数组里，元素 `type=="tool_use"`：

- `name` — 工具名（Read / Edit / Bash / Grep / Glob / Task / Skill / `mcp__*` / …）
- `input` — 工具参数对象
- `id` — 工具调用 ID（后续 user 行通过 `tool_use_id` 关联返回）

`message.model` 以 `<` 开头（如 `<synthetic>`）是 subagent 内部占位，**必须过滤**——否则会把占位模型混进 `models` 列。

## Token 字段

`type=="assistant"` 行的 `message.usage`：

- `input_tokens`
- `output_tokens`
- `cache_creation_input_tokens`
- `cache_read_input_tokens`

### 按 `requestId` 去重（强制）

同一条 message 会被 Claude Code 拆成多行（thinking / text / tool_use 各一行，共享相同的 `requestId` 和 `message.id`）。**必须按 `requestId` 去重累加 token**，否则会重复累加约 2-3x（假装会话 token 量是实际的 2-3 倍）。

`parse_sessions.aggregate()` 里：

```python
seen_requests: set[str] = set()
...
req_id = rec.get("requestId")
if req_id and req_id in seen_requests:
    continue
if req_id:
    seen_requests.add(req_id)
# 累加 usage
```

## 会话开始 / 结束

- 开始：第一行 `timestamp`
- 结束：最后一行 `timestamp`

`duration = end - start`。`fetch_commits_from_git()` 用这个区间跑 `git log --since={start} --until={end}`。

## API 错误 / 重试

- **错误**（`api_errors`）：行级 `apiErrorStatus` 字段存在即计入。
- **重试**（`api_retries`）：`type=="system"` + `subtype=="api_error"` 计入。

## Subagent 目录布局

会话独有的 subagent 工作目录：`{projectDir}/{sessionId}/subagents/`

```
{projectDir}/{sessionId}/
├── subagents/
│   ├── agent-<agentId>.meta.json   # 元信息：agentType / description
│   └── agent-<agentId>.jsonl       # subagent 自己的会话 jsonl（含 token usage）
└── tool-results/                   # 工具调用结果缓存（大文件）
```

`_analyze_subagents()` 扫 `agent-*.meta.json`，取 `agentType` / `description`；对应 jsonl 累加 token。**注意**：`Path("agent-xxx.meta.json").stem` 给的是 `agent-xxx.meta`——提取 `agent_id` 时要先 `.replace("agent-", "")` 再 `.removesuffix(".meta")`。

### Subagent token 经常为 0

Claude Code 对非主模型的 subagent，其 jsonl 里的 `usage` 字段经常全是 0（不是 bug，是 Claude Code 自身行为）。脚本仍逐行累加，输出"小数字"或 0 是正常的，不要当数据错误处理。

## 会话独有 vs 项目级共享内容

`{projectDir}/` 下既有「会话独有」也有「项目级共享」内容：

| 路径 | 性质 | delete 规则 |
|---|---|---|
| `{sessionId}.jsonl` | 会话独有 | 删 |
| `{sessionId}/` 目录（subagents + tool-results）| 会话独有 | 删（连带，三道断言保护：UUID 正则 + 父目录 + 同名）|
| `memory/` `todos/` `shellsnapshots/` | 项目级共享 | **绝不动** |
| `.ccsession_cache.json` | 项目级共享（本工程的 list 缓存）| **绝不动**——sessionId 是 UUID，绝不会与此撞名，靠 `SID_RE` 校验保护 |
