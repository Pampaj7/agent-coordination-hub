"""Guards on how much work a request does.

These are not micro-benchmarks — they pin *structural* properties that are easy to
regress silently and that nothing else would catch. `/context` and `/tasks` are polled
every few seconds by the TUI and the web dashboard, so an extra full-log scan is not a
rounding error: it is a doubling, on the hottest path, discovered months later.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from agent_relay.services import context as context_service
from agent_relay.services import coordination as coordination_service
from agent_relay.services import state as state_service
from tests.conftest import post_event


class ScanCounter:
    """Counts SELECTs against the events table for the duration of a block."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.statements: list[str] = []

    def __enter__(self) -> ScanCounter:
        sa_event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        sa_event.remove(self.engine, "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, *args: Any, **kwargs: Any) -> None:
        self.statements.append(" ".join(statement.split()))

    @property
    def event_scans(self) -> int:
        """SELECTs that read the whole events table, i.e. no id filter."""
        return sum(
            1
            for s in self.statements
            if s.upper().startswith("SELECT") and " FROM events" in s and "events.id = " not in s
        )


def seed(client: Any, count: int = 12) -> None:
    for index in range(count):
        post_event(client, summary=f"step {index}")
    post_event(
        client,
        event_type="BLOCKED",
        task="GH-151",
        summary="missing checkpoint",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        event_type="QUESTION",
        target_agent="niccolo-claude",
        summary="before or after resize?",
        details={},
        artifacts=[],
    )


def test_context_reads_the_event_log_exactly_once(client: Any, db: Session) -> None:
    """Regression: /context scanned the whole log twice.

    It needs both the raw events and the derived task view, and `build_tasks` used to
    re-read the log for itself — doubling the cost of the most-polled endpoint in the
    system. The log is now read once and handed over.
    """
    seed(client)
    with ScanCounter(db.get_bind()) as counter:
        context_service.build_context(db, "tether", window_hours=72, limit=10)
    assert counter.event_scans == 1, (
        f"expected one pass over the log, saw {counter.event_scans}:\n"
        + "\n".join(counter.statements)
    )


def test_tasks_reads_the_event_log_exactly_once(client: Any, db: Session) -> None:
    seed(client)
    with ScanCounter(db.get_bind()) as counter:
        state_service.build_tasks(db, project="tether")
    assert counter.event_scans == 1


def test_summary_reads_the_event_log_exactly_once(client: Any, db: Session) -> None:
    seed(client)
    with ScanCounter(db.get_bind()) as counter:
        coordination_service.build_summary(db, "tether", window_hours=72)
    assert counter.event_scans == 1


def test_the_task_fold_does_not_load_the_json_payloads(client: Any, db: Session) -> None:
    """The fold reads no JSON column, and at scale those are most of the bytes."""
    seed(client)
    with ScanCounter(db.get_bind()) as counter:
        state_service.build_tasks(db, project="tether")
    scan = next(s for s in counter.statements if " FROM events" in s)
    for column in ("events.artifacts_json", "events.metadata_json", "events.details_json"):
        assert column not in scan, f"{column} should be deferred for the task fold"


def test_deferred_columns_are_still_reachable(client: Any, db: Session) -> None:
    """Deferred, not dropped: a caller that touches one still gets it."""
    post_event(client, summary="with payload", details={"findings": ["x"]}, artifacts=["a.csv"])
    events = state_service.fetch_events(db, project="tether", defer_payloads=True)
    assert events[0].details_json == {"findings": ["x"]}
    assert events[0].artifacts_json == ["a.csv"]
