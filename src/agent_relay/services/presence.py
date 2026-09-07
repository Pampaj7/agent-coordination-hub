"""Agent liveness and claim hygiene.

Presence is deliberately *not* ``last_seen_at``. Any event an agent posts moves
``last_seen_at``, so it answers "when did this agent last say something" — which is
not the same question as "is this agent still alive". An agent can be alive and
quiet for an hour while a long training run finishes, and an agent can be dead
thirty seconds after its last UPDATE, still holding a claim nobody else may take.

``last_heartbeat_at`` is an explicit liveness signal and nothing else: the agent
loop pings the relay on a timer. That separation is what makes the derived status
trustworthy enough to act on — and acting on it is the point, because the failure
mode this whole module exists for is *a dead agent holding a live claim*.

Every rule here is a threshold comparison against configuration, with an injectable
``now``. No heuristics, no model calls: when the relay says a claim is stale, a human
can reproduce the arithmetic on the back of an envelope.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.config import Settings
from agent_relay.db.models import Agent, utcnow
from agent_relay.models.schemas import ReleaseRequest
from agent_relay.services.claims import (
    last_update_for,
    list_active_claims,
    release_task,
    touch_claim,
)
from agent_relay.services.events import touch_registry

logger = logging.getLogger(__name__)

ONLINE = "online"
IDLE = "idle"
OFFLINE = "offline"
UNKNOWN = "unknown"

#: Sort order for the presence list: who to look at first.
_STATUS_RANK = {ONLINE: 0, IDLE: 1, OFFLINE: 2, UNKNOWN: 3}


class AgentPresence(BaseModel):
    """One agent's liveness, plus what it currently owns."""

    agent: str
    status: str = Field(description="online | idle | offline | unknown")
    human_owner: str | None = None
    project: str | None = Field(default=None, description="Last project this agent worked in.")
    current_task: str | None = None
    status_note: str | None = None
    host: str | None = None
    pid: int | None = None
    version: str | None = None
    last_heartbeat_at: dt.datetime | None = None
    seconds_since_heartbeat: float | None = None
    last_seen_at: dt.datetime | None = None
    active_claims: list[str] = Field(
        default_factory=list, description="Tasks this agent currently holds a claim on."
    )


class StaleClaim(BaseModel):
    """An active claim nobody has touched in a while."""

    project: str
    task: str
    agent: str
    human_owner: str | None = None
    branch: str | None = None
    claimed_at: dt.datetime
    last_activity_at: dt.datetime
    idle_hours: float = Field(
        description="Idle time rounded for display. Never compare against this; see idle_seconds."
    )
    idle_seconds: float = Field(
        description="Exact idle time. The auto-release decision is made on this value."
    )
    owner_status: str = Field(description="Presence status of the claim's owner.")
    owner_offline: bool = Field(
        description="True only when the owner heartbeats and has stopped. The dangerous case."
    )


class SweepReport(BaseModel):
    """Outcome of one hygiene pass. Also the response body of ``POST /claims/sweep``."""

    swept_at: dt.datetime
    auto_release_enabled: bool
    claim_stale_hours: float
    claim_expiry_hours: float
    released: list[StaleClaim] = Field(default_factory=list)
    still_stale: list[StaleClaim] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def record_heartbeat(
    session: Session,
    *,
    agent: str,
    human_owner: str | None = None,
    project: str | None = None,
    task: str | None = None,
    status_note: str | None = None,
    host: str | None = None,
    pid: int | None = None,
    version: str | None = None,
) -> Agent:
    """Upsert the agents row from a heartbeat and return it.

    Only the fields the heartbeat actually carried are written. A lightweight ping
    that sends nothing but a name must not blank out the host, pid or status note a
    richer ping recorded a minute ago.
    """
    now = utcnow()

    if project:
        # Reuse the registry upsert: agents.last_project is a foreign key onto
        # projects.name, so a first-ever heartbeat has to register the project too.
        touch_registry(session, project=project, agent=agent, human_owner=human_owner, when=now)
        session.flush()

    row = session.get(Agent, agent)
    if row is None:
        row = Agent(name=agent, first_seen_at=now, last_seen_at=now)
        session.add(row)

    row.last_heartbeat_at = now
    if row.last_seen_at < now:
        row.last_seen_at = now
    if human_owner is not None:
        row.human_owner = human_owner
    if project is not None:
        row.last_project = project
    if task is not None:
        row.current_task = task
    if status_note is not None:
        row.status_note = status_note
    if host is not None:
        row.host = host
    if pid is not None:
        row.pid = pid
    if version is not None:
        row.version = version

    # A heartbeat that names a task is the agent saying "still on it", so it counts
    # as activity on the claim — otherwise a long quiet run would look abandoned.
    if project and task:
        touch_claim(session, project, task, agent)

    session.commit()
    session.refresh(row)
    return row


