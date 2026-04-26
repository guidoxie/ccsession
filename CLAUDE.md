# CLAUDE.md

给在本仓库改代码的 Claude Code（claude.ai/code）的工作指南。**只记录读代码看不出来的踩坑与不变量**——实现细节读源码即可。

## 项目概述

ccsession 是一个 Claude Code Skill，分析任意项目目录下的历史会话。脚本解析 `~/.claude/projects/{编码路径}/*.jsonl`，输出 JSON 供 Claude 渲染为 Markdown 表格。

## 架构

- **`skill/SKILL.md`** — Skill 入口（frontmatter + 渲染规范 + 各子命令执行流程 + 会话摘要 Prompt 模板）。表格列 / 时间格式 / Token 展示等渲染规则全在这里，**不在脚本里**。
- **`skill/scripts/parse_sessions.py`** — 核心解析。`aggregate()` 逐行读 jsonl 累加 token / 工具调用；输出 JSON 给 Claude 渲染。
- **`skill/scripts/delete_session.py`** — 两步确认删除（jsonl + 同名 sessionId 子目录），含 `--clean-orphan-dirs` 子命令。
- **`skill/scripts/cache_summary.py`** — 持久化缓存读写底层（`load_cache` / `save_cache` / `backfill_session_dicts` / `purge_entry` / `write_entries`）+ AI 摘要回写 CLI。
- **`skill/scripts/find_orphans.py`** — 发现 / 清理 Claude Code 退出后留下的孤儿子进程（macOS-only）。
- 软链：`~/.claude/skills/ccsession → skill/`。

## 关键设计决策

### JSONL 解析（踩坑点 + 不变量）

- **`requestId` 去重**：同一条 assistant message 会被拆成 thinking / text / tool_use 多行，token 必须按 `requestId` 去重，否则 output / cache 重复累加约 2-3x。
- **路径编码**（`encode_project_path()`）：项目路径中 `/` `_` `.` 全部替换为 `-`。例 `/Users/foo/.claude` → `-Users-foo--claude`（双横线来自 `/` 和 `.` 各转一次）。**编码不可逆**——`find_orphans.py` 判定孤儿时不要反向解码项目目录，应正向 encode 进程 cwd 后查集合。
- **`message.model` 过滤**：`<synthetic>` 等 `<` 开头的是 subagent 内部占位，必须过滤。
- **用户提问识别**：`type=="user"` 且 `message.content` 是字符串，且 `is_real_question()` 过滤系统注入（`<local-command-caveat>` `<bash-stdout>` 等）。
- **`raw_summary` 真实来源**：是 `/compact` 留给**新会话**首条 user 行的压缩长文（`type=="user"` + `isCompactSummary: true`，**不是** `type==summary`——后者从未出现过）。它描述的是上一段会话；AI 综合摘要时只能当"开场背景"，不能直接照抄。
- **subagent 元信息路径**：`{sessionId}/subagents/agent-*.meta.json`；提取 agent_id 时 `.meta` 后缀需 `removesuffix`。

### 会话摘要流水线

事实优先、AI 综合。脚本承担事实抽取（`fetch_commits_from_git()` 用 `git log --since/--until` 抽 cwd commits、`type==last-prompt` 抽最后用户提示）；AI 按 SKILL.md 中"会话摘要 Prompt 模板"综合 `commits → last_prompt → first/last_question → raw_summary` 生成。**脚本绝不生成摘要文本**。

### 持久化缓存（双层，schema v2）

`{project_dir}/.ccsession_cache.json` 按 `entries.<sid> → {mtime, size, session_dict, generated_at}` 缓存。

1. **Session-dict 层**（脚本自动维护）：jsonl 写完不变 → `_session_to_dict()` 是纯函数。`_cache_lookup_dict()` 按 `sessionId + mtime + size` 三段命中即返回完整 session_dict（含 tokens / tool_counts / files_edited / subagents / commits / cached_summary 等），**根本不调 `aggregate()`**——不读 jsonl、不跑 `git log`、不扫 subagent 目录。未命中那批走并发 `_aggregate_all()`；list 收尾**单次** `backfill_session_dicts()` 写回（避免 N 次文件覆盖）。
2. **AI 摘要层**（AI 维护）：命中 entry 但 `cached_summary` 为空时，AI 按 SKILL.md Prompt 模板生成 + 通过 `cache_summary.py --bulk` 回写到 `entries.<sid>.session_dict.cached_summary`（只更新该字段）。

