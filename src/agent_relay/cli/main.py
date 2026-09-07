"""``agent-relay`` command line interface.

Identity comes from the environment so an agent never has to repeat itself::

    export AGENT_RELAY_URL=http://relay.local:8077
    export AGENT_NAME=leo-codex
    export HUMAN_OWNER=leonardo
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Annotated, Any

import typer

from agent_relay.cli import render
from agent_relay.cli.client import RelayClient, RelayError, env

app = typer.Typer(
    name="agent-relay",
    help="Coordinate coding/research agents across humans, machines and projects.",
    no_args_is_help=True,
    add_completion=False,
)
post_app = typer.Typer(
    name="post", help="Post a structured coordination event.", no_args_is_help=True
)
app.add_typer(post_app)

# --- reusable options -------------------------------------------------------

AgentOpt = Annotated[str | None, typer.Option("--agent", "-a", help="Agent name [env: AGENT_NAME]")]
OwnerOpt = Annotated[str | None, typer.Option("--owner", help="Human owner [env: HUMAN_OWNER]")]
ProjectOpt = Annotated[str, typer.Option("--project", "-p", help="Project name")]
TaskOpt = Annotated[str, typer.Option("--task", "-t", help="Task id, e.g. GH-142")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print the raw API response.")]


def _identity(agent: str | None, owner: str | None) -> tuple[str, str | None]:
    resolved = agent or env("AGENT_NAME")
    if not resolved:
        raise typer.BadParameter(
            "No agent name. Pass --agent or set AGENT_NAME in the environment."
        )
    return resolved, owner or env("HUMAN_OWNER")


def _client() -> RelayClient:
    return RelayClient()


def _emit(payload: Any, rendered: str, as_json: bool) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str) if as_json else rendered)


def _fail(exc: RelayError) -> None:
    """Print an error a human (or an agent reading stderr) can act on, then exit 1."""
    typer.echo(str(exc), err=True)
    payload = exc.payload
    if isinstance(payload, dict) and payload.get("current_owner"):
        typer.echo(render.render_conflict(payload), err=True)
    elif payload:
        typer.echo(json.dumps(payload, indent=2, default=str), err=True)
    raise typer.Exit(code=1)


def _parse_details(items: list[str] | None) -> dict[str, Any]:
    """``--detail findings="EPE +0.7%"`` -> ``{"findings": ["EPE +0.7%"]}``.

    Repeating a key appends to a list, which is what "completed: a, b, c" wants.
    """
    details: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"--detail expects key=value, got {item!r}")
        key, value = key.strip(), value.strip()
        existing = details.get(key)
        if existing is None:
            details[key] = [value]
        else:
            existing.append(value)
    return details


def _post_event(body: dict[str, Any], as_json: bool) -> None:
    try:
        event = _client().post("/events", body)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(event, render.render_event(event), as_json)


# --- server -----------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port.")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes.")] = False,
) -> None:
    """Run the relay API server."""
    import uvicorn

    from agent_relay.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "agent_relay.main:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def health(as_json: JsonOpt = False) -> None:
    """Check that the relay is up and see which integrations are enabled."""
    try:
        payload = _client().get("/health")
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_health(payload), as_json)


# --- reading ----------------------------------------------------------------


@app.command()
def context(
    project: ProjectOpt,
    window_hours: Annotated[int | None, typer.Option(help="Lookback window.")] = None,
    limit: Annotated[int | None, typer.Option(help="Items per section.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Current state of a project. Run this before starting substantial work."""
    try:
        payload = _client().get("/context", project=project, window_hours=window_hours, limit=limit)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_context(payload), as_json)


@app.command()
def summary(
    project: ProjectOpt,
    window_hours: Annotated[int | None, typer.Option(help="Lookback window.")] = None,
    idle_hours: Annotated[int, typer.Option(help="Flag claims idle this long.")] = 24,
    as_json: JsonOpt = False,
) -> None:
    """Deterministic coordination summary: conflicts, blockers, open questions."""
    try:
        payload = _client().get(
            "/coordination/summary",
            project=project,
            window_hours=window_hours,
            idle_hours=idle_hours,
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_summary(payload), as_json)


