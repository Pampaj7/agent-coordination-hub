"""HTTP surface. Routers stay thin: validate, call a service, shape the response."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import text

from agent_relay import __version__
from agent_relay.api.deps import (
    AuthDep,
    GitHubDep,
    SessionDep,
    SettingsDep,
    SlackDep,
    caller_owner,
)
from agent_relay.config import redact_db_url
from agent_relay.db.models import utcnow
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import (
    ClaimConflict as ClaimConflictOut,
)
from agent_relay.models.schemas import (
    ClaimOut,
    ClaimRequest,
    CoordinationSummary,
    EventCreate,
    EventOut,
    HandoffRequest,
    HealthOut,
    ProjectContext,
    ReleaseRequest,
    TaskOut,
)
from agent_relay.services import claims as claim_service
from agent_relay.services import context as context_service
from agent_relay.services import coordination as coordination_service
from agent_relay.services import events as event_service
from agent_relay.services import slack_bot
from agent_relay.services import state as state_service
from agent_relay.services.claims import ClaimConflict, ClaimNotFound
from agent_relay.services.github import GitHubService
from agent_relay.services.slack import SlackNotifier

router = APIRouter(dependencies=[AuthDep])
public_router = APIRouter()  # /health stays reachable without a token


# --------------------------------------------------------------------------- health


@public_router.get("/health", response_model=HealthOut, tags=["meta"])
def health(session: SessionDep, settings: SettingsDep) -> HealthOut:
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        database = f"error: {type(exc).__name__}"

    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        time=utcnow(),
        database=database,
        integrations={
            "slack": "enabled" if settings.slack_enabled else "disabled",
            "slack_bot": "enabled" if settings.slack_bot_enabled else "disabled",
            "slack_events": "enabled" if settings.slack_events_enabled else "disabled",
            "github": "enabled" if settings.github_enabled else "disabled",
            "github_webhooks": "enabled" if settings.github_webhooks_enabled else "disabled",
            "github_polling": "enabled" if settings.github_polling_enabled else "disabled",
            "coordinator_llm": "enabled" if settings.coordinator_enabled else "disabled",
            "auto_release": "enabled" if settings.auto_release_enabled else "disabled",
            "dashboard": "enabled" if settings.dashboard_enabled else "disabled",
            "tailscale_auth": "enabled" if settings.tailscale_auth_enabled else "disabled",
            "auth": (
                "tailnet-identity"
                if settings.tailscale_auth_enabled
                else ("required" if settings.api_token else "open")
            ),
            "db_url": redact_db_url(settings.db_url),
        },
    )


# --------------------------------------------------------------------------- helpers


def _attributed(payload: Any, request: Request, settings: Any) -> Any:
    """Stamp the caller's real identity onto a write, when the tailnet knows it.

    Identity *overrides* a self-declared `human_owner` rather than merely filling a
    blank one. That is the entire point of turning it on: with a shared token any
    agent can post as anyone, and a field that can be set to a convenient value is not
    an attribution. Agent names stay self-declared — one person legitimately runs
    several — but the human behind them stops being a guess.
    """
    owner = caller_owner(request, settings)
    if owner is None:
        return payload
    return payload.model_copy(update={"human_owner": owner})


def _notify_slack(
    background: BackgroundTasks,
    slack: SlackNotifier,
    github: GitHubService,
    payload: dict[str, Any],
) -> None:
    """Queue a Slack post to run *after* the response is sent.

    Storage has already been committed by this point, so a Slack outage costs a log
    line and nothing else. Dispatch goes through ``slack_bot.announce``, which prefers
    the bot when a token is configured — that is the path that returns a message ts and
    records it, without which a human's threaded reply has no event to attach to.
    """
    if not slack.should_post(str(payload.get("event_type", ""))):
        return
    links = github.links_for(task=payload.get("task"), branch=payload.get("branch"))
    background.add_task(slack_bot.announce, payload, links, slack.settings)


def _conflict(exc: ClaimConflict, session: SessionDep) -> HTTPException:
    claim = exc.claim
    last = claim_service.last_update_for(session, claim.project, claim.task)
    body = ClaimConflictOut(
        detail=exc.detail,
        project=claim.project,
        task=claim.task,
        current_owner=claim.agent,
        human_owner=claim.human_owner,
        claimed_at=claim.claimed_at,
        branch=claim.branch,
        last_activity_at=claim.last_activity_at,
        last_update=event_service.to_out(last) if last else None,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=body.model_dump(mode="json"),
    )


# --------------------------------------------------------------------------- events


@router.post("/events", response_model=EventOut, status_code=201, tags=["events"])
def post_event(
    payload: EventCreate,
    session: SessionDep,
    github: GitHubDep,
    slack: SlackDep,
    background: BackgroundTasks,
    request: Request,
    settings: SettingsDep,
) -> EventOut:
    """Record a structured coordination event."""
    payload = _attributed(payload, request, settings)
    event = event_service.create_event(session, payload, commit=False)
    claim_service.touch_claim(session, payload.project, payload.task, payload.agent)
    session.commit()
    session.refresh(event)

    _notify_slack(background, slack, github, event_service.to_slack_payload(event))
    return event_service.to_out(event, github)


@router.get("/events", response_model=list[EventOut], tags=["events"])
def get_events(
    session: SessionDep,
    github: GitHubDep,
    project: str | None = None,
    agent: str | None = None,
    human_owner: str | None = None,
    task: str | None = None,
    event_type: EventType | None = None,
    target_agent: str | None = None,
    since: dt.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[EventOut]:
    """Filtered slice of the event log, newest first."""
    rows = event_service.list_events(
        session,
        project=project,
        agent=agent,
        human_owner=human_owner,
        task=task,
        event_type=event_type,
        target_agent=target_agent,
        since=since,
        limit=limit,
    )
    return [event_service.to_out(row, github) for row in rows]


@router.get("/events/{event_id}", response_model=EventOut, tags=["events"])
def get_event(event_id: int, session: SessionDep, github: GitHubDep) -> EventOut:
    event = event_service.get_event(session, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"No event with id {event_id}")
    return event_service.to_out(event, github)


# --------------------------------------------------------------------------- claims


@router.post("/claim", response_model=ClaimOut, tags=["claims"])
def post_claim(
    payload: ClaimRequest,
    session: SessionDep,
    github: GitHubDep,
    slack: SlackDep,
    background: BackgroundTasks,
    request: Request,
    settings: SettingsDep,
) -> ClaimOut:
    """Claim a task. 409 with the current owner's details if someone already holds it."""
    payload = _attributed(payload, request, settings)
    try:
        claim, created = claim_service.claim_task(session, payload)
    except ClaimConflict as exc:
        raise _conflict(exc, session) from exc

    if created:
        latest = claim_service.last_update_for(session, payload.project, payload.task)
        if latest is not None:
            _notify_slack(background, slack, github, event_service.to_slack_payload(latest))
    return ClaimOut.model_validate(claim)


