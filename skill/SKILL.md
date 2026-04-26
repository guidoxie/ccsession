---
name: ccsession
description: "分析某个项目目录下的所有 Claude Code 历史会话：列表、详情、删除、token 汇总；以及发现并清理 Claude Code 退出后留下的孤儿子进程。Use when user asks to list/inspect/summarize/delete Claude Code sessions for a project (e.g. /ccsession list, /ccsession show <id>, /ccsession delete <id>), OR to find/kill orphan processes left behind after Claude Code exited (e.g. /ccsession procs, /ccsession kill <pid>). Do NOT load for: app user sessions, login state, unrelated file analysis."
allowed-tools: ["Bash", "Read"]
user-invocable: true
argument-hint: "[list|show|delete|procs|kill] [--project <path>] [<sessionId>|<pid>[,<pid>...]]"
---

# ccsession

分析某个项目文件夹下所有 Claude Code 会话；并发现 / 清理 Claude Code 退出后留下的孤儿子进程（含 dev server 多层 fork 链整组优雅退出）。会话数据源为 `~/.claude/projects/{编码路径}/*.jsonl`（编码规则：项目绝对路径中的 `/`、`_`、`.` 全部替换为 `-`）。

## 子命令

| 命令 | 说明 |
|---|---|
| `/ccsession list [--project <路径>]` | 表格列出该项目所有会话 |
| `/ccsession show <sessionId>` | 详情：单行摘要表 + 用户提问 + 前 3 步 |
| `/ccsession show <sessionId> --full` | 详情：单行摘要表 + 用户提问 + 全部步骤 |
| `/ccsession delete <sessionId>` | 删除某条会话的 .jsonl（需用户确认） |
| `/ccsession procs` | 列出 Claude Code 退出后留下的孤儿子进程 |
| `/ccsession kill <pid>[,<pid>...]` | 清理指定孤儿进程（两步确认；SIGTERM→5s→SIGKILL） |

`--project` 缺省时使用当前 `$PWD`。`<sessionId>` 可以是完整 UUID 或前缀。`<pid>` 必须是完整数字。

## 调用脚本

```bash
# 所有命令统一用 --format json，脚本只输出数据，渲染由 Claude 完成
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode summary --format json
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode detail --session <id> --format json
python3 "${CLAUDE_SKILL_DIR}/scripts/parse_sessions.py" --project <路径> --mode detail --session <id> --format json --full
python3 "${CLAUDE_SKILL_DIR}/scripts/delete_session.py"  --project <路径> --session <id>          # 仅预览
python3 "${CLAUDE_SKILL_DIR}/scripts/delete_session.py"  --project <路径> --session <id> --force  # 实际删除
python3 "${CLAUDE_SKILL_DIR}/scripts/find_orphans.py"    --project <路径> --mode list --format json
python3 "${CLAUDE_SKILL_DIR}/scripts/find_orphans.py"    --project <路径> --mode kill --pids <ids> --format json          # 仅预览
python3 "${CLAUDE_SKILL_DIR}/scripts/find_orphans.py"    --project <路径> --mode kill --pids <ids> --format json --force  # 实际终止
```

脚本只依赖 Python 3 标准库，无第三方包。

---

## 渲染规范（Claude 必须遵守）

### 表格行格式（list 和 show 共用）

每个会话渲染为一行，列为：

`| 会话ID | 模型 | 时间 | 会话摘要 | 首个问题 | 最后提示 | AI 执行摘要 | 文件编辑 | Subagent | Token 用量 |`

各列取值规则：

| 列 | 数据来源 | 格式 |
|---|---|---|
| 会话ID | `session_id` | 前 8 位 |
| 模型 | `models` | 逗号拼接（如 `claude-opus-4-7, glm-5.1`） |
| 时间 | `start` `end` `duration` `user_turns` | `{start本地时间} → {end本地时间} · {duration} · {turns} 轮` |
| 会话摘要 | `raw_summary` / `commits` / `last_prompt` / `first_question` / `tool_counts` → **AI 综合** | 一句中文，简明扼要包含所有关键内容；不限字数；详见下方"会话摘要 Prompt 模板" |
| 首个问题 | `first_question` | 原样不截断（管道符 `\|` 转义、换行替空格） |
| 最后提示 | `last_prompt`（缺失时回退 `last_question`） | 原样不截断（同上） |
| AI 执行摘要 | `tool_counts` | 按次数降序：`Edit×27 / Read×24 / Bash×23` |
| 文件编辑 | `files_edited` | `{N} 个文件`（N 为 `len(files_edited)`）；无编辑时显示 `-` |
| Subagent | `subagents` `subagent_count` `subagent_tokens` | 列表行：`{N} 个 agent`（详细子表格在 show 段展示）；无 subagent 时显示 `-` |
| Token 用量 | `tokens` `subagent_tokens` | `in:{main_in:,}+{sub_in:,} / out:{main_out:,}+{sub_out:,} / cc:{main_cc:,}+{sub_cc:,} / cr:{main_cr:,}+{sub_cr:,}`；subagent 各维度为 0 时省略 `+0` 部分 ； 数值自动适配 k/m/g 单位展示，保留 1 位小数|

