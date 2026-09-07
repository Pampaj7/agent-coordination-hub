"""GET /coordination/summary: deterministic, no LLM."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.conftest import post_event


def summary(client: TestClient, **params: object) -> dict:
    response = client.get("/coordination/summary", params={"project": "tether", **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_summary_of_a_quiet_project_is_empty(client: TestClient) -> None:
    body = summary(client)
    assert body["active_agents"] == {}
    assert body["blocked"] == []
    assert body["possible_conflicts"] == []
    assert body["suggested_actions"] == []


def test_summary_reports_who_is_working_on_what(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    client.post("/claim", json={"agent": "niccolo-claude", "project": "tether", "task": "GH-138"})
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-150"})

    assert summary(client)["active_agents"] == {
        "leo-codex": ["GH-142", "GH-150"],
        "niccolo-claude": ["GH-138"],
    }


def test_summary_reports_blocked_work(client: TestClient) -> None:
    post_event(
        client,
        event_type="BLOCKED",
        agent="andrea-agent",
        task="GH-151",
        summary="missing checkpoint",
        details={},
        artifacts=[],
    )
    blocked = summary(client)["blocked"]
    assert len(blocked) == 1
    assert blocked[0]["agent"] == "andrea-agent"
    assert blocked[0]["task"] == "GH-151"
    assert blocked[0]["reason"] == "missing checkpoint"

    assert any("unblock GH-151" in a for a in summary(client)["suggested_actions"])


def test_two_agents_on_a_claimed_task_is_a_conflict(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(client, agent="leo-codex", summary="running the sweep")
    post_event(client, agent="niccolo-claude", summary="also running the sweep")

    conflicts = summary(client)["possible_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["task"] == "GH-142"
    assert conflicts[0]["claimed_by"] == "leo-codex"
    assert set(conflicts[0]["agents"]) == {"leo-codex", "niccolo-claude"}
    assert "niccolo-claude" in conflicts[0]["reason"]
    assert any("avoid duplicated work on GH-142" in a for a in summary(client)["suggested_actions"])


def test_two_agents_on_an_unclaimed_task_is_also_a_conflict(client: TestClient) -> None:
    post_event(client, agent="leo-codex", summary="poking at it")
    post_event(client, agent="niccolo-claude", summary="also poking at it")

    conflicts = summary(client)["possible_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["claimed_by"] is None


def test_one_agent_working_alone_is_not_a_conflict(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(client, agent="leo-codex", summary="working")
    post_event(client, agent="leo-codex", summary="still working")
    assert summary(client)["possible_conflicts"] == []


def test_summary_surfaces_open_questions_and_findings(client: TestClient) -> None:
    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="Masking before or after resize?",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        agent="leo-codex",
        summary="Horizon sweep done.",
        details={"findings": ["H=6 currently better than H=8 on DRENDS"]},
        artifacts=[],
    )
    post_event(
        client,
        event_type="DECISION",
        agent="leo-codex",
        summary="Freeze the preprocessing pipeline for GH-150.",
        details={},
        artifacts=[],
    )

    body = summary(client)
    assert [q["ref"] for q in body["unresolved_questions"]] == ["Q-1"]
    assert body["recent_findings"] == [
        "[GH-142] H=6 currently better than H=8 on DRENDS — leo-codex"
    ]
    assert body["recent_decisions"] == [
        "[GH-142] Freeze the preprocessing pipeline for GH-150. — leo-codex"
    ]
    assert any(
        a.startswith("resolve Q-1 from leo-codex to niccolo-claude")
        for a in body["suggested_actions"]
    )


def test_idle_claims_are_flagged(client: TestClient, db: Session) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    assert summary(client)["idle_claims"] == []

    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)).replace(tzinfo=None)
    db.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = 'GH-142'"),
        {"when": stale},
    )
    db.commit()

    body = summary(client, idle_hours=24)
    assert [c["task"] for c in body["idle_claims"]] == ["GH-142"]
    assert any("check on leo-codex" in a for a in body["suggested_actions"])

    assert summary(client, idle_hours=72)["idle_claims"] == []


def test_posting_about_your_task_keeps_the_claim_fresh(client: TestClient, db: Session) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)).replace(tzinfo=None)
    db.execute(
        text("UPDATE task_claims SET last_activity_at = :when WHERE task = 'GH-142'"),
        {"when": stale},
    )
    db.commit()
    assert summary(client)["idle_claims"] != []

    post_event(client, agent="leo-codex", summary="still on it")
    assert summary(client)["idle_claims"] == []


def test_window_excludes_old_activity_from_conflicts(client: TestClient, db: Session) -> None:
    post_event(client, agent="leo-codex", summary="old work")
    post_event(client, agent="niccolo-claude", summary="new work")
    assert summary(client)["possible_conflicts"] != []

    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=200)).replace(tzinfo=None)
    db.execute(
        text("UPDATE events SET created_at = :when WHERE agent = 'leo-codex'"), {"when": old}
    )
    db.commit()

    assert summary(client, window_hours=72)["possible_conflicts"] == []
    assert summary(client, window_hours=24 * 30)["possible_conflicts"] != []


def test_talking_about_a_task_is_not_a_conflict(client: TestClient) -> None:
    """QUESTION/ANSWER between agents is coordination working, not duplicated work."""
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(client, agent="leo-codex", summary="running the sweep")
    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="masking before or after resize?",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        event_type="ANSWER",
        agent="niccolo-claude",
        summary="before resize",
        details={},
        artifacts=[],
    )
    assert summary(client)["possible_conflicts"] == []


def test_a_handoff_does_not_leave_a_phantom_conflict(client: TestClient) -> None:
    """Work the previous owner did before handing over is history, not a conflict."""
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(client, agent="leo-codex", summary="did the preprocessing")
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

    assert summary(client)["possible_conflicts"] == []

    # ...but if the previous owner starts working on it again, that *is* a conflict.
    post_event(client, agent="leo-codex", summary="actually, one more tweak")
    conflicts = summary(client)["possible_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["claimed_by"] == "andrea-agent"
    assert conflicts[0]["agents"] == ["leo-codex"]


def test_blocked_tasks_name_the_agent_that_reported_it(client: TestClient) -> None:
    post_event(
        client,
        event_type="BLOCKED",
        agent="andrea-agent",
        task="GH-151",
        summary="missing checkpoint",
        details={},
        artifacts=[],
    )
    row = next(t for t in client.get("/tasks").json() if t["task"] == "GH-151")
    assert row["owner"] is None  # nobody claimed it
    assert row["blocked_by"] == "andrea-agent"
    assert row["blocked"] is True