@app.command()
def tasks(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    agent: AgentOpt = None,
    status: Annotated[
        str | None, typer.Option(help="claimed | blocked | unclaimed | released")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """List tasks known to the relay with ownership and blocked state."""
    try:
        payload = _client().get("/tasks", project=project, agent=agent, status=status)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_tasks(payload), as_json)


@app.command()
def events(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    agent: AgentOpt = None,
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    event_type: Annotated[str | None, typer.Option("--type", help="UPDATE, QUESTION, …")] = None,
    since: Annotated[str | None, typer.Option(help="ISO timestamp lower bound.")] = None,
    limit: Annotated[int, typer.Option(help="Max events.")] = 20,
    as_json: JsonOpt = False,
) -> None:
    """Read the event log."""
    try:
        payload = _client().get(
            "/events",
            project=project,
            agent=agent,
            task=task,
            event_type=event_type.upper() if event_type else None,
            since=since,
            limit=limit,
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_events(payload), as_json)


# --- ownership --------------------------------------------------------------


@app.command()
def claim(
    project: ProjectOpt,
    task: TaskOpt,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    branch: Annotated[str | None, typer.Option(help="Branch you will work on.")] = None,
    note: Annotated[str | None, typer.Option(help="What you intend to do.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Claim a task. Exits 1 with the current owner's details if it is already taken."""
    agent_name, human_owner = _identity(agent, owner)
    try:
        payload = _client().post(
            "/claim",
            {
                "agent": agent_name,
                "human_owner": human_owner,
                "project": project,
                "task": task,
                "branch": branch,
                "note": note,
            },
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_claim(payload), as_json)


@app.command()
def release(
    project: ProjectOpt,
    task: TaskOpt,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    summary_text: Annotated[
        str | None, typer.Option("--summary", "-s", help="Closing note.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Release a claim held by another agent.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Release a task claim. Do this when you finish or step away."""
    agent_name, human_owner = _identity(agent, owner)
    try:
        payload = _client().post(
            "/release",
            {
                "agent": agent_name,
                "human_owner": human_owner,
                "project": project,
                "task": task,
                "summary": summary_text,
                "force": force,
            },
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_claim(payload, verb="released"), as_json)


@app.command()
def handoff(
    project: ProjectOpt,
    task: TaskOpt,
    to: Annotated[str, typer.Option("--to", help="Receiving agent.")],
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="What you completed.")],
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    branch: Annotated[str | None, typer.Option(help="Branch to continue on.")] = None,
    continue_from: Annotated[
        str | None, typer.Option("--continue-from", help="Commit sha to continue from.")
    ] = None,
    inputs: Annotated[
        list[str] | None, typer.Option("--input", help="Input path/artifact. Repeatable.")
    ] = None,
    warnings: Annotated[
        list[str] | None, typer.Option("--warning", help="Caveat for the receiver. Repeatable.")
    ] = None,
    artifacts: Annotated[
        list[str] | None, typer.Option("--artifact", help="Produced artifact. Repeatable.")
    ] = None,
    keep_claim: Annotated[
        bool, typer.Option("--keep-claim", help="Do not transfer the task claim.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Hand a task to another agent. Transfers the claim unless --keep-claim."""
    agent_name, human_owner = _identity(agent, owner)
    try:
        payload = _client().post(
            "/handoff",
            {
                "agent": agent_name,
                "human_owner": human_owner,
                "target_agent": to,
                "project": project,
                "task": task,
                "summary": summary_text,
                "branch": branch,
                "continue_from": continue_from,
                "inputs": list(inputs or []),
                "warnings": list(warnings or []),
                "artifacts": list(artifacts or []),
                "transfer_claim": not keep_claim,
            },
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_event(payload), as_json)


# --- posting events ---------------------------------------------------------


def _event_body(
    event_type: str,
    *,
    agent: str,
    human_owner: str | None,
    project: str,
    summary_text: str,
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
        "summary": summary_text,
        "details": details or {},
        "artifacts": artifacts or [],
        "metadata": {},
    }


@post_app.command("update")
def post_update(
    project: ProjectOpt,
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="One-line milestone.")],
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    branch: Annotated[str | None, typer.Option(help="Branch.")] = None,
    detail: Annotated[
        list[str] | None,
        typer.Option("--detail", "-d", help="key=value, repeatable (e.g. findings='EPE +0.7%')"),
    ] = None,
    artifact: Annotated[
        list[str] | None, typer.Option("--artifact", help="Artifact path/URL. Repeatable.")
    ] = None,
    next_step: Annotated[
        list[str] | None, typer.Option("--next", help="What you will do next. Repeatable.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Report a milestone or finding."""
    agent_name, human_owner = _identity(agent, owner)
    details = _parse_details(detail)
    if next_step:
        details["next"] = list(next_step)
    _post_event(
        _event_body(
            "UPDATE",
            agent=agent_name,
            human_owner=human_owner,
            project=project,
            task=task,
            branch=branch,
            summary_text=summary_text,
            details=details,
            artifacts=list(artifact or []),
        ),
        as_json,
    )


@post_app.command("question")
def post_question(
    project: ProjectOpt,
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="The question.")],
    to: Annotated[str | None, typer.Option("--to", help="Agent most likely to know.")] = None,
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    detail: Annotated[list[str] | None, typer.Option("--detail", "-d")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Ask another agent something they probably already know."""
    agent_name, human_owner = _identity(agent, owner)
    _post_event(
        _event_body(
            "QUESTION",
            agent=agent_name,
            human_owner=human_owner,
            project=project,
            task=task,
            target_agent=to,
            summary_text=summary_text,
            details=_parse_details(detail),
        ),
        as_json,
    )


@post_app.command("answer")
def post_answer(
    project: ProjectOpt,
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="The answer.")],
    in_reply_to: Annotated[
        str | None, typer.Option("--in-reply-to", help="Question ref, e.g. Q-19.")
    ] = None,
    to: Annotated[str | None, typer.Option("--to", help="Agent who asked.")] = None,
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    detail: Annotated[
        list[str] | None,
        typer.Option("--detail", "-d", help="key=value, repeatable. Structure a long answer."),
    ] = None,
    artifact: Annotated[list[str] | None, typer.Option("--artifact")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Answer an open question. Always pass --in-reply-to so it closes cleanly.

    A substantive answer deserves structure as much as an update does: use `--detail`
    to separate the answer from its caveats, so a reader can see where the confidence
    ends.
    """
    agent_name, human_owner = _identity(agent, owner)
    _post_event(
        _event_body(
            "ANSWER",
            agent=agent_name,
            human_owner=human_owner,
            project=project,
            task=task,
            target_agent=to,
            in_reply_to=in_reply_to,
            summary_text=summary_text,
            details=_parse_details(detail),
            artifacts=list(artifact or []),
        ),
        as_json,
    )


@post_app.command("blocked")
def post_blocked(
    project: ProjectOpt,
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="What is blocking you.")],
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    needs: Annotated[
        list[str] | None, typer.Option("--needs", help="What would unblock you. Repeatable.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Report that you cannot continue."""
    agent_name, human_owner = _identity(agent, owner)
    details: dict[str, Any] = {"needs": list(needs)} if needs else {}
    _post_event(
        _event_body(
            "BLOCKED",
            agent=agent_name,
            human_owner=human_owner,
            project=project,
            task=task,
            summary_text=summary_text,
            details=details,
        ),
        as_json,
    )


@post_app.command("decision")
def post_decision(
    project: ProjectOpt,
    summary_text: Annotated[str, typer.Option("--summary", "-s", help="The decision.")],
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    detail: Annotated[
        list[str] | None, typer.Option("--detail", "-d", help="e.g. rationale='...'")
    ] = None,
    artifact: Annotated[list[str] | None, typer.Option("--artifact")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Record a durable technical or project decision. Use sparingly."""
    agent_name, human_owner = _identity(agent, owner)
    _post_event(
        _event_body(
            "DECISION",
            agent=agent_name,
            human_owner=human_owner,
            project=project,
            task=task,
            summary_text=summary_text,
            details=_parse_details(detail),
            artifacts=list(artifact or []),
        ),
        as_json,
    )


# --- V2: presence, hygiene, coordination -------------------------------------


@app.command()
def heartbeat(
    agent: AgentOpt = None,
    owner: OwnerOpt = None,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    task: Annotated[str | None, typer.Option("--task", "-t")] = None,
    note: Annotated[str | None, typer.Option("--note", help="What you are doing.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Report that you are alive.

    Distinct from posting an event: an agent can be working quietly for an hour and
    still needs to say so, otherwise its claim looks abandoned.
    """
    agent_name, human_owner = _identity(agent, owner)
    try:
        payload = _client().post(
            "/heartbeat",
            {
                "agent": agent_name,
                "human_owner": human_owner,
                "project": project,
                "task": task,
                "status_note": note,
                "host": socket.gethostname(),
                "pid": os.getpid(),
            },
        )
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_agents([payload]), as_json)


@app.command()
def agents(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    status: Annotated[str | None, typer.Option(help="online | idle | offline | unknown")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Who is alive right now, and what are they holding."""
    try:
        payload = _client().get("/agents", project=project, status=status)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_agents(payload), as_json)


@app.command()
def stale(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Claims whose owner has gone quiet — the usual cause of a blocked teammate."""
    try:
        payload = _client().get("/claims/stale", project=project)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_stale(payload), as_json)


@app.command()
def sweep(as_json: JsonOpt = False) -> None:
    """Run the stale-claim sweep now (auto-releases only if that is switched on)."""
    try:
        payload = _client().post("/claims/sweep", {})
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_sweep(payload), as_json)


@app.command()
def brief(
    project: ProjectOpt,
    window_hours: Annotated[int | None, typer.Option(help="Lookback window.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """A short readable briefing. Uses the LLM coordinator when configured."""
    try:
        payload = _client().get("/coordination/brief", project=project, window_hours=window_hours)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_brief(payload), as_json)


@app.command()
def tui(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    interval: Annotated[float, typer.Option(help="Seconds between refreshes.")] = 10.0,
    once: Annotated[bool, typer.Option("--once", help="Render one frame and exit.")] = False,
) -> None:
    """Live read-only dashboard: blockers, questions, who is working, recent activity.

    Leave it open in a split pane. It only ever reads, so it is safe unattended.
    """
    from agent_relay.cli import tui as dashboard

    try:
        dashboard.run_tui(_client(), project=project, interval=interval, once=once)
    except RelayError as exc:
        _fail(exc)


@app.command()
def inbox(
    agent: AgentOpt = None,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    as_json: JsonOpt = False,
) -> None:
    """What is waiting for you: questions you were asked, handoffs, your stalled tasks.

    Narrower than `context` on purpose. Run this when you want to know whether anything
    needs you, rather than what is going on generally.
    """
    agent_name, _owner = _identity(agent, None)
    try:
        payload = _client().get("/inbox", agent=agent_name, project=project)
    except RelayError as exc:
        _fail(exc)
        return
    _emit(payload, render.render_inbox(payload), as_json)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
