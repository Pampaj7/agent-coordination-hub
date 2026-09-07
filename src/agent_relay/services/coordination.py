"""``GET /coordination/summary``: rule-based team situational awareness.

Deliberately **not** an LLM. Everything here is a rule you can read in ten seconds
and a human can verify by hand. A future Coordinator Agent is expected to call this
endpoint and reason on top of its output, not to replace it — which is why the
result is a structured model rather than prose.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from agent_relay.db.models import utcnow
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import (
    BlockedItem,
    ClaimOut,
    CoordinationSummary,
    OpenQuestion,
    PossibleConflict,
)
from agent_relay.services import state as state_service
from agent_relay.services.claims import list_active_claims
from agent_relay.services.github import GitHubService

#: An active claim with no activity for this long is worth a human glance.
DEFAULT_IDLE_HOURS = 24
MAX_FINDINGS = 8


def build_summary(
    session: Session,
    project: str,
    *,
    window_hours: int = 72,
    idle_hours: int = DEFAULT_IDLE_HOURS,
    github: GitHubService | None = None,
) -> CoordinationSummary:
    now = utcnow()
    window_start = now - dt.timedelta(hours=window_hours)

    all_events = state_service.fetch_events(session, project=project)
    windowed = [e for e in all_events if e.created_at >= window_start]
    states = state_service.build_task_states(all_events, window_start=window_start)
    claims = list_active_claims(session, project=project)
    claim_by_task = {claim.task: claim for claim in claims}

    active_agents: dict[str, list[str]] = {}
    for claim in claims:
        active_agents.setdefault(claim.agent, []).append(claim.task)
    for tasks in active_agents.values():
        tasks.sort()

    blocked = [
        BlockedItem(
            task=state.task,
            agent=state.blocked_agent or "unknown",
            reason=state.blocked_reason or "no reason given",
            since=state.blocked_at or now,
        )
        for state in states.values()
        if state.blocked
    ]
    blocked.sort(key=lambda b: b.since)

    conflicts: list[PossibleConflict] = []
    for state in states.values():
        owner = claim_by_task.get(state.task)
        # Work done before the current claim started is history, not a conflict —
        # otherwise every handoff would flag its own predecessor forever.
        since = owner.claimed_at if owner is not None else None
        workers = {agent for agent, when in state.work_in_window if since is None or when >= since}
        others = sorted(workers - ({owner.agent} if owner else set()))
        if owner is not None and others:
            conflicts.append(
                PossibleConflict(
                    task=state.task,
                    agents=sorted(workers),
                    reason=(
                        f"{', '.join(others)} did work on {state.task} while it is claimed by "
                        f"{owner.agent}"
                    ),
                    claimed_by=owner.agent,
                )
            )
        elif owner is None and len(workers) > 1:
            conflicts.append(
                PossibleConflict(
                    task=state.task,
                    agents=sorted(workers),
                    reason=(
                        f"{len(workers)} agents did work on {state.task} in the last "
                        f"{window_hours}h with nobody holding the claim"
                    ),
                    claimed_by=None,
                )
            )
    conflicts.sort(key=lambda c: c.task)

    open_questions = state_service.build_open_questions(all_events, now=now)

    findings: list[str] = []
    for event in sorted(windowed, key=lambda e: e.id, reverse=True):
        if event.event_type not in (EventType.UPDATE.value, EventType.DECISION.value):
            continue
        for finding in state_service.extract_findings(event):
            label = (
                f"[{event.task}] {finding} — {event.agent}"
                if event.task
                else (f"{finding} — {event.agent}")
            )
            if label not in findings:
                findings.append(label)
        if len(findings) >= MAX_FINDINGS:
            break

    decisions = [
        f"[{e.task}] {e.summary} — {e.agent}" if e.task else f"{e.summary} — {e.agent}"
        for e in sorted(all_events, key=lambda e: e.id, reverse=True)
        if e.event_type == EventType.DECISION.value
    ][:MAX_FINDINGS]

    idle_cutoff = now - dt.timedelta(hours=idle_hours)
    idle_claims = [
        ClaimOut.model_validate(claim) for claim in claims if claim.last_activity_at < idle_cutoff
    ]

    return CoordinationSummary(
        project=project,
        generated_at=now,
        window_hours=window_hours,
        active_agents=active_agents,
        blocked=blocked,
        possible_conflicts=conflicts,
        unresolved_questions=open_questions,
        recent_findings=findings[:MAX_FINDINGS],
        recent_decisions=decisions,
        idle_claims=idle_claims,
        suggested_actions=_suggest(
            blocked=blocked,
            conflicts=conflicts,
            questions=open_questions,
            idle_claims=idle_claims,
            idle_hours=idle_hours,
            github=github,
        ),
    )


def _suggest(
    *,
    blocked: list[BlockedItem],
    conflicts: list[PossibleConflict],
    questions: list[OpenQuestion],
    idle_claims: list[ClaimOut],
    idle_hours: int,
    github: GitHubService | None,
) -> list[str]:
    """Turn the findings above into concrete next steps. One rule per situation."""
    actions: list[str] = []

    for question in questions[:5]:
        target = question.to_agent or "the team"
        actions.append(
            f"resolve {question.ref} from {question.from_agent} to {target} "
            f"(open {question.age_hours:.0f}h)"
        )
    for conflict in conflicts[:5]:
        actions.append(f"avoid duplicated work on {conflict.task}: {conflict.reason}")
    for item in blocked[:5]:
        link = github.task_url(item.task) if github else None
        suffix = f" ({link})" if link else ""
        actions.append(f"unblock {item.task} for {item.agent}: {item.reason}{suffix}")
    for claim in idle_claims[:5]:
        actions.append(
            f"check on {claim.agent}: {claim.task} claimed but idle for more than {idle_hours}h — "
            "release it if abandoned"
        )
    return actions
