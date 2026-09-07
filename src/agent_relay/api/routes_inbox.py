"""``GET /inbox``: what is waiting for one agent."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agent_relay.api.deps import AuthDep, GitHubDep, SessionDep
from agent_relay.services import inbox as inbox_service
from agent_relay.services.inbox import Inbox

router = APIRouter(dependencies=[AuthDep], tags=["state"])


@router.get("/inbox", response_model=Inbox)
def get_inbox(
    session: SessionDep,
    github: GitHubDep,
    agent: Annotated[str, Query(min_length=1, max_length=128)],
    project: str | None = None,
) -> Inbox:
    """Questions addressed to this agent, handoffs it received, and its stalled tasks.

    Deliberately narrow: `/context` is what is going on, this is what needs *you*. An
    empty inbox is a meaningful answer, not a missing one.
    """
    return inbox_service.build_inbox(session, agent, project=project, github=github)
