# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ccsession 是一个 Claude Code Skill，用于分析任意项目目录下的历史会话。脚本解析 `~/.claude/projects/{编码路径}/*.jsonl`，输出 JSON 供 Claude 渲染为 Markdown 表格。

## 架构

- **`skill/SKILL.md`** — Skill 入口定义（frontmatter + 渲染规范）。所有输出格式（表格列、时间格式、Token 展示等）定义在此文件中，**不在脚本里**。脚本只输出 JSON 数据。
- **`skill/scripts/parse_sessions.py`** — 核心解析脚本。`aggregate()` 逐行读取 jsonl，按 `requestId` 去重（同一条 assistant message 会被拆成 thinking/text/tool_use 多行），累加 token 和工具调用。支持 `--format json`（Claude 渲染用）和 `--format markdown`（fallback）。
- **`skill/scripts/delete_session.py`** — 两步确认删除。复用 parse_sessions 的 aggregate 函数，先预览（exit 2），`--force` 时才删除。
- **`skill/scripts/find_orphans.py`** — 发现并清理 Claude Code 退出后的孤儿子进程（macOS-only）。`list` 模式扫描所有进程；`kill` 模式两步确认（无 `--force` 预览 + exit 2，`--force` 才动手），策略 SIGTERM→等 5s→残留 SIGKILL→等 1s 复核。
- **软链** — `~/.claude/skills/ccsession → skill/`，全局可用。

## 关键设计决策

- **路径编码**：`encode_project_path()` 将项目路径中的 `/`、`_`、`.` 都替换为 `-`，以匹配 Claude Code 的 `~/.claude/projects/` 目录命名规则（例如 `/Users/foo/.claude` → `-Users-foo--claude`，双横线来自 `/` 和 `.` 各转一次）
- JSONL 中同一条 message 共享 `requestId`，token 必须按 `requestId` 去重，否则 output/cache 会被重复累加约 2-3 倍
- `message.model` 以 `<` 开头的是 subagent 内部占位值（如 `<synthetic>`），需过滤
- 用户提问识别：`type=="user"` 且 `message.content` 为字符串（非数组的 tool_result），且通过 `is_real_question()` 过滤系统注入内容（`<local-command-caveat>`、`<bash-stdout>` 等）
- 会话数据流：脚本输出 JSON（含 `commits` / `last_prompt` / `raw_summary` / `subagents` 等"事实信号"）→ Claude 按 SKILL.md 中的"会话摘要 Prompt 模板"综合生成一句中文摘要 → 组装 Markdown 表格
- **会话摘要流水线**（事实优先，AI 综合）：
  - 脚本承担事实抽取：`fetch_commits_from_git()` 用 `git log --since={start} --until={end}` 取本会话期间 cwd 的 commits（最权威）；`type==last-prompt` 条目里的 `lastPrompt` 是 Claude Code 主动落盘的"最后一条用户提示"。
  - **`raw_summary` 实际来源**：用户跑 `/compact` 时，Claude Code 把上一段会话压缩成长文本，写入**新会话**第一条 user 行（`type=="user"` + `isCompactSummary: true`，`message.content` 形如 `"This session is being continued from a previous conversation... Summary: ..."`）。脚本据此抽取，原文不截断，作为 AI 生成会话摘要时的"开场背景"信号——它描述的是上一段，不是当前会话。
  - SKILL.md 中"会话摘要 Prompt 模板"按 `commits（实际产出）→ last_prompt / first_question（用户意图）→ raw_summary（延续语境）→ tool_counts（执行性质）` 综合，**不限字数**，要求"包含所有关键产出或核心意图"。
  - 因此脚本 JSON 不再输出 `all_questions` / `question_modes` —— 用户问题列表本身不是高质量摘要源，去掉省 token。
