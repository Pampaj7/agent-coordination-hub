"""The inbox watcher — the closest thing to agents waking each other.

An agent cannot be woken: turn-based processes have no listener. A shell can, so this
wakes a shell, and the command it runs may be the one that starts an agent. The tests
drive it with an injected clock so nothing here actually sleeps.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_relay.cli import waiter
from agent_relay.cli.client import RelayError
from tests.conftest import post_event


class FakeClient:
    """Serves a scripted sequence of inbox responses."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, path: str, **params: Any) -> Any:
        assert path == "/inbox"
        item = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def question(ref: str) -> dict[str, Any]:
    return {"ref": ref, "from_agent": "pampaj-opus-5", "question": f"question {ref}"}


def test_it_does_not_fire_on_the_backlog_already_there() -> None:
    """Starting a watcher must not replay what you have already read."""
    client = FakeClient([{"questions_for_me": [question("Q-1")]}])
    lines: list[str] = []
    delivered = waiter.watch(
        client, "leo-codex", emit=lines.append, sleep=lambda _: None, max_polls=3
    )
    assert delivered == 0
    assert "1 item(s) already there" in lines[0]


def test_it_fires_once_on_a_new_arrival() -> None:
    client = FakeClient(
        [
            {"questions_for_me": []},
            {"questions_for_me": [question("Q-2")]},
        ]
    )
    lines: list[str] = []
    delivered = waiter.watch(
        client, "leo-codex", emit=lines.append, sleep=lambda _: None, max_polls=4
    )
    assert delivered == 1, "the same item must not fire twice"
    assert any("Q-2" in line for line in lines)


def test_an_answered_item_replaced_by_a_new_one_still_fires() -> None:
    """Counting would miss this: one leaves as another arrives, and the total is flat."""
    client = FakeClient(
        [
            {"questions_for_me": [question("Q-1")]},
            {"questions_for_me": [question("Q-9")]},
        ]
    )
    lines: list[str] = []
    delivered = waiter.watch(
        client, "leo-codex", emit=lines.append, sleep=lambda _: None, max_polls=2
    )
    assert delivered == 1
    assert any("Q-9" in line for line in lines)


def test_a_relay_that_goes_away_does_not_kill_the_watcher() -> None:
    """A restarted relay or a dropped tunnel is normal; keep watching."""
    client = FakeClient(
        [
            {"questions_for_me": []},
            RelayError("Cannot reach the relay"),
            {"questions_for_me": [question("Q-3")]},
        ]
    )
    lines: list[str] = []
    delivered = waiter.watch(
        client, "leo-codex", emit=lines.append, sleep=lambda _: None, max_polls=5
    )
    assert delivered == 1
    assert any("Cannot reach" in line for line in lines)


def test_it_watches_handoffs_and_stalled_tasks_too() -> None:
    client = FakeClient(
        [
            {},
            {
                "handoffs_to_me": [
                    {"ref": "E-7", "from_agent": "pampaj-opus-5", "summary": "over to you"}
                ],
                "my_tasks_needing_attention": [
                    {"project": "tether", "task": "GH-9", "reason": "blocked: no data"}
                ],
            },
        ]
    )
    lines: list[str] = []
    waiter.watch(client, "leo-codex", emit=lines.append, sleep=lambda _: None, max_polls=2)
    joined = "\n".join(lines)
    assert "E-7" in joined and "GH-9" in joined


def test_once_stops_after_the_first_arrival() -> None:
    client = FakeClient([{"questions_for_me": []}, {"questions_for_me": [question("Q-4")]}])
    waiter.watch(
        client, "leo-codex", emit=lambda _: None, sleep=lambda _: None, once=True, max_polls=9
    )
    assert client.calls == 2, "one seed, one poll that finds it, then stop"


def test_an_unparseable_command_fails_before_watching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better to fail now than at 3am on the first message that arrives."""
    with pytest.raises(ValueError):
        waiter.run("leo-codex", command="echo 'unterminated")


def test_refs_come_from_a_real_inbox(client: TestClient) -> None:
    """The shapes this parses are the ones the API actually returns."""
    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="before or after resize?",
        details={},
        artifacts=[],
    )
    inbox = client.get("/inbox", params={"agent": "niccolo-claude"}).json()
    assert waiter.inbox_refs(inbox) == {"Q-1"}
    assert "Q-1" in waiter.describe(inbox, {"Q-1"})
