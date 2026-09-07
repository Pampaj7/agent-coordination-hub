"""Event creation, validation and filtering."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import event_payload, iso, post_event


def test_health_reports_disabled_integrations(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["integrations"]["slack"] == "disabled"
    assert body["integrations"]["github"] == "disabled"
    assert body["integrations"]["auth"] == "open"


def test_create_event_round_trips_every_field(client: TestClient) -> None:
    created = post_event(client)

    assert created["id"] == 1
    assert created["ref"] == "E-1"
    assert created["event_type"] == "UPDATE"
    assert created["agent"] == "leo-codex"
    assert created["human_owner"] == "leonardo"
    assert created["project"] == "tether"
    assert created["task"] == "GH-142"
    assert created["branch"] == "exp/temporal-ablation"
    assert created["details"]["findings"] == ["EPE improved 0.7%"]
    assert created["artifacts"] == ["runs/ablation_horizon.csv"]
    assert created["created_at"].endswith("Z") or "+00:00" in created["created_at"]

    fetched = client.get(f"/events/{created['id']}").json()
    assert fetched == created


def test_questions_get_a_q_ref(client: TestClient) -> None:
    question = post_event(
        client,
        event_type="QUESTION",
        target_agent="niccolo-claude",
        summary="Did DRENDS preprocessing mask invalid depth before or after resize?",
        details={},
        artifacts=[],
    )
    assert question["ref"] == "Q-1"


def test_event_persists_across_requests(client: TestClient) -> None:
    post_event(client)
    # A separate request means a separate session: this proves it hit the database,
    # not just an in-memory object.
    assert len(client.get("/events").json()) == 1


def test_unknown_event_type_is_rejected(client: TestClient) -> None:
    response = client.post("/events", json=event_payload(event_type="GOSSIP"))
    assert response.status_code == 422


def test_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post("/events", json=event_payload(vibes="good"))
    assert response.status_code == 422


def test_handoff_event_requires_a_target(client: TestClient) -> None:
    response = client.post("/events", json=event_payload(event_type="HANDOFF", target_agent=None))
    assert response.status_code == 422


def test_missing_event_returns_404(client: TestClient) -> None:
    assert client.get("/events/999").status_code == 404


def test_filters_narrow_the_log(client: TestClient) -> None:
    post_event(client)
    post_event(client, agent="niccolo-claude", human_owner="niccolo", task="GH-138")
    post_event(client, project="drends", task="GH-201")
    post_event(
        client,
        event_type="BLOCKED",
        agent="andrea-agent",
        human_owner="andrea",
        task="GH-151",
        summary="Missing checkpoint.",
        details={},
        artifacts=[],
    )

    def refs(**params: object) -> set[str]:
        response = client.get("/events", params=params)
        assert response.status_code == 200, response.text
        return {e["ref"] for e in response.json()}

    assert refs(project="tether") == {"E-1", "E-2", "E-4"}
    assert refs(project="drends") == {"E-3"}
    assert refs(agent="niccolo-claude") == {"E-2"}
    assert refs(human_owner="leonardo") == {"E-1", "E-3"}
    assert refs(task="GH-151") == {"E-4"}
    assert refs(event_type="BLOCKED") == {"E-4"}
    assert refs(project="tether", event_type="UPDATE") == {"E-1", "E-2"}
    assert refs() == {"E-1", "E-2", "E-3", "E-4"}


def test_events_are_newest_first_and_limited(client: TestClient) -> None:
    for index in range(5):
        post_event(client, summary=f"step {index}")

    body = client.get("/events", params={"limit": 3}).json()
    assert [e["ref"] for e in body] == ["E-5", "E-4", "E-3"]


def test_since_filter_excludes_the_past(client: TestClient) -> None:
    post_event(client)
    assert client.get("/events", params={"since": iso(1)}).json() == []
    assert len(client.get("/events", params={"since": iso(-1)}).json()) == 1


def test_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/events", params={"limit": 10_000}).status_code == 422
    assert client.get("/events", params={"limit": 0}).status_code == 422
