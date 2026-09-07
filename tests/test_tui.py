"""The live dashboard, driven without a terminal.

Two things are load-bearing here and worth stating out loud:

* The CLI is wired to a real in-process API (same trick as ``test_cli.py``), so these
  tests fail if the dashboard and the endpoints ever disagree about a payload shape.
* Every request the dashboard makes is recorded, so "read-only" is asserted rather
  than assumed. A dashboard that refreshes unattended must never write.
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from rich.console import Console, RenderableType
from typer.testing import CliRunner

from agent_relay.cli import tui
from agent_relay.cli.client import RelayClient
from agent_relay.cli.main import app as cli_app
from agent_relay.models.enums import EVENT_LABELS, EventType

from .conftest import event_payload

runner = CliRunner()

#: Requests the CLI made, as ``(method, path)``. Reset per test by the fixture.
CALLS: list[tuple[str, str]] = []


@pytest.fixture
def cli(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Point ``httpx.request`` (used by the CLI client) at the in-process app."""
    CALLS.clear()

    def dispatch(method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.pop("timeout", None)
        CALLS.append((method.upper(), httpx.URL(url).path))
        return client.request(method, url, **kwargs)

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", dispatch)
    monkeypatch.setenv("AGENT_RELAY_URL", str(client.base_url))
    monkeypatch.setenv("AGENT_NAME", "leo-codex")
    monkeypatch.setenv("HUMAN_OWNER", "leonardo")
    # Rich sizes itself from COLUMNS when stdout is not a terminal; pin it so the
    # assertions below do not depend on the developer's window.
    monkeypatch.setenv("COLUMNS", "140")
    yield client


def run(*args: str) -> Any:
    return runner.invoke(cli_app, list(args))


def text_of(renderable: RenderableType, width: int = 120) -> str:
    """Render to a string with no terminal involved."""
    buffer = io.StringIO()
    Console(file=buffer, width=width, force_terminal=False).print(renderable)
    return buffer.getvalue()


def seed(client: TestClient) -> str:
    """A relay with everything the dashboard is supposed to surface. Returns the question ref."""
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    client.post("/events", json=event_payload(summary="Ablation done"))
    client.post(
        "/events",
        json=event_payload(
            event_type="BLOCKED",
            agent="andrea-agent",
            task="GH-151",
            summary="missing depth encoder checkpoint",
        ),
    )
    question = client.post(
        "/events",
        json=event_payload(
            event_type="QUESTION",
            task="GH-142",
            target_agent="niccolo-claude",
            summary="Masking before or after resize?",
        ),
    )
    client.post("/heartbeat", json={"agent": "leo-codex", "human_owner": "leonardo"})
    return str(question.json()["ref"])


# --- end to end -------------------------------------------------------------


def test_once_renders_everything_that_needs_a_human(cli: TestClient) -> None:
    question_ref = seed(cli)
    result = run("tui", "--once", "--project", "tether")
    assert result.exit_code == 0, result.output
    assert "GH-142" in result.output  # the claimed task
    assert "missing depth encoder checkpoint" in result.output  # the blocked reason
    assert question_ref.startswith("Q-")
    assert question_ref in result.output  # the open question ref
    assert "andrea-agent" in result.output  # who reported the block
    assert "leo-codex" in result.output  # who is online


def test_once_without_a_project_focuses_the_busiest_one(cli: TestClient) -> None:
    seed(cli)
    result = run("tui", "--once")
    assert result.exit_code == 0, result.output
    assert "all projects" in result.output
    assert "tether" in result.output


def test_once_against_an_empty_relay_shows_empty_states(cli: TestClient) -> None:
    result = run("tui", "--once")
    assert result.exit_code == 0, result.output
    assert "No blocked tasks" in result.output
    assert "No open questions" in result.output
    assert "No agents have checked in" in result.output
    assert "No activity recorded" in result.output


def test_unreachable_relay_is_a_message_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(method: str, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("agent_relay.cli.client.httpx.request", refuse)
    monkeypatch.setenv("AGENT_RELAY_URL", "http://127.0.0.1:9")
    result = run("tui", "--once")
    assert result.exit_code == 1
    assert "Cannot reach the relay" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, httpx.HTTPError)


def test_the_dashboard_never_writes(cli: TestClient) -> None:
    seed(cli)  # seeded straight through the TestClient, so it is not recorded
    CALLS.clear()

    assert run("tui", "--once", "--project", "tether").exit_code == 0
    assert CALLS, "the frame should have issued requests"
    assert {method for method, _ in CALLS} == {"GET"}
    assert {path for _, path in CALLS} == {
        "/tasks",
        "/context",
        "/coordination/summary",
        "/agents",
        "/events",
    }


def test_fetch_frame_skips_project_scoped_endpoints_when_there_is_no_project(
    cli: TestClient,
) -> None:
    """``/context`` and ``/coordination/summary`` 422 without a project — never call them blind."""
    frame = tui.fetch_frame(RelayClient())
    assert frame.focus_project is None
    assert frame.context == {} and frame.summary == {}
    assert [path for _, path in CALLS] == ["/tasks", "/agents", "/events"]


# --- project focus ----------------------------------------------------------


def test_busiest_project_is_stable_and_handles_nothing() -> None:
    assert tui.busiest_project([]) is None
    tasks = [
        {"project": "tether", "event_count": 2},
        {"project": "tether", "event_count": 1},
        {"project": "atlas", "event_count": 99},
    ]
    assert tui.busiest_project(tasks) == "tether"
    # A tie must not flicker between refreshes.
    tied = [{"project": "zeta", "event_count": 1}, {"project": "alpha", "event_count": 1}]
    assert tui.busiest_project(tied) == tui.busiest_project(list(reversed(tied))) == "alpha"


# --- individual panels, realistic and empty ---------------------------------

CONTEXT: dict[str, Any] = {
    "project": "tether",
    "active_claims": [{"task": "GH-142", "agent": "leo-codex"}],
    "blocked_tasks": [
        {
            "task": "GH-151",
            "owner": None,  # reported by someone who does not hold the claim
            "blocked_by": "andrea-agent",
            "blocked_reason": "waiting on the depth encoder weights",
            "last_activity_at": "2026-09-07T10:00:00+00:00",
        }
    ],
    "unresolved_questions": [
        {
            "ref": "Q-19",
            "from_agent": "leo-codex",
            "to_agent": None,
            "question": "Masking before or after resize?",
            "age_hours": 3.4,
        }
    ],
}

AGENTS: list[dict[str, Any]] = [
    {
        "agent": "leo-codex",
        "status": "online",
        "human_owner": "leonardo",
        "active_claims": ["GH-142"],
        "status_note": "running the ablation",
    }
]

EVENTS: list[dict[str, Any]] = [
    {
        "ref": "E-3",
        "event_type": "UPDATE",
        "agent": "leo-codex",
        "task": None,  # events do not have to belong to a task
        "summary": "Ablation done",
        "created_at": "2026-09-07T10:00:00+00:00",
    }
]


@pytest.mark.parametrize(
    ("render", "populated", "empty"),
    [
        (tui.render_blocked, CONTEXT, {}),
        (tui.render_questions, CONTEXT, {}),
        (tui.render_agents, AGENTS, []),
        (tui.render_activity, EVENTS, []),
        (tui.render_actions, {"project": "tether", "suggested_actions": ["unblock GH-151"]}, {}),
    ],
)
def test_every_panel_renders_populated_and_empty(render: Any, populated: Any, empty: Any) -> None:
    assert text_of(render(populated)).strip()
    assert text_of(render(empty)).strip()


def test_blocked_panel_separates_the_reporter_from_the_owner() -> None:
    out = text_of(tui.render_blocked(CONTEXT))
    assert "GH-151" in out
    assert "andrea-agent" in out  # blocked_by
    assert "unclaimed" in out  # owner is None
    assert "waiting on the depth encoder weights" in out
    assert "No blocked tasks" in text_of(tui.render_blocked({"blocked_tasks": []}))


def test_questions_panel_addresses_an_unrouted_question_to_anyone() -> None:
    out = text_of(tui.render_questions(CONTEXT))
    assert "Q-19" in out and "anyone" in out and "3h" in out


def test_activity_panel_survives_an_event_with_no_task() -> None:
    assert "Ablation done" in text_of(tui.render_activity(EVENTS))


def test_stats_row_counts_and_survives_a_missing_context() -> None:
    out = text_of(tui.render_stats(CONTEXT, AGENTS))
    # Singular when the count is one: "1 open questions" reads like a broken tool.
    assert "1 blocked task" in out
    assert "1 open question" in out and "1 open questions" not in out
    assert "1 agent online" in out and "1 agents online" not in out
    assert "0 blocked" in text_of(tui.render_stats({}, []))


def test_header_reports_connection_state_in_words() -> None:
    healthy = text_of(tui.render_header({"base_url": "http://x:8077", "interval": 10.0}))
    assert "connected" in healthy and "all projects" in healthy

    broken = text_of(
        tui.render_header({"base_url": "http://x:8077", "error": "Cannot reach the relay"})
    )
    assert "unreachable" in broken
    assert "last good frame" in broken


def test_dashboard_renders_before_the_first_successful_fetch() -> None:
    assert "Waiting for the relay" in text_of(tui.render_dashboard(None, error="down"))


# --- the details that make it readable --------------------------------------


def test_presence_emoji_maps_for_every_status() -> None:
    agents = [
        {"agent": f"a-{status}", "status": status}
        for status in ("online", "idle", "offline", "unknown")
    ]
    out = text_of(tui.render_agents(agents), width=200)
    for glyph in ("🟢", "🟡", "🔴", "⚪"):
        assert glyph in out
    # Colour is never the only signal: the word is there too.
    for status in ("online", "idle", "offline", "unknown"):
        assert status in out
    # An unrecognised status must not blow up or silently look online.
    assert "⚪" in text_of(tui.render_agents([{"agent": "x", "status": "weird"}]))


def test_event_glyphs_match_the_canonical_vocabulary() -> None:
    events = [
        {"event_type": str(event_type), "agent": "a", "summary": str(event_type)}
        for event_type in EventType
    ]
    out = text_of(tui.render_activity(events), width=200)
    for event_type, glyph in EVENT_LABELS.items():
        assert glyph in out, event_type
    assert {str(k): v for k, v in EVENT_LABELS.items()} == tui.GLYPHS


def test_long_summaries_are_truncated_not_wrapped() -> None:
    long = "x" * 400
    out = text_of(tui.render_activity([{"event_type": "UPDATE", "agent": "a", "summary": long}]))
    assert "…" in out
    assert "x" * 200 not in out
    # One event must still occupy one row, whatever the summary length.
    assert out.count("🔄") == 1


def test_nothing_overflows_a_narrow_terminal() -> None:
    frame = tui.Frame(
        base_url="http://127.0.0.1:8077",
        project=None,
        focus_project="tether",
        fetched_at=dt.datetime.now(dt.UTC),
        context=CONTEXT,
        agents=AGENTS,
        events=EVENTS,
        summary={"suggested_actions": ["unblock GH-151"]},
    )
    for width in (40, 60, 80):
        for line in text_of(tui.render_dashboard(frame), width=width).splitlines():
            assert len(line) <= width, f"width={width}: {line!r}"


def test_agent_markup_in_a_summary_is_not_interpreted() -> None:
    """Relay content is written by other agents; it is data, never display markup."""
    out = text_of(
        tui.render_activity([{"event_type": "UPDATE", "agent": "a", "summary": "[bold]"}])
    )
    assert "[bold]" in out


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, ["0 active claims", "0 blocked tasks", "0 open questions", "0 agents online"]),
        (1, ["1 active claim", "1 blocked task", "1 open question", "1 agent online"]),
        (7, ["7 active claims", "7 blocked tasks", "7 open questions", "7 agents online"]),
    ],
)
def test_stat_labels_pluralise_correctly(count: int, expected: list[str]) -> None:
    """The noun is not always the last word.

    A first attempt appended "s" to the whole phrase and produced "0 agent onlines",
    so both forms are spelled out. Numbers a reader distrusts are worse than no numbers.
    """
    context = {
        "active_claims": [{}] * count,
        "blocked_tasks": [{}] * count,
        "unresolved_questions": [{}] * count,
    }
    agents = [{"status": "online"}] * count
    out = text_of(tui.render_stats(context, agents))
    for phrase in expected:
        assert phrase in out


