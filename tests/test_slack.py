"""Slack must be a view, never a dependency.

The contract these tests pin down: an event is stored and returned to the caller
whether Slack succeeds, fails, times out, or is switched off entirely.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import app
from agent_relay.models.enums import EventType
from agent_relay.services.slack import SlackNotifier, format_event, redact
from tests.conftest import INTEGRATION_ENV, event_payload

WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxxxxxxSECRETxxxxxxxx"


class Recorder:
    """Stands in for Slack. Records calls, or fails on demand."""

    def __init__(self, behaviour: str = "ok") -> None:
        self.behaviour = behaviour
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any], **_: Any) -> httpx.Response:
        self.calls.append({"url": url, "json": json})
        if self.behaviour == "raise":
            raise httpx.ConnectError(f"connection refused to {url}")
        if self.behaviour == "timeout":
            raise httpx.ReadTimeout("slack took too long")
        if self.behaviour == "error":
            return httpx.Response(500, text="server_error", request=httpx.Request("POST", url))
        return httpx.Response(200, text="ok", request=httpx.Request("POST", url))


@pytest.fixture
def slack_client(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Recorder]]:
    """A relay with Slack 'configured', pointed at a recorder instead of the network."""
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)

    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    recorder = Recorder()
    monkeypatch.setattr(httpx.AsyncClient, "post", recorder.post, raising=False)

    with TestClient(app) as test_client:
        yield test_client, recorder

    reset_engine()
    get_settings.cache_clear()


def test_event_is_forwarded_to_slack(slack_client: tuple[TestClient, Recorder]) -> None:
    client, recorder = slack_client
    response = client.post("/events", json=event_payload())
    assert response.status_code == 201

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == WEBHOOK
    blocks = recorder.calls[0]["json"]["blocks"]
    rendered = str(blocks)
    assert "UPDATE" in rendered and "TETHER" in rendered and "GH-142" in rendered
    assert "leo-codex" in rendered
    # Never a raw JSON dump.
    assert "details_json" not in rendered
    assert "event_type" not in recorder.calls[0]["json"]["text"]


@pytest.mark.parametrize("behaviour", ["raise", "timeout", "error"])
def test_slack_failure_never_loses_the_event(
    slack_client: tuple[TestClient, Recorder], behaviour: str
) -> None:
    client, recorder = slack_client
    recorder.behaviour = behaviour

    response = client.post("/events", json=event_payload())
    assert response.status_code == 201, response.text
    assert response.json()["ref"] == "E-1"

    # Slack was attempted...
    assert len(recorder.calls) == 1
    # ...and the event is durably stored regardless.
    stored = client.get("/events", params={"project": "tether"}).json()
    assert len(stored) == 1
    assert stored[0]["summary"] == "Implemented H=8 and completed SCARED-C evaluation."


def test_claims_and_handoffs_reach_slack_too(slack_client: tuple[TestClient, Recorder]) -> None:
    client, recorder = slack_client
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-142",
            "summary": "Preprocessing complete.",
        },
    )
    client.post("/release", json={"agent": "andrea-agent", "project": "tether", "task": "GH-142"})

    posted = [str(call["json"]["text"]) for call in recorder.calls]
    assert any("CLAIM" in text for text in posted)
    assert any("HANDOFF" in text for text in posted)
    assert any("RELEASE" in text for text in posted)


def test_a_rejected_claim_is_not_announced(slack_client: tuple[TestClient, Recorder]) -> None:
    client, recorder = slack_client
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    recorder.calls.clear()

    conflict = client.post(
        "/claim", json={"agent": "niccolo-claude", "project": "tether", "task": "GH-142"}
    )
    assert conflict.status_code == 409
    assert recorder.calls == []


def test_slack_disabled_stores_events_and_posts_nothing(client: TestClient) -> None:
    """The default configuration: no webhook, everything else works."""
    assert client.get("/health").json()["integrations"]["slack"] == "disabled"

    response = client.post("/events", json=event_payload())
    assert response.status_code == 201
    assert len(client.get("/events").json()) == 1


def test_event_type_filter_limits_what_is_posted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("SLACK_EVENT_TYPES", "BLOCKED, DECISION")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    recorder = Recorder()
    monkeypatch.setattr(httpx.AsyncClient, "post", recorder.post, raising=False)

    with TestClient(app) as client:
        client.post("/events", json=event_payload(event_type="UPDATE"))
        assert recorder.calls == []
        client.post(
            "/events",
            json=event_payload(event_type="BLOCKED", summary="stuck", details={}, artifacts=[]),
        )
        assert len(recorder.calls) == 1
        assert len(client.get("/events").json()) == 2

    reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------- unit-level checks


def test_notifier_is_disabled_without_a_webhook() -> None:
    notifier = SlackNotifier(Settings(AGENT_RELAY_DB_URL="sqlite://"))
    assert notifier.enabled is False
    assert notifier.should_post("UPDATE") is False


def test_secrets_are_redacted_before_logging() -> None:
    message = f"connection refused to {WEBHOOK}"
    assert "SECRET" not in redact(message)
    assert "https://hooks.slack.com/<redacted>" in redact(message)


def test_every_event_type_renders_distinctly() -> None:
    seen: set[str] = set()
    for event_type in EventType:
        payload = format_event(
            {
                "event_type": event_type.value,
                "agent": "leo-codex",
                "project": "tether",
                "task": "GH-142",
                "summary": "something happened",
                "ref": "E-1",
            }
        )
        header = str(payload["blocks"][0]["text"]["text"])
        assert event_type.value in header
        glyph = header.split()[0]
        assert glyph not in seen, f"{event_type} reuses glyph {glyph}"
        seen.add(glyph)


def test_details_and_artifacts_render_as_readable_blocks() -> None:
    payload = format_event(
        {
            "event_type": "UPDATE",
            "agent": "leo-codex",
            "project": "tether",
            "task": "GH-142",
            "branch": "exp/temporal-ablation",
            "summary": "Implemented H=8.",
            "details": {"findings": ["EPE improved 0.7%"], "next": "test H=6"},
            "artifacts": ["runs/ablation_horizon.csv"],
            "ref": "E-1",
        },
        {"task": "https://github.com/acme/tether/issues/142"},
    )
    rendered = str(payload["blocks"])
    assert "*Findings*" in rendered
    assert "• EPE improved 0.7%" in rendered
    assert "*Next*" in rendered
    assert "runs/ablation_horizon.csv" in rendered
    assert "https://github.com/acme/tether/issues/142" in rendered
    assert "exp/temporal-ablation" in rendered


def test_long_lists_are_truncated_not_dumped() -> None:
    payload = format_event(
        {
            "event_type": "UPDATE",
            "agent": "leo-codex",
            "project": "tether",
            "summary": "many things",
            "details": {"completed": [f"item {i}" for i in range(50)]},
        }
    )
    rendered = str(payload["blocks"])
    assert "and 44 more" in rendered
    assert "item 49" not in rendered
