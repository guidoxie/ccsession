#!/usr/bin/env python3
"""Parse Claude Code session JSONL files for a given project directory.

Usage:
    parse_sessions.py --project <path> --mode summary
    parse_sessions.py --project <path> --mode detail --session <sessionId>
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def encode_project_path(abs_path: str) -> str:
    p = Path(abs_path).expanduser().resolve()
    return str(p).replace("/", "-").replace("_", "-")


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
    user_turns: int = 0
    first_question: str = ""
    last_question: str = ""
    all_questions: list[str] = field(default_factory=list)
    question_modes: list[str] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: {"in": 0, "out": 0, "cc": 0, "cr": 0}
    )
    corrupted_lines: int = 0  # JSON 解析失败的行数
    # 注：token 去重依赖 requestId，无 requestId 的 assistant 记录会被重复计数（影响 <1%）
    models: set[str] = field(default_factory=set)
    # Subagent
    subagents: list[dict] = field(default_factory=list)
    subagent_tokens: dict[str, int] = field(
        default_factory=lambda: {"in": 0, "out": 0, "cc": 0, "cr": 0}
    )
    # API errors
    api_errors: int = 0
    api_error_types: dict[str, int] = field(default_factory=dict)
    api_retries: int = 0
    api_retry_wait_ms: int = 0
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

        t = rec.get("type")

        # API error detection
        if rec.get("apiErrorStatus"):
            s.api_errors += 1
            status = str(rec["apiErrorStatus"])
            s.api_error_types[status] = s.api_error_types.get(status, 0) + 1

        if t == "system" and rec.get("subtype") == "api_error":
            s.api_retries += 1
            s.api_retry_wait_ms += int(rec.get("retryInMs", 0) or 0)

        if t == "user":
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                q = content.strip().replace("\n", " ")
                if is_real_question(q):
                    s.user_turns += 1
                    s.all_questions.append(q)
                    s.question_modes.append(rec.get("permissionMode", "default"))
                    if not s.first_question:
                        s.first_question = q[:80] + ("…" if len(q) > 80 else "")
                    s.last_question = q[:80] + ("…" if len(q) > 80 else "")
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
    s.subagents, s.subagent_tokens = _analyze_subagents(path.parent, s.session_id)
    return s


def _analyze_subagents(session_dir: Path, session_id: str) -> tuple[list[dict], dict[str, int]]:
    sub_dir = session_dir / session_id / "subagents"
    if not sub_dir.is_dir():
        return [], {"in": 0, "out": 0, "cc": 0, "cr": 0}
    agents = []
    totals = {"in": 0, "out": 0, "cc": 0, "cr": 0}
    for meta_file in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        agent_id = meta_file.stem.replace("agent-", "").removesuffix(".meta")
        jsonl_file = sub_dir / f"agent-{agent_id}.jsonl"
        info = {
            "id": agent_id,
            "type": meta.get("agentType", "?"),
            "desc": meta.get("description", ""),
            "tokens": {"in": 0, "out": 0, "cc": 0, "cr": 0},
        }
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
                info["tokens"]["in"] += u.get("input_tokens", 0) or 0
                info["tokens"]["out"] += u.get("output_tokens", 0) or 0
                info["tokens"]["cc"] += u.get("cache_creation_input_tokens", 0) or 0
                info["tokens"]["cr"] += u.get("cache_read_input_tokens", 0) or 0
        agents.append(info)
        for k in totals:
            totals[k] += info["tokens"][k]
    return agents, totals


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
    out.append("")

    if s.all_questions:
        out.append("## 用户提问")
        for i, q in enumerate(s.all_questions, 1):
            snippet = q if len(q) <= 200 else q[:200] + "…"
            out.append(f"{i}. {snippet}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze Claude Code sessions for a project.")
    ap.add_argument("--project", default=str(Path.cwd()), help="项目绝对路径（默认当前目录）")
    ap.add_argument("--mode", choices=["summary", "detail"], default="summary")
    ap.add_argument("--session", default=None, help="detail 模式下必须指定 sessionId")
    ap.add_argument("--full", action="store_true", help="detail 模式展示全部步骤（默认只展示前 3 步）")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown", help="输出格式")
    ap.add_argument("--sort", choices=["start", "end", "turns", "duration"], default="start",
                    help="summary 排序字段（默认 start）")
    ap.add_argument("--desc", action="store_true", help="降序排列（默认升序）")
    args = ap.parse_args()

    pdir = project_dir(args.project)
    files = find_sessions(args.project)
    if not files:
        print(f"未找到会话。期望目录：{pdir}")
        print(f"（对应项目路径：{args.project}）")
        return 0

    if args.mode == "summary":
        rows = [aggregate(f) for f in files]

        def sort_key(s: SessionStats):
            if args.sort == "turns":
                return s.user_turns
            if args.sort == "duration":
                a = datetime.fromisoformat(s.start.replace("Z", "+00:00")) if s.start else datetime.min.replace(tzinfo=None)
                b = datetime.fromisoformat(s.end.replace("Z", "+00:00")) if s.end else a
                return (b - a).total_seconds()
            if args.sort == "end":
                return s.end or ""
            return s.start or ""

        rows.sort(key=sort_key, reverse=args.desc)
        if args.format == "json":
            data = []
            for s in rows:
                data.append({
                    "session_id": s.session_id,
                    "models": sorted(s.models),
                    "start": s.start,
                    "end": s.end,
                    "duration": fmt_duration(s.start, s.end),
                    "user_turns": s.user_turns,
                    "all_questions": [q[:200] + ("…" if len(q) > 200 else "") for q in s.all_questions],
                    "question_modes": s.question_modes,
                    "first_question": s.first_question,
                    "last_question": s.last_question,
                    "tool_counts": s.tool_counts,
                    "tokens": s.tokens,
                    "subagents": s.subagents,
                    "subagent_tokens": s.subagent_tokens,
                    "api_errors": s.api_errors,
                    "api_error_types": s.api_error_types,
                    "api_retries": s.api_retries,
                    "api_retry_wait_ms": s.api_retry_wait_ms,
                    "files_edited": s.files_edited,
                    "corrupted_lines": s.corrupted_lines,
                })
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(render_summary(args.project, rows))
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
    stats = aggregate(target)
    if args.format == "json":
        detail_data = {
            "session_id": stats.session_id,
            "slug": stats.slug,
            "models": sorted(stats.models),
            "start": stats.start,
            "end": stats.end,
            "duration": fmt_duration(stats.start, stats.end),
            "user_turns": stats.user_turns,
            "all_questions": [q[:200] + ("…" if len(q) > 200 else "") for q in stats.all_questions],
            "question_modes": stats.question_modes,
            "first_question": stats.first_question,
            "last_question": stats.last_question,
            "tool_counts": stats.tool_counts,
            "steps": [{"ts": st.ts, "name": st.name, "detail": st.detail}
                       for st in (stats.steps if args.full else stats.steps[:3])],
            "total_steps": len(stats.steps),
            "tokens": stats.tokens,
            "subagents": stats.subagents,
            "subagent_tokens": stats.subagent_tokens,
            "api_errors": stats.api_errors,
            "api_error_types": stats.api_error_types,
            "api_retries": stats.api_retries,
            "api_retry_wait_ms": stats.api_retry_wait_ms,
            "files_edited": stats.files_edited,
            "corrupted_lines": stats.corrupted_lines,
        }
        print(json.dumps(detail_data, ensure_ascii=False, indent=2))
    else:
        print(render_detail(stats, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
