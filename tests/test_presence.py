"""Agent presence and stale-claim hygiene.

The contract these tests pin down: presence is derived from heartbeats alone and is
reproducible arithmetic (never a sleep), and the relay reports stale claims by
default but takes a task away from an agent only when explicitly configured to.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from agent_relay.api.routes_presence import router as presence_router
from agent_relay.config import Settings, get_settings
from agent_relay.db.models import Agent
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.main import app
from agent_relay.services import presence as presence_service
from tests.conftest import INTEGRATION_ENV, post_event

#: Env the relay reads for these features. A developer's real values must not decide
#: whether the auto-release tests pass.
PRESENCE_ENV = (
    "AGENT_RELAY_CLAIM_STALE_HOURS",
    "AGENT_RELAY_CLAIM_EXPIRY_HOURS",
    "AGENT_RELAY_HEARTBEAT_ONLINE_SECONDS",
    "AGENT_RELAY_HEARTBEAT_IDLE_SECONDS",
)


def mount_presence() -> None:
    """Mount the presence router if main.py has not wired it in yet. Idempotent."""
    if not any(getattr(route, "path", None) == "/heartbeat" for route in app.routes):
        app.include_router(presence_router)


@pytest.fixture(autouse=True)
def _clean_presence_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PRESENCE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def relay(client: TestClient) -> TestClient:
    mount_presence()
    return client


@pytest.fixture
def expiring(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Session]]:
    """A relay configured to actually take stale tasks back."""
    monkeypatch.chdir(tmp_path)
    for name in (*INTEGRATION_ENV, *PRESENCE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_CLAIM_STALE_HOURS", "1")
    monkeypatch.setenv("AGENT_RELAY_CLAIM_EXPIRY_HOURS", "2")

    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    mount_presence()
    with TestClient(app) as test_client, session_scope() as session:
        yield test_client, session
    reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------------- helpers


def ago(hours: float) -> dt.datetime:
    """Naive UTC, the shape the UTCDateTime column stores, for raw SQL backdating."""
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).replace(tzinfo=None)


def backdate_claim(session: Session, task: str, hours: float) -> None:
    session.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = :task AND active = 1"),
        {"when": ago(hours), "task": task},
    )
    session.commit()


def backdate_heartbeat(session: Session, agent: str, hours: float) -> None:
    session.execute(
        text("UPDATE agents SET last_heartbeat_at = :when WHERE name = :agent"),
        {"when": ago(hours), "agent": agent},
    )
    session.commit()


def heartbeat(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"agent": "leo-codex", "project": "tether"}
    payload.update(overrides)
    response = client.post("/heartbeat", json=payload)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def claim(client: TestClient, agent: str = "leo-codex", task: str = "GH-142") -> None:
    response = client.post("/claim", json={"agent": agent, "project": "tether", "task": task})
    assert response.status_code == 200, response.text


def agents(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/agents", params=params)
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()
    return rows


def stale(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/claims/stale", params=params)
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()
    return rows


def sweep(client: TestClient) -> dict[str, Any]:
    response = client.post("/claims/sweep")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------- heartbeat


def test_heartbeat_registers_an_unknown_agent(relay: TestClient) -> None:
    body = heartbeat(
        relay,
        human_owner="leonardo",
        status_note="running the horizon sweep",
        host="workstation-1",
        pid=4242,
        version="0.2.0",
    )
    assert body["agent"] == "leo-codex"
    assert body["status"] == "online"
    assert body["human_owner"] == "leonardo"
    assert body["host"] == "workstation-1"
    assert body["pid"] == 4242
    assert body["seconds_since_heartbeat"] < 5

    assert [a["agent"] for a in agents(relay)] == ["leo-codex"]


def test_a_bare_heartbeat_does_not_clobber_what_an_earlier_one_recorded(relay: TestClient) -> None:
    heartbeat(relay, host="workstation-1", pid=4242, version="0.2.0", status_note="sweeping")

    body = heartbeat(relay)  # just "I am alive"

    assert body["host"] == "workstation-1"
    assert body["pid"] == 4242
    assert body["version"] == "0.2.0"
    assert body["status_note"] == "sweeping"


def test_heartbeat_updates_the_fields_it_does_send(relay: TestClient) -> None:
    heartbeat(relay, status_note="sweeping", task="GH-142")
    body = heartbeat(relay, status_note="writing up", task="GH-150")
    assert body["status_note"] == "writing up"
    assert body["current_task"] == "GH-150"


def test_heartbeat_rejects_unknown_fields(relay: TestClient) -> None:
    response = relay.post("/heartbeat", json={"agent": "leo-codex", "mood": "confident"})
    assert response.status_code == 422


# --------------------------------------------------------------------- status rules


def test_presence_status_is_a_pure_threshold_function(settings: Settings) -> None:
    now = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)

    def status_at(minutes_ago: float) -> str:
        row = Agent(name="leo-codex", last_heartbeat_at=now - dt.timedelta(minutes=minutes_ago))
        return presence_service.presence_status(row, settings, now)

    # defaults: online <= 300s, idle <= 1800s.
    assert status_at(0) == "online"
    assert status_at(4) == "online"
    assert status_at(6) == "idle"
    assert status_at(29) == "idle"
    assert status_at(31) == "offline"
    assert status_at(60 * 24) == "offline"


def test_an_agent_that_never_heartbeats_is_unknown_not_offline(
    relay: TestClient, settings: Settings
) -> None:
    """Never having heartbeated is missing evidence, not evidence of death."""
    post_event(relay, agent="niccolo-claude")  # registers the agent, no heartbeat

    row = agents(relay)[0]
    assert row["agent"] == "niccolo-claude"
    assert row["status"] == "unknown"
    assert row["last_heartbeat_at"] is None
    assert row["seconds_since_heartbeat"] is None
    assert row["last_seen_at"] is not None

    transient = Agent(name="niccolo-claude")
    assert presence_service.presence_status(transient, settings) == "unknown"


# --------------------------------------------------------------------- GET /agents


def test_agents_lists_claims_and_orders_the_live_ones_first(relay: TestClient, db: Session) -> None:
    claim(relay, agent="leo-codex", task="GH-142")
    claim(relay, agent="leo-codex", task="GH-150")
    claim(relay, agent="niccolo-claude", task="GH-138")
    heartbeat(relay, agent="leo-codex")
    heartbeat(relay, agent="niccolo-claude")
    backdate_heartbeat(db, "niccolo-claude", hours=6)

    rows = agents(relay)
    assert [r["agent"] for r in rows] == ["leo-codex", "niccolo-claude"]
    assert rows[0]["status"] == "online"
    assert rows[0]["active_claims"] == ["GH-142", "GH-150"]
    assert rows[1]["status"] == "offline"
    assert rows[1]["active_claims"] == ["GH-138"]


def test_agents_filters_by_status(relay: TestClient, db: Session) -> None:
    heartbeat(relay, agent="leo-codex")
    heartbeat(relay, agent="niccolo-claude")
    post_event(relay, agent="andrea-agent")
    backdate_heartbeat(db, "niccolo-claude", hours=6)

    assert [r["agent"] for r in agents(relay, status="online")] == ["leo-codex"]
    assert [r["agent"] for r in agents(relay, status="offline")] == ["niccolo-claude"]
    assert [r["agent"] for r in agents(relay, status="unknown")] == ["andrea-agent"]
    assert agents(relay, status="idle") == []
    assert relay.get("/agents", params={"status": "sleepy"}).status_code == 422


def test_agents_filters_by_project(relay: TestClient) -> None:
    heartbeat(relay, agent="leo-codex", project="tether")
    heartbeat(relay, agent="niccolo-claude", project="drends")

    assert [r["agent"] for r in agents(relay, project="tether")] == ["leo-codex"]
    assert [r["agent"] for r in agents(relay, project="drends")] == ["niccolo-claude"]
    assert len(agents(relay)) == 2


def test_an_agent_holding_a_claim_shows_up_in_that_project(relay: TestClient) -> None:
    """Moved on to another project but still holding a task here: exactly who to chase."""
    claim(relay, agent="leo-codex", task="GH-142")  # claim lives in tether
    heartbeat(relay, agent="leo-codex", project="drends")

    row = agents(relay, project="tether")[0]
    assert row["agent"] == "leo-codex"
    assert row["project"] == "drends"
    assert row["active_claims"] == ["GH-142"]


# --------------------------------------------------------------------- stale claims


def test_stale_detection_respects_the_configured_threshold(
    relay: TestClient, db: Session, settings: Settings
) -> None:
    claim(relay)
    assert stale(relay) == []

    backdate_claim(db, "GH-142", hours=48)
    rows = stale(relay)
    assert [r["task"] for r in rows] == ["GH-142"]
    assert rows[0]["idle_hours"] == pytest.approx(48.0, abs=0.2)
    assert rows[0]["owner_status"] == "unknown"
    assert rows[0]["owner_offline"] is False  # never heartbeated: unknown, not gone

    # The service takes the threshold from settings, not from a hardcoded 24h.
    settings.claim_stale_hours = 72.0
    with session_scope() as session:
        assert presence_service.stale_claims(session, settings) == []


def test_a_stale_claim_held_by_a_silent_agent_is_flagged_offline(
    relay: TestClient, db: Session
) -> None:
    claim(relay)
    heartbeat(relay, task="GH-142")
    backdate_claim(db, "GH-142", hours=48)
    backdate_heartbeat(db, "leo-codex", hours=48)

    row = stale(relay)[0]
    assert row["owner_status"] == "offline"
    assert row["owner_offline"] is True
    assert row["agent"] == "leo-codex"


def test_stale_claims_filter_by_project(relay: TestClient, db: Session) -> None:
    claim(relay, task="GH-142")
    assert (
        relay.post(
            "/claim", json={"agent": "leo-codex", "project": "drends", "task": "GH-900"}
        ).status_code
        == 200
    )
    backdate_claim(db, "GH-142", hours=48)
    backdate_claim(db, "GH-900", hours=48)

    assert [r["task"] for r in stale(relay, project="tether")] == ["GH-142"]
    assert [r["task"] for r in stale(relay, project="drends")] == ["GH-900"]
    assert len(stale(relay)) == 2


def test_a_heartbeat_on_a_claimed_task_keeps_the_claim_fresh(
    relay: TestClient, db: Session
) -> None:
    """A long quiet run must not look abandoned while the agent is still pinging."""
    claim(relay)
    backdate_claim(db, "GH-142", hours=48)
    assert stale(relay) != []

    heartbeat(relay, agent="leo-codex", project="tether", task="GH-142")
    assert stale(relay) == []


def test_a_heartbeat_does_not_refresh_someone_elses_claim(relay: TestClient, db: Session) -> None:
    claim(relay, agent="leo-codex", task="GH-142")
    backdate_claim(db, "GH-142", hours=48)

    heartbeat(relay, agent="niccolo-claude", project="tether", task="GH-142")
    assert [r["task"] for r in stale(relay)] == ["GH-142"]


# --------------------------------------------------------------------- sweep


def test_sweep_reports_but_releases_nothing_by_default(relay: TestClient, db: Session) -> None:
    claim(relay)
    backdate_claim(db, "GH-142", hours=48)

    report = sweep(relay)
    assert report["auto_release_enabled"] is False
    assert report["released"] == []
    assert [c["task"] for c in report["still_stale"]] == ["GH-142"]
    assert report["errors"] == []

    # The claim is untouched: the task is still owned.
    assert [c["task"] for c in relay.get("/claims").json()] == ["GH-142"]


def test_sweep_of_a_healthy_project_is_empty(relay: TestClient) -> None:
    claim(relay)
    report = sweep(relay)
    assert report["released"] == []
    assert report["still_stale"] == []


def test_sweep_releases_an_expired_claim_and_frees_the_task(
    expiring: tuple[TestClient, Session],
) -> None:
    client, db = expiring
    claim(client)
    heartbeat(client, task="GH-142")
    backdate_claim(db, "GH-142", hours=72)
    backdate_heartbeat(db, "leo-codex", hours=72)

    report = sweep(client)
    assert report["auto_release_enabled"] is True
    assert [c["task"] for c in report["released"]] == ["GH-142"]
    assert report["still_stale"] == []

    assert client.get("/claims").json() == []
    # And the whole point: someone else can now pick the task up.
    response = client.post(
        "/claim", json={"agent": "niccolo-claude", "project": "tether", "task": "GH-142"}
    )
    assert response.status_code == 200, response.text


def test_auto_release_is_recorded_as_machine_made(expiring: tuple[TestClient, Session]) -> None:
    client, db = expiring
    claim(client)
    heartbeat(client, task="GH-142")
    backdate_claim(db, "GH-142", hours=72)
    backdate_heartbeat(db, "leo-codex", hours=72)
    sweep(client)

    events = client.get("/events", params={"event_type": "RELEASE"}).json()
    assert len(events) == 1
    release = events[0]
    assert release["agent"] == "relay"
    assert release["metadata"]["auto_released"] is True
    assert release["metadata"]["previous_owner"] == "leo-codex"
    assert release["metadata"]["idle_hours"] == pytest.approx(72.0, abs=0.2)
    assert release["summary"].startswith("Auto-released after 72.0h idle (agent leo-codex offline)")


def test_a_claim_idle_past_stale_but_not_past_expiry_is_only_reported(
    expiring: tuple[TestClient, Session],
) -> None:
    """stale_hours=1 says "tell a human"; expiry_hours=2 says "you may take it"."""
    client, db = expiring
    claim(client)
    backdate_claim(db, "GH-142", hours=1.5)

    report = sweep(client)
    assert report["released"] == []
    assert [c["task"] for c in report["still_stale"]] == ["GH-142"]
    assert [c["task"] for c in client.get("/claims").json()] == ["GH-142"]


def test_sweep_is_idempotent(expiring: tuple[TestClient, Session]) -> None:
    client, db = expiring
    claim(client)
    backdate_claim(db, "GH-142", hours=72)

    assert len(sweep(client)["released"]) == 1
    second = sweep(client)
    assert second["released"] == []
    assert second["still_stale"] == []
    assert len(client.get("/events", params={"event_type": "RELEASE"}).json()) == 1


def test_auto_release_uses_exact_idle_time_not_the_rounded_display_value(
    expiring: tuple[TestClient, Session],
) -> None:
    """Regression: the release decision must not be made on a rounded number.

    `idle_hours` is rounded to one decimal for display. Comparing that against the
    expiry threshold moves the real boundary by up to three minutes, and makes any
    expiry under ~6 minutes unreachable — a claim would be reported stale forever and
    never actually released.
    """
    client, db = expiring  # stale after 1h, expiry after 2h

    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})

    # 2h01m idle: past the 2h expiry, but rounds DOWN to 2.0h.
    idle_since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2, minutes=1)).replace(tzinfo=None)
    db.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = 'GH-142'"),
        {"when": idle_since},
    )
    db.commit()

    stale = client.get("/claims/stale").json()
    assert len(stale) == 1
    assert stale[0]["idle_hours"] == 2.0  # what a human reads
    assert stale[0]["idle_seconds"] > 2 * 3600  # what the decision uses

    report = client.post("/claims/sweep", json={}).json()
    assert [c["task"] for c in report["released"]] == ["GH-142"]
    assert report["still_stale"] == []
    assert client.get("/claims").json() == [], "the task must be free for someone else"


def test_a_claim_just_under_the_expiry_is_not_released(
    expiring: tuple[TestClient, Session],
) -> None:
    """The other side of the boundary: 1h59m must survive a 2h expiry."""
    client, db = expiring

    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    idle_since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1, minutes=59)).replace(tzinfo=None)
    db.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = 'GH-142'"),
        {"when": idle_since},
    )
    db.commit()

    report = client.post("/claims/sweep", json={}).json()
    assert report["released"] == []
    assert [c["task"] for c in report["still_stale"]] == ["GH-142"]
    assert client.get("/claims").json()[0]["agent"] == "leo-codex"
