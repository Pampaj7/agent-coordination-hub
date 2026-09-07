"""The background scheduler.

The contract worth pinning down: every job is optional, a failing job never stops the
loop or its siblings, and nothing runs at all unless it was switched on.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from agent_relay.config import Settings
from agent_relay.services.scheduler import Scheduler


def settings_with(**overrides: Any) -> Settings:
    return Settings(AGENT_RELAY_DB_URL="sqlite://", **overrides)


def test_nothing_is_scheduled_by_default_except_the_sweeper() -> None:
    plan = Scheduler(settings_with())._job_plan()
    # The sweeper is cheap and always useful; polling and status cost money or noise,
    # so they stay off until asked for.
    assert set(plan) == {"sweep"}


def test_polling_and_status_switch_on_with_config() -> None:
    scheduler = Scheduler(
        settings_with(
            GITHUB_OWNER="acme",
            GITHUB_REPO="tether",
            GITHUB_POLL_INTERVAL_SECONDS=600,
            AGENT_RELAY_STATUS_INTERVAL_MINUTES=60,
            AGENT_RELAY_STATUS_PROJECTS="tether,drends",
        )
    )
    plan = scheduler._job_plan()
    assert plan["poll"] == 600
    assert plan["status"] == 3600
    assert scheduler.enabled is True


def test_status_needs_projects_not_just_an_interval() -> None:
    plan = Scheduler(settings_with(AGENT_RELAY_STATUS_INTERVAL_MINUTES=60))._job_plan()
    assert "status" not in plan, "an interval with no projects has nothing to say"


def test_sweeper_can_be_switched_off_entirely() -> None:
    assert Scheduler(settings_with(AGENT_RELAY_SWEEPER_INTERVAL_SECONDS=0)).enabled is False


@pytest.mark.anyio
async def test_jobs_run_only_when_due(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = Scheduler(settings_with(AGENT_RELAY_SWEEPER_INTERVAL_SECONDS=300))
    calls: list[str] = []

    async def fake_sweep() -> None:
        calls.append("sweep")

    monkeypatch.setattr(scheduler, "run_sweep", fake_sweep)

    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert await scheduler.tick(start) == ["sweep"]
    # Too soon.
    assert await scheduler.tick(start + dt.timedelta(seconds=60)) == []
    # Due again.
    assert await scheduler.tick(start + dt.timedelta(seconds=301)) == ["sweep"]
    assert calls == ["sweep", "sweep"]


@pytest.mark.anyio
async def test_a_failing_job_does_not_stop_the_others(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = Scheduler(
        settings_with(
            GITHUB_OWNER="acme",
            GITHUB_REPO="tether",
            GITHUB_POLL_INTERVAL_SECONDS=60,
        )
    )
    ran: list[str] = []

    async def boom() -> None:
        raise RuntimeError("the sweeper exploded")

    async def ok_poll() -> None:
        ran.append("poll")

    monkeypatch.setattr(scheduler, "run_sweep", boom)
    monkeypatch.setattr(scheduler, "run_poll", ok_poll)

    ran_jobs = await scheduler.tick()
    assert ran_jobs == ["poll"]  # the failure is reported by omission...
    assert ran == ["poll"]  # ...and its sibling still ran
    assert scheduler.runs["sweep"] == 0


@pytest.mark.anyio
async def test_a_missing_optional_module_is_survivable(client: Any) -> None:
    """Polling calls into github_ingest; if that ever fails to import, the loop lives."""
    scheduler = Scheduler(
        settings_with(GITHUB_OWNER="acme", GITHUB_REPO="tether", GITHUB_POLL_INTERVAL_SECONDS=60)
    )
    # Real call, no GitHub configured beyond owner/repo and no network: must not raise.
    assert await scheduler.tick() is not None


@pytest.mark.anyio
async def test_status_post_falls_back_to_a_log_when_slack_is_off(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = Scheduler(settings_with())
    with caplog.at_level("INFO"):
        await scheduler.post_status("tether", "Leo is on GH-142.")
    assert "Leo is on GH-142." in caplog.text


@pytest.mark.anyio
async def test_status_post_survives_slack_being_down(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("slack unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse, raising=False)
    scheduler = Scheduler(settings_with(SLACK_WEBHOOK_URL="https://hooks.slack.com/services/x/y/z"))
    await scheduler.post_status("tether", "brief")  # must not raise


@pytest.mark.anyio
async def test_start_and_stop_are_idempotent(client: Any) -> None:
    scheduler = Scheduler(settings_with())
    scheduler.start()
    scheduler.start()  # second call is a no-op, not a second task
    assert scheduler._task is not None
    await scheduler.stop()
    assert scheduler._task is None
    await scheduler.stop()  # stopping twice is safe


@pytest.mark.anyio
async def test_a_disabled_scheduler_never_starts(client: Any) -> None:
    scheduler = Scheduler(settings_with(AGENT_RELAY_SWEEPER_INTERVAL_SECONDS=0))
    scheduler.start()
    assert scheduler._task is None