**缓存淘汰**：mtime/size 不一致 → entry 失效；`version != 2` 或文件损坏 → 全量失效（v1 自动废弃重建）；`delete_session.py` 删 jsonl 时同步 `purge_entry()` 清条目。**list/show 只读不写孤儿**（避免读路径写副作用）。

**Detail 模式不查 dict 缓存**：要 `steps` 字段必须跑 `aggregate()`；但仍读已存 entry 中 `cached_summary` 复用。不为 detail 维护独立缓存（避免与 list 缓存形状错位）。

**为什么不缓存 markdown 行**：AI 必须把表格 emit 在文本回复里（Bash stdout 在 Claude Code 里渲染成代码块、不变真表格），row markdown 缓存只省 AI 思考成本、不省字符 emission；且 SKILL.md 格式变更需要 render_version 全量失效，性价比低。

**写盘**：`tempfile + os.replace` 原子换文件，不加锁——并行 list 最坏丢一条新 entry，下次自然回填，可接受。

### 删除流程安全断言

- sessionId 是 UUID（36 字符 `0-9a-f-`），不可能与 `memory` / `todos` / `shellsnapshots` / `.ccsession_cache.json` 这类项目级共享内容撞名。删 jsonl 时连带删同名子目录（含独有的 `subagents/` `tool-results/`）安全。
- 三道断言：`SID_RE` 严格 UUID 正则 + `sub_dir.parent == project_root` + `sub_dir.name == session_id`；全过才 `shutil.rmtree`。
- 顺序：**先删子目录、后删 jsonl**——否则中间态下 list 仍能引用旧子目录。

### 孤儿子进程清理（macOS-only）

- **判定**（同时满足）：`ppid=1`（被 launchd 接管）+ cwd 落在 `~/.claude/projects/` 注册项目内 + 不是 live claude 子孙。
- **过宽路径排除**：`$HOME` / `$HOME` 上层 / `/` 即使在 `projects/` 下有对应编码，**不**作为孤儿匹配的项目根。否则 macOS 系统守护进程（cwd 在 `~/Library/Containers/...`）会被全量误判。
- **locale**：调 `ps` / `lsof` 时强制 `LC_ALL=C`。zh_CN 下 `ps -o lstart` 输出只有 4 个 token（`一 4月/20 09:40:29 2026`），与英文 5 token 错位。
- **live claude 识别**：`is_claude_command()` 用 `claude-code/cli.js` / `/claude(\s|$)` / 裸 `claude` 三类正则，避免 `~/.claude/skills/.../some.py` 误判为 claude 本体。
- **fork 链整组 SIGTERM**：`pgid > 1`（自己独立进程组）走 `os.killpg(pgid, SIG)` 整组发，否则退化 `os.kill(pid, SIG)`。**起因**：zsh wrapper → bun/go run → go-build/main 三层 fork 链，单 PID SIGTERM 不级联——杀 zsh 后 bun 暴露成二代孤儿、再杀 bun 后 go-build 暴露成三代孤儿。setsid 出来的进程组里只要还有进程 pgid 就有效（即使 leader 已死、pgid != pid），killpg 仍能整组发——所以判定用 `pgid > 1` 而非更严格的 `pgid == pid`。SIGKILL 升级同样走 killpg。

## 常用命令

```bash
# 直接跑脚本（开发调试）
python3 skill/scripts/parse_sessions.py --project "$PWD" --mode summary --format json
python3 skill/scripts/parse_sessions.py --project "$PWD" --mode detail --session <id> --format json [--full]
python3 skill/scripts/cache_summary.py   --project "$PWD" --bulk /tmp/writeback.json
python3 skill/scripts/delete_session.py  --project "$PWD" --session <id>          # 仅预览
python3 skill/scripts/delete_session.py  --project "$PWD" --session <id> --force  # 实际删除
python3 skill/scripts/find_orphans.py    --project "$PWD" --mode list --format json
python3 skill/scripts/find_orphans.py    --project "$PWD" --mode kill --pids <p1>,<p2> --format json [--force]

# 通过 Skill 调用
/ccsession list | show <id> [--full] | delete <id> | clean-orphan-dirs | procs | kill <pid>[,<pid>...]
```

## 依赖

Python 3 标准库，无第三方包。

## 远程仓库

`git@github.com:legdonkey/ccsession.git`