def presence_status(agent_row: Agent, settings: Settings, now: dt.datetime | None = None) -> str:
    """Derive ``online``/``idle``/``offline``/``unknown`` from the last heartbeat.

    ``unknown`` is distinct from ``offline`` on purpose: an agent that has never sent
    a heartbeat is probably one that does not implement them, and calling it offline
    would licence auto-releasing every claim held by an older agent build.
    """
    heartbeat = agent_row.last_heartbeat_at
    if heartbeat is None:
        return UNKNOWN
    age = ((now or utcnow()) - heartbeat).total_seconds()
    if age <= settings.heartbeat_online_seconds:
        return ONLINE
    if age <= settings.heartbeat_idle_seconds:
        return IDLE
    return OFFLINE


def _to_presence(
    row: Agent, settings: Settings, tasks: list[str], now: dt.datetime
) -> AgentPresence:
    since = (now - row.last_heartbeat_at).total_seconds() if row.last_heartbeat_at else None
    return AgentPresence(
        agent=row.name,
        status=presence_status(row, settings, now),
        human_owner=row.human_owner,
        project=row.last_project,
        current_task=row.current_task,
        status_note=row.status_note,
        host=row.host,
        pid=row.pid,
        version=row.version,
        last_heartbeat_at=row.last_heartbeat_at,
        seconds_since_heartbeat=round(since, 1) if since is not None else None,
        last_seen_at=row.last_seen_at,
        active_claims=sorted(tasks),
    )


def agent_presence(
    session: Session, agent_row: Agent, settings: Settings, *, now: dt.datetime | None = None
) -> AgentPresence:
    """Presence for a single agent — what ``POST /heartbeat`` hands straight back."""
    now = now or utcnow()
    claims = list_active_claims(session, agent=agent_row.name)
    return _to_presence(agent_row, settings, [c.task for c in claims], now)


def list_presence(
    session: Session,
    settings: Settings,
    *,
    project: str | None = None,
    status: str | None = None,
    now: dt.datetime | None = None,
) -> list[AgentPresence]:
    """Every known agent with its derived status, most actionable first.

    ``project`` means "involved in this project": the agent's last project, or an
    active claim in it. An agent that wandered off to another project but still holds
    a claim here is exactly who a project lead needs to see.
    """
    now = now or utcnow()
    rows = list(session.execute(select(Agent).order_by(Agent.name.asc())).scalars())

    tasks_by_agent: dict[str, list[str]] = defaultdict(list)
    for claim in list_active_claims(session, project=project):
        tasks_by_agent[claim.agent].append(claim.task)

    presences: list[AgentPresence] = []
    for row in rows:
        if project and row.last_project != project and row.name not in tasks_by_agent:
            continue
        presence = _to_presence(row, settings, tasks_by_agent.get(row.name, []), now)
        if status and presence.status != status.lower():
            continue
        presences.append(presence)

    presences.sort(
        key=lambda p: (
            _STATUS_RANK.get(p.status, len(_STATUS_RANK)),
            -(p.last_heartbeat_at.timestamp() if p.last_heartbeat_at else 0.0),
            p.agent,
        )
    )
    return presences


