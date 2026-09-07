"""Per-person priorities.

The contract: the board is ordered by *who pays for the delay*. An obligation that
leaves somebody else idle outranks one that only costs the person holding it, and
grouping follows the human rather than the process. Every test below tries to get an
item onto the wrong person's plate, or into the wrong band.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from agent_relay.services import priorities as priorities_service
from tests.conftest import post_event


def board(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/priorities", params=params)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def plate(client: TestClient, owner: str, **params: Any) -> list[dict[str, Any]]:
    for row in board(client, **params)["owners"]:
        if row["owner"] == owner:
            items: list[dict[str, Any]] = row["items"]
            return items
    return []


def register(client: TestClient, agent: str, owner: str) -> None:
    """Give the relay an agent row carrying a human owner."""
    response = client.post("/heartbeat", json={"agent": agent, "human_owner": owner})
    assert response.status_code == 200, response.text


def ask(
    client: TestClient,
    *,
    frm: str,
    to: str,
    task: str = "GH-1",
    frm_owner: str | None = None,
) -> dict[str, Any]:
    """Ask a question, without quietly re-owning the asker.

    `post_event` carries a default `human_owner`, and posting an event upserts it
    onto the author's agent row — so every helper here states the owner explicitly
    rather than letting the fixture decide who an agent belongs to.
    """
    return post_event(
        client,
        agent=frm,
        human_owner=frm_owner,
        event_type="QUESTION",
        project="tether",
        task=task,
        target_agent=to,
        summary="which calibration target?",
    )


# ----------------------------------------------------------------- grouping


def test_an_obligation_lands_on_the_person_not_the_process(client: TestClient) -> None:
    """A question to one of Niccolò's agents is Niccolò's, whichever agent it hit."""
    register(client, "nachomar-gpt", "niccolo")
    register(client, "pampaj-opus-5", "pampaj")
    ask(client, frm="pampaj-opus-5", to="nachomar-gpt", frm_owner="pampaj")

    owners = {row["owner"] for row in board(client)["owners"]}
    assert "niccolo" in owners
    assert plate(client, "niccolo")[0]["kind"] == "question"
    # And it is emphatically not on the asker's plate.
    assert plate(client, "pampaj") == []


def test_an_agent_with_no_owner_is_visible_not_dropped(client: TestClient) -> None:
    """An unowned agent holding obligations is a setup gap, and hiding it hides the gap."""
    register(client, "orphan-agent", "")
    post_event(
        client,
        agent="someone",
        human_owner="somebody",
        event_type="QUESTION",
        project="tether",
        target_agent="orphan-agent",
        summary="who owns this?",
    )

    items = plate(client, priorities_service.UNASSIGNED)
    assert [i["kind"] for i in items] == ["question"]


# ------------------------------------------------------------------ ranking


def test_a_question_outranks_the_persons_own_blocked_task(client: TestClient) -> None:
    """The core ordering rule, stated as a test.

    Being blocked costs one person's time. Leaving a question unanswered costs two.
    """
    register(client, "leo-codex", "pampaj")
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-9"})
    post_event(
        client,
        agent="leo-codex",
        human_owner="pampaj",
        event_type="BLOCKED",
        project="tether",
        task="GH-9",
        summary="waiting on the rig",
    )
    ask(client, frm="niccolo-gpt", to="leo-codex", task="GH-1", frm_owner="niccolo")

    kinds = [item["kind"] for item in plate(client, "pampaj")]
    assert kinds.index("question") < kinds.index("blocked")


def test_bands_are_ordered_question_stale_handoff_blocked_in_progress() -> None:
    """Pinned as a list so a reordering has to be deliberate, not incidental."""
    ordered = sorted(priorities_service.BANDS, key=lambda k: priorities_service.BANDS[k])
    assert ordered == ["question", "stale_claim", "handoff", "blocked", "in_progress"]
    # Exactly the bands where somebody else absorbs the cost.
    assert {"question", "stale_claim", "handoff"} == priorities_service.BLOCKING_OTHERS


def test_within_a_band_the_oldest_item_comes_first(client: TestClient, db: Session) -> None:
    register(client, "leo-codex", "pampaj")
    ask(client, frm="a", to="leo-codex", task="GH-new", frm_owner="niccolo")
    old = ask(client, frm="b", to="leo-codex", task="GH-old", frm_owner="niccolo")
    # `ref` is a derived property, not a column: backdate by primary key.
    db.execute(
        text("UPDATE events SET created_at = :when WHERE id = :id"),
        {
            "when": (dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)).replace(tzinfo=None),
            "id": old["id"],
        },
    )
    db.commit()

    tasks = [item["task"] for item in plate(client, "pampaj") if item["kind"] == "question"]
    assert tasks[0] == "GH-old"


def test_answered_questions_leave_the_board(client: TestClient) -> None:
    register(client, "leo-codex", "pampaj")
    question = ask(client, frm="niccolo-gpt", to="leo-codex", frm_owner="niccolo")
    assert plate(client, "pampaj")

    post_event(
        client,
        agent="leo-codex",
        human_owner="pampaj",
        event_type="ANSWER",
        project="tether",
        task="GH-1",
        in_reply_to=question["ref"],
        summary="the 9x6 checkerboard",
    )
    assert [i["kind"] for i in plate(client, "pampaj")] == []


# ------------------------------------------------------------ whose list first


def test_the_person_blocking_the_most_others_is_listed_first(client: TestClient) -> None:
    register(client, "busy-agent", "gabriele")
    register(client, "leo-codex", "pampaj")
    # pampaj merely has work in progress; gabriele is sitting on two questions.
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-9"})
    ask(client, frm="leo-codex", to="busy-agent", task="GH-1", frm_owner="pampaj")
    ask(client, frm="leo-codex", to="busy-agent", task="GH-2", frm_owner="pampaj")

    rows = board(client)["owners"]
    assert rows[0]["owner"] == "gabriele"
    assert rows[0]["blocking_others"] == 2
    # In-progress work is not an obligation anyone else is waiting on.
    pampaj = next(r for r in rows if r["owner"] == "pampaj")
    assert pampaj["blocking_others"] == 0


def test_top_names_the_single_most_urgent_item(client: TestClient) -> None:
    register(client, "leo-codex", "pampaj")
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-9"})
    ask(client, frm="niccolo-gpt", to="leo-codex", frm_owner="niccolo")

    row = next(r for r in board(client)["owners"] if r["owner"] == "pampaj")
    assert row["top"] == "which calibration target?"


# ------------------------------------------------------------------- filters


def test_in_progress_work_can_be_filtered_out(client: TestClient) -> None:
    register(client, "leo-codex", "pampaj")
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-9"})

    assert [i["kind"] for i in plate(client, "pampaj")] == ["in_progress"]
    assert board(client, include_in_progress=False)["owners"] == []


def test_a_task_is_never_listed_as_both_stale_and_in_progress(
    client: TestClient, db: Session
) -> None:
    """The same claim counted twice would inflate everyone's plate."""
    register(client, "leo-codex", "pampaj")
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-9"})
    db.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = 'GH-9'"),
        {"when": (dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)).replace(tzinfo=None)},
    )
    db.commit()

    kinds = [item["kind"] for item in plate(client, "pampaj")]
    assert kinds.count("stale_claim") == 1
    assert "in_progress" not in kinds


