---
name: ccsession
description: "分析某个项目目录下的所有 Claude Code 历史会话：列表、详情、删除、token 汇总。Use when user asks to list/inspect/summarize/delete Claude Code sessions for a project (e.g. /ccsession list, /ccsession show <id>, /ccsession delete <id>). Do NOT load for: app user sessions, login state, unrelated file analysis."
allowed-tools: ["Bash", "Read"]
user-invocable: true
argument-hint: "[list|show|delete] [--project <path>] [<sessionId>]"
---

# ccsession

分析某个项目文件夹下所有 Claude Code 会话。数据源为 `~/.claude/projects/{编码路径}/*.jsonl`（编码规则：项目绝对路径的 `/` 全部替换为 `-`）。

## 子命令

| 命令 | 说明 |
|---|---|
| `/ccsession list [--project <路径>]` | 表格列出该项目所有会话 |
| `/ccsession show <sessionId>` | 详情：单行摘要表 + 用户提问 + 前 3 步 |
| `/ccsession show <sessionId> --full` | 详情：单行摘要表 + 用户提问 + 全部步骤 |
| `/ccsession delete <sessionId>` | 删除某条会话的 .jsonl（需用户确认） |

`--project` 缺省时使用当前 `$PWD`。`<sessionId>` 可以是完整 UUID 或前缀。

## 调用脚本

```bash
# 所有命令统一用 --format json，脚本只输出数据，渲染由 Claude 完成
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode summary --format json
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode detail --session <id> --format json
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode detail --session <id> --format json --full
python3 "${CLAUDE_SKILL_DIR}/scripts/delete_session.py"  --project <路径> --session <id>          # 仅预览
python3 "${CLAUDE_SKILL_DIR}/scripts/delete_session.py"  --project <路径> --session <id> --force  # 实际删除
```

脚本只依赖 Python 3 标准库，无第三方包。

---

## 渲染规范（Claude 必须遵守）

### 表格行格式（list 和 show 共用）

每个会话渲染为一行，列为：

`| 会话ID | 模型 | 时间 | 问题摘要 | 首个问题 | 最后问题 | AI 执行摘要 | 文件编辑 | Subagent | Token 用量 |`

各列取值规则：

| 列 | 数据来源 | 格式 |
|---|---|---|
| 会话ID | `session_id` | 前 8 位 |
| 模型 | `models` | 逗号拼接（如 `claude-opus-4-7, glm-5.1`） |
| 时间 | `start` `end` `duration` `user_turns` | `{start本地时间} → {end本地时间} · {duration} · {turns} 轮` |
| 问题摘要 | `all_questions` → **AI 生成** | 一句中文 ≤60 字，综合所有提问归纳 |
| 首个问题 | `first_question` | 原样，末尾加 `…` |
| 最后问题 | `last_question` | 原样，末尾加 `…` |
| AI 执行摘要 | `tool_counts` | 按次数降序：`Edit×27 / Read×24 / Bash×23` |
| 文件编辑 | `files_edited` | `{N} 个文件`（N 为 `len(files_edited)`）；无编辑时显示 `-` |
| Subagent | `subagents` `subagent_tokens` | `{N} 个 agent`；无 subagent 时显示 `-` |
| Token 用量 | `tokens` `subagent_tokens` | `in:{main_in:,}+{sub_in:,} / out:{main_out:,}+{sub_out:,} / cc:{main_cc:,}+{sub_cc:,} / cr:{main_cr:,}+{sub_cr:,}`；subagent 各维度为 0 时省略 `+0` 部分 ； 数值自动适配 k/m/g 单位展示，保留 1 位小数|

### `/ccsession list` 执行流程

1. 解析 `--project`，缺省取用户当前 `$PWD`。
2. 调 `parse_sessions.py --mode summary --format json`，获取 JSON 数组。
3. 对每个会话生成问题摘要。
4. 按表格行格式渲染每个会话（多行表格 + 表头）。
5. 多个会话时，底部加合计 tokens 行（合计包含 subagent tokens）。
6. 作为文本回复发出。