def stale_claims(
    session: Session,
    settings: Settings,
    *,
    project: str | None = None,
    now: dt.datetime | None = None,
) -> list[StaleClaim]:
    """Active claims idle for longer than ``claim_stale_hours``, worst first."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(hours=settings.claim_stale_hours)
    agents = {row.name: row for row in session.execute(select(Agent)).scalars()}

    stale: list[StaleClaim] = []
    for claim in list_active_claims(session, project=project):
        if claim.last_activity_at >= cutoff:
            continue
        owner = agents.get(claim.agent)
        owner_status = presence_status(owner, settings, now) if owner is not None else UNKNOWN
        idle_seconds = (now - claim.last_activity_at).total_seconds()
        stale.append(
            StaleClaim(
                project=claim.project,
                task=claim.task,
                agent=claim.agent,
                human_owner=claim.human_owner,
                branch=claim.branch,
                claimed_at=claim.claimed_at,
                last_activity_at=claim.last_activity_at,
                idle_hours=round(idle_seconds / 3600.0, 1),
                idle_seconds=idle_seconds,
                owner_status=owner_status,
                # Only a heartbeating agent that stopped counts as offline; an agent
                # that never heartbeats is unknown, and unknown is not evidence.
                owner_offline=owner_status == OFFLINE,
            )
        )

    stale.sort(key=lambda s: s.idle_seconds, reverse=True)
    return stale


def _auto_release(session: Session, stale: StaleClaim) -> None:
    """Force-release one stale claim and mark the RELEASE event as machine-made."""
    release_task(
        session,
        ReleaseRequest(
            agent="relay",
            project=stale.project,
            task=stale.task,
            human_owner=None,
            summary=(
                f"Auto-released after {stale.idle_hours}h idle "
                f"(agent {stale.agent} {stale.owner_status})."
            ),
            force=True,
        ),
    )

    # release_task already records forced/previous_owner; layer the sweeper's own
    # provenance on top so a human reading the log can tell a machine release from a
    # colleague stealing a task. Rebind rather than mutate: JSONText is not tracked.
    event = last_update_for(session, stale.project, stale.task)
    if event is not None:
        event.metadata_json = {
            **(event.metadata_json or {}),
            "auto_released": True,
            "previous_owner": stale.agent,
            "idle_hours": stale.idle_hours,
        }
        session.commit()


def sweep(session: Session, settings: Settings, *, now: dt.datetime | None = None) -> SweepReport:
    """One claim-hygiene pass: report every stale claim, release the expired ones.

    Reporting and releasing are separate settings on purpose. ``claim_stale_hours``
    decides what a human is told about; ``claim_expiry_hours`` (0 = never) decides
    what the relay is allowed to take away. Most teams should run this reporting-only
    for a while before they trust it to act.
    """
    now = now or utcnow()
    released: list[StaleClaim] = []
    still_stale: list[StaleClaim] = []
    errors: list[str] = []

    for stale in stale_claims(session, settings, now=now):
        # Compare exact idle time, not the display-rounded hours: rounding to one
        # decimal place moves the threshold by up to three minutes, and swallows any
        # expiry shorter than about six.
        expiry_seconds = settings.claim_expiry_hours * 3600.0
        if not settings.auto_release_enabled or stale.idle_seconds < expiry_seconds:
            still_stale.append(stale)
            continue
        try:
            _auto_release(session, stale)
        # One malformed or racing claim must not abort the whole hygiene pass: log it,
        # report it as still-stale, and keep going.
        except Exception as exc:
            session.rollback()
            logger.exception("auto-release failed for %s/%s", stale.project, stale.task)
            errors.append(f"{stale.project}/{stale.task}: {type(exc).__name__}: {exc}")
            still_stale.append(stale)
        else:
            logger.info(
                "auto-released %s/%s held by %s after %sh idle",
                stale.project,
                stale.task,
                stale.agent,
                stale.idle_hours,
            )
            released.append(stale)

    return SweepReport(
        swept_at=now,
        auto_release_enabled=settings.auto_release_enabled,
        claim_stale_hours=settings.claim_stale_hours,
        claim_expiry_hours=settings.claim_expiry_hours,
        released=released,
        still_stale=still_stale,
        errors=errors,
    )
