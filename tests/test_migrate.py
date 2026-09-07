"""The additive schema migration.

The relay upgrades a database that has real coordination history in it, so the only
acceptable behaviour is: add what is missing, touch nothing else, and be safe to run
on every boot.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, inspect, text

from agent_relay.db.migrate import migrate
from agent_relay.db.models import Base

# The v1 schema, as it actually shipped: no source/slack columns, no ingest ledger.
V1_EVENTS = """
CREATE TABLE events (
    id INTEGER NOT NULL PRIMARY KEY,
    event_type VARCHAR(16) NOT NULL,
    agent VARCHAR(128) NOT NULL,
    human_owner VARCHAR(128),
    project VARCHAR(128) NOT NULL,
    task VARCHAR(128),
    branch VARCHAR(255),
    target_agent VARCHAR(128),
    in_reply_to VARCHAR(32),
    summary VARCHAR(2000) NOT NULL,
    details_json TEXT,
    artifacts_json TEXT,
    metadata_json TEXT,
    created_at DATETIME NOT NULL
)
"""


def v1_database(tmp_path: Any) -> str:
    url = f"sqlite:///{tmp_path}/v1.db"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(V1_EVENTS))
        conn.execute(
            text(
                "INSERT INTO events (event_type, agent, project, task, summary, created_at) "
                "VALUES ('UPDATE','leo-codex','tether','GH-142','v1 history','2026-01-01')"
            )
        )
    engine.dispose()
    return url


def test_upgrade_adds_what_is_missing_and_keeps_the_history(tmp_path: Any) -> None:
    engine = create_engine(v1_database(tmp_path))
    applied = migrate(engine)

    assert "events.source" in applied
    assert "events.slack_ts" in applied

    columns = {c["name"] for c in inspect(engine).get_columns("events")}
    assert {"source", "slack_ts", "slack_channel"} <= columns
    assert "ingest_records" in inspect(engine).get_table_names()

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT summary, source FROM events")).all()
    assert rows == [("v1 history", "agent")], "history preserved and provenance backfilled"


def test_migrating_twice_is_a_no_op(tmp_path: Any) -> None:
    engine = create_engine(v1_database(tmp_path))
    migrate(engine)
    assert migrate(engine) == [], "a current database must need no changes"


def test_a_hand_patched_database_still_gets_its_backfill(tmp_path: Any) -> None:
    """Regression: the backfill only ran when *this* run added the column.

    If `events.source` already existed but held NULLs — someone added it by hand, or a
    previous upgrade was interrupted after the ALTER — the backfill was skipped
    forever, leaving NULLs in a column the ORM types as non-optional.
    """
    url = v1_database(tmp_path)
    engine = create_engine(url)
    # Simulate the interrupted upgrade: the column exists, the backfill never ran.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN source VARCHAR(16)"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM events")).scalar() is None

    migrate(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM events")).scalar() == "agent"


def test_migration_creates_a_fresh_database_from_nothing(tmp_path: Any) -> None:
    engine = create_engine(f"sqlite:///{tmp_path}/new.db")
    migrate(engine)
    tables = set(inspect(engine).get_table_names())
    assert {t.name for t in Base.metadata.sorted_tables} <= tables
