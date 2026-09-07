"""Per-person priorities: what each human should look at first, and why.

Every other view in this relay is organised by *thing* — a project, a task, an event
stream. This one is organised by *person*, because that is the question three people
sharing a relay actually ask: "of everything on this board, which piece is mine, and
which of my pieces is the one that matters?"

Ranking rule
------------
Items are ranked by **who pays for the delay**, not by how hard they are or how old.
Something where another person is stopped waiting on you always outranks something
where the person stopped is you: your own blocked task costs one person's time, an
unanswered question costs two. The bands, highest first:

1. ``question``     someone asked you and stopped. They are idle until you reply.
2. ``stale_claim``  you hold a task nobody has touched. It looks taken, so no one
                    else will pick it up — you are blocking everybody, silently.
3. ``handoff``      work was deliberately parked on your desk and never picked up.
4. ``blocked``      your own task is blocked. Costly, but the cost is yours.
5. ``in_progress``  claims that are moving. Listed so the board is complete, not
                    because they need action.

Within a band the oldest item wins: among equally-costly obligations, the one that
has been waiting longest is the one most likely to have been forgotten.

Grouping follows the *person*, not the process, exactly as the inbox does — an agent
is a process someone started, and a question addressed to one of Niccolò's agents is
Niccolò's obligation regardless of which of his agents is running.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.config import Settings
from agent_relay.db.models import Agent, utcnow
from agent_relay.models.enums import EventType
from agent_relay.services import presence as presence_service
from agent_relay.services import state as state_service
from agent_relay.services.claims import list_active_claims

#: Owner label for agents nobody has claimed. Kept visible on purpose: an unowned
#: agent holding a task is a gap in the setup, and dropping it would hide the gap.
UNASSIGNED = "(unassigned)"

#: How far back a HANDOFF still counts as sitting on someone's desk.
HANDOFF_WINDOW_HOURS = 72

QUESTION = "question"
STALE_CLAIM = "stale_claim"
HANDOFF = "handoff"
BLOCKED = "blocked"
IN_PROGRESS = "in_progress"

#: Band per kind. Lower is more urgent. See the module docstring for the reasoning.
BANDS: dict[str, int] = {
    QUESTION: 1,
    STALE_CLAIM: 2,
    HANDOFF: 3,
    BLOCKED: 4,
    IN_PROGRESS: 5,
}

#: Bands where the cost of doing nothing lands on somebody else.
BLOCKING_OTHERS = {QUESTION, STALE_CLAIM, HANDOFF}


class PriorityItem(BaseModel):
    """One thing a person should do, with the reason it sits where it sits."""

    band: int = Field(description="1 is most urgent. See BANDS.")
    kind: str = Field(description="question | stale_claim | handoff | blocked | in_progress")
    agent: str = Field(description="Which of the person's agents this lands on.")
    project: str
    task: str | None = None
    ref: str | None = None
    headline: str
    why: str = Field(description="Who pays for the delay. The justification for the band.")
    age_hours: float
    do_next: str | None = Field(default=None, description="The exact command that closes this.")


class OwnerPriorities(BaseModel):
    """Everything on one person's plate, most urgent first."""

    owner: str
    agents: list[str] = Field(default_factory=list)
    items: list[PriorityItem] = Field(default_factory=list)
    blocking_others: int = Field(
        default=0, description="Items whose delay costs somebody else. The headline number."
    )
    top: str | None = Field(default=None, description="Headline of the single most urgent item.")


class PriorityBoard(BaseModel):
    generated_at: dt.datetime
    project: str | None = None
    owners: list[OwnerPriorities] = Field(default_factory=list)


def owner_of(agent_owners: dict[str, str | None], agent: str | None) -> str:
    """Map an agent to the person responsible for it."""
    if not agent:
        return UNASSIGNED
    return agent_owners.get(agent) or UNASSIGNED


