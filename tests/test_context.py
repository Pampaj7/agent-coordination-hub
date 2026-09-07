"""GET /context: the briefing an agent reads before working."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import post_event


def seed(client: TestClient) -> None:
    """A small slice of a realistic day on project `tether`."""
    client.post(
        "/claim",
        json={
            "agent": "leo-codex",
            "human_owner": "leonardo",
            "project": "tether",
            "task": "GH-142",
            "branch": "exp/temporal-ablation",
        },
    )
    post_event(client, summary="Implemented H=8 and completed SCARED-C evaluation.")
    post_event(
        client,
        event_type="QUESTION",
        target_agent="niccolo-claude",
        summary="Did DRENDS preprocessing mask invalid depth before or after resize?",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        event_type="DECISION",
        summary="Standardise on H=6 for all DRENDS runs.",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        event_type="BLOCKED",
        agent="andrea-agent",
        human_owner="andrea",
        task="GH-151",
        branch=None,
        summary="Missing checkpoint for the depth encoder.",
        details={},
        artifacts=[],
    )
    post_event(client, project="drends", task="GH-201", summary="Unrelated project noise.")


def test_context_summarises_the_project(client: TestClient) -> None:
    seed(client)
    ctx = client.get("/context", params={"project": "tether"}).json()

    assert ctx["project"] == "tether"
    assert ctx["window_hours"] == 72

    assert [c["task"] for c in ctx["active_claims"]] == ["GH-142"]
    assert ctx["active_claims"][0]["agent"] == "leo-codex"

    assert [u["summary"] for u in ctx["recent_updates"]] == [
        "Implemented H=8 and completed SCARED-C evaluation."
    ]
    assert [q["ref"] for q in ctx["unresolved_questions"]] == ["Q-3"]
    assert ctx["unresolved_questions"][0]["to_agent"] == "niccolo-claude"
    assert [t["task"] for t in ctx["blocked_tasks"]] == ["GH-151"]
    assert ctx["blocked_tasks"][0]["blocked_reason"] == "Missing checkpoint for the depth encoder."
    assert [d["summary"] for d in ctx["recent_decisions"]] == [
        "Standardise on H=6 for all DRENDS runs."
    ]
    assert ctx["artifacts"] == ["runs/ablation_horizon.csv"]

    agents = {a["agent"] for a in ctx["active_agents"]}
    assert agents == {"leo-codex", "andrea-agent"}
    leo = next(a for a in ctx["active_agents"] if a["agent"] == "leo-codex")
    assert leo["human_owner"] == "leonardo"
    assert leo["active_tasks"] == ["GH-142"]


def test_context_is_scoped_to_one_project(client: TestClient) -> None:
    seed(client)
    ctx = client.get("/context", params={"project": "drends"}).json()

    assert ctx["active_claims"] == []
    assert [u["task"] for u in ctx["recent_updates"]] == ["GH-201"]
    assert ctx["unresolved_questions"] == []


def test_context_for_an_unknown_project_is_empty_not_an_error(client: TestClient) -> None:
    ctx = client.get("/context", params={"project": "nothing-here"}).json()
    assert ctx["project"] == "nothing-here"
    assert ctx["active_claims"] == []
    assert ctx["recent_updates"] == []
    assert ctx["blocked_tasks"] == []


def test_context_stays_bounded(client: TestClient) -> None:
    for index in range(30):
        post_event(client, summary=f"step {index}")

    ctx = client.get("/context", params={"project": "tether", "limit": 5}).json()
    assert len(ctx["recent_updates"]) == 5
    # Newest first, so the last steps posted are the ones returned.
    assert ctx["recent_updates"][0]["summary"] == "step 29"


def test_an_answer_closes_the_question(client: TestClient) -> None:
    seed(client)
    assert (
        len(client.get("/context", params={"project": "tether"}).json()["unresolved_questions"])
        == 1
    )

    post_event(
        client,
        event_type="ANSWER",
        agent="niccolo-claude",
        human_owner="niccolo",
        in_reply_to="Q-3",
        summary="Masking is applied before resize.",
        details={},
        artifacts=[],
    )
    assert client.get("/context", params={"project": "tether"}).json()["unresolved_questions"] == []


def test_an_answer_from_the_addressee_closes_it_even_without_a_ref(client: TestClient) -> None:
    """Agents forget to cite refs. The addressee replying on the same task counts."""
    seed(client)
    post_event(
        client,
        event_type="ANSWER",
        agent="niccolo-claude",
        summary="Before resize.",
        details={},
        artifacts=[],
    )
    assert client.get("/context", params={"project": "tether"}).json()["unresolved_questions"] == []


def test_an_answer_from_a_bystander_does_not_close_it(client: TestClient) -> None:
    seed(client)
    post_event(
        client,
        event_type="ANSWER",
        agent="c-claude",
        summary="No idea, sorry.",
        details={},
        artifacts=[],
    )
    assert (
        len(client.get("/context", params={"project": "tether"}).json()["unresolved_questions"])
        == 1
    )


def test_progress_clears_the_blocked_flag(client: TestClient) -> None:
    seed(client)
    tasks = {t["task"]: t for t in client.get("/tasks", params={"project": "tether"}).json()}
    assert tasks["GH-151"]["blocked"] is True
    assert tasks["GH-151"]["status"] == "blocked"

    post_event(
        client,
        agent="andrea-agent",
        task="GH-151",
        branch=None,
        summary="Checkpoint restored from the archive; resuming.",
        details={},
        artifacts=[],
    )
    tasks = {t["task"]: t for t in client.get("/tasks", params={"project": "tether"}).json()}
    assert tasks["GH-151"]["blocked"] is False
    assert client.get("/context", params={"project": "tether"}).json()["blocked_tasks"] == []


def test_tasks_view_reports_ownership_and_status(client: TestClient) -> None:
    seed(client)
    rows = {t["task"]: t for t in client.get("/tasks", params={"project": "tether"}).json()}

    assert rows["GH-142"]["owner"] == "leo-codex"
    assert rows["GH-142"]["status"] == "claimed"
    assert rows["GH-142"]["branch"] == "exp/temporal-ablation"
    assert rows["GH-142"]["event_count"] >= 3
    assert rows["GH-151"]["owner"] is None
    assert rows["GH-151"]["status"] == "blocked"

    assert [t["task"] for t in client.get("/tasks", params={"status": "blocked"}).json()] == [
        "GH-151"
    ]
    assert [t["task"] for t in client.get("/tasks", params={"agent": "leo-codex"}).json()] == [
        "GH-142"
    ]


def test_released_tasks_keep_a_row(client: TestClient) -> None:
    seed(client)
    client.post("/release", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    rows = {t["task"]: t for t in client.get("/tasks", params={"project": "tether"}).json()}
    assert rows["GH-142"]["status"] == "released"
    assert rows["GH-142"]["owner"] is None
