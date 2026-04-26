# ccsession

分析 Claude Code 项目会话历史的 Skill：列表、详情、Token 统计、删除；以及发现并清理 Claude Code 退出后留下的孤儿子进程。

## 功能

- **列表 / 详情**：表格展示所有会话（会话ID / 模型 / 时间 / 会话摘要 / 首末提示 / AI 执行摘要 / 文件编辑 / Subagent / Token 用量），并发聚合 + 持久化缓存（活跃项目第二次跑 list 脚本侧成本压成 0）；详情含 API 错误 / commits / 文件编辑 / Subagent 子表格 / 执行步骤
- **会话摘要**：事实优先、AI 综合——脚本抽 `git log` commits + `last_prompt` + `/compact` 压缩，AI 按 SKILL.md Prompt 模板生成不限字数中文摘要；摘要持久化到 `.ccsession_cache.json` 复用
- **删除**：两步确认删 `.jsonl` + 该会话独有的同名 sessionId 子目录；项目级共享目录（`memory/` `todos/` `shellsnapshots/` `.ccsession_cache.json`）红线保留；附 `clean-orphan-dirs` 清理历史遗留
- **孤儿子进程清理**（macOS-only）：发现 Claude Code 退出后被 launchd 接管的子进程；两步确认 + SIGTERM→5s→SIGKILL；`pgid > 1` 走 `killpg` 整组发，dev server 三层 fork 链（zsh wrapper → bun/go → 编译产物）一次到位
- **排序**：默认 `end DESC → user_turns DESC → duration DESC` 三级倒序；`--sort` 单键回退
- **Token 统计**：主会话 + Subagent 分开展示，支持 k/m/g 单位

## 结构

```
ccsession/
├── CLAUDE.md
├── README.md
└── skill/
    ├── SKILL.md              # Skill 定义（user-invocable）+ 渲染规范
    ├── scripts/
    │   ├── parse_sessions.py # 解析 JSONL，输出 JSON/Markdown；附 cached_summary 字段
    │   ├── delete_session.py # 两步确认删除（删 jsonl 时同步清缓存条目）
    │   ├── cache_summary.py  # AI 摘要回写工具（--bulk / --session+--text）
    │   └── find_orphans.py   # 发现 / 清理 claude 退出后的孤儿子进程
    └── references/
        └── session_schema.md # JSONL 字段速查
```

## 安装

```bash
ln -s /path/to/ccsession/skill ~/.claude/skills/ccsession
```

在新 Claude Code 会话里输入 `/ccsession` 即可调用。

## 使用

### 通过 Skill 调用

| 命令                                   | 说明                              |
| ------------------------------------ | ------------------------------- |
| `/ccsession list [--project <path>]` | 表格列出所有会话                        |
| `/ccsession show <sessionId>`        | 会话详情（默认展示前 3 步）                 |
| `/ccsession show <sessionId> --full` | 会话详情（展示全部步骤）                    |
| `/ccsession delete <sessionId>`      | 删除会话 .jsonl 与同名 sessionId 子目录（两步确认）  |
| `/ccsession clean-orphan-dirs`       | 清理项目目录下所有无对应 .jsonl 的孤儿子目录（两步确认） |
| `/ccsession procs`                   | 列出 Claude Code 退出后的孤儿子进程        |
| `/ccsession kill <pid>[,<pid>...]`   | 清理孤儿进程（两步确认；SIGTERM→5s→SIGKILL） |

`--project` 缺省时使用当前工作目录。`<sessionId>` 支持完整 UUID 或前缀匹配。`<pid>` 必须是完整数字。

**孤儿进程判定（同时满足）**：(1) `ppid=1`（父进程已死，被 launchd 接管）；(2) `cwd` 落在 `~/.claude/projects/` 注册的项目目录内；(3) 不是任何 live claude 进程的子孙。每条孤儿同时附带 `descendants` 字段（当前快照的 ppid 链子孙），kill 默认对 session leader 走 `os.killpg(pgid, SIG)` 整组发信号——避免「杀掉 zsh 外壳后 bun/go 二代孤儿暴露」的级联问题。仅支持 macOS（依赖 `ps` `lsof`）。

