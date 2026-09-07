"""Derived state: the rules that turn an event log into "what is going on right now".

Every rule here is deterministic and small enough to hold in your head. That is the
point — when an agent is told a task is blocked, a human must be able to say exactly
why without reading any model output.

Rules
-----
blocked
    A task is blocked when its most recent ``BLOCKED`` event is newer than every
    ``UPDATE``/``ANSWER``/``DECISION``/``HANDOFF``/``RELEASE`` on the same task.
unresolved question
    A ``QUESTION`` is unresolved when no ``ANSWER`` cites its ref via ``in_reply_to``
    and (when it was directed at someone) that agent has posted no later ``ANSWER``
    on the same project+task.
possible conflict
    More than one distinct agent posting events on the same task inside the window,
    or an agent posting on a task actively claimed by a different agent.

Scale note: these fold over the project's events in Python rather than pushing the
logic into SQL. For three researchers and a few thousand events that is instant and
far easier to audit than the equivalent window functions.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.db.models import Event, TaskClaim, utcnow
from agent_relay.models.enums import UNBLOCKING_EVENTS, WORK_EVENTS, EventType
from agent_relay.models.schemas import OpenQuestion, TaskOut
from agent_relay.services.claims import list_active_claims
from agent_relay.services.github import GitHubService

#: details keys we treat as "a finding worth surfacing to the team".
FINDING_KEYS = ("findings", "finding", "results", "result", "conclusion")


@dataclass
class TaskState:
    """Everything we know about one task, folded from its events."""

    project: str
    task: str
    agents: set[str] = field(default_factory=set)
    event_count: int = 0
    events_in_window: int = 0
    agents_in_window: set[str] = field(default_factory=set)
    #: (agent, when) for work events inside the window, so a conflict check can
    #: ignore anything that happened before the current claim started.
    work_in_window: list[tuple[str, dt.datetime]] = field(default_factory=list)
    last_activity_at: dt.datetime | None = None
    last_event_type: EventType | None = None
    branch: str | None = None
    blocked_event_id: int = 0
    blocked_reason: str | None = None
    blocked_agent: str | None = None
    blocked_at: dt.datetime | None = None
    unblocked_event_id: int = 0

    @property
    def blocked(self) -> bool:
        return self.blocked_event_id > self.unblocked_event_id


def fetch_events(
    session: Session, project: str | None = None, since: dt.datetime | None = None
) -> list[Event]:
    stmt = select(Event)
    if project:
        stmt = stmt.where(Event.project == project)
    if since:
        stmt = stmt.where(Event.created_at >= since)
    return list(session.execute(stmt.order_by(Event.id.asc())).scalars())


def build_task_states(
    events: list[Event], window_start: dt.datetime | None = None
) -> dict[tuple[str, str], TaskState]:
    """Fold the event stream into per-task state. ``events`` must be id-ascending."""
    states: dict[tuple[str, str], TaskState] = {}
    for event in events:
        if not event.task:
            continue
        key = (event.project, event.task)
        state = states.get(key)
        if state is None:
            state = states[key] = TaskState(project=event.project, task=event.task)

        state.agents.add(event.agent)
        state.event_count += 1
        state.last_activity_at = event.created_at
        state.last_event_type = EventType(event.event_type)
        if event.branch:
            state.branch = event.branch
        event_type = EventType(event.event_type)
        if window_start is None or event.created_at >= window_start:
            state.events_in_window += 1
            state.agents_in_window.add(event.agent)
            if event_type in WORK_EVENTS:
                state.work_in_window.append((event.agent, event.created_at))

        if event_type is EventType.BLOCKED:
            state.blocked_event_id = event.id
            state.blocked_reason = event.summary
            state.blocked_agent = event.agent
            state.blocked_at = event.created_at
        elif event_type in UNBLOCKING_EVENTS:
            state.unblocked_event_id = event.id
    return states


def build_open_questions(events: list[Event], now: dt.datetime | None = None) -> list[OpenQuestion]:
    """Questions with no answer, newest first."""
    now = now or utcnow()
    questions = [e for e in events if e.event_type == EventType.QUESTION.value]
    if not questions:
        return []

    answers = [e for e in events if e.event_type == EventType.ANSWER.value]
    answered_refs = {a.in_reply_to for a in answers if a.in_reply_to}
    # Fallback for agents that answer without citing a ref: the addressee replying
    # later on the same project+task counts as an answer.
    by_responder: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)
    for answer in answers:
        by_responder[(answer.agent, answer.project, answer.task)].append(answer.id)

    open_questions: list[OpenQuestion] = []
    for question in questions:
        if question.ref in answered_refs:
            continue
        if question.target_agent:
            later = by_responder.get((question.target_agent, question.project, question.task), [])
            if any(answer_id > question.id for answer_id in later):
                continue
        age = (now - question.created_at).total_seconds() / 3600.0
        open_questions.append(
            OpenQuestion(
                ref=question.ref,
                project=question.project,
                task=question.task,
                from_agent=question.agent,
                to_agent=question.target_agent,
                question=question.summary,
                asked_at=question.created_at,
                age_hours=round(age, 1),
            )
        )
    open_questions.sort(key=lambda q: q.asked_at, reverse=True)
    return open_questions


def claims_by_task(claims: list[TaskClaim]) -> dict[tuple[str, str], TaskClaim]:
    return {(c.project, c.task): c for c in claims}


def build_tasks(
    session: Session,
    *,
    project: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    github: GitHubService | None = None,
    limit: int = 200,
) -> list[TaskOut]:
    """The ``GET /tasks`` view: every task the relay has heard about."""
    events = fetch_events(session, project=project)
    states = build_task_states(events)
    active = claims_by_task(list_active_claims(session, project=project))

    # Tasks that were claimed and released still deserve a row.
    stmt = select(TaskClaim)
    if project:
        stmt = stmt.where(TaskClaim.project == project)
    all_claims = list(session.execute(stmt).scalars())
    ever_claimed = {(c.project, c.task) for c in all_claims}
    for key in ever_claimed:
        if key not in states:
            states[key] = TaskState(project=key[0], task=key[1])

    rows: list[TaskOut] = []
    for (proj, task), state in states.items():
        claim = active.get((proj, task))
        if state.blocked:
            task_status = "blocked"
        elif claim is not None:
            task_status = "claimed"
        elif (proj, task) in ever_claimed:
            task_status = "released"
        else:
            task_status = "unclaimed"

        last_activity = state.last_activity_at
        if claim is not None and (last_activity is None or claim.last_activity_at > last_activity):
            last_activity = claim.last_activity_at

        rows.append(
            TaskOut(
                task=task,
                project=proj,
                owner=claim.agent if claim else None,
                human_owner=claim.human_owner if claim else None,
                status=task_status,
                branch=(claim.branch if claim and claim.branch else state.branch),
                blocked=state.blocked,
                blocked_reason=state.blocked_reason if state.blocked else None,
                blocked_by=state.blocked_agent if state.blocked else None,
                last_activity_at=last_activity,
                last_event_type=state.last_event_type,
                event_count=state.event_count,
                github_url=github.task_url(task) if github else None,
            )
        )

    if agent:
        rows = [r for r in rows if r.owner == agent]
    if status:
        rows = [r for r in rows if r.status == status.lower()]

    rows.sort(
        key=lambda r: r.last_activity_at or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True
    )
    return rows[:limit]


def extract_findings(event: Event) -> list[str]:
    """Pull the human-meaningful findings out of an event's details block."""
    details = event.details_json or {}
    found: list[str] = []
    for key in FINDING_KEYS:
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, (list, tuple)):
            found.extend(str(v).strip() for v in value if str(v).strip())
    return found
