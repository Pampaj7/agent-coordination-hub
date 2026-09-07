"""Database schema.

Four tables, deliberately:

``events``       append-only structured coordination log (the source of truth for the relay)
``task_claims``  who currently owns which task
``agents``       lightweight registry, derived from events (first/last seen, human owner)
``projects``     lightweight registry, derived from events

``agents`` and ``projects`` are pure conveniences: they are rebuilt from events on
every write, so losing them loses nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent_relay.db.types import JSONText, UTCDateTime


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One structured coordination event. Never updated, never deleted."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    agent: Mapped[str] = mapped_column(String(128), index=True)
    human_owner: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    project: Mapped[str] = mapped_column(String(128), index=True)
    task: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    branch: Mapped[str | None] = mapped_column(String(255), default=None)
    target_agent: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    # For ANSWER events: the ref ("Q-19") of the question being answered.
    in_reply_to: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    summary: Mapped[str] = mapped_column(String(2000))
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText, default=None)
    artifacts_json: Mapped[list[str] | None] = mapped_column(JSONText, default=None)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONText, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True, default=utcnow)
    #: Where this event came from: "agent" (default), "github", "slack", "coordinator".
    source: Mapped[str] = mapped_column(String(16), index=True, default="agent")
    #: Slack message ts of the posted copy. This is the anchor that lets a human's
    #: threaded reply in Slack become an ANSWER event on the right question.
    slack_ts: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    slack_channel: Mapped[str | None] = mapped_column(String(32), default=None)

    __table_args__ = (
        Index("ix_events_project_created", "project", "created_at"),
        Index("ix_events_project_task", "project", "task"),
    )

    @property
    def ref(self) -> str:
        """Short human-quotable handle. Questions get ``Q-<id>``, everything else ``E-<id>``."""
        return f"Q-{self.id}" if self.event_type == "QUESTION" else f"E-{self.id}"


class TaskClaim(Base):
    """Ownership of a task by an agent. At most one active claim per (project, task)."""

    __tablename__ = "task_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(128), index=True)
    task: Mapped[str] = mapped_column(String(128), index=True)
    agent: Mapped[str] = mapped_column(String(128), index=True)
    human_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    branch: Mapped[str | None] = mapped_column(String(255), default=None)
    note: Mapped[str | None] = mapped_column(String(2000), default=None)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    claimed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    released_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    last_activity_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        # Partial unique index: the database itself refuses a second active claim,
        # so a race between two agents cannot produce duplicated ownership.
        Index(
            "uq_active_claim_per_task",
            "project",
            "task",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active"),
        ),
    )


class Project(Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class Agent(Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    human_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    last_project: Mapped[str | None] = mapped_column(
        ForeignKey("projects.name", ondelete="SET NULL"), default=None
    )
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True, default=utcnow)
    #: Explicit liveness signal, distinct from last_seen_at (which any event updates).
    #: An agent can be posting nothing and still be alive, or dead mid-task.
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime, index=True, default=None
    )
    host: Mapped[str | None] = mapped_column(String(255), default=None)
    pid: Mapped[int | None] = mapped_column(default=None)
    version: Mapped[str | None] = mapped_column(String(64), default=None)
    #: Free-text "what I am doing", set on the heartbeat.
    status_note: Mapped[str | None] = mapped_column(String(500), default=None)
    current_task: Mapped[str | None] = mapped_column(String(128), default=None)

    __table_args__ = (UniqueConstraint("name", name="uq_agent_name"),)


class IngestRecord(Base):
    """Idempotency ledger for inbound webhooks and pollers.

    GitHub retries deliveries and Slack retries events; without this, one push
    would become three identical UPDATE events.
    """

    __tablename__ = "ingest_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    #: Delivery id, Slack event id, or a synthesised natural key for pollers.
    external_id: Mapped[str] = mapped_column(String(255))
    event_id: Mapped[int | None] = mapped_column(default=None)
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_ingest_source_external"),)
