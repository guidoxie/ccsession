#!/usr/bin/env python3
"""Parse Claude Code session JSONL files for a given project directory.

Usage:
    parse_sessions.py --project <path> --mode summary
    parse_sessions.py --project <path> --mode detail --session <sessionId>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CACHE_FILENAME = ".ccsession_cache.json"


def encode_project_path(abs_path: str) -> str:
    # Claude Code 实际编码会把 `/`、`_`、`.` 都压成 `-`（比如 /Users/foo/.claude → -Users-foo--claude）
    p = Path(abs_path).expanduser().resolve()
    return str(p).replace("/", "-").replace("_", "-").replace(".", "-")


def project_dir(abs_path: str) -> Path:
    return CLAUDE_PROJECTS / encode_project_path(abs_path)


def find_sessions(abs_path: str) -> list[Path]:
    d = project_dir(abs_path)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield None


@dataclass
class Step:
    ts: str
    name: str
    detail: str


@dataclass
class SessionStats:
    session_id: str
    slug: str = ""
    start: str = ""
    end: str = ""
    cwd: str = ""  # 用于 git log 区间查询
    user_turns: int = 0
    first_question: str = ""
    last_question: str = ""
    last_prompt: str = ""  # 来自 type==last-prompt 条目的 lastPrompt（比尾部 user 行更准）
    raw_summary: str = ""  # /compact 留下的"前序会话整体压缩"（type==user + isCompactSummary）
    commits: list[dict] = field(default_factory=list)  # [{"hash","subject"}]，会话期间 cwd 的 commits
    tool_counts: dict[str, int] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: {"in": 0, "out": 0, "cc": 0, "cr": 0}
    )
    corrupted_lines: int = 0  # JSON 解析失败的行数
    # 注：token 去重依赖 requestId，无 requestId 的 assistant 记录会被重复计数（影响 <1%）
    models: set[str] = field(default_factory=set)
    # Subagent
    subagents: list[dict] = field(default_factory=list)  # [{"type","desc","tokens"}]
    subagent_count: int = 0
    subagent_tokens: dict[str, int] = field(
        default_factory=lambda: {"in": 0, "out": 0, "cc": 0, "cr": 0}
    )
    # API errors
    api_errors: int = 0
    api_retries: int = 0
    # File edits
    files_edited: list[str] = field(default_factory=list)


def classify(name: str, input_: dict) -> tuple[str, str]:
    """Map a tool call to (category_label, short_detail)."""
    if not isinstance(input_, dict):
        input_ = {}
    if name == "Bash":
        return "Bash", str(input_.get("command", ""))[:160]
    if name == "NotebookEdit":
        return "NotebookEdit", str(input_.get("notebook_path", ""))[:160]
    if name == "Skill":
        skill_name = input_.get("skill", "?")
        args = input_.get("args", "") or ""
        return f"Skill[{skill_name}]", str(args)[:100]
    if name in ("Task", "Agent"):
        sub = input_.get("subagent_type", "")
        desc = input_.get("description", "")
        label = f"Agent[{sub}]" if sub else "Agent"
        return label, str(desc)[:100]
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "?"
        tool = parts[-1] if len(parts) > 2 else ""
        return f"MCP[{server}]", tool
    # generic tool (Read/Edit/Write/Grep/Glob/...)
    if "file_path" in input_:
        return name, str(input_["file_path"])
    if "pattern" in input_:
        return name, str(input_["pattern"])[:100]
    if "path" in input_:
        return name, str(input_["path"])
    try:
        return name, json.dumps(input_, ensure_ascii=False)[:100]
    except Exception:
        return name, ""


def is_real_question(content: str) -> bool:
    noise_prefixes = (
        "<local-command-caveat>",
        "<local-command-stdout",
        "<local-command-stdin",
        "<bash-input>",
        "<bash-stdout>",
        "This session is being continued",
    )
    return not content.startswith(noise_prefixes)


def aggregate(path: Path) -> SessionStats:
    s = SessionStats(session_id=path.stem)
    seen_requests: set[str] = set()
    for rec in iter_jsonl(path):
        if rec is None:
            s.corrupted_lines += 1
            continue
        if not isinstance(rec, dict):
            continue
        ts = rec.get("timestamp", "")
        if ts:
            if not s.start:
                s.start = ts
            s.end = ts
        if not s.slug and rec.get("slug"):
            s.slug = rec["slug"]
        if not s.cwd and rec.get("cwd"):
            s.cwd = rec["cwd"]

        t = rec.get("type")

        # API error detection
        if rec.get("apiErrorStatus"):
            s.api_errors += 1

        if t == "system" and rec.get("subtype") == "api_error":
            s.api_retries += 1

        # /compact：上一段会话的整体压缩，写入新会话首条 user 行（type==user + isCompactSummary）
        # message.content 是长文本，原样留给 AI 作"开场背景"信号
        if t == "user" and rec.get("isCompactSummary"):
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and not s.raw_summary:
                s.raw_summary = content

        # Claude Code 主动落盘的"该会话最后一条用户提示"
        if t == "last-prompt":
            lp = rec.get("lastPrompt") or ""
            if lp:
                s.last_prompt = lp.strip().replace("\n", " ")

        if t == "user":
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                q = content.strip().replace("\n", " ")
                if is_real_question(q):
                    s.user_turns += 1
                    if not s.first_question:
                        s.first_question = q
                    s.last_question = q
        elif t == "assistant":
            req_id = rec.get("requestId")
            if req_id and req_id in seen_requests:
                continue
            if req_id:
                seen_requests.add(req_id)
            msg = rec.get("message") or {}
            model = msg.get("model")
            if model and not model.startswith("<"):
                s.models.add(model)
            u = msg.get("usage") or {}
            s.tokens["in"] += u.get("input_tokens", 0) or 0
            s.tokens["out"] += u.get("output_tokens", 0) or 0
            s.tokens["cc"] += u.get("cache_creation_input_tokens", 0) or 0
            s.tokens["cr"] += u.get("cache_read_input_tokens", 0) or 0
            content = msg.get("content") or []
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct == "tool_use":
                        cat, detail = classify(c.get("name", ""), c.get("input") or {})
                        s.tool_counts[cat] = s.tool_counts.get(cat, 0) + 1
                        s.steps.append(Step(ts, cat, detail))
                        # File edit tracking
                        if cat in ("Edit", "Write", "NotebookEdit") and detail:
                            if detail not in s.files_edited:
                                s.files_edited.append(detail)
                    elif ct == "server_tool_use":
                        sname = c.get("name", "server_tool")
                        s.tool_counts[f"ServerTool[{sname}]"] = s.tool_counts.get(f"ServerTool[{sname}]", 0) + 1

    # Subagent analysis
    s.subagents, s.subagent_count, s.subagent_tokens = _analyze_subagents(path.parent, s.session_id)
    # 会话期间 cwd 内的 git commits（最权威的"做了什么"信号）
    fetch_commits_from_git(s)
    return s


# git commit message 比 jsonl 中的 Bash 命令字符串好抠（heredoc 形式无法稳定 regex）
def fetch_commits_from_git(stats: SessionStats) -> None:
    if not (stats.cwd and stats.start and stats.end):
        return
    try:
        result = subprocess.run(
            [
                "git", "-C", stats.cwd, "log",
                f"--since={stats.start}", f"--until={stats.end}",
                "--format=%h%x09%s", "--no-merges",
            ],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return
    if result.returncode != 0:
        return
    for line in result.stdout.strip().splitlines():
        if "\t" in line:
            h, subj = line.split("\t", 1)
            stats.commits.append({"hash": h, "subject": subj[:120]})


def _analyze_subagents(session_dir: Path, session_id: str) -> tuple[list[dict], int, dict[str, int]]:
    sub_dir = session_dir / session_id / "subagents"
    if not sub_dir.is_dir():
        return [], 0, {"in": 0, "out": 0, "cc": 0, "cr": 0}
    agents: list[dict] = []
    totals = {"in": 0, "out": 0, "cc": 0, "cr": 0}
    for meta_file in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        agent_id = meta_file.stem.replace("agent-", "").removesuffix(".meta")
        jsonl_file = sub_dir / f"agent-{agent_id}.jsonl"
        agent_tokens = {"in": 0, "out": 0, "cc": 0, "cr": 0}
        if jsonl_file.exists():
            seen: set[str] = set()
            for rec in iter_jsonl(jsonl_file):
                if rec is None:
                    continue
                if rec.get("type") != "assistant":
                    continue
                rid = rec.get("requestId")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                u = (rec.get("message") or {}).get("usage") or {}
                agent_tokens["in"] += u.get("input_tokens", 0) or 0
                agent_tokens["out"] += u.get("output_tokens", 0) or 0
                agent_tokens["cc"] += u.get("cache_creation_input_tokens", 0) or 0
                agent_tokens["cr"] += u.get("cache_read_input_tokens", 0) or 0
        agents.append({
            "type": meta.get("agentType", "?"),
            "desc": meta.get("description", ""),
            "tokens": agent_tokens,
        })
        for k in totals:
            totals[k] += agent_tokens[k]
    return agents, len(agents), totals


def summary_line(tc: dict[str, int]) -> str:
    if not tc:
        return "(无工具调用)"
    return " / ".join(f"{k}×{v}" for k, v in sorted(tc.items(), key=lambda x: -x[1]))


def fmt_duration(start: str, end: str) -> str:
    if not (start and end):
        return "-"
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    s = int((b - a).total_seconds())
    if s < 0:
        return "-"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def fmt_ts(ts: str) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def fmt_time_only(ts: str) -> str:
    if not ts:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone().strftime("%H:%M:%S")


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(project: str, rows: list[SessionStats]) -> str:
    out: list[str] = []
    out.append(f"# Claude Code 会话摘要 — `{project}`")
    out.append("")
    out.append(f"共 **{len(rows)}** 个会话。")
    out.append("")
    header = [
        "会话ID",
        "模型",
        "时间",
        "首个问题",
        "AI 执行摘要",
        "Token 用量",
    ]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join(["---"] * len(header)) + "|")
    totals = {"in": 0, "out": 0, "cc": 0, "cr": 0}
    for s in rows:
        totals["in"] += s.tokens["in"]
        totals["out"] += s.tokens["out"]
        totals["cc"] += s.tokens["cc"]
        totals["cr"] += s.tokens["cr"]
        tok = (
            f"in:{s.tokens['in']:,} / out:{s.tokens['out']:,}"
            f" / cc:{s.tokens['cc']:,} / cr:{s.tokens['cr']:,}"
        )
        time_col = (
            f"{fmt_ts(s.start)} → {fmt_ts(s.end)}"
            f"<br>{fmt_duration(s.start, s.end)} · {s.user_turns} 轮"
        )
        row = [
            s.session_id[:8],
            ", ".join(sorted(s.models)) or "-",
            time_col,
            md_escape(s.first_question or "(无)"),
            md_escape(summary_line(s.tool_counts)),
            tok,
        ]
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    if len(rows) > 1:
        out.append(
            f"**合计 tokens** — input: {totals['in']:,} / output: {totals['out']:,} "
            f"/ cache_creation: {totals['cc']:,} / cache_read: {totals['cr']:,}"
        )
        out.append("")
    out.append("查看某个会话详情：`/ccsession show <会话ID>`（完整 ID，非截断）")
    return "\n".join(out)


def render_detail(s: SessionStats, full: bool = False, step_preview: int = 3) -> str:
    out: list[str] = []
    out.append(f"# 会话详情 — `{s.session_id}`")
    out.append("")
    out.append(f"- **Slug**: {s.slug or '-'}")
    out.append(f"- **模型**: {', '.join(sorted(s.models)) or '-'}")
    out.append(f"- **时间**: {fmt_ts(s.start)} → {fmt_ts(s.end)}  (时长 {fmt_duration(s.start, s.end)})")
    out.append(f"- **对话轮次**: {s.user_turns}")
    out.append(
        f"- **Tokens**: input {s.tokens['in']:,} / output {s.tokens['out']:,} "
        f"/ cache_creation {s.tokens['cc']:,} / cache_read {s.tokens['cr']:,}"
    )
    out.append(f"- **AI 执行摘要**: {summary_line(s.tool_counts)}")
    if s.commits:
        commit_line = " ; ".join(f"{c['hash']} {c['subject']}" for c in s.commits[:5])
        if len(s.commits) > 5:
            commit_line += f" …(+{len(s.commits) - 5})"
        out.append(f"- **本会话提交**: {commit_line}")
    if s.last_prompt:
        out.append(f"- **最后提示**: {s.last_prompt}")
    out.append("")

    if s.subagents:
        out.append(f"## Subagent ({len(s.subagents)} 个)")
        out.append("")
        out.append("| Agent 类型 | 描述 | Token 用量 |")
        out.append("|---|---|---|")
        for a in s.subagents:
            tk = a["tokens"]
            tok = f"in:{tk['in']:,} / out:{tk['out']:,}"
            if tk.get("cc"):
                tok += f" / cc:{tk['cc']:,}"
            if tk.get("cr"):
                tok += f" / cr:{tk['cr']:,}"
            desc = a.get("desc", "")
            if len(desc) > 60:
                desc = desc[:60] + "…"
            out.append(f"| {a['type']} | {md_escape(desc)} | {tok} |")
        out.append("")

    out.append("## AI 执行步骤")
    if not s.steps:
        out.append("_无工具调用_")
    else:
        show_steps = s.steps if full else s.steps[:step_preview]
        for i, step in enumerate(show_steps, 1):
            detail = step.detail if step.detail else ""
            out.append(f"{i}. `[{fmt_time_only(step.ts)}]` **{step.name}** — {detail}")
        if not full and len(s.steps) > step_preview:
            remaining = len(s.steps) - step_preview
            out.append("")
            out.append(
                f"_… 共 {len(s.steps)} 步，还有 {remaining} 步未展示。"
                f"加 `--full` 查看全部：`/ccsession show {s.session_id} --full`_"
            )
    return "\n".join(out)


def _duration_secs(s: SessionStats) -> float:
    """会话时长（秒），用于排序。空 start/end 或解析失败时返回 0.0。"""
    if not (s.start and s.end):
        return 0.0
    try:
        a = datetime.fromisoformat(s.start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(s.end.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _load_cache_entries(project_root: Path) -> dict:
    """读 .ccsession_cache.json 的 entries 映射（v2 schema）。

    文件不存在 / 损坏 / 版本不符 → 返回空 dict。list 路径只读不写孤儿条目；
    脚本侧背填走 backfill_session_dicts() 在 list 收尾批量执行。
    """
    p = project_root / CACHE_FILENAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != 2:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _cache_lookup_dict(entries: dict, jsonl_path: Path) -> dict | None:
    """按 sessionId + mtime + size 三段命中判定。

    命中：返回 entry["session_dict"] 的副本（避免上层修改污染缓存）。
    未命中：返回 None。
    """
    if not entries:
        return None
    entry = entries.get(jsonl_path.stem)
    if not isinstance(entry, dict):
        return None
    try:
        st = jsonl_path.stat()
    except OSError:
        return None
    if entry.get("mtime") != st.st_mtime or entry.get("size") != st.st_size:
        return None
    sdict = entry.get("session_dict")
    if not isinstance(sdict, dict):
        return None
    # 浅拷贝即可——上层只读 + 排序，不修改嵌套结构
    return dict(sdict)


def _duration_secs_dict(d: dict) -> float:
    """会话时长（秒），dict 版本——给缓存命中后的排序用。"""
    start, end = d.get("start") or "", d.get("end") or ""
    if not (start and end):
        return 0.0
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _sort_dicts(rows: list[dict], sort_field: str | None, desc: bool) -> list[dict]:
    """按 SessionStats 时代相同的语义对 dict 列表排序。"""
    if sort_field is None:
        # 默认多键全 DESC：end → user_turns → duration
        rows.sort(
            key=lambda d: (d.get("end") or "", d.get("user_turns") or 0, _duration_secs_dict(d)),
            reverse=True,
        )
        return rows

    def single_key(d: dict):
        if sort_field == "turns":    return d.get("user_turns") or 0
        if sort_field == "duration": return _duration_secs_dict(d)
        if sort_field == "end":      return d.get("end") or ""
        return d.get("start") or ""
    rows.sort(key=single_key, reverse=desc)
    return rows


def _aggregate_safe(path: Path) -> SessionStats:
    """aggregate() 包一层异常隔离——单个会话解析失败不拖垮整批。"""
    try:
        return aggregate(path)
    except Exception as e:
        # 占位 stub，至少有 session_id 让上层渲染不崩
        s = SessionStats(session_id=path.stem)
        s.corrupted_lines = -1  # 用 -1 标记"整文件解析失败"
        print(f"[warn] aggregate failed for {path.name}: {e}", file=sys.stderr)
        return s


def _aggregate_all(files: list[Path], workers: int) -> list[SessionStats]:
    """summary 模式批量聚合：每个会话独立、IO 重，用线程池并发。"""
    if workers <= 0:
        workers = min(8, (os.cpu_count() or 4))
    if workers <= 1 or len(files) <= 1:
        return [_aggregate_safe(f) for f in files]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # executor.map 保持输入顺序
        return list(ex.map(_aggregate_safe, files))


def _session_to_dict(
    s: SessionStats,
    detail: bool = False,
    full: bool = False,
    cached_summary: str = "",
) -> dict:
    """构造给 Claude 渲染的 JSON。字段已按"会话摘要新流水线"精简。

    cached_summary：命中缓存时附该字段；未命中时为空字符串，AI 现场生成并回写。
    """
    d = {
        "session_id": s.session_id,
        "models": sorted(s.models),
        "start": s.start,
        "end": s.end,
        "duration": fmt_duration(s.start, s.end),
        "user_turns": s.user_turns,
        "first_question": s.first_question,
        "last_question": s.last_question,
        "last_prompt": s.last_prompt,
        "raw_summary": s.raw_summary,
        "commits": s.commits,
        "tool_counts": s.tool_counts,
        "tokens": s.tokens,
        "subagents": s.subagents,
        "subagent_count": s.subagent_count,
        "subagent_tokens": s.subagent_tokens,
        "api_errors": s.api_errors,
        "api_retries": s.api_retries,
        "files_edited": s.files_edited,
        "corrupted_lines": s.corrupted_lines,
        "cached_summary": cached_summary,
    }
    if detail:
        d["slug"] = s.slug
        steps_view = s.steps if full else s.steps[:3]
        d["steps"] = [{"ts": st.ts, "name": st.name, "detail": st.detail} for st in steps_view]
        d["total_steps"] = len(s.steps)
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze Claude Code sessions for a project.")
    ap.add_argument("--project", default=str(Path.cwd()), help="项目绝对路径（默认当前目录）")
    ap.add_argument("--mode", choices=["summary", "detail"], default="summary")
    ap.add_argument("--session", default=None, help="detail 模式下必须指定 sessionId")
    ap.add_argument("--full", action="store_true", help="detail 模式展示全部步骤（默认只展示前 3 步）")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown", help="输出格式")
    ap.add_argument("--sort", choices=["start", "end", "turns", "duration"], default=None,
                    help="单字段排序；不指定时按 end DESC → user_turns DESC → duration DESC 多键排序")
    ap.add_argument("--desc", action="store_true",
                    help="降序排列（仅在显式传 --sort 时生效；默认多键排序已是 desc）")
    ap.add_argument("--workers", type=int, default=0,
                    help="summary 模式并发线程数；0=自动（min(8, cpu)），1=串行")
    args = ap.parse_args()

    pdir = project_dir(args.project)
    files = find_sessions(args.project)
    if not files:
        print(f"未找到会话。期望目录：{pdir}")
        print(f"（对应项目路径：{args.project}）")
        return 0

    entries = _load_cache_entries(pdir)

    if args.mode == "summary":
        if args.format == "json":
            # 缓存优先：分流命中 / 未命中
            cached_dicts: dict[str, dict] = {}
            miss_paths: list[Path] = []
            for path in files:
                sdict = _cache_lookup_dict(entries, path)
                if sdict is not None:
                    cached_dicts[path.stem] = sdict
                else:
                    miss_paths.append(path)

            # 仅 miss 子集走并发 aggregate
            miss_stats = _aggregate_all(miss_paths, args.workers) if miss_paths else []

            # 构造 path → dict 映射；未命中转 dict 时 cached_summary 留空（待 AI 现场生成）
            sdict_by_stem: dict[str, dict] = dict(cached_dicts)
            backfill_items: dict[str, dict] = {}
            for path, stats in zip(miss_paths, miss_stats):
                d = _session_to_dict(stats, cached_summary="")
                sdict_by_stem[path.stem] = d
                try:
                    st = path.stat()
                    backfill_items[path.stem] = {
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                        "session_dict": d,
                    }
                except OSError:
                    pass

            # 按 find_sessions 原顺序铺开，再按 sort 规则排序
            rows = [sdict_by_stem[p.stem] for p in files if p.stem in sdict_by_stem]
            rows = _sort_dicts(rows, args.sort, args.desc)

            # 先批量回填脚本侧缓存（保证 AI 后续 cache_summary --bulk 时 entry 已存在），再 print
            if backfill_items:
                try:
                    from cache_summary import backfill_session_dicts as _backfill  # noqa: E402
                    _backfill(pdir, backfill_items)
                except Exception as e:
                    print(f"[warn] cache backfill failed: {e}", file=sys.stderr)

            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0

        # markdown 兜底路径：不走 dict 缓存（fallback 仅用于命令行调试）
        rows_stats = _aggregate_all(files, args.workers)
        if args.sort is None:
            rows_stats.sort(key=lambda s: (s.end or "", s.user_turns, _duration_secs(s)), reverse=True)
        else:
            def single_key(s: SessionStats):
                if args.sort == "turns":    return s.user_turns
                if args.sort == "duration": return _duration_secs(s)
                if args.sort == "end":      return s.end or ""
                return s.start or ""
            rows_stats.sort(key=single_key, reverse=args.desc)
        print(render_summary(args.project, rows_stats))
        return 0

    if not args.session:
        print("detail 模式必须通过 --session 指定 sessionId", file=sys.stderr)
        return 2
    target = next((f for f in files if f.stem == args.session or f.stem.startswith(args.session)), None)
    if not target:
        print(f"未找到 session {args.session}。该项目下可用 sessionId（前 5 个）：")
        for f in files[:5]:
            print(f"  - {f.stem}")
        return 1

    # detail 模式：始终跑 aggregate（要 steps），但读取已存 entry 中的 cached_summary 复用
    stats = aggregate(target)
    cached_sum = ""
    cached_entry_dict = _cache_lookup_dict(entries, target)
    if cached_entry_dict is not None:
        cs = cached_entry_dict.get("cached_summary")
        if isinstance(cs, str):
            cached_sum = cs
    if args.format == "json":
        detail_data = _session_to_dict(
            stats, detail=True, full=args.full, cached_summary=cached_sum,
        )
        print(json.dumps(detail_data, ensure_ascii=False, indent=2))
    else:
        print(render_detail(stats, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
