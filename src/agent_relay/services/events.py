"""Event log service: create, read, filter.

Everything else in the relay is derived from this log. Events are append-only —
there is no update or delete path, deliberately, so the history an agent reads
today is the history it read yesterday.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from agent_relay.db.models import Agent, Event, Project, utcnow
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import EventCreate, EventOut
from agent_relay.services.github import GitHubService

REF_PATTERN = re.compile(r"^(?:[QE]-)?(\d+)$", re.IGNORECASE)
MAX_LIMIT = 500


def parse_ref(ref: str | None) -> int | None:
    """``Q-19`` / ``E-4`` / ``19`` -> ``19``. Anything else -> ``None``."""
    if not ref:
        return None
    match = REF_PATTERN.match(ref.strip())
    return int(match.group(1)) if match else None


def touch_registry(
    session: Session,
    *,
    project: str,
    agent: str,
    human_owner: str | None,
    when: dt.datetime | None = None,
) -> None:
    """Keep the derived agents/projects registries current. Cheap upserts."""
    when = when or utcnow()

    project_row = session.get(Project, project)
    if project_row is None:
        session.add(Project(name=project, first_seen_at=when, last_seen_at=when))
    elif project_row.last_seen_at < when:
        project_row.last_seen_at = when

    agent_row = session.get(Agent, agent)
    if agent_row is None:
        session.add(
            Agent(
                name=agent,
                human_owner=human_owner,
                last_project=project,
                first_seen_at=when,
                last_seen_at=when,
            )
        )
    else:
        if human_owner:
            agent_row.human_owner = human_owner
        if agent_row.last_seen_at <= when:
            agent_row.last_seen_at = when
            agent_row.last_project = project


def create_event(session: Session, payload: EventCreate, *, commit: bool = True) -> Event:
    """Persist one event. The caller decides when to commit (handoff writes two rows)."""
    created_at = payload.timestamp or utcnow()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)

    in_reply_to = payload.in_reply_to
    if in_reply_to and (ref_id := parse_ref(in_reply_to)) is not None:
        in_reply_to = f"Q-{ref_id}"  # normalise "19" / "q-19" to the canonical form

    event = Event(
        event_type=payload.event_type.value,
        agent=payload.agent,
        human_owner=payload.human_owner,
        project=payload.project,
        task=payload.task,
        branch=payload.branch,
        target_agent=payload.target_agent,
        in_reply_to=in_reply_to,
        summary=payload.summary,
        details_json=payload.details or None,
        artifacts_json=payload.artifacts or None,
        metadata_json=payload.metadata or None,
        created_at=created_at,
    )
    session.add(event)
    session.flush()  # assign the id so `ref` is usable immediately

    touch_registry(
        session,
        project=payload.project,
        agent=payload.agent,
        human_owner=payload.human_owner,
        when=created_at,
    )
    if commit:
        session.commit()
        session.refresh(event)
    return event


def get_event(session: Session, event_id: int) -> Event | None:
    return session.get(Event, event_id)


def _apply_filters(
    stmt: Select[tuple[Event]],
    *,
    project: str | None = None,
    agent: str | None = None,
    human_owner: str | None = None,
    task: str | None = None,
    event_type: EventType | None = None,
    target_agent: str | None = None,
    since: dt.datetime | None = None,
) -> Select[tuple[Event]]:
    if project:
        stmt = stmt.where(Event.project == project)
    if agent:
        stmt = stmt.where(Event.agent == agent)
    if human_owner:
        stmt = stmt.where(Event.human_owner == human_owner)
    if task:
        stmt = stmt.where(Event.task == task)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type.value)
    if target_agent:
        stmt = stmt.where(Event.target_agent == target_agent)
    if since:
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.UTC)
        stmt = stmt.where(Event.created_at >= since)
    return stmt


def list_events(
    session: Session,
    *,
    project: str | None = None,
    agent: str | None = None,
    human_owner: str | None = None,
    task: str | None = None,
    event_type: EventType | None = None,
    target_agent: str | None = None,
    since: dt.datetime | None = None,
    limit: int = 50,
    newest_first: bool = True,
) -> list[Event]:
    stmt = _apply_filters(
        select(Event),
        project=project,
        agent=agent,
        human_owner=human_owner,
        task=task,
        event_type=event_type,
        target_agent=target_agent,
        since=since,
    )
    order = Event.id.desc() if newest_first else Event.id.asc()
    stmt = stmt.order_by(order).limit(max(1, min(limit, MAX_LIMIT)))
    return list(session.execute(stmt).scalars())


def to_out(event: Event, github: GitHubService | None = None) -> EventOut:
    """DB row -> wire model, with a GitHub link attached when we can build one."""
    github_url = github.task_url(event.task) if github else None
    return EventOut(
        id=event.id,
        ref=event.ref,
        event_type=EventType(event.event_type),
        agent=event.agent,
        human_owner=event.human_owner,
        project=event.project,
        task=event.task,
        branch=event.branch,
        target_agent=event.target_agent,
        in_reply_to=event.in_reply_to,
        summary=event.summary,
        details=event.details_json or {},
        artifacts=event.artifacts_json or [],
        metadata=event.metadata_json or {},
        created_at=event.created_at,
        github_url=github_url,
    )


def to_slack_payload(event: Event) -> dict[str, Any]:
    """Plain dict snapshot for the Slack formatter, detached from the DB session."""
    return {
        "id": event.id,
        "ref": event.ref,
        "event_type": event.event_type,
        "agent": event.agent,
        "human_owner": event.human_owner,
        "project": event.project,
        "task": event.task,
        "branch": event.branch,
        "target_agent": event.target_agent,
        "in_reply_to": event.in_reply_to,
        "summary": event.summary,
        "details": event.details_json or {},
        "artifacts": event.artifacts_json or [],
    }
