"""MCP server that exposes the relay as native agent tools over stdio.

Claude Code and Codex speak MCP, so an agent configured with this server simply
*has* the coordination tools instead of having to remember CLI invocations.
Coordination only happens when it is free, and this is the cheapest it gets.

There are two layers on purpose:

* ``tool_*`` functions take a :class:`RelayClient` and return a :class:`ToolResult`
  (text for the model, raw payload for everything else). They import nothing from
  the MCP SDK, so the whole surface is unit-testable without it.
* :func:`build_server` wires those into the SDK. ``mcp`` is imported lazily, so
  importing ``agent_relay`` — or running the test suite — never needs the optional
  dependency.

Like the CLI, this talks to the relay over HTTP instead of touching SQLite: one
write path keeps the event log consistent, and agents on other machines behave the
same as agents on this one.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from agent_relay.cli import render
from agent_relay.cli.client import RelayClient, RelayError, env

SERVER_NAME = "agent-relay"

INSTRUCTIONS = """\
Agent Relay coordinates several coding/research agents working on the same
projects. Read `get_context` before starting substantial work on a project, claim
a task with `claim_task` before you modify it, and post an update at every
milestone so the other agents are not guessing. Tools return plain text meant to
be read, not JSON to be parsed.
"""

INSTALL_HINT = (
    "The MCP Python SDK is not installed. Install it with: uv sync --extra mcp\n"
    "(or `uv pip install 'mcp>=1.9'`), then re-launch agent-relay-mcp."
)

#: How many detail lines of a single event we are willing to spend tokens on.
MAX_DETAIL_VALUES = 6


class IdentityError(RuntimeError):
    """AGENT_NAME is missing. Actionable on its own, so it carries its own message."""


class MissingSdkError(RuntimeError):
    """The optional ``mcp`` dependency is absent."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool produced: ``text`` for the model, ``data`` for callers that care.

    ``ok`` is False for handled failures. The text is still readable in that case —
    a tool result must never be a traceback.
    """

    text: str
    data: Any = None
    ok: bool = True


# --------------------------------------------------------------------------- identity


def resolve_identity() -> tuple[str, str | None]:
    """Agent name (required) and human owner (optional), from the environment.

    Resolved exactly like the CLI does, so an agent that already exports these for
    ``agent-relay`` needs no extra configuration here.
    """
    agent = env("AGENT_NAME")
    if not agent:
        raise IdentityError(
            "AGENT_NAME is not set, so this write cannot be attributed to anyone.\n"
            "Set it in the environment that launches this MCP server, for example\n"
            '  "env": {"AGENT_NAME": "leo-codex", "HUMAN_OWNER": "leonardo"}\n'
            "in your MCP server config, or `export AGENT_NAME=...` in the shell.\n"
            "Do not invent a name: the relay uses it to decide who owns which task."
        )
    return agent, env("HUMAN_OWNER")


def make_client() -> RelayClient:
    """A client per call: AGENT_RELAY_URL / AGENT_RELAY_API_TOKEN stay live."""
    return RelayClient()


# --------------------------------------------------------------------------- errors


def _detail_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return f"  detail: {payload}"
    return "  detail: " + json.dumps(payload, default=str)


def _error_result(exc: Exception) -> ToolResult:
    """Turn an exception into text the model can act on."""
    if isinstance(exc, IdentityError):
        return ToolResult(str(exc), None, ok=False)

    if isinstance(exc, RelayError):
        payload = exc.payload
        # A claim conflict is the one error where the *next action* matters more
        # than the error itself, so spell it out rather than dumping the 409 body.
        if isinstance(payload, dict) and payload.get("current_owner"):
            return ToolResult(
                render.render_conflict(payload)
                + f"\n   -> Do NOT work on {payload.get('task')}. Pick another task, or ask "
                f"{payload.get('current_owner')} with post_question.",
                payload,
                ok=False,
            )
        lines = [f"Relay error: {exc}"]
        if detail := _detail_text(payload):
            lines.append(detail)
        return ToolResult("\n".join(lines), payload, ok=False)

    return ToolResult(
        f"Unexpected {type(exc).__name__} while talking to the relay: {exc}\n"
        "The event was probably not recorded. Retry once; if it fails again, tell "
        "your human owner rather than continuing silently.",
        None,
        ok=False,
    )


def _readable[**P](fn: Callable[P, ToolResult]) -> Callable[P, ToolResult]:
    """Guarantee every tool returns readable text instead of raising at the model."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        try:
            return fn(*args, **kwargs)
        except (RelayError, IdentityError) as exc:
            return _error_result(exc)
        except Exception as exc:  # noqa: BLE001 - a tool result is text, never a traceback
            return _error_result(exc)

    return wrapper


