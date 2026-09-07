"""Coordinator endpoints: prose on top of the deterministic summary."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from agent_relay.api.deps import AuthDep, GitHubDep, SessionDep, SettingsDep
from agent_relay.services import coordinator as coordinator_service

router = APIRouter(dependencies=[AuthDep], tags=["coordination"])


@router.get("/coordination/brief")
async def get_brief(
    session: SessionDep,
    settings: SettingsDep,
    github: GitHubDep,
    project: str,
    window_hours: Annotated[int, Query(ge=1, le=24 * 90)] | None = None,
) -> dict[str, Any]:
    """A short readable briefing for a project.

    Always returns something: with an API key configured the prose is written by the
    model, without one it falls back to a deterministic briefing. The `source` field
    says which you got, and the audited `summary` is always included alongside so the
    prose can be checked against the facts it came from.
    """
    return await coordinator_service.build_brief(
        session,
        project,
        settings=settings,
        window_hours=window_hours,
        github=github,
    )
