"""Test fixtures.

Two guarantees the whole suite depends on:

* No test needs Slack or GitHub credentials. Every integration env var is stripped
  and the working directory is moved to a tmp dir so a developer's real ``.env``
  can never leak in and make the suite pass (or fail) for the wrong reason.
* Each test gets its own SQLite file and a freshly built engine.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.main import app

INTEGRATION_ENV = (
    "SLACK_WEBHOOK_URL",
    "SLACK_EVENT_TYPES",
    "GITHUB_TOKEN",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "AGENT_RELAY_API_TOKEN",
    "AGENT_RELAY_URL",
    "AGENT_NAME",
    "HUMAN_OWNER",
)


@pytest.fixture
def settings(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")

    get_settings.cache_clear()
    reset_engine()
    resolved = get_settings()
    init_db(resolved)
    yield resolved
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(settings: Settings) -> Iterator[Session]:
    with session_scope() as session:
        yield session


# --------------------------------------------------------------------- helpers


def event_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "UPDATE",
        "agent": "leo-codex",
        "human_owner": "leonardo",
        "project": "tether",
        "task": "GH-142",
        "branch": "exp/temporal-ablation",
        "summary": "Implemented H=8 and completed SCARED-C evaluation.",
        "details": {"completed": ["implemented H=8"], "findings": ["EPE improved 0.7%"]},
        "artifacts": ["runs/ablation_horizon.csv"],
    }
    payload.update(overrides)
    return payload


def post_event(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/events", json=event_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def iso(offset_hours: float = 0.0) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(hours=offset_hours)).isoformat()
