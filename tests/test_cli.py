"""CLI -> HTTP -> API -> database round trips.

The CLI is wired to a real TestClient rather than a mock, so these tests fail if the
CLI and the API ever disagree about a payload shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_relay.cli.main import app as cli_app

runner = CliRunner()


@pytest.fixture
def cli(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Point ``httpx.request`` (used by the CLI client) at the in-process app."""

    def dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.pop("timeout", None)
        return client.request(method, url, **kwargs)

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", dispatch)
    monkeypatch.setenv("AGENT_RELAY_URL", str(client.base_url))
    monkeypatch.setenv("AGENT_NAME", "leo-codex")
    monkeypatch.setenv("HUMAN_OWNER", "leonardo")
    yield client


def run(*args: str) -> Any:
    return runner.invoke(cli_app, list(args))


def test_health_command(cli: TestClient) -> None:
    result = run("health")
    assert result.exit_code == 0, result.output
    assert "status   : ok" in result.output
    assert "slack    : disabled" in result.output


def test_post_update_reaches_the_database(cli: TestClient) -> None:
    result = run(
        "post",
        "update",
        "--project",
        "tether",
        "--task",
        "GH-142",
        "--branch",
        "exp/temporal-ablation",
        "--summary",
        "Finished H=8 experiment",
        "--detail",
        "findings=EPE improved 0.7%",
        "--detail",
        "findings=H>8 appears worse on DRENDS",
        "--artifact",
        "runs/ablation_horizon.csv",
        "--next",
        "test H=6",
    )
    assert result.exit_code == 0, result.output
    assert "UPDATE" in result.output

    stored = cli.get("/events").json()
    assert len(stored) == 1
    event = stored[0]
    assert event["agent"] == "leo-codex"  # from AGENT_NAME
    assert event["human_owner"] == "leonardo"  # from HUMAN_OWNER
    assert event["task"] == "GH-142"
    assert event["branch"] == "exp/temporal-ablation"
    assert event["summary"] == "Finished H=8 experiment"
    assert event["details"]["findings"] == [
        "EPE improved 0.7%",
        "H>8 appears worse on DRENDS",
    ]
    assert event["details"]["next"] == ["test H=6"]
    assert event["artifacts"] == ["runs/ablation_horizon.csv"]


def test_identity_comes_from_flags_or_env(cli: TestClient) -> None:
    assert (
        run(
            "post", "update", "--project", "tether", "--summary", "x", "--agent", "b-claude"
        ).exit_code
        == 0
    )
    assert cli.get("/events").json()[0]["agent"] == "b-claude"