@router.post("/release", response_model=ClaimOut, tags=["claims"])
def post_release(
    payload: ReleaseRequest,
    session: SessionDep,
    github: GitHubDep,
    slack: SlackDep,
    background: BackgroundTasks,
    request: Request,
    settings: SettingsDep,
) -> ClaimOut:
    """Release a task claim."""
    payload = _attributed(payload, request, settings)
    try:
        claim = claim_service.release_task(session, payload)
    except ClaimNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClaimConflict as exc:
        raise _conflict(exc, session) from exc

    latest = claim_service.last_update_for(session, payload.project, payload.task)
    if latest is not None:
        _notify_slack(background, slack, github, event_service.to_slack_payload(latest))
    return ClaimOut.model_validate(claim)


@router.post("/handoff", response_model=EventOut, tags=["claims"])
def post_handoff(
    payload: HandoffRequest,
    session: SessionDep,
    github: GitHubDep,
    slack: SlackDep,
    background: BackgroundTasks,
    request: Request,
    settings: SettingsDep,
) -> EventOut:
    """Hand a task to another agent, moving the claim with it by default."""
    payload = _attributed(payload, request, settings)
    try:
        event, _claim = claim_service.handoff_task(session, payload)
    except ClaimConflict as exc:
        raise _conflict(exc, session) from exc

    _notify_slack(background, slack, github, event_service.to_slack_payload(event))
    return event_service.to_out(event, github)


@router.get("/claims", response_model=list[ClaimOut], tags=["claims"])
def get_claims(
    session: SessionDep, project: str | None = None, agent: str | None = None
) -> list[ClaimOut]:
    """All currently active claims."""
    return [
        ClaimOut.model_validate(c)
        for c in claim_service.list_active_claims(session, project=project, agent=agent)
    ]


# --------------------------------------------------------------------------- state


@router.get("/tasks", response_model=list[TaskOut], tags=["state"])
def get_tasks(
    session: SessionDep,
    github: GitHubDep,
    project: str | None = None,
    agent: str | None = None,
    task_status: Annotated[
        str | None, Query(alias="status", pattern="^(claimed|blocked|unclaimed|released)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[TaskOut]:
    """Every task the relay knows about, with ownership and blocked state."""
    return state_service.build_tasks(
        session, project=project, agent=agent, status=task_status, github=github, limit=limit
    )


@router.get("/context", response_model=ProjectContext, tags=["state"])
def get_context(
    session: SessionDep,
    github: GitHubDep,
    settings: SettingsDep,
    project: str,
    window_hours: Annotated[int, Query(ge=1, le=24 * 90)] | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] | None = None,
) -> ProjectContext:
    """Concise current state of a project — what an agent should read before working."""
    return context_service.build_context(
        session,
        project,
        window_hours=window_hours or settings.context_window_hours,
        limit=limit or settings.context_recent_limit,
        github=github,
    )


@router.get("/coordination/summary", response_model=CoordinationSummary, tags=["coordination"])
def get_coordination_summary(
    session: SessionDep,
    github: GitHubDep,
    settings: SettingsDep,
    project: str,
    window_hours: Annotated[int, Query(ge=1, le=24 * 90)] | None = None,
    idle_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
) -> CoordinationSummary:
    """Deterministic coordination view: conflicts, blockers, open questions, next steps."""
    return coordination_service.build_summary(
        session,
        project,
        window_hours=window_hours or settings.context_window_hours,
        idle_hours=idle_hours,
        github=github,
    )


# --------------------------------------------------------------------------- github


@router.get("/github/issues/{number}", tags=["github"])
async def get_github_issue(number: int, github: GitHubDep) -> dict[str, Any]:
    """Read-only passthrough for issue metadata. 503 when GitHub is not configured."""
    if not github.enabled:
        raise HTTPException(status_code=503, detail="GitHub is not configured on this relay.")
    issue = await github.get_issue(number)
    if issue is None:
        raise HTTPException(status_code=404, detail=f"Issue {number} not found or unreadable.")
    return issue


@router.get("/github/pulls", tags=["github"])
async def get_github_pulls(
    github: GitHubDep, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[dict[str, Any]]:
    """Open PR metadata. 503 when GitHub is not configured."""
    if not github.enabled:
        raise HTTPException(status_code=503, detail="GitHub is not configured on this relay.")
    return await github.list_open_pulls(limit=limit)
