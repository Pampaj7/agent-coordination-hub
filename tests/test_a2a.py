"""A2A discovery and message ingestion.

The promise is narrow and must stay honest: another framework's agent can find this
relay at ``/.well-known/agent.json`` and send it one of three instructions. Anything
else gets a well-formed refusal — never a guess, and never a bare 500, because a peer
agent's only contract is the JSON-RPC envelope.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_relay import __version__
from agent_relay.api.routes_v2 import (
    a2a_router,
    a2a_rpc_router,
    experiments_router,
    overview_router,
)
from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import create_app
from agent_relay.services.a2a import parse_intent
from tests.conftest import INTEGRATION_ENV, post_event

TOKEN = "s3cr3t-team-token"


def v2_app() -> FastAPI:
    """The relay plus the V2 routers (``main.create_app`` is owned elsewhere)."""
    app = create_app()
    existing = {getattr(route, "path", None) for route in app.routes}
    for router in (experiments_router, overview_router, a2a_router, a2a_rpc_router):
        if not any(getattr(route, "path", None) in existing for route in router.routes):
            app.include_router(router)
    return app


@pytest.fixture
def v2_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(v2_app()) as client:
        yield client


@pytest.fixture
def secured_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A relay behind the shared bearer token, with a public base URL configured."""
    monkeypatch.chdir(tmp_path)
    for name in (*INTEGRATION_ENV, "AGENT_RELAY_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_API_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_RELAY_PUBLIC_URL", "https://relay.example.com/")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(v2_app()) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