### `/ccsession show <sessionId>` 执行流程

1. 调 `parse_sessions.py --mode detail --session <id> --format json`，获取 JSON 对象。
2. 生成问题摘要。
3. **第一部分：单行摘要表格**（与 list 行格式完全一致，只有一行）。
   ```
   # 会话详情 — `{session_id}`

   | 会话ID | 模型 | 时间 | 问题摘要 | 首个问题 | 最后问题 | AI 执行摘要 | 文件编辑 | Subagent | Token 用量 |
   |---|---|---|---|---|---|---|---|---|---|
   | 927d520f | ... | ... | ... | ... | ... | ... | ... | ... | ... |
   ```
4. **第二部分：API 错误**（仅当 `api_errors > 0` 时展示）
   ```
   ## API 错误
   - 错误 {api_errors} 次：{status×count / ...}
   - 重试 {api_retries} 次，总等待 {api_retry_wait_ms/1000:.1f}s
   ```
5. **第三部分：用户提问**
   每条提问前加 `[模式]` 标识，模式来自 `question_modes` 数组（与 `all_questions` 一一对应）

   ```
   ## 用户提问
   1. [plan] 第一个问题内容（截断 200 字）
   2. [acceptEdits] `/ccsession list`（slash command 简写）
   3. [default] 怎么markdown格式显示都不正常了？
   ...
   ```
6. **第四部分：文件编辑**（仅当 `files_edited` 非空时展示）
   ```
   ## 文件编辑 ({N} 个文件)
   1. path/to/file1
   2. path/to/file2
   ...
   ```
7. **第五部分：Subagent**（仅当 `subagents` 非空时展示）
   ```
   ## Subagent ({N} 个)
   | Agent 类型 | 描述 | Token 用量 |
   |---|---|---|
   | Explore | 探索模型测试端点 | in:12k / out:3k |
   | Plan | 设计实现方案 | in:8k / out:2k |
   ```
   Token 用量格式：`in:{in:,} / out:{out:,}`，cc/cr 为 0 时省略。
8. **第六部分：AI 执行步骤**（默认前 3 步，末尾提示"共 N 步，加 `--full` 查看全部"）
   不带 `--full` 时，JSON 中 `steps` 只含前 3 条，`total_steps` 字段为实际总数。用 `total_steps` 计算剩余步数。
   ```
   ## AI 执行步骤
   1. `[HH:MM:SS]` **工具名** — detail
   ...

   _…共 {total_steps} 步，还有 {total_steps - len(steps)} 步未展示。加 `--full` 查看全部：`/ccsession show {id} --full`_
   ```
9. 当用户要求看全部步骤时，调 `--format json --full` 重新获取。
10. 若脚本报「未找到」，把候选 ID 展示给用户。

### `/ccsession delete <sessionId>`

1. **先**调不带 `--force` 的 `delete_session.py`，获得会话概要 + 目标文件路径。
2. 把概要展示给用户，**必须**明文询问「确认永久删除？(yes / no)」。
3. 仅当用户回复明确肯定（`yes` / `y` / `确认` 等）时，再加 `--force` 重新调用。
4. 任何情况下**只删除目标 `.jsonl`**，绝不触碰同目录下其它文件或子目录（如 `memory/`、`todos/`）。

---

## 问题摘要生成规则

- 综合会话中所有用户提问，归纳为一句中文描述
- 不超过 60 字
- 突出核心任务/意图，忽略技术细节和 slash command 调用
- 如果问题只有 1 个，直接精简该问题即可

## 工具分类规则（用于 AI 执行摘要）

- `Bash` → 展示 `command`
- `Skill` → 展示为 `Skill[<skill-name>]`
- `Task` / `Agent` → 展示为 `Agent[<subagent_type>]` + description
- `mcp__server__tool` → 展示为 `MCP[server]` + tool
- 其它（Read/Edit/Write/Grep/Glob …）→ 展示 `file_path` 或 `pattern`

## 参考

- `${CLAUDE_SKILL_DIR}/references/session_schema.md` — Claude Code 会话 jsonl 字段速查。