def test_missing_identity_is_a_clear_error(
    cli: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_NAME", raising=False)
    result = run("post", "update", "--project", "tether", "--summary", "x")
    assert result.exit_code != 0
    assert "AGENT_NAME" in result.output


def test_claim_release_cycle(cli: TestClient) -> None:
    claimed = run(
        "claim", "--project", "tether", "--task", "GH-142", "--branch", "exp/temporal-ablation"
    )
    assert claimed.exit_code == 0, claimed.output
    assert "GH-142 claimed by leo-codex (leonardo)" in claimed.output
    assert cli.get("/claims").json()[0]["agent"] == "leo-codex"

    released = run(
        "release", "--project", "tether", "--task", "GH-142", "--summary", "done for today"
    )
    assert released.exit_code == 0, released.output
    assert cli.get("/claims").json() == []


def test_claim_collision_exits_nonzero_with_the_owner(cli: TestClient) -> None:
    assert run("claim", "--project", "tether", "--task", "GH-142").exit_code == 0

    result = run("claim", "--project", "tether", "--task", "GH-142", "--agent", "niccolo-claude")
    assert result.exit_code == 1
    assert "CLAIM CONFLICT" in result.output
    assert "current owner : leo-codex" in result.output
    assert "Coordinate with the owner" in result.output


def test_handoff_command(cli: TestClient) -> None:
    run("claim", "--project", "tether", "--task", "GH-142")
    result = run(
        "handoff",
        "--project",
        "tether",
        "--task",
        "GH-142",
        "--to",
        "andrea-agent",
        "--summary",
        "Preprocessing complete",
        "--continue-from",
        "82bd18f",
        "--input",
        "data/scared_processed/",
        "--warning",
        "Do not modify scripts/preprocess_scared.py until GH-150 finishes.",
    )
    assert result.exit_code == 0, result.output
    assert "HANDOFF" in result.output

    assert cli.get("/claims").json()[0]["agent"] == "andrea-agent"
    event = cli.get("/events", params={"event_type": "HANDOFF"}).json()[0]
    assert event["details"]["continue_from"] == "82bd18f"


def test_question_answer_flow(cli: TestClient) -> None:
    run(
        "post",
        "question",
        "--project",
        "tether",
        "--task",
        "GH-142",
        "--to",
        "niccolo-claude",
        "--summary",
        "Masking before or after resize?",
    )

    context = run("context", "--project", "tether")
    assert "UNRESOLVED QUESTIONS" in context.output
    assert "Q-1" in context.output

    run(
        "post",
        "answer",
        "--project",
        "tether",
        "--task",
        "GH-142",
        "--agent",
        "niccolo-claude",
        "--in-reply-to",
        "Q-1",
        "--summary",
        "Before resize.",
    )

    context = run("context", "--project", "tether")
    assert "UNRESOLVED QUESTIONS" not in context.output


def test_context_and_summary_render_for_humans(cli: TestClient) -> None:
    run("claim", "--project", "tether", "--task", "GH-142")
    run(
        "post",
        "update",
        "--project",
        "tether",
        "--task",
        "GH-142",
        "--summary",
        "Ablation done",
        "--detail",
        "findings=H=6 beats H=8",
    )
    run(
        "post",
        "blocked",
        "--project",
        "tether",
        "--task",
        "GH-151",
        "--agent",
        "andrea-agent",
        "--summary",
        "missing checkpoint",
        "--needs",
        "the depth encoder weights",
    )

    context = run("context", "--project", "tether")
    assert context.exit_code == 0, context.output
    assert "ACTIVE CLAIMS" in context.output
    assert "GH-142" in context.output
    assert "BLOCKED" in context.output

    summary = run("summary", "--project", "tether")
    assert summary.exit_code == 0, summary.output
    assert "SUGGESTED ACTIONS" in summary.output
    assert "unblock GH-151" in summary.output
    assert "H=6 beats H=8" in summary.output


def test_tasks_and_events_listing(cli: TestClient) -> None:
    run("claim", "--project", "tether", "--task", "GH-142")
    run("post", "update", "--project", "tether", "--task", "GH-142", "--summary", "working")

    tasks = run("tasks", "--project", "tether")
    assert "GH-142" in tasks.output and "claimed" in tasks.output and "leo-codex" in tasks.output

    events = run("events", "--project", "tether", "--type", "update")
    assert "working" in events.output
    assert "CLAIM" not in events.output


def test_json_output_is_machine_readable(cli: TestClient) -> None:
    run("post", "update", "--project", "tether", "--task", "GH-142", "--summary", "hello")
    result = run("context", "--project", "tether", "--json")
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["project"] == "tether"
    assert parsed["recent_updates"][0]["summary"] == "hello"


def test_empty_context_says_so_instead_of_crashing(cli: TestClient) -> None:
    result = run("context", "--project", "nothing-here")
    assert result.exit_code == 0
    assert "nothing recorded" in result.output


def test_unreachable_relay_gives_actionable_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(method: str, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", refuse)
    monkeypatch.setenv("AGENT_RELAY_URL", "http://127.0.0.1:9")
    result = run("health")
    assert result.exit_code == 1
    assert "Cannot reach the relay" in result.output
    assert "agent-relay serve" in result.output
