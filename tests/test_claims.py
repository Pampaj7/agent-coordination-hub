"""Task ownership: claim, collision, release, handoff."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_relay.db.models import TaskClaim
from agent_relay.services import claims as claim_service
from tests.conftest import post_event


def claim(client: TestClient, agent: str, task: str = "GH-142", **extra: object) -> object:
    return client.post("/claim", json={"agent": agent, "project": "tether", "task": task, **extra})


def test_claim_creates_ownership_and_logs_an_event(client: TestClient) -> None:
    response = claim(client, "leo-codex", human_owner="leonardo", branch="exp/temporal-ablation")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["agent"] == "leo-codex"
    assert body["human_owner"] == "leonardo"
    assert body["branch"] == "exp/temporal-ablation"
    assert body["active"] is True
    assert body["released_at"] is None

    events = client.get("/events", params={"event_type": "CLAIM"}).json()
    assert len(events) == 1
    assert events[0]["agent"] == "leo-codex"
    assert events[0]["task"] == "GH-142"


def test_claim_collision_returns_409_with_owner_details(client: TestClient) -> None:
    claim(client, "leo-codex", human_owner="leonardo", branch="exp/temporal-ablation")
    post_event(client, summary="Ran the H=8 ablation.")

    response = claim(client, "niccolo-claude")
    assert response.status_code == 409

    detail = response.json()["detail"]
    assert detail["current_owner"] == "leo-codex"
    assert detail["human_owner"] == "leonardo"
    assert detail["project"] == "tether"
    assert detail["task"] == "GH-142"
    assert detail["branch"] == "exp/temporal-ablation"
    assert detail["claimed_at"]
    assert detail["last_activity_at"]
    assert detail["last_update"]["summary"] == "Ran the H=8 ablation."
    assert "leo-codex" in detail["detail"]

    # The loser must not have taken ownership or created a claim event.
    assert [c["agent"] for c in client.get("/claims").json()] == ["leo-codex"]
    assert len(client.get("/events", params={"event_type": "CLAIM"}).json()) == 1


def test_reclaiming_your_own_task_is_idempotent(client: TestClient) -> None:
    claim(client, "leo-codex")
    response = claim(client, "leo-codex", branch="exp/temporal-ablation", note="continuing")
    assert response.status_code == 200
    assert response.json()["branch"] == "exp/temporal-ablation"

    assert len(client.get("/claims").json()) == 1
    # No duplicate CLAIM event: re-claiming is a refresh, not news.
    assert len(client.get("/events", params={"event_type": "CLAIM"}).json()) == 1


def test_the_database_itself_forbids_two_active_claims(db: Session) -> None:
    """Belt and braces: even a direct INSERT bypassing the service layer is refused."""
    now = "2026-01-01 00:00:00"
    insert = text(
        "INSERT INTO task_claims (project, task, agent, active, claimed_at, last_activity_at) "
        "VALUES ('tether', 'GH-142', :agent, 1, :now, :now)"
    )
    db.execute(insert, {"agent": "leo-codex", "now": now})
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(insert, {"agent": "niccolo-claude", "now": now})
        db.commit()


def test_different_tasks_and_projects_do_not_collide(client: TestClient) -> None:
    assert claim(client, "leo-codex", task="GH-142").status_code == 200
    assert claim(client, "niccolo-claude", task="GH-138").status_code == 200
    other = client.post("/claim", json={"agent": "c-claude", "project": "drends", "task": "GH-142"})
    assert other.status_code == 200
    assert len(client.get("/claims").json()) == 3
    assert len(client.get("/claims", params={"project": "tether"}).json()) == 2


def test_release_frees_the_task_for_someone_else(client: TestClient) -> None:
    claim(client, "leo-codex")
    response = client.post(
        "/release",
        json={
            "agent": "leo-codex",
            "project": "tether",
            "task": "GH-142",
            "summary": "H=8 sweep finished.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["released_at"] is not None

    assert client.get("/claims").json() == []
    assert claim(client, "niccolo-claude").status_code == 200

    releases = client.get("/events", params={"event_type": "RELEASE"}).json()
    assert releases[0]["summary"] == "H=8 sweep finished."


def test_release_by_a_stranger_is_refused_unless_forced(client: TestClient) -> None:
    claim(client, "leo-codex")

    refused = client.post(
        "/release", json={"agent": "niccolo-claude", "project": "tether", "task": "GH-142"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["current_owner"] == "leo-codex"
    assert client.get("/claims").json() != []

    forced = client.post(
        "/release",
        json={
            "agent": "niccolo-claude",
            "project": "tether",
            "task": "GH-142",
            "force": True,
        },
    )
    assert forced.status_code == 200
    assert client.get("/claims").json() == []

    event = client.get("/events", params={"event_type": "RELEASE"}).json()[0]
    assert event["metadata"] == {"forced": True, "previous_owner": "leo-codex"}


def test_releasing_an_unclaimed_task_is_404(client: TestClient) -> None:
    response = client.post(
        "/release", json={"agent": "leo-codex", "project": "tether", "task": "GH-999"}
    )
    assert response.status_code == 404


def test_handoff_moves_the_claim_and_records_the_context(client: TestClient) -> None:
    claim(client, "leo-codex", branch="exp/temporal-ablation")

    response = client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "human_owner": "leonardo",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-142",
            "summary": "Dataset preprocessing is complete.",
            "continue_from": "82bd18f",
            "inputs": ["data/scared_processed/"],
            "warnings": ["Do not modify scripts/preprocess_scared.py until GH-150 finishes."],
        },
    )
    assert response.status_code == 200, response.text

    event = response.json()
    assert event["event_type"] == "HANDOFF"
    assert event["target_agent"] == "andrea-agent"
    assert event["details"]["continue_from"] == "82bd18f"
    assert event["details"]["inputs"] == ["data/scared_processed/"]
    assert event["details"]["warnings"][0].startswith("Do not modify")

    claims = client.get("/claims").json()
    assert len(claims) == 1
    assert claims[0]["agent"] == "andrea-agent"
    assert claims[0]["branch"] == "exp/temporal-ablation"


def test_handoff_can_keep_the_claim_with_the_sender(client: TestClient) -> None:
    claim(client, "leo-codex")
    response = client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-142",
            "summary": "Take a look while I keep the branch.",
            "transfer_claim": False,
        },
    )
    assert response.status_code == 200
    assert client.get("/claims").json()[0]["agent"] == "leo-codex"


def test_handoff_of_someone_elses_task_is_refused(client: TestClient) -> None:
    claim(client, "leo-codex")
    response = client.post(
        "/handoff",
        json={
            "agent": "niccolo-claude",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-142",
            "summary": "Passing this along.",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["current_owner"] == "leo-codex"
    assert client.get("/claims").json()[0]["agent"] == "leo-codex"


def test_handoff_on_an_unclaimed_task_claims_it_for_the_receiver(client: TestClient) -> None:
    response = client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-160",
            "summary": "Nobody owns this; you take it.",
        },
    )
    assert response.status_code == 200
    assert client.get("/claims").json()[0]["agent"] == "andrea-agent"


def test_handoff_losing_a_race_reports_a_conflict_not_a_crash(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receiver's claim insert loses to a concurrent claimer.

    Simulated by letting the handoff's *first* ownership read miss a row that is in
    fact already there, which is exactly what a racing transaction looks like. The
    insert then hits the partial unique index, and the service must turn that into a
    409 naming the real owner rather than a 500.
    """
    db.execute(
        text(
            "INSERT INTO task_claims "
            "(project, task, agent, active, claimed_at, last_activity_at) "
            "VALUES ('tether', 'GH-160', 'c-claude', 1, :now, :now)"
        ),
        {"now": "2026-01-01 00:00:00"},
    )
    db.commit()

    real_get = claim_service.get_active_claim
    calls = {"n": 0}

    def racy_get(session: Session, project: str, task: str) -> TaskClaim | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the racing read
        return real_get(session, project, task)

    monkeypatch.setattr(claim_service, "get_active_claim", racy_get)

    response = client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-160",
            "summary": "You take it.",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["current_owner"] == "c-claude"
    # The task still has exactly one owner, and it is not the handoff receiver.
    owners = [c["agent"] for c in client.get("/claims").json() if c["task"] == "GH-160"]
    assert owners == ["c-claude"]
