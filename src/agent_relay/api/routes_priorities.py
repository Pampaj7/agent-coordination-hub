"""``GET /priorities``: what each person should look at first, and why."""

from __future__ import annotations

from fastapi import APIRouter

from agent_relay.api.deps import AuthDep, SessionDep, SettingsDep
from agent_relay.services import priorities as priorities_service
from agent_relay.services.priorities import PriorityBoard

router = APIRouter(dependencies=[AuthDep], tags=["state"])


@router.get("/priorities", response_model=PriorityBoard)
def get_priorities(
    session: SessionDep,
    settings: SettingsDep,
    project: str | None = None,
    include_in_progress: bool = True,
) -> PriorityBoard:
    """One ranked list per person, ordered by who pays for the delay.

    ``/inbox`` answers "what needs this agent"; this answers "of the three of us,
    who is holding up whom". Set ``include_in_progress=false`` to see only the items
    that actually need a decision.
    """
    return priorities_service.build_priorities(
        session,
        settings,
        project=project,
        include_in_progress=include_in_progress,
    )
