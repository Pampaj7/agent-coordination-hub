"""``GET /context``: the briefing an agent reads before it starts working.

Bounded on purpose. An agent asking for context wants "what do I need to know to not
duplicate work or contradict someone", not the full history — that is what
``GET /events`` is for.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.db.models import Agent, Event, utcnow
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import AgentActivity, ClaimOut, ProjectContext
from agent_relay.services import events as event_service
from agent_relay.services import state as state_service
from agent_relay.services.claims import list_active_claims
from agent_relay.services.github import GitHubService

MAX_ARTIFACTS = 15


def _recent(events: list[Event], event_type: EventType, limit: int) -> list[Event]:
    matching = [e for e in events if e.event_type == event_type.value]
    return sorted(matching, key=lambda e: e.id, reverse=True)[:limit]


def build_context(
    session: Session,
    project: str,
    *,
    window_hours: int = 72,
    limit: int = 10,
    github: GitHubService | None = None,
) -> ProjectContext:
    now = utcnow()
    window_start = now - dt.timedelta(hours=window_hours)

    all_events = state_service.fetch_events(session, project=project)
    windowed = [e for e in all_events if e.created_at >= window_start]

    claims = list_active_claims(session, project=project)
    task_rows = state_service.build_tasks(session, project=project, github=github)
    blocked = [row for row in task_rows if row.blocked]

    # Unresolved questions are checked against the whole log: an old unanswered
    # question is exactly the thing that must not fall out of the window.
    open_questions = state_service.build_open_questions(all_events, now=now)

    # Active agents = anyone holding a claim, plus anyone who posted in the window.
    tasks_by_agent: dict[str, list[str]] = {}
    for claim in claims:
        tasks_by_agent.setdefault(claim.agent, []).append(claim.task)
    for event in windowed:
        tasks_by_agent.setdefault(event.agent, [])

    owner_rows = session.execute(
        select(Agent).where(Agent.name.in_(list(tasks_by_agent) or [""]))
    ).scalars()
    owner_of: dict[str, str | None] = {row.name: row.human_owner for row in owner_rows}
    last_seen: dict[str, dt.datetime] = {}
    for event in all_events:
        last_seen[event.agent] = event.created_at

    active_agents = [
        AgentActivity(
            agent=name,
            human_owner=owner_of.get(name),
            active_tasks=sorted(tasks),
            last_seen_at=last_seen.get(name),
        )
        for name, tasks in sorted(tasks_by_agent.items())
    ]

    artifacts: list[str] = []
    for event in sorted(windowed, key=lambda e: e.id, reverse=True):
        for artifact in event.artifacts_json or []:
            text = str(artifact)
            if text not in artifacts:
                artifacts.append(text)
        if len(artifacts) >= MAX_ARTIFACTS:
            break

    github_block: dict[str, object] = {}
    if github and github.enabled:
        github_block = {
            "repo": f"{github.settings.github_owner}/{github.settings.github_repo}",
            "repo_url": github.repo_url,
            "task_links": {row.task: row.github_url for row in task_rows[:limit] if row.github_url},
            "artifact_links": {a: url for a in artifacts if (url := github.artifact_url(a))},
        }

    return ProjectContext(
        project=project,
        generated_at=now,
        window_hours=window_hours,
        active_claims=[ClaimOut.model_validate(c) for c in claims],
        active_agents=active_agents,
        recent_updates=[
            event_service.to_out(e, github) for e in _recent(windowed, EventType.UPDATE, limit)
        ],
        unresolved_questions=open_questions[:limit],
        blocked_tasks=blocked[:limit],
        recent_decisions=[
            event_service.to_out(e, github) for e in _recent(all_events, EventType.DECISION, limit)
        ],
        recent_handoffs=[
            event_service.to_out(e, github) for e in _recent(windowed, EventType.HANDOFF, limit)
        ],
        artifacts=artifacts[:MAX_ARTIFACTS],
        github=github_block,
    )
