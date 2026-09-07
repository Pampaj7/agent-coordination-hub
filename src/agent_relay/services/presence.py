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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_relay.config import Settings
from agent_relay.db.models import Agent, Event, utcnow
from agent_relay.models.schemas import ReleaseRequest
from agent_relay.services.claims import (
    last_update_for,
    list_active_claims,
    release_task,
    touch_claim,
)
from agent_relay.services.events import touch_registry

logger = logging.getLogger(__name__)

#: Liveness, derived from the heartbeat alone.
ONLINE = "online"
IDLE = "idle"
OFFLINE = "offline"
UNKNOWN = "unknown"

#: Activity, derived from liveness *plus* evidence of actual work. A heartbeat only
#: proves the process is alive; ``working`` additionally requires that the agent is
#: holding a task or has just produced something. The distinction matters because
#: "online" on its own is what an idling shell looks like.
WORKING = "working"

#: Sort order for the presence list: who to look at first.
_STATUS_RANK = {WORKING: 0, ONLINE: 1, IDLE: 2, OFFLINE: 3, UNKNOWN: 4}


class AgentPresence(BaseModel):
    """One agent's liveness, plus what it currently owns."""

    agent: str
    status: str = Field(description="working | online | idle | offline | unknown. What to display.")
    liveness: str = Field(
        default=UNKNOWN,
        description=(
            "online | idle | offline | unknown, from the heartbeat alone. "
            "``status`` is this, upgraded to ``working`` when there is work evidence."
        ),
    )
    busy_reason: str | None = Field(
        default=None,
        description="Why the agent counts as working. None unless status is 'working'.",
    )
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


def work_status(
    liveness: str,
    *,
    held_tasks: list[str],
    last_event_at: dt.datetime | None,
    settings: Settings,
    now: dt.datetime,
) -> tuple[str, str | None]:
    """Upgrade ``online`` to ``working`` when the agent shows evidence of work.

    A heartbeat proves a process is alive, nothing more: a shell sitting at a prompt
    heartbeats exactly like one that is mid-task. Two things distinguish them, and
    either is enough:

    * the agent holds an active claim — it has told everyone it owns a task; or
    * it posted an event within the online window — it has just produced something.

    Only ``online`` is ever upgraded. An idle or offline agent that still holds a
    claim is precisely the case ``stale_claims`` exists to flag, and calling it
    "working" would hide it.
    """
    if liveness != ONLINE:
        return liveness, None
    if held_tasks:
        return WORKING, "holds " + ", ".join(sorted(held_tasks))
    if last_event_at is not None:
        age = (now - last_event_at).total_seconds()
        if 0 <= age <= settings.heartbeat_online_seconds:
            return WORKING, f"posted {int(age)}s ago"
    return liveness, None


def last_event_at_by_agent(
    session: Session, agents: list[str] | None = None
) -> dict[str, dt.datetime]:
    """When each agent last posted something of its own.

    Restricted to ``source == "agent"``: an event ingested from GitHub or Slack is
    activity *about* the agent, not activity *by* it, and must not make an absent
    agent look busy.
    """
    stmt = (
        select(Event.agent, func.max(Event.created_at))
        .where(Event.source == "agent")
        .group_by(Event.agent)
    )
    if agents is not None:
        if not agents:
            return {}
        stmt = stmt.where(Event.agent.in_(agents))
    return {name: ts for name, ts in session.execute(stmt) if ts is not None}


def _to_presence(
    row: Agent,
    settings: Settings,
    tasks: list[str],
    now: dt.datetime,
    *,
    held_tasks: list[str] | None = None,
    last_event_at: dt.datetime | None = None,
) -> AgentPresence:
    since = (now - row.last_heartbeat_at).total_seconds() if row.last_heartbeat_at else None
    liveness = presence_status(row, settings, now)
    status, busy_reason = work_status(
        liveness,
        # Work evidence is deliberately unscoped by project: an agent holding a task
        # in another project is working, even when this view is filtered to one.
        held_tasks=tasks if held_tasks is None else held_tasks,
        last_event_at=last_event_at,
        settings=settings,
        now=now,
    )
    return AgentPresence(
        agent=row.name,
        status=status,
        liveness=liveness,
        busy_reason=busy_reason,
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
    tasks = [c.task for c in claims]
    return _to_presence(
        agent_row,
        settings,
        tasks,
        now,
        held_tasks=tasks,
        last_event_at=last_event_at_by_agent(session, [agent_row.name]).get(agent_row.name),
    )


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

    # Claims are fetched unscoped and filtered here rather than in SQL: the project
    # filter must narrow what is *displayed* without narrowing the evidence used to
    # decide whether an agent is working.
    tasks_by_agent: dict[str, list[str]] = defaultdict(list)
    held_by_agent: dict[str, list[str]] = defaultdict(list)
    for claim in list_active_claims(session):
        held_by_agent[claim.agent].append(claim.task)
        if project is None or claim.project == project:
            tasks_by_agent[claim.agent].append(claim.task)

    last_events = last_event_at_by_agent(session, [row.name for row in rows])

    presences: list[AgentPresence] = []
    for row in rows:
        if project and row.last_project != project and row.name not in tasks_by_agent:
            continue
        presence = _to_presence(
            row,
            settings,
            tasks_by_agent.get(row.name, []),
            now,
            held_tasks=held_by_agent.get(row.name, []),
            last_event_at=last_events.get(row.name),
        )
        # Match either name: `--status online` keeps returning working agents, which
        # are online by definition, while `--status working` narrows to just those.
        if status and status.lower() not in {presence.status, presence.liveness}:
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
    min_idle_hours: float | None = None,
) -> list[StaleClaim]:
    """Active claims idle for longer than ``claim_stale_hours``, worst first.

    ``min_idle_hours`` overrides that threshold; the sweeper uses it so a short
    auto-release expiry is not floored by a longer reporting threshold.
    """
    now = now or utcnow()
    threshold = settings.claim_stale_hours if min_idle_hours is None else min_idle_hours
    cutoff = now - dt.timedelta(hours=threshold)
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

    # Gather candidates at whichever threshold is lower. Reporting starts at
    # claim_stale_hours, but if auto-release is set shorter than that, filtering on the
    # stale threshold first would make the expiry unreachable *and* invisible — the
    # claim would not even appear in still_stale.
    threshold_hours = settings.claim_stale_hours
    if settings.auto_release_enabled:
        threshold_hours = min(threshold_hours, settings.claim_expiry_hours)

    for stale in stale_claims(session, settings, now=now, min_idle_hours=threshold_hours):
        # Compare exact idle time, not the display-rounded hours: rounding to one
        # decimal place moves the threshold by up to three minutes, and swallows any
        # expiry shorter than about six.
        expiry_seconds = settings.claim_expiry_hours * 3600.0
        if not settings.auto_release_enabled or stale.idle_seconds < expiry_seconds:
            still_stale.append(stale)
            continue
        # Never reclaim from an agent that is demonstrably alive. A heartbeat can be a
        # bare liveness ping that carries no task, so a long quiet run legitimately
        # looks idle while the agent is very much working — and this module exists to
        # catch *dead* agents, not slow ones.
        if stale.owner_status == ONLINE:
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
