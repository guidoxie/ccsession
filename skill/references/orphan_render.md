# procs / kill 渲染规范

`/ccsession procs` 与 `/ccsession kill` 的表格列、字段映射、空态文案、确认提示——AI 只在这两个子命令真被调用时 Read 此文件。

## `find_orphans.py` JSON 返回结构

### `--mode list --format json`

```json
{
  "scope": {"live_claude_pids": [12345, 67890]},
  "orphans": [
    {
      "pid": 11111,
      "pgid": 11111,
      "command": "...",
      "cwd": "/Users/foo/Projects/bar",
      "project": "/Users/foo/Projects/bar",
      "is_current_project": true,
      "started": "2026-04-23 09:40:29",
      "elapsed": "04-18:43:42",
      "rss_mb": 87.4,
      "descendants": [{"pid": 22222, "command": "..."}, ...]
    },
    ...
  ],
  "total": 3
}
```

### `--mode kill --pids <ids> --format json`（无 `--force`，预览）

返回 exit code 2 + JSON：

```json
{
  "preview": true,
  "targets": [{ /* 同 orphans[i] 结构 */ }],
  "skipped": [{"pid": ..., "reason": "not orphan / not in project / already dead"}]
}
```

### `--mode kill --pids <ids> --format json --force`（实际终止）

```json
{
  "killed": [
    {
      "pid": 11111,
      "use_pgroup": true,
      "command": "...",
      "method": "SIGTERM",
      "elapsed_ms": 120,
      "alive": false
    },
    ...
  ],
  "still_alive": [...],
  "skipped": [...]
}
```

`method` 取值：`SIGTERM` / `SIGKILL` / `SIGTERM_late` / `SIGKILL_failed` / `already_dead` / `permission_denied`。

## `/ccsession procs` 渲染流程

1. 调 `find_orphans.py --mode list --format json`，解析 `scope.live_claude_pids` / `orphans[]` / `total`。
2. 列表为空时：明确告知「未发现孤儿进程」，简述判定规则（ppid=1 + cwd 在 claude 项目内 + 非 live claude 子孙）。
3. 列表非空时：渲染表格 + 表格底部给出「清理示例：`/ccsession kill <pid>`（多个用逗号分隔）」。

### 孤儿进程表格列

| 列 | 数据来源 | 格式 |
|---|---|---|
| PID | `pid` | 原值 |
| pgid | `pgid` | 原值；只要 `pgid > 1`（自己的独立进程组），kill 都会走 killpg 整组发 |
| 命令 | `command` | 截断 60 字符，超出加 `…`；用反引号包裹 |
| cwd | `cwd` | 原值；用反引号包裹 |
| 项目 | `project` | 项目根（非 cwd）；标注 `(当前)` 当 `is_current_project=true` |
| 启动 | `started` | `YYYY-MM-DD HH:MM:SS` |
| 已运行 | `elapsed` | 原值（`ps` 给的格式，如 `04-18:43:42` 表示 4 天 18 小时） |
| RSS | `rss_mb` | `{x.x} MB`（值已是 MB） |
| 子孙 | `descendants` | `{N} 个`；非空时在主行下用缩进列出每条 `└─ pid · command`（典型场景：zsh wrapper → bun/go → go-build/main 这种三层链） |

## `/ccsession kill <pid>[,<pid>...]` 渲染流程

1. **先**调不带 `--force` 的 `find_orphans.py --mode kill --pids <ids>`，获得预览（`preview: true`）+ exit code 2。
2. 渲染预览：列出 `targets`（即将终止的进程）和 `skipped`（无法处理的 PID 及原因）。
3. 如果 `target.descendants` 非空，提醒用户该 PID 实际是 fork 链根（zsh wrapper），整组 SIGTERM 会一并清掉子孙。
4. **必须**明文询问「确认终止以上 N 个进程？(yes / no)」，并提示策略「先 SIGTERM 等 5 秒，残留再 SIGKILL；自己的独立进程组（pgid > 1）走 killpg 整组发信号，dev server 三层 fork 链一次到位」。
5. 仅当用户回复明确肯定（`yes` / `y` / `确认` 等）时，加 `--force` 重新调用。
6. 渲染最终结果：`killed[]` / `still_alive[]` / `skipped[]` 三段。
7. 即使 `targets` 为空也要走完两步流程（脚本会返回 exit 2 + 空 targets），向用户确认无可操作后退出。

### kill 结果表格列

| 列 | 数据来源 | 格式 |
|---|---|---|
| PID | `killed[].pid` | 原值 |
| 范围 | `killed[].use_pgroup` | `进程组` / `单 PID` |
| 命令 | `killed[].command` | 截断 60 字符 |
| 方式 | `killed[].method` | 原值（SIGTERM / SIGKILL / 等） |
| 耗时 | `killed[].elapsed_ms` | `{ms} ms` |
| 状态 | `killed[].alive` | `✅ 已退出` / `⚠️ 仍在运行` |
