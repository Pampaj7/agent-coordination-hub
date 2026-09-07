"""Human-readable rendering for the CLI.

Plain text on purpose: the output is read by humans in a terminal *and* by coding
agents capturing stdout, so it stays greppable and free of control characters.
Pass ``--json`` anywhere for the raw API response.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

GLYPHS = {
    "UPDATE": "🔄",
    "QUESTION": "❓",
    "ANSWER": "💬",
    "CLAIM": "🔒",
    "HANDOFF": "🤝",
    "BLOCKED": "🚧",
    "DECISION": "📌",
    "RELEASE": "🔓",
}


def _dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(value: Any) -> str:
    """Compact relative time: 3m, 5h, 2d."""
    moment = _dt(value)
    if moment is None:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    seconds = (dt.datetime.now(dt.UTC) - moment).total_seconds()
    if seconds < 90:
        return f"{max(int(seconds), 0)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items()]
    return [str(value)]


def event_line(event: dict[str, Any]) -> str:
    glyph = GLYPHS.get(str(event.get("event_type")), "•")
    task = event.get("task") or "-"
    target = f" -> {event['target_agent']}" if event.get("target_agent") else ""
    return (
        f"{glyph} {event.get('event_type', ''):<8} {event.get('ref', ''):>6}  "
        f"{ago(event.get('created_at')):>4} ago  "
        f"{event.get('project', '')}/{task}  {event.get('agent', '')}{target}\n"
        f"        {event.get('summary', '')}"
    )


def render_event(event: dict[str, Any]) -> str:
    out = [event_line(event)]
    for key, value in (event.get("details") or {}).items():
        rows = _lines(value)
        if rows:
            out.append(f"        {key}: " + "; ".join(rows))
    if artifacts := event.get("artifacts"):
        out.append("        artifacts: " + ", ".join(str(a) for a in artifacts))
    if branch := event.get("branch"):
        out.append(f"        branch: {branch}")
    if url := event.get("github_url"):
        out.append(f"        github: {url}")
    return "\n".join(out)


def render_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(no events)"
    return "\n".join(event_line(e) for e in events)


def render_claim(claim: dict[str, Any], *, verb: str = "claimed") -> str:
    owner = claim.get("human_owner")
    who = f"{claim.get('agent')}" + (f" ({owner})" if owner else "")
    branch = f"  branch={claim['branch']}" if claim.get("branch") else ""
    return (
        f"🔒 {claim.get('task')} {verb} by {who} in {claim.get('project')}{branch}\n"
        f"   claimed_at={claim.get('claimed_at')}  "
        f"last_activity={ago(claim.get('last_activity_at'))} ago"
    )


def render_conflict(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"conflict: {payload}"
    lines = [
        "⛔ CLAIM CONFLICT",
        f"   {payload.get('detail', '')}",
        f"   task          : {payload.get('project')}/{payload.get('task')}",
        f"   current owner : {payload.get('current_owner')}"
        + (f" ({payload['human_owner']})" if payload.get("human_owner") else ""),
        f"   claimed at    : {payload.get('claimed_at')}",
        f"   last activity : {ago(payload.get('last_activity_at'))} ago",
    ]
    if branch := payload.get("branch"):
        lines.append(f"   branch        : {branch}")
    if last := payload.get("last_update"):
        lines.append(f"   last update   : [{last.get('event_type')}] {last.get('summary')}")
    lines.append("   -> Coordinate with the owner, or pick another task.")
    return "\n".join(lines)


def render_tasks(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "(no tasks)"
    header = f"{'TASK':<12} {'STATUS':<10} {'OWNER':<16} {'BRANCH':<24} {'LAST':>5}  PROJECT"
    rows = [header, "-" * len(header)]
    for task in tasks:
        flag = "🚧 " if task.get("blocked") else ""
        rows.append(
            f"{task.get('task', '')!s:<12} {flag + str(task.get('status', '')):<10} "
            f"{task.get('owner') or '-'!s:<16} {task.get('branch') or '-'!s:<24} "
            f"{ago(task.get('last_activity_at')):>5}  {task.get('project', '')}"
        )
        if reason := task.get("blocked_reason"):
            rows.append(f"{'':<12} blocked: {reason}")
    return "\n".join(rows)


def _section(title: str, body: list[str]) -> list[str]:
    if not body:
        return []
    return [f"\n{title}"] + [f"  {line}" for line in body]


def render_context(ctx: dict[str, Any]) -> str:
    out = [
        f"=== CONTEXT · {ctx.get('project')} "
        f"(last {ctx.get('window_hours')}h, generated {ctx.get('generated_at')}) ==="
    ]

    out += _section(
        "ACTIVE CLAIMS",
        [
            f"{c.get('task'):<12} {c.get('agent')}"
            + (f" ({c['human_owner']})" if c.get("human_owner") else "")
            + (f"  branch={c['branch']}" if c.get("branch") else "")
            + f"  idle {ago(c.get('last_activity_at'))}"
            for c in ctx.get("active_claims", [])
        ],
    )
    out += _section(
        "ACTIVE AGENTS",
        [
            f"{a.get('agent'):<18} tasks={', '.join(a.get('active_tasks') or []) or '-'}"
            f"  seen {ago(a.get('last_seen_at'))} ago"
            for a in ctx.get("active_agents", [])
        ],
    )
    out += _section(
        "BLOCKED",
        [
            f"{t.get('task'):<12} {t.get('owner') or t.get('blocked_by') or '-'}: "
            f"{t.get('blocked_reason')}"
            for t in ctx.get("blocked_tasks", [])
        ],
    )
    out += _section(
        "UNRESOLVED QUESTIONS",
        [
            f"{q.get('ref'):<6} {q.get('from_agent')} -> {q.get('to_agent') or 'anyone'} "
            f"({q.get('age_hours')}h): {q.get('question')}"
            for q in ctx.get("unresolved_questions", [])
        ],
    )
    out += _section(
        "RECENT UPDATES",
        [
            f"{ago(e.get('created_at')):>4} ago {e.get('agent')} [{e.get('task') or '-'}] "
            f"{e.get('summary')}"
            for e in ctx.get("recent_updates", [])
        ],
    )
    out += _section(
        "RECENT DECISIONS",
        [
            f"{e.get('ref')} {e.get('agent')}: {e.get('summary')}"
            for e in ctx.get("recent_decisions", [])
        ],
    )
    out += _section(
        "RECENT HANDOFFS",
        [
            f"{e.get('agent')} -> {e.get('target_agent')} "
            f"[{e.get('task') or '-'}]: {e.get('summary')}"
            for e in ctx.get("recent_handoffs", [])
        ],
    )
    out += _section("ARTIFACTS", list(ctx.get("artifacts") or []))
    github = ctx.get("github") or {}
    if github.get("repo"):
        out += _section("GITHUB", [f"{github['repo']} — {github.get('repo_url')}"])
    if len(out) == 1:
        out.append("\n(nothing recorded for this project yet)")
    return "\n".join(out)


def render_summary(summary: dict[str, Any]) -> str:
    out = [f"=== COORDINATION · {summary.get('project')} (last {summary.get('window_hours')}h) ==="]
    out += _section(
        "ACTIVE AGENTS",
        [
            f"{agent}: {', '.join(tasks) or '-'}"
            for agent, tasks in (summary.get("active_agents") or {}).items()
        ],
    )
    out += _section(
        "BLOCKED",
        [
            f"{b.get('agent')} / {b.get('task')}: {b.get('reason')} (since {b.get('since')})"
            for b in summary.get("blocked", [])
        ],
    )
    out += _section(
        "POSSIBLE CONFLICTS",
        [f"{c.get('task')}: {c.get('reason')}" for c in summary.get("possible_conflicts", [])],
    )
    out += _section(
        "UNRESOLVED QUESTIONS",
        [
            f"{q.get('ref')} from {q.get('from_agent')} to {q.get('to_agent') or 'anyone'}: "
            f"{q.get('question')}"
            for q in summary.get("unresolved_questions", [])
        ],
    )
    out += _section("RECENT FINDINGS", list(summary.get("recent_findings") or []))
    out += _section("RECENT DECISIONS", list(summary.get("recent_decisions") or []))
    out += _section(
        "IDLE CLAIMS",
        [
            f"{c.get('agent')} / {c.get('task')} idle {ago(c.get('last_activity_at'))}"
            for c in summary.get("idle_claims", [])
        ],
    )
    out += _section("SUGGESTED ACTIONS", list(summary.get("suggested_actions") or []))
    if len(out) == 1:
        out.append("\n(nothing to coordinate — no activity for this project)")
    return "\n".join(out)


def render_health(health: dict[str, Any]) -> str:
    integrations = health.get("integrations") or {}
    return (
        f"status   : {health.get('status')}\n"
        f"version  : {health.get('version')}\n"
        f"time     : {health.get('time')}\n"
        f"database : {health.get('database')} ({integrations.get('db_url')})\n"
        f"slack    : {integrations.get('slack')}\n"
        f"github   : {integrations.get('github')}\n"
        f"auth     : {integrations.get('auth')}"
    )