**路径编码规则**：项目路径中的 `/`、`_`、`.` 都会被替换为 `-`，用于匹配 `~/.claude/projects/` 下的目录名。例如：
- `/home/user/my_project` → `-home-user-my-project`
- `/home/user/project/my_app` → `-home-user-project-my-app`
- `/Users/foo/.claude` → `-Users-foo--claude`（`.` 也会被压成 `-`）

### 直接运行脚本

```bash
# 摘要（默认线程池并发，会话越多越明显）
python3 skill/scripts/parse_sessions.py --project /path/to/project --mode summary --format json
python3 skill/scripts/parse_sessions.py --project /path/to/project --mode summary --format json --workers 1   # 强制串行
python3 skill/scripts/parse_sessions.py --project /path/to/project --mode summary --format json --workers 16  # 自定义并发数

# 详情（默认 3 步）
python3 skill/scripts/parse_sessions.py --project /path/to/project \
    --mode detail --session <id> --format json

# 详情（全部步骤）
python3 skill/scripts/parse_sessions.py --project /path/to/project \
    --mode detail --session <id> --format json --full

# 删除（jsonl + 同名 sessionId 子目录）
python3 skill/scripts/delete_session.py --project <path> --session <id>          # 预览
python3 skill/scripts/delete_session.py --project <path> --session <id> --force  # 执行

# 清理孤儿子目录（历史遗留的、无对应 .jsonl 的 sessionId 命名子目录）
python3 skill/scripts/delete_session.py --project <path> --clean-orphan-dirs          # 预览
python3 skill/scripts/delete_session.py --project <path> --clean-orphan-dirs --force  # 执行

# 孤儿进程：列表
python3 skill/scripts/find_orphans.py --project <path> --mode list --format json

# 孤儿进程：终止（两步确认）
python3 skill/scripts/find_orphans.py --project <path> --mode kill --pids <p1>,<p2> --format json          # 预览
python3 skill/scripts/find_orphans.py --project <path> --mode kill --pids <p1>,<p2> --format json --force  # 执行
```

## 依赖

Python 3 标准库，无第三方包。

## 示例

### 示例：`/ccsession list`


#### Claude Code 会话摘要 — `/path/to/project`

共 **3** 个会话。

| 会话ID     | 模型              | 时间                                                    | 会话摘要                      | 首个问题           | 最后提示           | AI 执行摘要                               | 文件编辑  | Subagent  | Token 用量                                                           |
| -------- | --------------- | ----------------------------------------------------- | ------------------------- | -------------- | -------------- | ------------------------------------- | ----- | --------- | ------------------------------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮   | 拆分 auth middleware 并补单元测试 | 帮我看一下这个项目的结构…  | 跑一下测试看看有没有问题… | Bash×23 / Read×18 / Edit×12 / Grep×5  | 3 个文件 | -         | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m                         |
| 63c04f2c | claude-opus-4-7 | 2026-04-19 14:21:05 → 2026-04-19 18:15:333h54m · 28 轮 | 修复 API 路由注册并添加单元测试        | 路由注册好像有问题…     | 测试全部通过了…     | Bash×45 / Edit×32 / Read×28 / Write×4 | 8 个文件 | 2 个 agent | in:320.6k+15.2k / out:45.8k+3.1k / cc:210.3k+8.4k / cr:5.8m+420.5k |
| b2cf1a09 | glm-5.1         | 2026-04-18 09:12:40 → 2026-04-18 10:05:1852m · 6 轮    | 配置 Docker 部署环境            | 怎么用 docker 部署… | 帮我提交一下代码…   | Bash×8 / Read×5 / Edit×3              | 2 个文件 | -         | in:22.1k / out:5.6k / cc:45.0k / cr:380.2k                         |

**合计 tokens** — input: 427.9k+15.2k / output: 63.7k+3.1k / cache\_creation: 375.8k+8.4k / cache\_read: 8.3m+420.5k


### 示例：`/ccsession show <sessionId>`


#### 会话详情 — `a3b370e5-2c63-42e3-831f-65744c89b44a`