### `/ccsession list` 执行流程

1. 解析 `--project`，缺省取用户当前 `$PWD`。
2. 调 `parse_sessions.py --mode summary --format json`，获取 JSON 数组。
3. 对每个会话按"会话摘要 Prompt 模板"一行润色（脚本已提供 `raw_summary` / `commits` / `last_prompt` / `first_question` 候选）。
4. 按表格行格式渲染每个会话（多行表格 + 表头）。
5. 多个会话时，底部加合计 tokens 行（合计包含 subagent tokens）。
6. 作为文本回复发出。

### `/ccsession show <sessionId>` 执行流程

1. 调 `parse_sessions.py --mode detail --session <id> --format json`，获取 JSON 对象。
2. 按"会话摘要 Prompt 模板"生成会话摘要。
3. **第一部分：单行摘要表格**（与 list 行格式完全一致，只有一行）。
   ```
   # 会话详情 — `{session_id}`

   | 会话ID | 模型 | 时间 | 会话摘要 | 首个问题 | 最后提示 | AI 执行摘要 | 文件编辑 | Subagent | Token 用量 |
   |---|---|---|---|---|---|---|---|---|---|
   | 927d520f | ... | ... | ... | ... | ... | ... | ... | ... | ... |
   ```
4. **第二部分：API 错误**（仅当 `api_errors > 0` 时展示）
   ```
   ## API 错误
   - 错误 {api_errors} 次，重试 {api_retries} 次
   ```
5. **第三部分：本会话提交**（仅当 `commits` 非空时展示）
   ```
   ## 本会话提交 ({N} 个)
   1. {hash} {subject}
   2. {hash} {subject}
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
   ## Subagent ({subagent_count} 个)
   | Agent 类型 | 描述 | Token 用量 |
   |---|---|---|
   | Explore | 探索现有摘要逻辑 | in:12,345 / out:3,456 |
   | Plan | 设计实现方案 | in:8,901 / out:2,345 |
   ```
   - `Agent 类型` ← `subagents[i].type`
   - `描述` ← `subagents[i].desc`（超过 60 字符截断加 `…`）
   - `Token 用量` ← `subagents[i].tokens`，格式 `in:{in:,} / out:{out:,}`，cc/cr 非 0 时追加 `/ cc:{cc:,} / cr:{cr:,}`
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

### `/ccsession procs` 执行流程

1. 调 `find_orphans.py --mode list --format json`，获取孤儿进程列表。
2. 解析返回的 JSON：`scope.live_claude_pids`（活着的 claude PID）、`orphans[]`（孤儿明细）、`total`。
3. 渲染表格（见下方表格行格式）。
4. 列表为空时：明确告知「未发现孤儿进程」，并简述判定规则（ppid=1 + cwd 在 claude 项目内 + 非 live claude 子孙）。
5. 列表非空时：表格底部给出「清理示例：`/ccsession kill <pid>`（多个用逗号分隔）」。

#### 孤儿进程表格格式

| 列 | 数据来源 | 格式 |
|---|---|---|
| PID | `pid` | 原值 |
| pgid | `pgid` | 原值；只要 `pgid > 1`（自己的独立进程组），kill 都会走 killpg 整组发 |
| 命令 | `command` | 截断 60 字符，超出加 `…`；用反引号包裹 |
| cwd | `cwd` | 原值；用反引号包裹 |
| 项目 | `project` | 项目根（非 cwd）；标注 `(当前)` 当 `is_current_project=true` |
| 启动 | `started` | `YYYY-MM-DD HH:MM:SS` |
| 已运行 | `elapsed` | 原值（ps 给的格式，如 `04-18:43:42` 表示 4 天 18 小时） |
| RSS | `rss_mb` | `{x.x} MB`（值已是 MB） |
| 子孙 | `descendants` | `{N} 个`；非空时在主行下用缩进列出每条 `└─ pid · command`（典型场景：zsh wrapper → bun/go → go-build/main 这种三层链） |