def build_priorities(
    session: Session,
    settings: Settings,
    *,
    project: str | None = None,
    now: dt.datetime | None = None,
    include_in_progress: bool = True,
) -> PriorityBoard:
    """Fold relay state into one ranked list per person.

    Every input is read once and shared: this runs on every dashboard refresh, and a
    view that costs five scans of the event table is a view people turn off.
    """
    now = now or utcnow()
    events = state_service.fetch_events(session, project=project)
    task_states = state_service.build_task_states(events)

    agent_rows: list[tuple[str, str | None]] = [
        (name, owner) for name, owner in session.execute(select(Agent.name, Agent.human_owner))
    ]
    agent_owners: dict[str, str | None] = dict(agent_rows)
    agents_by_owner: dict[str, set[str]] = {}
    for name, owner in agent_rows:
        agents_by_owner.setdefault(owner or UNASSIGNED, set()).add(name)

    items: dict[str, list[PriorityItem]] = {}

    def add(owner: str, item: PriorityItem) -> None:
        items.setdefault(owner, []).append(item)
        agents_by_owner.setdefault(owner, set()).add(item.agent)

    # --- band 1: questions addressed to one of this person's agents ---------
    for question in state_service.build_open_questions(events, now=now):
        if not question.to_agent:
            # Unaddressed questions belong to the project, not to a person. They are
            # already on the /context board; putting them on everyone's plate here
            # would make every person's list identical and therefore useless.
            continue
        add(
            owner_of(agent_owners, question.to_agent),
            PriorityItem(
                band=BANDS[QUESTION],
                kind=QUESTION,
                agent=question.to_agent,
                project=question.project,
                task=question.task,
                ref=question.ref,
                headline=question.question,
                why=f"{question.from_agent} is waiting on this answer",
                age_hours=question.age_hours,
                do_next=(
                    f"agent-relay post answer --project {question.project}"
                    + (f" --task {question.task}" if question.task else "")
                    + f" --in-reply-to {question.ref} --summary '...'"
                ),
            ),
        )

    # --- band 2: stale claims -----------------------------------------------
    stale_tasks: set[tuple[str, str]] = set()
    for stale in presence_service.stale_claims(session, settings, now=now):
        if project and stale.project != project:
            continue
        stale_tasks.add((stale.project, stale.task))
        add(
            owner_of(agent_owners, stale.agent),
            PriorityItem(
                band=BANDS[STALE_CLAIM],
                kind=STALE_CLAIM,
                agent=stale.agent,
                project=stale.project,
                task=stale.task,
                headline=f"{stale.task} has not moved in {stale.idle_hours}h",
                why=(
                    "the task looks taken, so nobody else will pick it up"
                    + (" — and the agent is offline" if stale.owner_offline else "")
                ),
                age_hours=stale.idle_seconds / 3600.0,
                do_next=(
                    f"agent-relay post update --project {stale.project} --task {stale.task}"
                    " --summary '...'  # or release it"
                ),
            ),
        )

    # --- band 3: handoffs parked on a desk ----------------------------------
    cutoff = now - dt.timedelta(hours=HANDOFF_WINDOW_HOURS)
    for event in events:
        if event.event_type != EventType.HANDOFF.value or not event.target_agent:
            continue
        if event.created_at < cutoff:
            continue
        add(
            owner_of(agent_owners, event.target_agent),
            PriorityItem(
                band=BANDS[HANDOFF],
                kind=HANDOFF,
                agent=event.target_agent,
                project=event.project,
                task=event.task,
                ref=event.ref,
                headline=event.summary,
                why=f"{event.agent} handed this over and moved on",
                age_hours=round((now - event.created_at).total_seconds() / 3600.0, 1),
                do_next=(
                    f"agent-relay claim --project {event.project} --task {event.task}"
                    if event.task
                    else None
                ),
            ),
        )

    # --- bands 4 and 5: the person's own claims -----------------------------
    for claim in list_active_claims(session, project=project):
        state = task_states.get((claim.project, claim.task))
        idle_hours = round((now - claim.last_activity_at).total_seconds() / 3600.0, 1)
        owner = owner_of(agent_owners, claim.agent)
        if state is not None and state.blocked:
            add(
                owner,
                PriorityItem(
                    band=BANDS[BLOCKED],
                    kind=BLOCKED,
                    agent=claim.agent,
                    project=claim.project,
                    task=claim.task,
                    headline=f"{claim.task} is blocked: {state.blocked_reason}",
                    why="the task cannot move until this is escalated or answered",
                    age_hours=idle_hours,
                    do_next=(
                        f"agent-relay post question --project {claim.project}"
                        f" --task {claim.task} --to <agent> --summary '...'"
                    ),
                ),
            )
        elif include_in_progress and (claim.project, claim.task) not in stale_tasks:
            # Stale claims are already listed in band 2; repeating them here would
            # make the same task look like two obligations.
            add(
                owner,
                PriorityItem(
                    band=BANDS[IN_PROGRESS],
                    kind=IN_PROGRESS,
                    agent=claim.agent,
                    project=claim.project,
                    task=claim.task,
                    headline=f"{claim.task} in progress",
                    why="moving; listed for completeness",
                    age_hours=idle_hours,
                ),
            )

    owners: list[OwnerPriorities] = []
    for owner, agent_names in agents_by_owner.items():
        owned = sorted(items.get(owner, []), key=lambda i: (i.band, -i.age_hours))
        if not owned:
            continue
        blocking = sum(1 for i in owned if i.kind in BLOCKING_OTHERS)
        owners.append(
            OwnerPriorities(
                owner=owner,
                agents=sorted(agent_names),
                items=owned,
                blocking_others=blocking,
                top=owned[0].headline,
            )
        )

    # Whoever is holding up the most other people goes at the top of the board.
    owners.sort(key=lambda o: (-o.blocking_others, o.items[0].band, -len(o.items), o.owner))
    return PriorityBoard(generated_at=now, project=project, owners=owners)
