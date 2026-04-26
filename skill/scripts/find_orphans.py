#!/usr/bin/env python3
"""发现并清理 Claude Code 退出后留下的孤儿子进程（macOS-only）。

孤儿判定（同时满足）:
  1. ppid == 1（父已死，被 launchd 接管）
  2. cwd 落在某个 ~/.claude/projects/ 注册项目目录内（cwd 编码后查表）
  3. 不是任何 live claude 进程的子孙

模式:
  --mode list                          列出当前用户下的孤儿进程
  --mode kill --pids <p1,p2,...>       无 --force 返回预览 + exit 2
  --mode kill --pids <ids> --force     SIGTERM → 等 5s → 残留 SIGKILL → 等 1s 复核

只用 Python 3 标准库；macOS 之外的平台直接退出。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SIGTERM_WAIT_S = 5.0
SIGKILL_WAIT_S = 1.0
POLL_INTERVAL_S = 0.25
LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

# 过宽路径：即使在 ~/.claude/projects/ 里有对应编码，也不作为孤儿匹配的项目根。
# 否则 cwd 在 $HOME 下的 macOS 系统守护进程都会被误判（它们 ppid 也是 1）。
HOME = str(Path.home())
TOO_BROAD_AS_PROJECT = {HOME, str(Path.home().parent), "/"}


def encode_path(abs_path: str) -> str:
    """与 parse_sessions.encode_project_path 一致的编码规则（/、_、. 都转 -），但容忍不存在的路径。"""
    return abs_path.rstrip("/").replace("/", "-").replace("_", "-").replace(".", "-")


def claude_project_encodings() -> set[str]:
    if not CLAUDE_PROJECTS.is_dir():
        return set()
    return {p.name for p in CLAUDE_PROJECTS.iterdir() if p.is_dir()}


def match_claude_project(cwd: str, encodings: set[str]) -> str | None:
    """从 cwd 向上逐级查找，返回最具体（最深）的 claude 项目真实路径；没匹配到返回 None。

    跳过过宽路径（$HOME / / 等），否则 cwd 在 $HOME 下的 macOS 守护进程会全部误判。
    """
    if not cwd or not cwd.startswith("/"):
        return None
    p = cwd.rstrip("/")
    while p:
        if p not in TOO_BROAD_AS_PROJECT and encode_path(p) in encodings:
            return p
        idx = p.rfind("/")
        if idx <= 0:
            break
        p = p[:idx]
    return None


def read_ps() -> list[dict]:
    """跑 ps，解析为进程记录列表。LC_ALL=C 强制英文 locale，避免 zh_CN 下 lstart token 数变化。"""
    env = {**os.environ, "LC_ALL": "C", "LC_TIME": "C"}
    try:
        result = subprocess.run(
            ["ps", "-axwwo", "pid=,ppid=,pgid=,user=,lstart=,etime=,rss=,command="],
            capture_output=True, text=True, check=True, env=env,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"ps failed: {e}", file=sys.stderr)
        return []
    procs: list[dict] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # tokens: pid ppid pgid user lstart(5) etime rss command(rest)
        tokens = line.split(None, 11)
        if len(tokens) < 12:
            continue
        try:
            pid = int(tokens[0])
            ppid = int(tokens[1])
            pgid = int(tokens[2])
        except ValueError:
            continue
        user = tokens[3]
        lstart_raw = " ".join(tokens[4:9])
        try:
            lstart_dt = datetime.strptime(lstart_raw, LSTART_FORMAT)
            lstart_iso = lstart_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            lstart_iso = "-"
        etime = tokens[9]
        try:
            rss_kb = int(tokens[10])
        except ValueError:
            rss_kb = 0
        command = tokens[11]
        procs.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "user": user,
            "started": lstart_iso,
            "lstart_raw": lstart_raw,
            "elapsed": etime,
            "rss_kb": rss_kb,
            "command": command,
        })
    return procs


def read_cwds() -> dict[int, str]:
    """跑 lsof -d cwd 拿到每个 PID 的 cwd（macOS 上无 /proc，这是最稳的办法）。"""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["lsof", "-d", "cwd", "-Fpn"],
            capture_output=True, text=True, env=env,
        )
    except FileNotFoundError:
        print("lsof not found", file=sys.stderr)
        return {}
    cwds: dict[int, str] = {}
    cur_pid: int | None = None
    for line in result.stdout.splitlines():
        if not line:
            continue
        code, val = line[0], line[1:]
        if code == "p":
            try:
                cur_pid = int(val)
            except ValueError:
                cur_pid = None
        elif code == "n" and cur_pid is not None:
            # -d cwd 限定了 FD，每个 PID 只会有一条 n 记录
            cwds.setdefault(cur_pid, val)
    return cwds


def is_claude_command(command: str) -> bool:
    """识别 claude code 的 CLI 进程本体——而非碰巧路径里含 claude-code 的脚本。"""
    if "claude-code/cli.js" in command:
        return True
    # /usr/local/bin/claude 这类完整路径调用
    if re.search(r"/claude(\s|$)", command):
        return True
    # 直接 exec'd 的 claude 命令
    if command == "claude" or command.startswith("claude "):
        return True
    return False


def collect_descendants(seed_pids: set[int], procs: list[dict]) -> set[int]:
    """BFS 从 seed_pids 出发收集所有子孙 PID（含自身）。"""
    by_ppid: dict[int, list[int]] = {}
    for p in procs:
        by_ppid.setdefault(p["ppid"], []).append(p["pid"])
    visited = set(seed_pids)
    queue = list(seed_pids)
    while queue:
        pid = queue.pop()
        for child in by_ppid.get(pid, []):
            if child not in visited:
                visited.add(child)
                queue.append(child)
    return visited


def fmt_rss_mb(rss_kb: int) -> float:
    return round(rss_kb / 1024, 1)


def find_orphans(current_project: str | None, only_current: bool = False) -> dict:
    encodings = claude_project_encodings()
    procs = read_ps()
    cwds = read_cwds()
    by_pid = {p["pid"]: p for p in procs}

    live_claude = {p["pid"] for p in procs if is_claude_command(p["command"])}
    excluded = collect_descendants(live_claude, procs)

    me = os.getpid()
    excluded.add(me)

    orphans: list[dict] = []
    for p in procs:
        if p["pid"] in excluded:
            continue
        if p["ppid"] != 1:
            continue
        cwd = cwds.get(p["pid"])
        if not cwd:
            continue
        proj = match_claude_project(cwd, encodings)
        if not proj:
            continue
        is_current = current_project is not None and proj == current_project
        if only_current and not is_current:
            continue
        # 子孙树：当前快照里 ppid 链下的所有进程（典型是 zsh wrapper → bun/go → go-build）
        subtree_pids = collect_descendants({p["pid"]}, procs) - {p["pid"]}
        descendants = []
        for sp in sorted(subtree_pids):
            d = by_pid.get(sp)
            if not d:
                continue
            descendants.append({
                "pid": sp,
                "ppid": d["ppid"],
                "pgid": d["pgid"],
                "command": d["command"][:200],
            })
        orphans.append({
            "pid": p["pid"],
            "ppid": p["ppid"],
            "pgid": p["pgid"],
            "user": p["user"],
            "started": p["started"],
            "lstart_raw": p["lstart_raw"],
            "elapsed": p["elapsed"],
            "rss_mb": fmt_rss_mb(p["rss_kb"]),
            "cwd": cwd,
            "project": proj,
            "is_current_project": is_current,
            "command": p["command"],
            "descendants": descendants,
        })

    return {
        "scope": {
            "current_project": current_project,
            "only_current": only_current,
            "claude_projects_found": len(encodings),
            "live_claude_pids": sorted(live_claude),
        },
        "orphans": orphans,
        "total": len(orphans),
    }


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户
        return True


def kill_one(target: dict) -> dict:
    """对单个目标走 SIGTERM → 等 5s → 残留 SIGKILL → 等 1s 复核 流程。

    pgid==pid（session leader）时用 os.killpg 给整个进程组发，整条 fork 链
    （zsh wrapper → dev server → 编译产物）一次到位；否则退化为 os.kill 单 PID。
    """
    pid = target["pid"]
    pgid = target.get("pgid", pid)
    # 只要进程在自己的进程组里（不是 init 组 1，也不是默认组 0），就用 killpg 整组发。
    # daemonize 出来的链常见情况是 pgid == 中间退出 fork 父的 pid（pgid != pid），
    # 但 pgroup 仍然有效——所以判定不能要求 pgid==pid。
    use_pgroup = pgid > 1
    record = {
        "pid": pid,
        "pgid": pgid,
        "use_pgroup": use_pgroup,
        "command": target["command"],
        "cwd": target["cwd"],
    }

    def _send(sig):
        if use_pgroup:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)

    try:
        _send(signal.SIGTERM)
    except ProcessLookupError:
        return {**record, "method": "already_dead", "elapsed_ms": 0, "alive": False}
    except PermissionError:
        return {**record, "method": "permission_denied", "elapsed_ms": 0, "alive": is_alive(pid)}

    start = time.monotonic()
    deadline = start + SIGTERM_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        if not is_alive(pid):
            return {
                **record,
                "method": "SIGTERM",
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "alive": False,
            }

    # SIGTERM 5s 内未退出，升级到 SIGKILL
    try:
        _send(signal.SIGKILL)
    except ProcessLookupError:
        return {
            **record,
            "method": "SIGTERM_late",
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "alive": False,
        }
    sk_deadline = time.monotonic() + SIGKILL_WAIT_S
    while time.monotonic() < sk_deadline:
        time.sleep(POLL_INTERVAL_S)
        if not is_alive(pid):
            return {
                **record,
                "method": "SIGKILL",
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "alive": False,
            }
    return {
        **record,
        "method": "SIGKILL_failed",
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "alive": True,
    }


def kill_orphans(pid_args: list[int], force: bool, current_project: str | None) -> tuple[dict, int]:
    """重新跑一次孤儿检测做防 race 校验，再决定是否动手。返回 (data, exit_code)。"""
    snapshot = find_orphans(current_project, only_current=False)
    by_pid = {o["pid"]: o for o in snapshot["orphans"]}

    targets: list[dict] = []
    skipped: list[dict] = []
    for pid in pid_args:
        if pid in by_pid:
            targets.append(by_pid[pid])
            continue
        # PID 没在最新快照里——给一个具体的原因
        try:
            os.kill(pid, 0)
            skipped.append({"pid": pid, "reason": "not_orphan"})
        except ProcessLookupError:
            skipped.append({"pid": pid, "reason": "not_found"})
        except PermissionError:
            skipped.append({"pid": pid, "reason": "permission_denied"})

    if not force:
        return (
            {
                "preview": True,
                "targets": targets,
                "skipped": skipped,
                "message": (
                    "以上进程将被终止（先 SIGTERM 等 5 秒，残留再 SIGKILL）。"
                    "请向用户确认后，加 --force 重新调用本脚本执行。"
                ),
            },
            2,
        )

    killed = [kill_one(t) for t in targets]
    still_alive = [k for k in killed if k.get("alive")]
    return (
        {
            "force": True,
            "killed": killed,
            "skipped": skipped,
            "still_alive": still_alive,
        },
        0 if not still_alive else 1,
    )


def render_list_md(data: dict) -> str:
    out: list[str] = ["# Claude 孤儿进程"]
    out.append("")
    scope = data["scope"]
    out.append(
        f"扫描了 **{scope['claude_projects_found']}** 个 claude 项目目录，"
        f"live claude PID: {scope['live_claude_pids'] or '(无)'}。"
    )
    out.append("")
    if not data["orphans"]:
        out.append("_未发现孤儿进程。_")
        return "\n".join(out)
    out.append(f"共 **{data['total']}** 个孤儿进程：")
    out.append("")
    out.append("| PID | pgid | 命令 | cwd | 启动 | 已运行 | RSS | 子孙 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for o in data["orphans"]:
        cmd = o["command"]
        if len(cmd) > 60:
            cmd = cmd[:60] + "…"
        descn = len(o.get("descendants", []))
        desc_col = f"{descn} 个" if descn else "-"
        out.append(
            f"| {o['pid']} | {o.get('pgid', '-')} | `{cmd}` | `{o['cwd']}` | {o['started']} "
            f"| {o['elapsed']} | {o['rss_mb']} MB | {desc_col} |"
        )
        # 把每条 fork 链子孙摊一下，便于命令行直接看清杀谁
        for d in o.get("descendants", []):
            dcmd = d["command"]
            if len(dcmd) > 60:
                dcmd = dcmd[:60] + "…"
            out.append(f"| └─{d['pid']} | {d['pgid']} | `{dcmd}` | | | | | |")
    out.append("")
    out.append("清理：`/ccsession kill <pid>[,<pid>...]`（pgid==pid 时整个进程组一起 SIGTERM）")
    return "\n".join(out)


def render_kill_md(data: dict) -> str:
    out: list[str] = []
    if data.get("preview"):
        out.append("## 即将终止的进程")
        if not data["targets"]:
            out.append("_没有匹配的孤儿进程。_")
        else:
            out.append("")
            out.append("| PID | 命令 | cwd | 已运行 |")
            out.append("|---|---|---|---|")
            for t in data["targets"]:
                cmd = t["command"]
                if len(cmd) > 60:
                    cmd = cmd[:60] + "…"
                out.append(f"| {t['pid']} | `{cmd}` | `{t['cwd']}` | {t['elapsed']} |")
        if data.get("skipped"):
            out.append("")
            out.append("跳过：")
            for s in data["skipped"]:
                out.append(f"- PID {s['pid']}: {s['reason']}")
        out.append("")
        out.append(data.get("message", ""))
        return "\n".join(out)

    out.append("## 终止结果")
    out.append("")
    if data.get("killed"):
        out.append("| PID | 方式 | 耗时 | 状态 |")
        out.append("|---|---|---|---|")
        for k in data["killed"]:
            status = "✅ 已退出" if not k.get("alive") else "⚠️ 仍在运行"
            out.append(f"| {k['pid']} | {k['method']} | {k['elapsed_ms']} ms | {status} |")
    if data.get("skipped"):
        out.append("")
        out.append("跳过：")
        for s in data["skipped"]:
            out.append(f"- PID {s['pid']}: {s['reason']}")
    return "\n".join(out)


def parse_pid_list(raw: str) -> list[int]:
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            print(f"非法 PID: {token!r}", file=sys.stderr)
            sys.exit(2)
    return out


def main() -> int:
    if platform.system() != "Darwin":
        print("当前脚本仅在 macOS 验证过；其它平台未支持。", file=sys.stderr)
        return 0

    ap = argparse.ArgumentParser(
        description="发现并清理 Claude Code 退出后的孤儿子进程"
    )
    ap.add_argument("--project", default=str(Path.cwd()), help="当前项目绝对路径（默认 $PWD），用于标记 is_current_project")
    ap.add_argument("--mode", choices=["list", "kill"], default="list")
    ap.add_argument("--pids", default="", help="kill 模式下逗号分隔的 PID 列表")
    ap.add_argument("--force", action="store_true", help="kill 模式下实际执行；否则只返回预览（exit 2）")
    ap.add_argument("--only-current", action="store_true", help="list 模式下只列出当前项目的孤儿（默认列出所有 claude 项目）")
    ap.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = ap.parse_args()

    current_project = args.project

    if args.mode == "list":
        data = find_orphans(current_project, only_current=args.only_current)
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(render_list_md(data))
        return 0

    # mode == "kill"
    pids = parse_pid_list(args.pids)
    if not pids:
        print("kill 模式必须通过 --pids 指定 PID（逗号分隔）", file=sys.stderr)
        return 2
    data, code = kill_orphans(pids, args.force, current_project)
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_kill_md(data))
    return code


if __name__ == "__main__":
    sys.exit(main())