# --------------------------------------------------------------------------- rendering
#
# ``agent_relay.cli.render`` is already written to be read rather than parsed, so
# context/summary/claims/conflicts reuse it verbatim. The list renderers below are
# purpose-written: the CLI pads them into fixed-width tables, and padding is pure
# token cost to a model that does not care about column alignment.


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, dict):
        return [f"{k}={v}" for k, v in value.items()]
    return [str(value)]


#: details keys that hold a result worth repeating to another agent. Kept in step
#: with ``services.state.FINDING_KEYS``, but duplicated rather than imported: this
#: module is an HTTP client and must not drag the server-side services in.
FINDING_KEYS = ("findings", "finding", "results", "result", "conclusion")


def finding_lines(context: dict[str, Any]) -> list[str]:
    """Pull findings out of the recent updates in a context payload.

    ``render_context`` shows only each update's one-line summary, and the actual
    number or conclusion lives in ``details``. Making a model call ``list_events``
    to recover it is exactly the friction this server exists to remove.
    """
    out: list[str] = []
    for event in context.get("recent_updates") or []:
        details = event.get("details") or {}
        for key in FINDING_KEYS:
            for value in _values(details.get(key)):
                line = f"{event.get('agent')} [{event.get('task') or '-'}]: {value}"
                if line not in out:
                    out.append(line)
    return out