- **JSON 输出性能优化**：不带 `--full` 时 `steps` 只输出前 3 条 + `total_steps` 总数字段；冗余字段（`api_error_types`、`api_retry_wait_ms`）已删除；`first_question` / `last_question` / `last_prompt` 全部不截断输出（之前 80 字符截断让用户在表里看不到完整原文）；`subagents` 字段恢复为详细列表（`type` / `desc` / `tokens`）以便 show 段渲染子表格。
- **summary 模式并发聚合**：`_aggregate_all()` 用 `ThreadPoolExecutor` 并发跑每个会话的 `aggregate()`。每个会话独立、IO 重（jsonl 行解析 + subagent meta 读取 + `git log` 子进程），线程池能直接打平 N 倍。`--workers` 参数控制并发数（0=自动 `min(8, cpu_count())`，1=强制串行用于排错）。`_aggregate_safe()` 单文件异常隔离，避免一条会话坏行拖垮整批；`executor.map` 保持输入顺序，输出与串行版本字节级一致。
- **delete_session 连带子目录 + clean-orphan-dirs**：sessionId 是 UUID（36 字符 `0-9a-f-`），不可能与 `memory` / `todos` / `shellsnapshots` / `.ccsession_cache.json` 这类项目级共享内容撞名。所以删 jsonl 时连带删同名 sessionId 子目录（含该会话独有的 `subagents/` 与 `tool-results/`）是安全的。安全断言：`SID_RE` 严格匹配 UUID + `sub_dir.parent == project_root` + `sub_dir.name == session_id`，三道校验过了才 `shutil.rmtree`。删除顺序：先删子目录、后删 jsonl，避免出现"jsonl 已删但 list 仍能引用旧子目录"的中间态。`--clean-orphan-dirs` 子命令扫所有 UUID 命名但没对应 jsonl 的子目录（历史遗留 + 旧版只删 jsonl 留下的孤儿），同样两步确认。
- **list 默认多键排序**：`--sort` 不指定时走 `(end, user_turns, duration)` 三键全 DESC。设计动机：用户最常关心"最近活跃 + 互动密度高 + 跑得久"的会话排前面。空 `end`（即破损会话）字符串比较时为最小，`reverse=True` 后被推到末尾。`--sort` 显式传值则回退单键模式（与 `--desc` 配合），向后兼容。`_duration_secs()` 抽取为辅助函数，多键排序与单键 duration 排序复用同一份解析逻辑。
- **list/show 表格三列排版**：会话摘要列首句加粗作小标题（≤30 字）+ 冒号后详细叙述；首个问题、最后提示列用「」中文引号包裹原文。设计动机：AI 综合产出 vs 用户原话视觉上要区分；粗体首句让用户扫读时一眼锁核心；不引入 emoji / `<br>` / 列表符号，避免表格被撑高、保持工程文档风格。
- Subagent 解析：读取 `{sessionId}/subagents/agent-*.meta.json` 获取元信息，对应 `agent-*.jsonl` 读取 token（注意 `.meta` 后缀需 `removesuffix`）
- **孤儿判定（find_orphans.py）**：同时满足 ppid=1 + cwd 落在某个 `~/.claude/projects/` 注册项目内 + 不是任何 live claude 子孙。**不反向解码项目目录**——而是 encode 进程 cwd 后查集合，因为 `_` `.` `/` 都被压成 `-`，编码不可逆。
- **过宽路径排除**：`$HOME`、`$HOME` 上层、`/` 即使在 `~/.claude/projects/` 里有对应编码也不作为孤儿匹配的项目根；否则 macOS 系统守护进程（cwd 在 `~/Library/Containers/...`）会被全量误判。
- **locale 处理**：调 `ps`/`lsof` 时强制 `LC_ALL=C`；zh_CN 下 `ps -o lstart` 输出只有 4 个 token（`一 4月/20 09:40:29 2026`），与英文 5 token 错位，必须用 C locale。
- **live claude 识别**：`is_claude_command()` 用 `claude-code/cli.js` / `/claude(\s|$)` / 裸 `claude` 命令三类正则匹配，避免把 `~/.claude/skills/.../some.py` 误判为 claude 本体。
- **fork 链整组 SIGTERM**：`kill_one()` 在 `pgid > 1`（不是 init 组、有自己独立的进程组）时改用 `os.killpg(pgid, SIG)` 整组发，否则退化为 `os.kill(pid, SIG)`。**起因**：实操中 zsh wrapper → bun/go run → go-build/main 的三层 fork 链单 PID SIGTERM 不级联，杀 zsh 后 bun 暴露成「二代孤儿」、再杀 bun 后 go-build 又暴露成「三代孤儿」，要发三轮 SIGTERM 才干净。setsid 出来的孤儿进程组里只要还有进程，pgid 就有效（即便创建组的 leader 已死，pgid != pid），killpg 仍然能整组发——所以判定用 `pgid > 1` 而非更严格的 `pgid == pid`。SIGKILL 升级路径同样走 killpg。
- **descendants 字段**：`find_orphans()` 给每条孤儿附 `descendants[]`（当前快照里 ppid 链下的所有子孙 PID + 命令），用于让用户在 list 时看清整条 fork 链；`collect_descendants()` 用 BFS 从根 PID 出发收集，已存在并复用。

## 常用命令

```bash
# 直接运行脚本测试
python3 skill/scripts/parse_sessions.py --project "$PWD" --mode summary --format json
python3 skill/scripts/parse_sessions.py --project "$PWD" --mode detail --session <id> --format json
python3 skill/scripts/parse_sessions.py --project "$PWD" --mode detail --session <id> --format json --full
python3 skill/scripts/find_orphans.py    --project "$PWD" --mode list --format json
python3 skill/scripts/find_orphans.py    --project "$PWD" --mode kill --pids <p1>,<p2> --format json          # 仅预览
python3 skill/scripts/find_orphans.py    --project "$PWD" --mode kill --pids <p1>,<p2> --format json --force  # 实际终止

# 通过 Skill 调用
/ccsession list
/ccsession show <sessionId>
/ccsession show <sessionId> --full
/ccsession delete <sessionId>
/ccsession procs
/ccsession kill <pid>[,<pid>...]
```

## 依赖

Python 3 标准库，无第三方包。

## 远程仓库

SSH: `git@github.com:legdonkey/ccsession.git`
