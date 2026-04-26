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
- 会话数据流：脚本输出 JSON → Claude 读取 JSON 并生成问题摘要 → 组装 Markdown 表格
- **JSON 输出性能优化**：不带 `--full` 时 `steps` 只输出前 3 条 + `total_steps` 总数字段，`all_questions` 截断到 200 字符，避免大 JSON 拖慢 Claude 渲染
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
