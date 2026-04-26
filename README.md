# ccsession

分析 Claude Code 项目会话历史的 Skill：列表、详情、Token 统计、删除；以及发现并清理 Claude Code 退出后留下的孤儿子进程。

## 功能

- **列表**：表格展示所有会话，包含会话ID、模型、时间、问题摘要、首个问题、最后问题、AI 执行摘要、文件编辑、Subagent、Token 用量等
- **详情**：单行摘要 + 用户提问（含 Claude 模式标识）+ 文件编辑 + Subagent + AI 执行步骤
- **删除**：两步确认删除会话 `.jsonl` 文件
- **排序**：列表支持按开始时间、结束时间、轮次、时长排序
- **Token 统计**：主会话 + Subagent 分开展示，支持 k/m/g 单位
- **API 错误追踪**：统计错误次数、重试次数和等待时间
- **孤儿进程清理**：发现 Claude Code 退出后被 launchd 接管、cwd 仍在 claude 项目内的子进程；两步确认 + SIGTERM→5s→SIGKILL；session leader（pgid==pid）走 killpg 整组发信号，dev server 三层 fork 链（zsh wrapper → bun/go → 编译产物）一次到位（macOS-only）

## 结构

```
ccsession/
├── CLAUDE.md
├── README.md
└── skill/
    ├── SKILL.md              # Skill 定义（user-invocable）+ 渲染规范
    ├── scripts/
    │   ├── parse_sessions.py # 解析 JSONL，输出 JSON/Markdown
    │   ├── delete_session.py # 两步确认删除
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
| `/ccsession delete <sessionId>`      | 删除会话 .jsonl（需确认）                |
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
# 摘要
python3 skill/scripts/parse_sessions.py --project /path/to/project --mode summary --format json

# 详情（默认 3 步）
python3 skill/scripts/parse_sessions.py --project /path/to/project \
    --mode detail --session <id> --format json

# 详情（全部步骤）
python3 skill/scripts/parse_sessions.py --project /path/to/project \
    --mode detail --session <id> --format json --full

# 删除
python3 skill/scripts/delete_session.py --project <path> --session <id>          # 预览
python3 skill/scripts/delete_session.py --project <path> --session <id> --force  # 执行

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

| 会话ID     | 模型              | 时间                                                    | 问题摘要                      | 首个问题           | 最后问题           | AI 执行摘要                               | 文件编辑  | Subagent  | Token 用量                                                           |
| -------- | --------------- | ----------------------------------------------------- | ------------------------- | -------------- | -------------- | ------------------------------------- | ----- | --------- | ------------------------------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮   | 探索 new-api 项目结构并实现认证中间件重构 | 帮我看一下这个项目的结构…  | 跑一下测试看看有没有问题… | Bash×23 / Read×18 / Edit×12 / Grep×5  | 3 个文件 | -         | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m                         |
| 63c04f2c | claude-opus-4-7 | 2026-04-19 14:21:05 → 2026-04-19 18:15:333h54m · 28 轮 | 修复 API 路由注册和添加单元测试        | 路由注册好像有问题…     | 测试全部通过了…     | Bash×45 / Edit×32 / Read×28 / Write×4 | 8 个文件 | 2 个 agent | in:320.6k+15.2k / out:45.8k+3.1k / cc:210.3k+8.4k / cr:5.8m+420.5k |
| b2cf1a09 | glm-5.1         | 2026-04-18 09:12:40 → 2026-04-18 10:05:1852m · 6 轮    | 配置 Docker 部署环境            | 怎么用 docker 部署… | 帮我提交一下代码…   | Bash×8 / Read×5 / Edit×3              | 2 个文件 | -         | in:22.1k / out:5.6k / cc:45.0k / cr:380.2k                         |

**合计 tokens** — input: 427.9k+15.2k / output: 63.7k+3.1k / cache\_creation: 375.8k+8.4k / cache\_read: 8.3m+420.5k


### 示例：`/ccsession show <sessionId>`


#### 会话详情 — `a3b370e5-2c63-42e3-831f-65744c89b44a`

| 会话ID     | 模型              | 时间                                                  | 问题摘要                      | 首个问题          | 最后问题          | AI 执行摘要                              | 文件编辑  | Subagent | Token 用量                                   |
| -------- | --------------- | --------------------------------------------------- | ------------------------- | ------------- | ------------- | ------------------------------------ | ----- | -------- | ------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮 | 探索 new-api 项目结构并实现认证中间件重构 | 帮我看一下这个项目的结构… | 跑一下测试看看有没有问题… | Bash×23 / Read×18 / Edit×12 / Grep×5 | 3 个文件 | -        | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m |

##### 用户提问

1. \[plan] 帮我看一下这个项目的结构，特别是认证模块
2. \[acceptEdits] 把 auth middleware 拆分成独立文件
3. \[default] 跑一下测试看看有没有问题
   ...

##### 文件编辑 (3 个文件)

1. middleware/auth.go
2. routes/api.go
3. tests/auth\_test.go

##### AI 执行步骤

1. `[19:31:05]` **Read** — middleware/auth.go
2. `[19:32:18]` **Read** — routes/api.go
3. `[19:33:42]` **Edit** — middleware/auth.go

_…共 58 步，还有 55 步未展示。加_ _`--full`_ _查看全部：`/ccsession show a3b370e5 --full`_