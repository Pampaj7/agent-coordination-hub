"""HTTP surface for agent presence and claim hygiene. Thin: validate, call, shape."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_relay.api.deps import AuthDep, SessionDep, SettingsDep, caller_owner
from agent_relay.services import presence as presence_service
from agent_relay.services.presence import AgentPresence, StaleClaim, SweepReport

router = APIRouter(dependencies=[AuthDep])

#: Accepted values of the ?status= filter on GET /agents.
STATUS_PATTERN = "^(working|online|idle|offline|unknown)$"


class HeartbeatRequest(BaseModel):
    """Payload for ``POST /heartbeat``.

    Everything but ``agent`` is optional and omission means "unchanged", so a bare
    liveness ping can stay a bare liveness ping.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, max_length=128)
    human_owner: str | None = Field(default=None, max_length=128)
    project: str | None = Field(default=None, max_length=128)
    task: str | None = Field(
        default=None, max_length=128, description="Task the agent is working on right now."
    )
    status_note: str | None = Field(default=None, max_length=500)
    host: str | None = Field(default=None, max_length=255)
    pid: int | None = Field(default=None, ge=0)
    version: str | None = Field(default=None, max_length=64)


@router.post("/heartbeat", response_model=AgentPresence, tags=["presence"])
def post_heartbeat(
    payload: HeartbeatRequest,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> AgentPresence:
    """Record an explicit liveness signal; also keeps the agent's own claim fresh.

    Attributed the same way writes are: the heartbeat is what populates the agent
    registry, so leaving it unattributed produced agents with no owner in `GET /agents`
    — the one view whose entire job is telling you who is working.
    """
    row = presence_service.record_heartbeat(
        session,
        agent=payload.agent,
        human_owner=caller_owner(request, settings) or payload.human_owner,
        project=payload.project,
        task=payload.task,
        status_note=payload.status_note,
        host=payload.host,
        pid=payload.pid,
        version=payload.version,
    )
    return presence_service.agent_presence(session, row, settings)


@router.get("/agents", response_model=list[AgentPresence], tags=["presence"])
def get_agents(
    session: SessionDep,
    settings: SettingsDep,
    project: str | None = None,
    status: Annotated[str | None, Query(pattern=STATUS_PATTERN)] = None,
) -> list[AgentPresence]:
    """Who is around, what they hold, and how long since they last checked in."""
    return presence_service.list_presence(session, settings, project=project, status=status)


@router.get("/claims/stale", response_model=list[StaleClaim], tags=["claims"])
def get_stale_claims(
    session: SessionDep, settings: SettingsDep, project: str | None = None
) -> list[StaleClaim]:
    """Active claims nobody has touched in ``claim_stale_hours``, worst first."""
    return presence_service.stale_claims(session, settings, project=project)


@router.post("/claims/sweep", response_model=SweepReport, tags=["claims"])
def post_sweep(session: SessionDep, settings: SettingsDep) -> SweepReport:
    """Run the hygiene pass now. Releases nothing unless auto-release is configured."""
    return presence_service.sweep(session, settings)
