"""Task ownership.

One rule: at most one *active* claim per ``(project, task)``. It is enforced twice —
by a check in this module for a good error message, and by a partial unique index in
SQLite so that two agents racing on different machines still cannot both win.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_relay.db.models import Event, TaskClaim, utcnow
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import ClaimRequest, EventCreate, HandoffRequest, ReleaseRequest
from agent_relay.services.events import create_event


class ClaimConflict(Exception):
    """Raised when an operation would take a task away from its current owner."""

    def __init__(self, claim: TaskClaim, detail: str) -> None:
        super().__init__(detail)
        self.claim = claim
        self.detail = detail


class ClaimNotFound(Exception):
    def __init__(self, project: str, task: str) -> None:
        super().__init__(f"No active claim on {task} in project {project}")
        self.project = project
        self.task = task


def get_active_claim(session: Session, project: str, task: str) -> TaskClaim | None:
    stmt = select(TaskClaim).where(
        TaskClaim.project == project,
        TaskClaim.task == task,
        TaskClaim.active.is_(True),
    )
    return session.execute(stmt).scalars().first()


def list_active_claims(
    session: Session, project: str | None = None, agent: str | None = None
) -> list[TaskClaim]:
    stmt = select(TaskClaim).where(TaskClaim.active.is_(True))
    if project:
        stmt = stmt.where(TaskClaim.project == project)
    if agent:
        stmt = stmt.where(TaskClaim.agent == agent)
    return list(session.execute(stmt.order_by(TaskClaim.claimed_at.asc())).scalars())


def last_update_for(session: Session, project: str, task: str) -> Event | None:
    """Most recent event on a task — shown in a conflict so the loser knows the state."""
    stmt = (
        select(Event)
        .where(Event.project == project, Event.task == task)
        .order_by(Event.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def touch_claim(session: Session, project: str, task: str | None, agent: str) -> None:
    """Refresh last_activity_at when the owning agent posts about its task."""
    if not task:
        return
    claim = get_active_claim(session, project, task)
    if claim is not None and claim.agent == agent:
        claim.last_activity_at = utcnow()


def claim_task(session: Session, request: ClaimRequest) -> tuple[TaskClaim, bool]:
    """Claim a task. Returns ``(claim, created)``; re-claiming your own task is a no-op.

    Raises :class:`ClaimConflict` when another agent already owns it.
    """
    existing = get_active_claim(session, request.project, request.task)
    if existing is not None:
        if existing.agent != request.agent:
            raise ClaimConflict(
                existing,
                f"Task {request.task} in project {request.project} is already claimed by "
                f"{existing.agent}.",
            )
        # Idempotent re-claim: refresh metadata, do not spam the log with a second CLAIM.
        if request.branch:
            existing.branch = request.branch
        if request.note:
            existing.note = request.note
        if request.human_owner:
            existing.human_owner = request.human_owner
        existing.last_activity_at = utcnow()
        session.commit()
        session.refresh(existing)
        return existing, False

    now = utcnow()
    claim = TaskClaim(
        project=request.project,
        task=request.task,
        agent=request.agent,
        human_owner=request.human_owner,
        branch=request.branch,
        note=request.note,
        active=True,
        claimed_at=now,
        last_activity_at=now,
    )
    session.add(claim)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race against the unique index: re-read and report the winner.
        session.rollback()
        winner = get_active_claim(session, request.project, request.task)
        if winner is None:  # pragma: no cover - only if the row vanished mid-race
            raise
        raise ClaimConflict(
            winner,
            f"Task {request.task} in project {request.project} was just claimed by {winner.agent}.",
        ) from None

    create_event(
        session,
        EventCreate(
            event_type=EventType.CLAIM,
            agent=request.agent,
            human_owner=request.human_owner,
            project=request.project,
            task=request.task,
            branch=request.branch,
            summary=request.note or f"Claimed {request.task}.",
        ),
        commit=False,
    )
    session.commit()
    session.refresh(claim)
    return claim, True


def release_task(session: Session, request: ReleaseRequest) -> TaskClaim:
    """Release a claim. Only the owner may release it, unless ``force`` is set."""
    claim = get_active_claim(session, request.project, request.task)
    if claim is None:
        raise ClaimNotFound(request.project, request.task)
    if claim.agent != request.agent and not request.force:
        raise ClaimConflict(
            claim,
            f"Task {request.task} is claimed by {claim.agent}, not {request.agent}. "
            "Pass force=true to release it anyway.",
        )

    forced = claim.agent != request.agent
    now = utcnow()
    claim.active = False
    claim.released_at = now
    claim.last_activity_at = now
    session.flush()

    summary = request.summary or f"Released {request.task}."
    if forced:
        summary = f"{summary} (force-released a claim held by {claim.agent})"
    create_event(
        session,
        EventCreate(
            event_type=EventType.RELEASE,
            agent=request.agent,
            human_owner=request.human_owner or claim.human_owner,
            project=request.project,
            task=request.task,
            branch=claim.branch,
            summary=summary,
            metadata={"forced": True, "previous_owner": claim.agent} if forced else {},
        ),
        commit=False,
    )
    session.commit()
    session.refresh(claim)
    return claim


def handoff_task(session: Session, request: HandoffRequest) -> tuple[Event, TaskClaim | None]:
    """Record a handoff and (by default) move the claim to the receiving agent."""
    claim = get_active_claim(session, request.project, request.task)
    if claim is not None and claim.agent not in (request.agent, request.target_agent):
        raise ClaimConflict(
            claim,
            f"Task {request.task} is claimed by {claim.agent}; {request.agent} cannot hand it off.",
        )

    details: dict[str, object] = {}
    if request.continue_from:
        details["continue_from"] = request.continue_from
    if request.inputs:
        details["inputs"] = request.inputs
    if request.warnings:
        details["warnings"] = request.warnings

    event = create_event(
        session,
        EventCreate(
            event_type=EventType.HANDOFF,
            agent=request.agent,
            human_owner=request.human_owner,
            project=request.project,
            task=request.task,
            branch=request.branch or (claim.branch if claim else None),
            target_agent=request.target_agent,
            summary=request.summary,
            details=details,
            artifacts=request.artifacts,
        ),
        commit=False,
    )

    new_claim: TaskClaim | None = claim
    if request.transfer_claim:
        now = utcnow()
        if claim is not None and claim.agent == request.target_agent:
            claim.last_activity_at = now  # already there; nothing to move
        else:
            if claim is not None:
                claim.active = False
                claim.released_at = now
                claim.last_activity_at = now
                session.flush()  # free the unique index before inserting the new owner
            new_claim = TaskClaim(
                project=request.project,
                task=request.task,
                agent=request.target_agent,
                human_owner=None,
                branch=request.branch or (claim.branch if claim else None),
                note=f"Received handoff from {request.agent}.",
                active=True,
                claimed_at=now,
                last_activity_at=now,
            )
            session.add(new_claim)
            try:
                session.flush()
            except IntegrityError:
                # Someone claimed this task between our check and this insert. Report
                # the winner as a conflict rather than a 500.
                session.rollback()
                winner = get_active_claim(session, request.project, request.task)
                if winner is None:  # pragma: no cover - only if the row vanished mid-race
                    raise
                raise ClaimConflict(
                    winner,
                    f"Task {request.task} was claimed by {winner.agent} while the handoff "
                    "was being recorded.",
                ) from None

    session.commit()
    session.refresh(event)
    if new_claim is not None:
        session.refresh(new_claim)
    return event, new_claim
