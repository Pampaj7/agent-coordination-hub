"""``GET /coordination/overview``: the same rules as ``/coordination/summary``, but
across every project at once.

Per-project coordination answers "what is happening in tether". A human running
several agents across several repos has a different question: "where should I look
first, and is anyone spread too thin". This module answers that one, and it answers
it by calling :func:`agent_relay.services.coordination.build_summary` once per
project — the blocked/conflict/question rules live in exactly one place and are not
restated here.

The genuinely cross-project signal is ``overloaded_agents``: an agent holding claims
in more than one project at the same time. Nothing inside a single project's summary
can see that, and it is almost always the thing worth a human's attention.

Deterministic, no LLM, no network (except the optional GitHub link helper, which is
pure string work).
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.config import Settings
from agent_relay.db.models import Event, Project, TaskClaim, utcnow
from agent_relay.services import coordination as coordination_service
from agent_relay.services.github import GitHubService

MAX_ACTIONS = 12
#: Beyond this an "overview" stops being an overview; the per-project endpoint is
#: the right tool once you know which project you care about.
MAX_PROJECTS = 50


class ProjectOverview(BaseModel):
    """One project's row in the portfolio view. Counts only — no prose."""

    project: str
    active_claims: int = 0
    blocked: int = 0
    open_questions: int = 0
    conflicts: int = 0
    idle_claims: int = 0
    events_in_window: int = 0
    agents: list[str] = Field(default_factory=list, description="Agents holding a claim here.")


class OverloadedAgent(BaseModel):
    """An agent holding claims in more than one project at once."""

    agent: str
    project_count: int
    task_count: int
    projects: dict[str, list[str]] = Field(default_factory=dict, description="project -> tasks")


class RelayOverview(BaseModel):
    """Cross-project situational awareness. Rule-based, cheap, auditable."""

    generated_at: dt.datetime
    window_hours: int
    projects: list[ProjectOverview] = Field(default_factory=list)
    overloaded_agents: list[OverloadedAgent] = Field(default_factory=list)
    agents_across_projects: dict[str, dict[str, list[str]]] = Field(
        default_factory=dict, description="agent -> {project: [tasks]} for every claim holder"
    )
    busiest_projects: list[str] = Field(
        default_factory=list, description="Project names, most events in the window first."
    )
    totals: dict[str, int] = Field(default_factory=dict)
    suggested_actions: list[str] = Field(default_factory=list)


def list_projects(session: Session) -> list[str]:
    """Every project the relay has heard of.

    The ``projects`` registry is derived and could in principle lag, so the event log
    and the claim table get a vote too. Cheap union, always right.
    """
    names: set[str] = set()
    names.update(session.execute(select(Project.name)).scalars())
    names.update(session.execute(select(Event.project).distinct()).scalars())
    names.update(
        session.execute(
            select(TaskClaim.project).where(TaskClaim.active.is_(True)).distinct()
        ).scalars()
    )
    return sorted(name for name in names if name)


def _events_in_window(session: Session, window_start: dt.datetime) -> dict[str, int]:
    """One query for the whole portfolio rather than one per project."""
    rows = session.execute(select(Event.project).where(Event.created_at >= window_start)).scalars()
    counts: dict[str, int] = {}
    for project in rows:
        counts[project] = counts.get(project, 0) + 1
    return counts