def render_task_lines(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "(no tasks match)"
    out: list[str] = []
    for task in tasks:
        bits = [f"{task.get('task')} [{task.get('status')}]"]
        if owner := task.get("owner"):
            human = task.get("human_owner")
            bits.append(f"owner={owner}" + (f" ({human})" if human else ""))
        if branch := task.get("branch"):
            bits.append(f"branch={branch}")
        bits.append(f"project={task.get('project')}")
        bits.append(f"last activity {render.ago(task.get('last_activity_at'))} ago")
        out.append(" | ".join(bits))
        if task.get("blocked") and (reason := task.get("blocked_reason")):
            out.append(f"    BLOCKED ({task.get('blocked_by') or '?'}): {reason}")
    return "\n".join(out)


def render_event_lines(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(no events match)"
    out: list[str] = []
    for event in events:
        target = f" -> {event['target_agent']}" if event.get("target_agent") else ""
        reply = f" (re {event['in_reply_to']})" if event.get("in_reply_to") else ""
        out.append(
            f"{event.get('ref')} {event.get('event_type')} "
            f"{render.ago(event.get('created_at'))} ago | "
            f"{event.get('project')}/{event.get('task') or '-'} | "
            f"{event.get('agent')}{target}{reply}: {event.get('summary')}"
        )
        # Details carry the actual payload (findings, next steps, needs). Skipping
        # them would make the log look busy while saying nothing.
        for key, value in (event.get("details") or {}).items():
            if rows := _values(value)[:MAX_DETAIL_VALUES]:
                out.append(f"    {key}: " + "; ".join(rows))
        if artifacts := event.get("artifacts"):
            out.append("    artifacts: " + ", ".join(str(a) for a in artifacts))
    return "\n".join(out)


# --------------------------------------------------------------------------- core tools
#
# Layer 1: no MCP import anywhere below. Every function takes an explicit client so
# tests can point it at an in-process app.


def _event_body(
    event_type: str,
    *,
    agent: str,
    human_owner: str | None,
    project: str,
    summary: str,
    task: str | None = None,
    branch: str | None = None,
    target_agent: str | None = None,
    in_reply_to: str | None = None,
    details: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "agent": agent,
        "human_owner": human_owner,
        "project": project,
        "task": task,
        "branch": branch,
        "target_agent": target_agent,
        "in_reply_to": in_reply_to,
        "summary": summary,
        "details": details or {},
        "artifacts": artifacts or [],
        "metadata": {},
    }


@_readable
def tool_get_context(
    client: RelayClient, project: str, window_hours: int | None = None
) -> ToolResult:
    payload = client.get("/context", project=project, window_hours=window_hours)
    text = render.render_context(payload)
    if findings := finding_lines(payload):
        text += "\n\nRECENT FINDINGS\n" + "\n".join(f"  {line}" for line in findings)
    return ToolResult(text, payload)


def inbox_text(payload: dict[str, Any]) -> str:
    """Render an inbox for a model to read.

    Deliberately imperative and short: each item says what it is and what closes it,
    because an agent that has to infer the next action from a data dump usually
    infers nothing.
    """
    from agent_relay.services.inbox import Inbox, as_text

    return as_text(Inbox.model_validate(payload))


@_readable
def tool_my_inbox(client: RelayClient, project: str | None = None) -> ToolResult:
    agent, _owner = resolve_identity()
    payload = client.get("/inbox", agent=agent, project=project)
    return ToolResult(inbox_text(payload), payload)


@_readable
def tool_coordination_summary(client: RelayClient, project: str) -> ToolResult:
    payload = client.get("/coordination/summary", project=project)
    return ToolResult(render.render_summary(payload), payload)


@_readable
def tool_list_tasks(
    client: RelayClient, project: str | None = None, status: str | None = None
) -> ToolResult:
    payload = client.get("/tasks", project=project, status=status)
    return ToolResult(render_task_lines(payload), payload)


@_readable
def tool_list_events(
    client: RelayClient,
    project: str | None = None,
    task: str | None = None,
    event_type: str | None = None,
    limit: int = 20,
) -> ToolResult:
    payload = client.get(
        "/events",
        project=project,
        task=task,
        event_type=event_type.upper() if event_type else None,
        limit=limit,
    )
    return ToolResult(render_event_lines(payload), payload)


@_readable
def tool_claim_task(
    client: RelayClient,
    project: str,
    task: str,
    branch: str | None = None,
    note: str | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/claim",
        {
            "agent": agent,
            "human_owner": human_owner,
            "project": project,
            "task": task,
            "branch": branch,
            "note": note,
        },
    )
    return ToolResult(render.render_claim(payload), payload)


@_readable
def tool_release_task(
    client: RelayClient, project: str, task: str, summary: str | None = None
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/release",
        {
            "agent": agent,
            "human_owner": human_owner,
            "project": project,
            "task": task,
            "summary": summary,
            "force": False,
        },
    )
    # "released", not "released by": render_claim already supplies the "by".
    return ToolResult(render.render_claim(payload, verb="released"), payload)


@_readable
def tool_handoff_task(
    client: RelayClient,
    project: str,
    task: str,
    to_agent: str,
    summary: str,
    continue_from: str | None = None,
    inputs: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/handoff",
        {
            "agent": agent,
            "human_owner": human_owner,
            "target_agent": to_agent,
            "project": project,
            "task": task,
            "summary": summary,
            "continue_from": continue_from,
            "inputs": list(inputs or []),
            "warnings": list(warnings or []),
            "artifacts": [],
            "transfer_claim": True,
        },
    )
    return ToolResult(render.render_event(payload), payload)


@_readable
def tool_post_update(
    client: RelayClient,
    project: str,
    summary: str,
    task: str | None = None,
    branch: str | None = None,
    findings: list[str] | None = None,
    next_steps: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    details: dict[str, Any] = {}
    # "findings" and "next" are the keys the coordination summary reads; using
    # anything else would post the information into a hole.
    if findings:
        details["findings"] = list(findings)
    if next_steps:
        details["next"] = list(next_steps)
    payload = client.post(
        "/events",
        _event_body(
            "UPDATE",
            agent=agent,
            human_owner=human_owner,
            project=project,
            task=task,
            branch=branch,
            summary=summary,
            details=details,
            artifacts=list(artifacts or []),
        ),
    )
    return ToolResult(render.render_event(payload), payload)


@_readable
def tool_post_question(
    client: RelayClient,
    project: str,
    summary: str,
    to_agent: str | None = None,
    task: str | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/events",
        _event_body(
            "QUESTION",
            agent=agent,
            human_owner=human_owner,
            project=project,
            task=task,
            target_agent=to_agent,
            summary=summary,
        ),
    )
    return ToolResult(render.render_event(payload), payload)


@_readable
def tool_post_answer(
    client: RelayClient,
    project: str,
    summary: str,
    in_reply_to: str | None = None,
    task: str | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/events",
        _event_body(
            "ANSWER",
            agent=agent,
            human_owner=human_owner,
            project=project,
            task=task,
            in_reply_to=in_reply_to,
            summary=summary,
        ),
    )
    return ToolResult(render.render_event(payload), payload)


@_readable
def tool_post_blocked(
    client: RelayClient,
    project: str,
    summary: str,
    task: str | None = None,
    needs: list[str] | None = None,
) -> ToolResult:
    agent, human_owner = resolve_identity()
    details: dict[str, Any] = {"needs": list(needs)} if needs else {}
    payload = client.post(
        "/events",
        _event_body(
            "BLOCKED",
            agent=agent,
            human_owner=human_owner,
            project=project,
            task=task,
            summary=summary,
            details=details,
        ),
    )
    return ToolResult(render.render_event(payload), payload)


@_readable
def tool_post_decision(
    client: RelayClient, project: str, summary: str, task: str | None = None
) -> ToolResult:
    agent, human_owner = resolve_identity()
    payload = client.post(
        "/events",
        _event_body(
            "DECISION",
            agent=agent,
            human_owner=human_owner,
            project=project,
            task=task,
            summary=summary,
        ),
    )
    return ToolResult(render.render_event(payload), payload)


# --------------------------------------------------------------------------- MCP tools
#
# Layer 2: the functions the SDK actually introspects. Their signatures become the
# JSON schema and their docstrings become the tool descriptions the model reads,
# so both are written for an agent deciding *whether to call*, not for a reviewer.


def get_context(project: str, window_hours: int | None = None) -> str:
    """Read the current state of a project before starting substantial work on it.

    Call this first, every session. Returns active task claims (who is working on
    what right now), blocked tasks, unresolved questions, recent findings,
    decisions and handoffs. Use it to avoid redoing work another agent has already
    done and to spot the task you should not touch.
    """
    return tool_get_context(make_client(), project, window_hours).text


def my_inbox(project: str | None = None) -> str:
    """Check whether anything is waiting specifically for YOU.

    Narrower than get_context, and the one to call when you want to know if you owe
    anyone something: questions another agent addressed to you and nobody has
    answered, work handed to you (with the warnings you must read before touching
    it), and tasks you hold that have stopped moving. An empty result is a real
    answer — it means you are clear.

    Call it at the start of a session alongside get_context, and again before you
    finish, so you do not leave a teammate blocked on a question you never saw.
    """
    return tool_my_inbox(make_client(), project).text


def claim_task(project: str, task: str, branch: str | None = None, note: str | None = None) -> str:
    """Take ownership of a task before you start modifying anything for it.

    Call this BEFORE editing code, running experiments or opening a PR for a task.
    If another agent already holds it you get an error naming the current owner —
    in that case do NOT work on that task: pick another one, or ask the owner with
    post_question. Pass `branch` when you know where you will work.
    """
    return tool_claim_task(make_client(), project, task, branch, note).text


def release_task(project: str, task: str, summary: str | None = None) -> str:
    """Give up ownership of a task when you finish it or step away from it.

    Call this as soon as you stop working, even mid-way: a stale claim blocks other
    agents. Put what you actually completed in `summary`.
    """
    return tool_release_task(make_client(), project, task, summary).text


def handoff_task(
    project: str,
    task: str,
    to_agent: str,
    summary: str,
    continue_from: str | None = None,
    inputs: list[str] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """Pass a task to another agent, transferring the claim to them.

    Call this instead of release_task when a specific agent should continue your
    work. `summary` is what you completed, `continue_from` the commit/tag they
    should start from, `inputs` the files or artifacts they need, and `warnings`
    the things that will break if they are not careful. Be concrete: this text is
    the only context the receiving agent gets.
    """
    return tool_handoff_task(
        make_client(), project, task, to_agent, summary, continue_from, inputs, warnings
    ).text


def post_update(
    project: str,
    summary: str,
    task: str | None = None,
    branch: str | None = None,
    findings: list[str] | None = None,
    next_steps: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> str:
    """Report a milestone, result or discovery other agents need to know about.

    Call this at every milestone and whenever you learn something that changes what
    someone else should do (a benchmark number, a broken assumption, a file you
    rewrote). Put measurable results in `findings` and what you are about to do in
    `next_steps` — those two feed the coordination summary other agents read.
    """
    return tool_post_update(
        make_client(), project, summary, task, branch, findings, next_steps, artifacts
    ).text


def post_question(
    project: str, summary: str, to_agent: str | None = None, task: str | None = None
) -> str:
    """Ask another agent something they already know, instead of investigating it.

    Call this when the answer lives in work someone else did. Name `to_agent` when
    get_context shows who owns the relevant task; leave it empty to ask anyone. The
    question stays visible as unresolved until someone posts an answer, so ask
    precisely.
    """
    return tool_post_question(make_client(), project, summary, to_agent, task).text


def post_answer(
    project: str, summary: str, in_reply_to: str | None = None, task: str | None = None
) -> str:
    """Answer an open question raised by another agent.

    Call this when get_context lists an unresolved question you can answer. Always
    pass `in_reply_to` with the question's ref (e.g. "Q-19") so it stops being shown
    as unresolved to everyone else.
    """
    return tool_post_answer(make_client(), project, summary, in_reply_to, task).text


def post_blocked(
    project: str, summary: str, task: str | None = None, needs: list[str] | None = None
) -> str:
    """Report that you cannot continue, and say exactly what would unblock you.

    Call this instead of silently stopping or guessing around a missing dependency.
    `needs` should list concrete unblockers (a file, a credential, a decision); they
    become suggested actions for the other agents and their humans.
    """
    return tool_post_blocked(make_client(), project, summary, task, needs).text


def post_decision(project: str, summary: str, task: str | None = None) -> str:
    """Record a durable technical or project decision, with its rationale in the text.

    Use sparingly: this is for choices later work must respect ("we standardise on
    UTC timestamps in the event log"), not for progress. Decisions are surfaced to
    every agent that reads the project context afterwards.
    """
    return tool_post_decision(make_client(), project, summary, task).text


def list_tasks(project: str | None = None, status: str | None = None) -> str:
    """List tasks the relay knows about, with owner and blocked state.

    Use it to find something safe to pick up. `status` filters to one of
    "claimed", "blocked", "unclaimed" or "released"; omit `project` to look across
    every project.
    """
    return tool_list_tasks(make_client(), project, status).text


def list_events(
    project: str | None = None,
    task: str | None = None,
    event_type: str | None = None,
    limit: int = 20,
) -> str:
    """Read the raw coordination log, newest first, when get_context is not enough.

    Use it to reconstruct how a task got where it is. `event_type` is one of
    UPDATE, QUESTION, ANSWER, CLAIM, HANDOFF, BLOCKED, DECISION, RELEASE.
    """
    return tool_list_events(make_client(), project, task, event_type, limit).text


def coordination_summary(project: str) -> str:
    """Get a rule-based read on what is going wrong in a project right now.

    Call this when you are deciding what to do next, or before asking a human to
    intervene. Returns possible duplicated effort, blockers, idle claims, open
    questions and concrete suggested actions.
    """
    return tool_coordination_summary(make_client(), project).text


#: Registration order is the order the model sees. Read-then-write, because that is
#: the order an agent should work in.
TOOLS: tuple[Callable[..., str], ...] = (
    get_context,
    my_inbox,
    claim_task,
    release_task,
    handoff_task,
    post_update,
    post_question,
    post_answer,
    post_blocked,
    post_decision,
    list_tasks,
    list_events,
    coordination_summary,
)

TOOL_NAMES: tuple[str, ...] = tuple(fn.__name__ for fn in TOOLS)


# --------------------------------------------------------------------------- resources


def context_resource(project: str) -> str:
    """Current coordination state of one project."""
    return get_context(project)


def tasks_resource() -> str:
    """Every task the relay knows about, across all projects."""
    return list_tasks()


#: uri -> function. Resources are the read-only half of the surface; a client that
#: prefers attaching context over calling tools gets the same text either way.
RESOURCES: tuple[tuple[str, Callable[..., str]], ...] = (
    ("relay://context/{project}", context_resource),
    ("relay://tasks", tasks_resource),
)


# --------------------------------------------------------------------------- wiring


def _load_fastmcp() -> Any:
    """Import the SDK server class on demand, across both major SDK versions.

    The class was renamed in mcp 2.x (``FastMCP`` -> ``MCPServer``) but the surface we
    use — the constructor kwargs, ``add_tool``, ``resource`` and ``run`` — is
    unchanged, so supporting both is a two-line import rather than two code paths.
    We try 2.x first because that is what a fresh install resolves to.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ModuleNotFoundError:
        pass
    try:
        # mcp 1.x only; on 2.x this module exists but no longer exports FastMCP.
        from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]
    except (
        ModuleNotFoundError,
        ImportError,
    ) as exc:  # pragma: no cover - depends on the environment
        raise MissingSdkError(INSTALL_HINT) from exc
    return FastMCP


def build_server() -> Any:
    """Build the FastMCP server. Raises :class:`MissingSdkError` if ``mcp`` is absent."""
    fastmcp = _load_fastmcp()
    server = fastmcp(name=SERVER_NAME, instructions=INSTRUCTIONS)
    for fn in TOOLS:
        server.add_tool(fn)
    # Called rather than used as `@server.resource(...)`: the SDK's decorators are
    # untyped, and calling them keeps mypy strict happy without a pile of ignores.
    for uri, resource_fn in RESOURCES:
        server.resource(uri)(resource_fn)
    return server


def main() -> None:
    """Run the relay MCP server on stdio — the transport Claude Code and Codex use."""
    try:
        server = build_server()
    except MissingSdkError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