### `/ccsession kill <pid>[,<pid>...]`

1. **先**调不带 `--force` 的 `find_orphans.py --mode kill --pids <ids>`，获得预览 JSON（`preview: true`，`targets[]`，`skipped[]`）和 exit code 2。
2. 渲染预览：列出 `targets`（即将终止的进程）和 `skipped`（无法处理的 PID 及原因）；如果 `target.descendants` 非空，提醒用户该 PID 实际是 fork 链根（zsh wrapper），整组 SIGTERM 会一并清掉子孙。
3. **必须**明文询问「确认终止以上 N 个进程？(yes / no)」，并提示策略「先 SIGTERM 等 5 秒，残留再 SIGKILL；自己的独立进程组（pgid > 1）走 killpg 整组发信号，dev server 三层 fork 链一次到位」。
4. 仅当用户明确肯定（`yes` / `y` / `确认` 等）时，加 `--force` 重新调用。
5. 渲染最终结果：`killed[]`（每条含 `method`：SIGTERM / SIGKILL / SIGTERM_late / SIGKILL_failed / already_dead / permission_denied；`use_pgroup`：是否走 killpg；`elapsed_ms` 总耗时；`alive` 是否仍在）；`still_alive[]`（未能终止的）；`skipped[]`。
6. 即使 `targets` 为空也要走完两步流程（脚本会返回 exit 2 + 空 targets），向用户确认无可操作后退出。

#### kill 结果表格格式

| 列 | 数据来源 | 格式 |
|---|---|---|
| PID | `killed[].pid` | 原值 |
| 范围 | `killed[].use_pgroup` | `进程组` / `单 PID` |
| 命令 | `killed[].command` | 截断 60 字符 |
| 方式 | `killed[].method` | 原值（SIGTERM / SIGKILL / 等） |
| 耗时 | `killed[].elapsed_ms` | `{ms} ms` |
| 状态 | `killed[].alive` | `✅ 已退出` / `⚠️ 仍在运行` |

---

## 会话摘要 Prompt 模板

为每个会话生成「会话摘要」列时，按下面这套 Prompt 自我引导：

> 你正在为一段 Claude Code 会话生成「会话摘要」。
>
> **输入信号**（按权重）：
> 1. `raw_summary`：**前序会话**经 `/compact` 留下的整体压缩（长文本，叙事性）。**注意**：它描述的是上一段会话，不是当前会话本身——把它当作"开场背景"用来判断当前会话的延续语境，**不要直接照抄它的结论**。
> 2. `commits`：本会话期间在项目 cwd 内产生的 git commits（`hash` + `subject`）。这是当前会话**实际产出**的最权威信号。
> 3. `last_prompt`：用户最后一条原始提示（Claude Code 主动落盘）。
> 4. `first_question` / `last_question`：首末用户提问原文。
> 5. `tool_counts`：工具调用次数分布，用来判断这是"诊断型"还是"实施型"会话。
>
> **输出**：一句中文，简明扼要、**包含所有关键产出或核心意图**，不限字数。
>
> **构思路径**：
> - 如果 `commits` 非空 → 主信号必须来自 commit subjects（多个 commit 合并叙述）。`raw_summary` / `last_prompt` 仅作背景理解。
> - 如果 `commits` 为空但 `last_prompt` 与 `first_question` 显著不同 → 写成"用户想做 X，最终聚焦到 Y"。
> - 如果会话仅有 `first_question`、无 commits、工具调用极少 → 明确写"未执行"或"仅诊断未改动"等限定。
> - 如果 `raw_summary` 非空但 `commits` 为空 → 从 `raw_summary` 中提取与本会话 `first_question` / `last_prompt` 主题相关的延续意图，写成"延续上一段 X，本段聚焦 Y"。
>
> **风格**：
> - 围绕"做了什么 / 想做什么"叙事，**不要**罗列工具调用次数。
> - **不要**出现 slash command 字面（如 `/ccsession list`、`/compact`），用其语义替代。
> - 中文表达，避免直译英文 commit subject 的语序。

## 工具分类规则（用于 AI 执行摘要）

- `Bash` → 展示 `command`
- `Skill` → 展示为 `Skill[<skill-name>]`
- `Task` / `Agent` → 展示为 `Agent[<subagent_type>]` + description
- `mcp__server__tool` → 展示为 `MCP[server]` + tool
- 其它（Read/Edit/Write/Grep/Glob …）→ 展示 `file_path` 或 `pattern`

## 参考

- `${CLAUDE_SKILL_DIR}/references/session_schema.md` — Claude Code 会话 jsonl 字段速查。
