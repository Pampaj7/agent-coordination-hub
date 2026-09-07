"""MCP tools -> HTTP -> API -> database round trips.

Same trick as ``test_cli.py``: ``httpx.request`` is pointed at an in-process
TestClient, so these run against the real API without a live server.

The file splits in two. Everything that exercises the SDK-free core runs
unconditionally — that layer is where the logic lives, and it must stay testable on
a checkout that has not installed the optional ``mcp`` extra. The handful of tests
that need the SDK skip when it is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_relay.cli.client import RelayClient
from agent_relay.mcp import server as mcp_server


def _attr(obj: object, *names: str) -> object:
    """Read the first attribute that exists.

    The MCP SDK renamed its model fields from camelCase to snake_case in 2.x
    (`uriTemplate` -> `uri_template`, `inputSchema` -> `input_schema`). The server
    supports both SDK majors, so these tests must too.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"none of {names} on {type(obj).__name__}")


@pytest.fixture
def relay(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[RelayClient]:
    """A RelayClient whose HTTP calls land on the in-process app."""

    def dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.pop("timeout", None)
        return client.request(method, url, **kwargs)

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", dispatch)
    monkeypatch.setenv("AGENT_RELAY_URL", str(client.base_url))
    monkeypatch.setenv("AGENT_NAME", "leo-codex")
    monkeypatch.setenv("HUMAN_OWNER", "leonardo")
    yield RelayClient()


# --------------------------------------------------------------------- core: reading


def test_get_context_surfaces_what_an_agent_must_know(relay: RelayClient) -> None:
    mcp_server.tool_claim_task(relay, "tether", "GH-142", branch="exp/temporal-ablation")
    mcp_server.tool_post_update(
        relay, "tether", "Ablation done", task="GH-142", findings=["H=6 beats H=8"]
    )
    mcp_server.tool_post_question(
        relay, "tether", "Masking before or after resize?", to_agent="niccolo-claude"
    )
    blocked = mcp_server.tool_post_blocked(
        relay, "tether", "missing checkpoint", task="GH-151", needs=["depth encoder weights"]
    )
    assert blocked.ok

    result = mcp_server.tool_get_context(relay, "tether")
    assert result.ok
    assert "ACTIVE CLAIMS" in result.text and "GH-142" in result.text
    assert "BLOCKED" in result.text and "missing checkpoint" in result.text
    assert "UNRESOLVED QUESTIONS" in result.text
    assert "Masking before or after resize?" in result.text
    # The finding lives in details; a model should not have to call list_events for it.
    assert "RECENT FINDINGS" in result.text and "H=6 beats H=8" in result.text
    # The raw payload rides along for anything that wants structure, not prose.
    assert result.data["project"] == "tether"


def test_empty_context_says_so(relay: RelayClient) -> None:
    result = mcp_server.tool_get_context(relay, "nothing-here")
    assert result.ok
    assert "nothing recorded" in result.text


def test_coordination_summary_suggests_actions(relay: RelayClient) -> None:
    mcp_server.tool_post_blocked(relay, "tether", "missing checkpoint", task="GH-151")
    result = mcp_server.tool_coordination_summary(relay, "tether")
    assert result.ok
    assert "SUGGESTED ACTIONS" in result.text
    assert "unblock GH-151" in result.text


def test_list_tasks_is_compact_and_filterable(relay: RelayClient) -> None:
    mcp_server.tool_claim_task(relay, "tether", "GH-142", branch="exp/temporal-ablation")
    mcp_server.tool_post_blocked(relay, "tether", "missing checkpoint", task="GH-151")

    every = mcp_server.tool_list_tasks(relay, project="tether")
    assert "GH-142 [claimed] | owner=leo-codex (leonardo) | branch=exp/temporal-ablation" in (
        every.text
    )
    assert "BLOCKED" in every.text

    only_blocked = mcp_server.tool_list_tasks(relay, project="tether", status="blocked")
    assert "GH-151" in only_blocked.text
    assert "GH-142" not in only_blocked.text

    assert mcp_server.tool_list_tasks(relay, project="empty").text == "(no tasks match)"


def test_list_events_filters_by_type_and_keeps_details(relay: RelayClient) -> None:
    mcp_server.tool_claim_task(relay, "tether", "GH-142")
    mcp_server.tool_post_update(
        relay,
        "tether",
        "Finished H=8",
        task="GH-142",
        findings=["EPE improved 0.7%"],
        next_steps=["test H=6"],
        artifacts=["runs/ablation_horizon.csv"],
    )

    result = mcp_server.tool_list_events(relay, project="tether", event_type="update")
    assert result.ok
    assert "UPDATE" in result.text and "Finished H=8" in result.text
    assert "findings: EPE improved 0.7%" in result.text
    assert "next: test H=6" in result.text
    assert "artifacts: runs/ablation_horizon.csv" in result.text
    assert "CLAIM" not in result.text  # the filter really reached the API

    # Lower-case event types are accepted; an LLM should not have to remember casing.
    assert mcp_server.tool_list_events(relay, project="tether", event_type="UPDATE").text == (
        result.text
    )


# --------------------------------------------------------------------- core: writing


def test_post_update_creates_a_real_event(relay: RelayClient, client: TestClient) -> None:
    result = mcp_server.tool_post_update(
        relay,
        "tether",
        "Finished H=8 experiment",
        task="GH-142",
        branch="exp/temporal-ablation",
        findings=["EPE improved 0.7%"],
        next_steps=["test H=6"],
        artifacts=["runs/ablation_horizon.csv"],
    )
    assert result.ok, result.text

    stored = client.get("/events").json()
    assert len(stored) == 1
    event = stored[0]
    assert event["event_type"] == "UPDATE"
    assert event["agent"] == "leo-codex"  # from AGENT_NAME
    assert event["human_owner"] == "leonardo"  # from HUMAN_OWNER
    assert event["task"] == "GH-142"
    assert event["branch"] == "exp/temporal-ablation"
    assert event["details"]["findings"] == ["EPE improved 0.7%"]
    assert event["details"]["next"] == ["test H=6"]
    assert event["artifacts"] == ["runs/ablation_horizon.csv"]


def test_claim_then_release_cycle(relay: RelayClient, client: TestClient) -> None:
    claimed = mcp_server.tool_claim_task(relay, "tether", "GH-142", note="starting the ablation")
    assert claimed.ok
    assert "GH-142 claimed by leo-codex (leonardo)" in claimed.text
    assert client.get("/claims").json()[0]["agent"] == "leo-codex"

    released = mcp_server.tool_release_task(relay, "tether", "GH-142", summary="done for today")
    assert released.ok
    assert "released by leo-codex" in released.text
    assert client.get("/claims").json() == []


def test_conflicting_claim_names_the_owner_and_says_to_stop(
    relay: RelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mcp_server.tool_claim_task(relay, "tether", "GH-142").ok

    monkeypatch.setenv("AGENT_NAME", "niccolo-claude")
    result = mcp_server.tool_claim_task(relay, "tether", "GH-142")

    assert not result.ok
    assert "CLAIM CONFLICT" in result.text
    assert "current owner : leo-codex" in result.text
    assert "Do NOT work on GH-142" in result.text
    assert "post_question" in result.text
    assert result.data["current_owner"] == "leo-codex"


def test_question_then_answer_closes_the_question(relay: RelayClient) -> None:
    asked = mcp_server.tool_post_question(
        relay, "tether", "Masking before or after resize?", to_agent="niccolo-claude", task="GH-142"
    )
    assert asked.ok
    ref = asked.data["ref"]

    assert "UNRESOLVED QUESTIONS" in mcp_server.tool_get_context(relay, "tether").text

    answered = mcp_server.tool_post_answer(
        relay, "tether", "Before resize.", in_reply_to=ref, task="GH-142"
    )
    assert answered.ok

    assert "UNRESOLVED QUESTIONS" not in mcp_server.tool_get_context(relay, "tether").text


def test_handoff_moves_the_claim(relay: RelayClient, client: TestClient) -> None:
    mcp_server.tool_claim_task(relay, "tether", "GH-142")

    result = mcp_server.tool_handoff_task(
        relay,
        "tether",
        "GH-142",
        "andrea-agent",
        "Preprocessing complete",
        continue_from="82bd18f",
        inputs=["data/scared_processed/"],
        warnings=["Do not modify scripts/preprocess_scared.py until GH-150 finishes."],
    )
    assert result.ok, result.text
    assert "HANDOFF" in result.text
    assert "andrea-agent" in result.text

    assert client.get("/claims").json()[0]["agent"] == "andrea-agent"
    event = client.get("/events", params={"event_type": "HANDOFF"}).json()[0]
    assert event["details"]["continue_from"] == "82bd18f"
    assert event["details"]["inputs"] == ["data/scared_processed/"]


def test_post_decision_is_recorded(relay: RelayClient, client: TestClient) -> None:
    result = mcp_server.tool_post_decision(
        relay, "tether", "Standardise on UTC in the event log.", task="GH-142"
    )
    assert result.ok
    assert client.get("/events").json()[0]["event_type"] == "DECISION"


# --------------------------------------------------------------------- core: failures


def test_missing_agent_name_is_an_actionable_message_not_a_crash(
    relay: RelayClient, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.delenv("AGENT_NAME", raising=False)

    for result in (
        mcp_server.tool_post_update(relay, "tether", "x"),
        mcp_server.tool_claim_task(relay, "tether", "GH-142"),
        mcp_server.tool_handoff_task(relay, "tether", "GH-142", "andrea-agent", "x"),
    ):
        assert not result.ok
        assert "AGENT_NAME" in result.text
        assert "Do not invent a name" in result.text

    assert client.get("/events").json() == []  # nothing was written under a made-up name


def test_reads_still_work_without_an_agent_name(
    relay: RelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity gates writes only: reading context should never be blocked."""
    monkeypatch.delenv("AGENT_NAME", raising=False)
    assert mcp_server.tool_get_context(relay, "tether").ok


def test_unreachable_relay_gives_readable_text_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(method: str, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", refuse)
    monkeypatch.setenv("AGENT_RELAY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_NAME", "leo-codex")
    offline = RelayClient()

    for result in (
        mcp_server.tool_get_context(offline, "tether"),
        mcp_server.tool_post_update(offline, "tether", "x"),
    ):
        assert not result.ok
        assert "Cannot reach the relay" in result.text
        assert "agent-relay serve" in result.text
        assert "Traceback" not in result.text


def test_api_errors_come_back_with_their_detail(relay: RelayClient) -> None:
    result = mcp_server.tool_release_task(relay, "tether", "never-claimed")
    assert not result.ok
    assert "Relay error" in result.text
    assert "404" in result.text
    assert "never-claimed" in result.text


def test_unexpected_exceptions_are_still_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is a backstop for bugs, not just for RelayError."""

    def explode(method: str, url: str, **kwargs: Any) -> httpx.Response:
        raise ValueError("something nobody predicted")

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", explode)
    result = mcp_server.tool_get_context(RelayClient(), "tether")
    assert not result.ok
    assert "Unexpected ValueError" in result.text
    assert "something nobody predicted" in result.text


# --------------------------------------------------------------------- tool surface


def test_every_advertised_tool_exists_and_is_documented() -> None:
    """The docstrings become the descriptions the model reads, so they are required."""
    expected = {
        "get_context",
        "my_inbox",
        "claim_task",
        "release_task",
        "handoff_task",
        "post_update",
        "post_question",
        "post_answer",
        "post_blocked",
        "post_decision",
        "list_tasks",
        "list_events",
        "coordination_summary",
    }
    assert set(mcp_server.TOOL_NAMES) == expected
    for fn in mcp_server.TOOLS:
        assert (fn.__doc__ or "").strip(), f"{fn.__name__} has no description for the model"
        assert fn.__annotations__.get("return") == "str"


def test_importing_the_package_does_not_require_the_sdk() -> None:
    """``import agent_relay.mcp`` must stay free of the optional dependency."""
    import agent_relay.mcp as package

    assert callable(package.main)
    with pytest.raises(AttributeError):
        _ = package.does_not_exist  # type: ignore[attr-defined]


def test_missing_sdk_message_tells_you_how_to_install_it() -> None:
    assert "uv sync --extra mcp" in mcp_server.INSTALL_HINT


# --------------------------------------------------------------------- SDK-only tests


def test_server_builds_with_the_expected_tools_and_resources() -> None:
    pytest.importorskip("mcp", reason="optional extra: uv sync --extra mcp")

    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(mcp_server.TOOL_NAMES)
    assert all(tool.description for tool in tools)

    templates = asyncio.run(server.list_resource_templates())
    static = asyncio.run(server.list_resources())
    uris = {str(_attr(t, "uri_template", "uriTemplate")) for t in templates} | {
        str(r.uri) for r in static
    }
    assert "relay://context/{project}" in uris
    assert any(uri.startswith("relay://tasks") for uri in uris)


def test_tool_schemas_expose_the_arguments_a_model_needs() -> None:
    pytest.importorskip("mcp", reason="optional extra: uv sync --extra mcp")

    server = mcp_server.build_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    claim = _attr(tools["claim_task"], "input_schema", "inputSchema")
    assert set(claim["required"]) == {"project", "task"}
    assert set(claim["properties"]) == {"project", "task", "branch", "note"}

    handoff = _attr(tools["handoff_task"], "input_schema", "inputSchema")
    assert set(handoff["required"]) == {"project", "task", "to_agent", "summary"}
