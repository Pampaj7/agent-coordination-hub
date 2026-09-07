"""The optional shared bearer token."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_relay.config import get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import app
from tests.conftest import INTEGRATION_ENV, event_payload

TOKEN = "s3cr3t-team-token"


@pytest.fixture
def secured(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(app) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


def test_auth_is_off_by_default(client: TestClient) -> None:
    assert client.post("/events", json=event_payload()).status_code == 201
    assert client.get("/events").status_code == 200


def test_requests_without_a_token_are_rejected(secured: TestClient) -> None:
    assert secured.get("/events").status_code == 401
    assert secured.post("/events", json=event_payload()).status_code == 401
    assert (
        secured.post(
            "/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"}
        ).status_code
        == 401
    )


def test_a_valid_token_works(secured: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert secured.post("/events", json=event_payload(), headers=headers).status_code == 201
    assert len(secured.get("/events", headers=headers).json()) == 1


def test_a_wrong_token_is_rejected(secured: TestClient) -> None:
    assert secured.get("/events", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert secured.get("/events", headers={"Authorization": TOKEN}).status_code == 401
    assert secured.get("/events", headers={"Authorization": "Basic xyz"}).status_code == 401


def test_health_stays_open_for_monitoring(secured: TestClient) -> None:
    response = secured.get("/health")
    assert response.status_code == 200
    assert response.json()["integrations"]["auth"] == "required"
    # The token itself is never echoed back.
    assert TOKEN not in response.text