| 会话ID     | 模型              | 时间                                                  | 会话摘要                      | 首个问题          | 最后提示          | AI 执行摘要                              | 文件编辑  | Subagent | Token 用量                                   |
| -------- | --------------- | --------------------------------------------------- | ------------------------- | ------------- | ------------- | ------------------------------------ | ----- | -------- | ------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮 | 拆分 auth middleware 并补单元测试 | 帮我看一下这个项目的结构… | 跑一下测试看看有没有问题… | Bash×23 / Read×18 / Edit×12 / Grep×5 | 3 个文件 | -        | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m |

##### 本会话提交 (2 个)

1. `9d3f1ab` 拆分 auth middleware 为独立包
2. `b27c0e4` 补充 auth middleware 单元测试

##### 文件编辑 (3 个文件)

1. middleware/auth.go
2. routes/api.go
3. tests/auth\_test.go

##### Subagent (2 个)

| Agent 类型 | 描述 | Token 用量 |
| ------- | ----------- | -------------------- |
| Explore | 探索现有 auth 结构 | in:12,345 / out:3,456 |
| Plan    | 设计中间件拆分方案   | in:8,901 / out:2,345  |

##### AI 执行步骤

1. `[19:31:05]` **Read** — middleware/auth.go
2. `[19:32:18]` **Read** — routes/api.go
3. `[19:33:42]` **Edit** — middleware/auth.go

_…共 58 步，还有 55 步未展示。加_ _`--full`_ _查看全部：`/ccsession show a3b370e5 --full`_

## 修改日志

| 日期 | 变更类型 | 变更描述 |
|---|---|---|
| 2026-04-27 | 文档 | **三文件分工 + 拆引用**：CLAUDE.md / SKILL.md / README.md 按"权威决策 / 运行指令 / 用户面向"分工去重；`session_schema.md` 修正过期信息（路径编码补 `_` `.` 转 `-`、token 去重事实修正）+ 补充 `isCompactSummary` / `last-prompt` / subagent 目录布局 / `apiErrorStatus` 等字段；新增 `references/orphan_render.md` 收纳 procs/kill 表格规范，SKILL.md 主体 procs/kill 段从 ~40 行精简为 ~10 行骨架（list 路径不再加载孤儿渲染规范进 context） |
| 2026-04-27 | 性能优化 | **持久化缓存（schema v2）**：`.ccsession_cache.json` 缓存完整 session_dict（含 tokens / tool_counts / files_edited / subagent / commits / cached_summary 等），命中时脚本**不调 `aggregate()`**——不读 jsonl、不跑 `git log`、不扫 subagent 目录；AI 摘要层独立维护（`cache_summary.py --bulk` 写回）；`delete` 时同步清条目；本机 new-api 10 会话 cold 228ms → warm 67ms（≈3.4x）|
| 2026-04-26 | 重构 + 精修 | **会话摘要流水线** 改为"事实优先，AI 综合"：脚本抽 git commits（`git log --since/--until`）+ `isCompactSummary` 行的 `/compact` 压缩 + `last_prompt`，AI 按 SKILL.md Prompt 模板综合生成；不限字数；首问 / 最后提示不截断；恢复 Subagent 子表格 |
| 2026-04-26 | 用户体验 | **list 排版与排序**：表格三列（会话摘要 / 首个问题 / 最后提示）粗体小标题 + 「」中文引号区分 AI 总结 vs 用户原话；默认按 `end DESC → user_turns DESC → duration DESC` 三级排序；`--sort` 显式传值回退单键模式 |
| 2026-04-26 | 性能 + 删除流程 | summary 模式 `ThreadPoolExecutor` 并发聚合（`--workers` 控制）；`delete` 连带删同名 sessionId 子目录（subagents + tool-results），新增 `clean-orphan-dirs` 子命令；三道安全断言（UUID 正则 + 父目录 + 同名）防误伤共享目录 |
| 2026-04-25 | 新功能 | **`procs` / `kill` 孤儿子进程清理**（macOS-only）：判定 ppid=1 + cwd 在 claude 项目内 + 非 live claude 子孙；`pgid > 1` 走 `killpg` 整组发处理 zsh wrapper → bun/go → go-build 三层 fork 链；list/show 表格同期新增「最后问题 / 最后提示」列 |
| 2026-04-19 | 初始版本 | 首发 ccsession Skill：list / show / delete 三个子命令，jsonl 解析、Token 统计（含 subagent）、API 错误追踪、文件编辑追踪 |