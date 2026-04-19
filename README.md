# ccsession

分析 Claude Code 项目会话历史的 Skill：列表、详情、Token 统计、删除。

## 功能

- **列表**：表格展示所有会话，包含会话ID、模型、时间、问题摘要、AI 执行摘要、文件编辑、Subagent、Token 用量等
- **详情**：单行摘要 + 用户提问（含 Claude 模式标识）+ 文件编辑 + Subagent + AI 执行步骤
- **删除**：两步确认删除会话 `.jsonl` 文件
- **排序**：列表支持按开始时间、结束时间、轮次、时长排序
- **Token 统计**：主会话 + Subagent 分开展示，支持 k/m/g 单位
- **API 错误追踪**：统计错误次数、重试次数和等待时间

## 结构

```
ccsession/
├── CLAUDE.md
├── README.md
└── skill/
    ├── SKILL.md              # Skill 定义（user-invocable）+ 渲染规范
    ├── scripts/
    │   ├── parse_sessions.py # 解析 JSONL，输出 JSON/Markdown
    │   └── delete_session.py # 两步确认删除
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

| 命令                                   | 说明               |
| ------------------------------------ | ---------------- |
| `/ccsession list [--project <path>]` | 表格列出所有会话         |
| `/ccsession show <sessionId>`        | 会话详情（默认展示前 3 步）  |
| `/ccsession show <sessionId> --full` | 会话详情（展示全部步骤）     |
| `/ccsession delete <sessionId>`      | 删除会话 .jsonl（需确认） |

`--project` 缺省时使用当前工作目录。`<sessionId>` 支持完整 UUID 或前缀匹配。

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
```

## 依赖

Python 3 标准库，无第三方包。

## 示例

### 示例：`/ccsession list`


#### Claude Code 会话摘要 — `/path/to/project`

共 **3** 个会话。

| 会话ID     | 模型              | 时间                                                    | 问题摘要                      | 首个问题           | AI 执行摘要                               | 文件编辑  | Subagent  | Token 用量                                                           |
| -------- | --------------- | ----------------------------------------------------- | ------------------------- | -------------- | ------------------------------------- | ----- | --------- | ------------------------------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮   | 探索 new-api 项目结构并实现认证中间件重构 | 帮我看一下这个项目的结构…  | Bash×23 / Read×18 / Edit×12 / Grep×5  | 3 个文件 | -         | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m                         |
| 63c04f2c | claude-opus-4-7 | 2026-04-19 14:21:05 → 2026-04-19 18:15:333h54m · 28 轮 | 修复 API 路由注册和添加单元测试        | 路由注册好像有问题…     | Bash×45 / Edit×32 / Read×28 / Write×4 | 8 个文件 | 2 个 agent | in:320.6k+15.2k / out:45.8k+3.1k / cc:210.3k+8.4k / cr:5.8m+420.5k |
| b2cf1a09 | glm-5.1         | 2026-04-18 09:12:40 → 2026-04-18 10:05:1852m · 6 轮    | 配置 Docker 部署环境            | 怎么用 docker 部署… | Bash×8 / Read×5 / Edit×3              | 2 个文件 | -         | in:22.1k / out:5.6k / cc:45.0k / cr:380.2k                         |

**合计 tokens** — input: 427.9k+15.2k / output: 63.7k+3.1k / cache\_creation: 375.8k+8.4k / cache\_read: 8.3m+420.5k


### 示例：`/ccsession show <sessionId>`


#### 会话详情 — `a3b370e5-2c63-42e3-831f-65744c89b44a`

| 会话ID     | 模型              | 时间                                                  | 问题摘要                      | 首个问题          | AI 执行摘要                              | 文件编辑  | Subagent | Token 用量                                   |
| -------- | --------------- | --------------------------------------------------- | ------------------------- | ------------- | ------------------------------------ | ----- | -------- | ------------------------------------------ |
| a3b370e5 | claude-opus-4-7 | 2026-04-19 19:30:22 → 2026-04-19 20:08:4738m · 14 轮 | 探索 new-api 项目结构并实现认证中间件重构 | 帮我看一下这个项目的结构… | Bash×23 / Read×18 / Edit×12 / Grep×5 | 3 个文件 | -        | in:85.2k / out:12.3k / cc:120.5k / cr:2.1m |

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