def send(client: TestClient, text: str, **extra: Any) -> dict[str, Any]:
    """One ``message/send`` call, shaped the way an A2A client shapes it."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "m-1",
                "parts": [{"kind": "text", "text": text}],
            },
            **extra,
        },
    }
    response = client.post("/a2a", json=payload)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def result_text(body: dict[str, Any]) -> str:
    assert "error" not in body, body
    parts = body["result"]["parts"]
    return "\n".join(part["text"] for part in parts if part["kind"] == "text")


# ------------------------------------------------------------------ agent card


def test_the_agent_card_is_served_and_lists_the_skills(v2_client: TestClient) -> None:
    response = v2_client.get("/.well-known/agent.json")
    assert response.status_code == 200
    card = response.json()  # valid JSON or this raises

    assert card["name"] == "agent-relay"
    assert card["version"] == __version__
    assert card["defaultInputModes"] == ["text"]
    assert card["defaultOutputModes"] == ["text"]
    assert {skill["id"] for skill in card["skills"]} == {
        "post_event",
        "get_context",
        "claim_task",
        "coordination_summary",
    }
    for skill in card["skills"]:
        assert skill["name"] and skill["description"]
        assert skill["tags"] and skill["examples"]


def test_the_card_does_not_overclaim(v2_client: TestClient) -> None:
    """Streaming and push notifications are not implemented, so they must read false."""
    card = v2_client.get("/.well-known/agent.json").json()
    assert card["capabilities"]["streaming"] is False
    assert card["capabilities"]["pushNotifications"] is False
    assert card["x-supported-methods"] == ["message/send"]


def test_the_card_uses_the_public_base_url(secured_client: TestClient) -> None:
    card = secured_client.get("/.well-known/agent.json").json()
    assert card["url"] == "https://relay.example.com/a2a"


# --------------------------------------------------------------- message/send


def test_context_for_a_project_returns_context_derived_text(v2_client: TestClient) -> None:
    post_event(v2_client, project="tether", task="GH-142", summary="H=8 ablation finished")
    v2_client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})

    text = result_text(send(v2_client, "context for tether"))
    assert "tether" in text
    assert "GH-142" in text
    assert "leo-codex" in text
    assert "H=8 ablation finished" in text


def test_listing_tasks_returns_the_task_rows(v2_client: TestClient) -> None:
    post_event(v2_client, project="tether", task="GH-142")
    text = result_text(send(v2_client, "list tasks for tether"))
    assert "GH-142" in text


def test_posting_an_update_creates_a_real_event(v2_client: TestClient) -> None:
    body = send(
        v2_client,
        "post update for tether task GH-142: retrained with the new split",
        metadata={"agent": "peer-agent", "human_owner": "leonardo"},
    )
    assert body["result"]["metadata"]["intent"] == "post_update"

    events = v2_client.get("/events", params={"project": "tether"}).json()
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "UPDATE"
    assert event["agent"] == "peer-agent"
    assert event["human_owner"] == "leonardo"
    assert event["task"] == "GH-142"
    assert event["summary"] == "retrained with the new split"
    assert event["metadata"] == {"source": "a2a"}
    assert event["ref"] in result_text(body)


def test_an_anonymous_update_is_attributed_honestly(v2_client: TestClient) -> None:
    send(v2_client, "post update for tether: switched to the new split")
    events = v2_client.get("/events").json()
    assert events[0]["agent"] == "a2a-client"


def test_an_unsupported_instruction_lists_what_is_supported(v2_client: TestClient) -> None:
    body = send(v2_client, "please retrain the model and deploy it to production")
    assert body["result"]["metadata"]["intent"] == "unsupported"
    text = result_text(body)
    assert "context for <project>" in text
    assert "post update for <project>" in text


def test_an_empty_message_is_refused_politely(v2_client: TestClient) -> None:
    body = send(v2_client, "   ")
    assert body["result"]["metadata"]["intent"] == "unsupported"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("context for tether", ("get_context", "tether")),
        ("what is the status of drends", ("get_context", "drends")),
        ("summary tether", ("get_context", "tether")),
        ("list tasks in tether", ("list_tasks", "tether")),
        ("tasks drends", ("list_tasks", "drends")),
        ("post update for tether: done", ("post_update", "tether")),
        ("log an update on drends: done", ("post_update", "drends")),
        ("delete everything", ("unsupported", None)),
        ("context", ("unsupported", None)),
    ],
)
def test_intent_parsing_is_deterministic(text: str, expected: tuple[str, str | None]) -> None:
    intent = parse_intent(text)
    assert (intent.name, intent.project) == expected


# ----------------------------------------------------------- jsonrpc envelope


def test_an_unknown_method_is_a_jsonrpc_error_not_an_http_error(v2_client: TestClient) -> None:
    response = v2_client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 7, "method": "tasks/get", "params": {}}
    )
    assert response.status_code == 200  # JSON-RPC carries its own errors
    body = response.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert body["error"]["data"]["supported"] == ["message/send"]


def test_malformed_json_is_a_parse_error_not_a_500(v2_client: TestClient) -> None:
    response = v2_client.post(
        "/a2a", content=b"{not json at all", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32700


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "method": "message/send"},  # no jsonrpc version
        {"jsonrpc": "2.0", "id": 1},  # no method
        {"jsonrpc": "1.0", "id": 1, "method": "message/send"},
        [1, 2, 3],  # not an object
    ],
)
def test_invalid_envelopes_return_invalid_request(v2_client: TestClient, payload: Any) -> None:
    body = v2_client.post("/a2a", json=payload).json()
    assert body["error"]["code"] == -32600


def test_non_object_params_return_invalid_params(v2_client: TestClient) -> None:
    body = v2_client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": "tether"}
    ).json()
    assert body["error"]["code"] == -32602


# ------------------------------------------------------------------------ auth


def test_discovery_stays_open_but_the_rpc_endpoint_does_not(secured_client: TestClient) -> None:
    assert secured_client.get("/.well-known/agent.json").status_code == 200

    call = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}
    assert secured_client.post("/a2a", json=call).status_code == 401

    authorised = secured_client.post(
        "/a2a", json=call, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert authorised.status_code == 200
    assert authorised.json()["result"]["metadata"]["intent"] == "unsupported"


def test_the_card_never_leaks_the_token(secured_client: TestClient) -> None:
    assert TOKEN not in secured_client.get("/.well-known/agent.json").text
