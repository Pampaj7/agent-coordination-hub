"""What is waiting for one specific agent.

``/context`` answers "what is going on"; an agent then has to read all of it and work
out which parts concern it. That is the wrong division of labour: the relay already
knows who a question was addressed to, who received a handoff and who holds a stalled
claim, so it can answer "what needs *me*" directly.

The distinction matters for an LLM agent in particular. A full project context is
mostly ambient information it must not act on, and burying two actionable items inside
forty lines of ambient state is how they get missed. An inbox is short by construction:
if it is empty, there is nothing to do, and that is a useful thing to be able to say.

Everything here is derived from the same event log and the same rules as ``/context`` —
no new state, no separate bookkeeping that could disagree with it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.db.models import Agent, utcnow
from agent_relay.models.enums import EventType
from agent_relay.services import state as state_service
from agent_relay.services.claims import list_active_claims
from agent_relay.services.github import GitHubService

#: A handoff older than this is history, not an in-tray item.
HANDOFF_WINDOW_HOURS = 72


class InboxQuestion(BaseModel):
    ref: str
    addressed_to: str | None = Field(
        default=None,
        description="The agent it was actually sent to; differs from you when a sibling "
        "agent of the same human was named.",
    )
    project: str
    task: str | None = None
    from_agent: str
    question: str
    age_hours: float
    answer_with: str = Field(description="The exact command that closes this.")


class InboxHandoff(BaseModel):
    ref: str
    addressed_to: str | None = Field(default=None)
    project: str
    task: str | None = None
    from_agent: str
    summary: str
    continue_from: str | None = None
    inputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    received_hours_ago: float


class InboxTask(BaseModel):
    project: str
    task: str
    reason: str
    branch: str | None = None
    idle_hours: float | None = None
    github_url: str | None = None


class Inbox(BaseModel):
    """Short by construction. An empty inbox is a real answer."""

    agent: str
    generated_at: dt.datetime
    questions_for_me: list[InboxQuestion] = Field(default_factory=list)
    handoffs_to_me: list[InboxHandoff] = Field(default_factory=list)
    my_tasks_needing_attention: list[InboxTask] = Field(default_factory=list)
    my_claims: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.questions_for_me or self.handoffs_to_me or self.my_tasks_needing_attention)


def sibling_agents(session: Session, agent: str) -> set[str]:
    """Every agent name belonging to the same human as ``agent``.

    A person legitimately runs several agents — a CLI, a Claude Code session, a Codex
    one — and they come and go. Addressing a question to one of them and having it be
    invisible to the others is how a teammate ends up blocked waiting for an answer
    nobody can see. Questions and handoffs are aimed at a *person*; only claims are
    aimed at a process.
    """
    row = session.get(Agent, agent)
    owner = row.human_owner if row is not None else None
    if not owner:
        return {agent}
    stmt = select(Agent.name).where(Agent.human_owner == owner)
    return set(session.execute(stmt).scalars()) | {agent}


def build_inbox(
    session: Session,
    agent: str,
    *,
    project: str | None = None,
    now: dt.datetime | None = None,
    github: GitHubService | None = None,
) -> Inbox:
    now = now or utcnow()
    events = state_service.fetch_events(session, project=project)
    # Questions and handoffs follow the person; claims stay tied to the process.
    mine = sibling_agents(session, agent)

    # Questions addressed to this agent that nobody has answered. Reuses the same
    # resolution rule as /context, so the two can never disagree about what is open.
    open_questions = [
        q for q in state_service.build_open_questions(events, now=now) if q.to_agent in mine
    ]
    questions = [
        InboxQuestion(
            ref=q.ref,
            project=q.project,
            task=q.task,
            from_agent=q.from_agent,
            question=q.question,
            age_hours=q.age_hours,
            addressed_to=q.to_agent,
            answer_with=(
                f"agent-relay post answer --project {q.project}"
                + (f" --task {q.task}" if q.task else "")
                + f" --in-reply-to {q.ref} --summary '...'"
            ),
        )
        for q in open_questions
    ]

    cutoff = now - dt.timedelta(hours=HANDOFF_WINDOW_HOURS)
    handoffs = [
        InboxHandoff(
            ref=event.ref,
            project=event.project,
            task=event.task,
            from_agent=event.agent,
            summary=event.summary,
            continue_from=str((event.details_json or {}).get("continue_from") or "") or None,
            inputs=[str(i) for i in (event.details_json or {}).get("inputs", [])],
            warnings=[str(w) for w in (event.details_json or {}).get("warnings", [])],
            received_hours_ago=round((now - event.created_at).total_seconds() / 3600.0, 1),
            addressed_to=event.target_agent,
        )
        for event in events
        if event.event_type == EventType.HANDOFF.value
        and event.target_agent in mine
        and event.created_at >= cutoff
    ]
    handoffs.reverse()  # newest first

    # Tasks this agent owns that are not moving: blocked, or claimed and gone quiet.
    claims = list_active_claims(session, project=project, agent=agent)
    task_states = state_service.build_task_states(events)
    needing: list[InboxTask] = []
    for claim in claims:
        state = task_states.get((claim.project, claim.task))
        idle_hours = round((now - claim.last_activity_at).total_seconds() / 3600.0, 1)
        if state is not None and state.blocked:
            needing.append(
                InboxTask(
                    project=claim.project,
                    task=claim.task,
                    reason=f"blocked: {state.blocked_reason}",
                    branch=claim.branch,
                    idle_hours=idle_hours,
                    github_url=github.task_url(claim.task) if github else None,
                )
            )
    return Inbox(
        agent=agent,
        generated_at=now,
        questions_for_me=questions,
        handoffs_to_me=handoffs,
        my_tasks_needing_attention=needing,
        my_claims=sorted(f"{c.project}/{c.task}" for c in claims),
    )


def as_text(inbox: Inbox) -> str:
    """Render for an agent to read. Short, imperative, and says when there is nothing."""
    if inbox.is_empty:
        holding = ", ".join(inbox.my_claims) or "nothing"
        return f"Nothing waiting for {inbox.agent}. Currently holding: {holding}."

    lines: list[str] = [f"=== INBOX · {inbox.agent} ==="]
    if inbox.questions_for_me:
        lines.append("\nQUESTIONS FOR YOU — answer these, you are the one who was asked")
        for q in inbox.questions_for_me:
            where = f" [{q.task}]" if q.task else ""
            via = "" if q.addressed_to in (None, inbox.agent) else f" (sent to {q.addressed_to})"
            lines.append(
                f"  {q.ref}{where} from {q.from_agent} ({q.age_hours}h){via}: {q.question}"
            )
            lines.append(f"      -> {q.answer_with}")
    if inbox.handoffs_to_me:
        lines.append("\nHANDED TO YOU — read the warnings before touching anything")
        for h in inbox.handoffs_to_me:
            where = f" [{h.task}]" if h.task else ""
            lines.append(
                f"  {h.ref}{where} from {h.from_agent} ({h.received_hours_ago}h): {h.summary}"
            )
            if h.continue_from:
                lines.append(f"      continue from: {h.continue_from}")
            for item in h.inputs:
                lines.append(f"      input: {item}")
            for warning in h.warnings:
                lines.append(f"      ⚠ {warning}")
    if inbox.my_tasks_needing_attention:
        lines.append("\nYOUR TASKS THAT ARE NOT MOVING")
        for t in inbox.my_tasks_needing_attention:
            lines.append(f"  {t.project}/{t.task}: {t.reason} (idle {t.idle_hours}h)")
    if inbox.my_claims:
        lines.append("\nHOLDING: " + ", ".join(inbox.my_claims))
    return "\n".join(lines)


def to_dict(inbox: Inbox) -> dict[str, Any]:
    return inbox.model_dump(mode="json")