def test_project_filter_narrows_the_board(client: TestClient) -> None:
    register(client, "leo-codex", "pampaj")
    ask(client, frm="niccolo-gpt", to="leo-codex", task="GH-1", frm_owner="niccolo")

    assert plate(client, "pampaj", project="tether")
    assert plate(client, "pampaj", project="some-other-project") == []


def test_an_unaddressed_question_is_nobodys_priority(client: TestClient) -> None:
    """A question to the room belongs on /context, not on three identical plates.

    Putting it on everyone's list would make every person's priorities the same, and
    a list that is the same for everybody tells nobody what to do.
    """
    register(client, "leo-codex", "pampaj")
    post_event(
        client,
        agent="niccolo-gpt",
        human_owner="niccolo",
        event_type="QUESTION",
        project="tether",
        summary="does anyone know the frame rate?",
    )

    assert board(client)["owners"] == []


# ------------------------------------------------------------------ rendering


def test_empty_board_says_so_rather_than_printing_nothing() -> None:
    from agent_relay.cli import render

    assert "Nothing is waiting" in render.render_priorities({"owners": []})


def test_rendered_board_shows_the_reason_and_the_command() -> None:
    from agent_relay.cli import render

    out = render.render_priorities(
        {
            "owners": [
                {
                    "owner": "niccolo",
                    "agents": ["nachomar-gpt"],
                    "blocking_others": 1,
                    "items": [
                        {
                            "kind": "question",
                            "task": "GH-3",
                            "headline": "which calibration target?",
                            "why": "pampaj-opus-5 is waiting on this answer",
                            "age_hours": 2.0,
                            "do_next": "agent-relay post answer ...",
                        }
                    ],
                }
            ]
        }
    )
    assert "niccolo" in out
    assert "1 blocking others" in out
    assert "waiting on this answer" in out
    assert "agent-relay post answer" in out


@pytest.mark.parametrize("kind", sorted(priorities_service.BANDS))
def test_every_kind_has_a_glyph_in_both_renderers(kind: str) -> None:
    """A kind with no glyph renders as a bullet and silently loses its meaning."""
    from agent_relay.cli import render, tui

    assert kind in render.PRIORITY_GLYPHS
    assert kind in tui.PRIORITY_GLYPHS
