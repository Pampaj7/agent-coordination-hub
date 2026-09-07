"""Request/response models. These are the wire contract; DB models stay internal."""

from __future__ import annotations

import datetime as dt
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_relay.models.enums import EventType


class EventCreate(BaseModel):
    """Payload for ``POST /events``."""

    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    agent: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    human_owner: str | None = Field(default=None, max_length=128)
    task: str | None = Field(default=None, max_length=128)
    branch: str | None = Field(default=None, max_length=255)
    target_agent: str | None = Field(default=None, max_length=128)
    in_reply_to: str | None = Field(
        default=None, max_length=32, description="For ANSWER events: the question ref, e.g. Q-19."
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form structured body, e.g. {'completed': [...], 'findings': [...]}.",
    )
    artifacts: list[str] = Field(
        default_factory=list, description="Paths, run ids, commit shas, URLs."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: dt.datetime | None = Field(
        default=None, description="Defaults to server time (UTC) when omitted."
    )

    @model_validator(mode="after")
    def _check_directed_events(self) -> Self:
        if self.event_type is EventType.HANDOFF and not self.target_agent:
            raise ValueError("HANDOFF events require target_agent")
        return self


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ref: str
    event_type: EventType
    agent: str
    human_owner: str | None = None
    project: str
    task: str | None = None
    branch: str | None = None
    target_agent: str | None = None
    in_reply_to: str | None = None
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
    source: str = Field(
        default="agent",
        description='Where the event came from: "agent", "github", "slack" or "a2a".',
    )
    delivery_warning: str | None = Field(
        default=None,
        description="Set when the event names a target_agent the relay has never seen. "
        "The message is stored, but nobody's inbox will surface it.",
    )
    github_url: str | None = None


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=128)
    human_owner: str | None = Field(default=None, max_length=128)
    branch: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=128)
    human_owner: str | None = Field(default=None, max_length=128)
    summary: str | None = Field(default=None, max_length=2000)
    force: bool = Field(
        default=False, description="Release a claim owned by another agent (logged as such)."
    )


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, max_length=128)
    target_agent: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    human_owner: str | None = Field(default=None, max_length=128)
    branch: str | None = Field(default=None, max_length=255)
    continue_from: str | None = Field(
        default=None, max_length=255, description="Commit sha / tag the receiver should start from."
    )
    inputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    transfer_claim: bool = Field(
        default=True, description="Move the active task claim to target_agent."
    )


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project: str
    task: str
    agent: str
    human_owner: str | None = None
    branch: str | None = None
    note: str | None = None
    active: bool
    claimed_at: dt.datetime
    released_at: dt.datetime | None = None
    last_activity_at: dt.datetime


class ClaimConflict(BaseModel):
    """Body of the 409 returned when a task is already owned."""

    detail: str
    project: str
    task: str
    current_owner: str
    human_owner: str | None = None
    claimed_at: dt.datetime
    branch: str | None = None
    last_activity_at: dt.datetime
    last_update: EventOut | None = None


class TaskOut(BaseModel):
    task: str
    project: str
    owner: str | None = None
    human_owner: str | None = None
    status: str = Field(description="claimed | blocked | unclaimed | released")
    branch: str | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    blocked_by: str | None = Field(
        default=None, description="Agent that reported the block (may not own the task)."
    )
    last_activity_at: dt.datetime | None = None
    last_event_type: EventType | None = None
    event_count: int = 0
    github_url: str | None = None


class OpenQuestion(BaseModel):
    ref: str
    project: str
    task: str | None = None
    from_agent: str
    to_agent: str | None = None
    question: str
    asked_at: dt.datetime
    age_hours: float


class AgentActivity(BaseModel):
    agent: str
    human_owner: str | None = None
    active_tasks: list[str] = Field(default_factory=list)
    last_seen_at: dt.datetime | None = None


class ProjectContext(BaseModel):
    """Deliberately bounded: the current state, not the full history."""

    project: str
    generated_at: dt.datetime
    window_hours: int
    active_claims: list[ClaimOut] = Field(default_factory=list)
    active_agents: list[AgentActivity] = Field(default_factory=list)
    recent_updates: list[EventOut] = Field(default_factory=list)
    unresolved_questions: list[OpenQuestion] = Field(default_factory=list)
    blocked_tasks: list[TaskOut] = Field(default_factory=list)
    recent_decisions: list[EventOut] = Field(default_factory=list)
    recent_handoffs: list[EventOut] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    github: dict[str, Any] = Field(default_factory=dict)


class PossibleConflict(BaseModel):
    task: str
    agents: list[str]
    reason: str
    claimed_by: str | None = None


class BlockedItem(BaseModel):
    task: str
    agent: str
    reason: str
    since: dt.datetime


class CoordinationSummary(BaseModel):
    """Rule-based, no LLM. A future coordinator agent consumes this."""

    project: str
    generated_at: dt.datetime
    window_hours: int
    active_agents: dict[str, list[str]] = Field(
        default_factory=dict, description="agent -> tasks currently claimed"
    )
    blocked: list[BlockedItem] = Field(default_factory=list)
    possible_conflicts: list[PossibleConflict] = Field(default_factory=list)
    unresolved_questions: list[OpenQuestion] = Field(default_factory=list)
    recent_findings: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)
    idle_claims: list[ClaimOut] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    version: str
    time: dt.datetime
    database: str
    integrations: dict[str, Any] = Field(default_factory=dict)