def test_findings_panel_shows_what_was_learned() -> None:
    """The activity stream shows what happened; this shows what is now known."""
    summary = {
        "project": "event-rgb",
        "recent_findings": [
            "[GH-2] zero paper PubMed su event camera in chirurgia — pampaj-opus-5",
            "[GH-2] la calibrazione cross-modale resta aperta — niccolo-claude",
        ],
    }
    out = text_of(tui.render_findings(summary), width=140)
    assert "FINDINGS" in out and "event-rgb" in out
    assert "GH-2" in out
    assert "zero paper PubMed" in out
    assert "pampaj-opus-5" in out


def test_findings_panel_has_an_empty_state_that_says_what_to_do() -> None:
    out = text_of(tui.render_findings({"project": "event-rgb", "recent_findings": []}))
    assert "Nothing recorded yet" in out
    assert "findings=" in out, "the empty state should name the flag that fills it"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("[GH-2] EPE improved 0.7% — leo-codex", ("GH-2", "EPE improved 0.7%", "leo-codex")),
        ("no task here — someone", ("", "no task here", "someone")),
        ("bare finding with no attribution", ("", "bare finding with no attribution", "")),
        ("[GH-9] em — dash — in — text — agent", ("GH-9", "em — dash — in — text", "agent")),
    ],
)
def test_finding_strings_split_into_columns(entry: str, expected: tuple[str, str, str]) -> None:
    """The relay ships these pre-formatted; splitting them back keeps the columns aligned.

    The last case matters: an em dash inside the finding itself must not be mistaken
    for the attribution separator, so the split takes the *last* one.
    """
    assert tui._split_finding(entry) == expected


def test_wide_terminals_get_two_columns_and_narrow_ones_do_not() -> None:
    """Purpose-split, not size-split: attention on the left, ambient state on the right."""
    frame = tui.Frame(
        project="event-rgb",
        focus_project="event-rgb",
        base_url="http://relay",
        fetched_at=dt.datetime.now(dt.UTC),
        context={"blocked_tasks": [], "unresolved_questions": [], "active_claims": []},
        summary={"project": "event-rgb", "suggested_actions": [], "recent_findings": []},
        agents=[],
        events=[],
        tasks=[],
    )
    wide = text_of(tui.render_dashboard(frame), width=170).splitlines()
    narrow = text_of(tui.render_dashboard(frame), width=100).splitlines()

    # Side by side, one line carries two panel borders; stacked, never more than one.
    assert any(line.count("╭─") == 2 for line in wide), "wide should place panels side by side"
    assert all(line.count("╭─") <= 1 for line in narrow), "narrow must stay single column"
    # Both layouts must still contain every panel.
    for heading in ("BLOCKED", "OPEN QUESTIONS", "FINDINGS", "WHO IS WORKING", "RECENT ACTIVITY"):
        assert heading in "\n".join(wide) and heading in "\n".join(narrow)