def build_overview(
    session: Session,
    settings: Settings,
    *,
    window_hours: int | None = None,
    idle_hours: int = coordination_service.DEFAULT_IDLE_HOURS,
    github: GitHubService | None = None,
) -> RelayOverview:
    now = utcnow()
    hours = window_hours or settings.context_window_hours
    window_start = now - dt.timedelta(hours=hours)

    projects = list_projects(session)[:MAX_PROJECTS]
    event_counts = _events_in_window(session, window_start)

    rows: list[ProjectOverview] = []
    agents_across: dict[str, dict[str, list[str]]] = {}
    blocked_by_project: dict[str, int] = {}
    questions_by_project: dict[str, int] = {}
    conflicts_by_project: dict[str, int] = {}
    idle_by_project: dict[str, int] = {}

    for project in projects:
        summary = coordination_service.build_summary(
            session,
            project,
            window_hours=hours,
            idle_hours=idle_hours,
            github=github,
        )
        claimed_tasks = sum(len(tasks) for tasks in summary.active_agents.values())
        for agent, tasks in summary.active_agents.items():
            agents_across.setdefault(agent, {})[project] = sorted(tasks)

        blocked_by_project[project] = len(summary.blocked)
        questions_by_project[project] = len(summary.unresolved_questions)
        conflicts_by_project[project] = len(summary.possible_conflicts)
        idle_by_project[project] = len(summary.idle_claims)

        rows.append(
            ProjectOverview(
                project=project,
                active_claims=claimed_tasks,
                blocked=len(summary.blocked),
                open_questions=len(summary.unresolved_questions),
                conflicts=len(summary.possible_conflicts),
                idle_claims=len(summary.idle_claims),
                events_in_window=event_counts.get(project, 0),
                agents=sorted(summary.active_agents),
            )
        )

    overloaded = [
        OverloadedAgent(
            agent=agent,
            project_count=len(by_project),
            task_count=sum(len(tasks) for tasks in by_project.values()),
            projects={p: by_project[p] for p in sorted(by_project)},
        )
        for agent, by_project in sorted(agents_across.items())
        if len(by_project) > 1
    ]
    # Most-split agent first; that is the one a human should look at.
    overloaded.sort(key=lambda a: (-a.project_count, -a.task_count, a.agent))

    busiest = sorted(rows, key=lambda r: (-r.events_in_window, r.project))
    totals = {
        "projects": len(rows),
        "agents": len(agents_across),
        "active_claims": sum(r.active_claims for r in rows),
        "blocked": sum(r.blocked for r in rows),
        "open_questions": sum(r.open_questions for r in rows),
        "conflicts": sum(r.conflicts for r in rows),
        "idle_claims": sum(r.idle_claims for r in rows),
        "events_in_window": sum(r.events_in_window for r in rows),
        "overloaded_agents": len(overloaded),
    }

    return RelayOverview(
        generated_at=now,
        window_hours=hours,
        projects=rows,
        overloaded_agents=overloaded,
        agents_across_projects={a: agents_across[a] for a in sorted(agents_across)},
        busiest_projects=[r.project for r in busiest if r.events_in_window > 0],
        totals=totals,
        suggested_actions=_suggest(rows=rows, overloaded=overloaded, idle_hours=idle_hours),
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _suggest(
    *,
    rows: list[ProjectOverview],
    overloaded: list[OverloadedAgent],
    idle_hours: int,
) -> list[str]:
    """Portfolio-level next steps. One rule per situation, same as per-project.

    Ordered by how much a human's attention is worth: split agents first (nobody
    else can see that), then things that are stuck, then things that are wasteful.
    """
    actions: list[str] = []

    for agent in overloaded[:5]:
        where = ", ".join(
            f"{project} {'/'.join(tasks)}" if tasks else project
            for project, tasks in agent.projects.items()
        )
        actions.append(
            f"{agent.agent} holds claims in {agent.project_count} projects ({where}) — "
            "consider releasing one"
        )

    for row in sorted(rows, key=lambda r: (-r.blocked, r.project)):
        if row.blocked:
            actions.append(f"unblock {row.project}: {_plural(row.blocked, 'blocked task')} waiting")
    for row in sorted(rows, key=lambda r: (-r.conflicts, r.project)):
        if row.conflicts:
            actions.append(
                f"check {row.project} for duplicated work: "
                f"{_plural(row.conflicts, 'possible conflict')}"
            )
    for row in sorted(rows, key=lambda r: (-r.open_questions, r.project)):
        if row.open_questions:
            actions.append(
                f"answer {_plural(row.open_questions, 'open question')} in {row.project}"
            )
    for row in sorted(rows, key=lambda r: (-r.idle_claims, r.project)):
        if row.idle_claims:
            actions.append(
                f"review {_plural(row.idle_claims, 'claim')} in {row.project} idle for more than "
                f"{idle_hours}h — release them if abandoned"
            )
    return actions[:MAX_ACTIONS]
