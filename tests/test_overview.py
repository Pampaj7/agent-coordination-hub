"""The cross-project view.

Per-project coordination already has its own tests; what matters here is that the
portfolio view *aggregates* those rules faithfully and surfaces the one signal no
single-project summary can see — an agent holding claims in more than one project.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_relay.api.routes_v2 import (
    a2a_router,
    a2a_rpc_router,
    experiments_router,
    overview_router,
)
from agent_relay.config import Settings
from agent_relay.main import create_app
from tests.conftest import post_event


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


def claim(client: TestClient, agent: str, project: str, task: str) -> None:
    response = client.post("/claim", json={"agent": agent, "project": project, "task": task})
    assert response.status_code == 200, response.text


def overview(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/coordination/overview", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by_project(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["project"]: row for row in body["projects"]}


# ------------------------------------------------------------------ empty relay


def test_an_empty_relay_returns_an_empty_overview(v2_client: TestClient) -> None:
    body = overview(v2_client)
    assert body["projects"] == []
    assert body["overloaded_agents"] == []
    assert body["agents_across_projects"] == {}
    assert body["busiest_projects"] == []
    assert body["suggested_actions"] == []
    assert body["totals"]["projects"] == 0
    assert body["totals"]["active_claims"] == 0


# -------------------------------------------------------------- counting rules


def test_projects_are_counted_independently(v2_client: TestClient) -> None:
    post_event(v2_client, project="tether", task="GH-142")
    post_event(v2_client, project="tether", task="GH-143")
    post_event(v2_client, project="drends", agent="nina", task="GH-201")
    claim(v2_client, "leo-codex", "tether", "GH-142")
    claim(v2_client, "nina", "drends", "GH-201")

    rows = by_project(overview(v2_client))
    assert set(rows) == {"tether", "drends"}
    assert rows["tether"]["active_claims"] == 1
    assert rows["tether"]["agents"] == ["leo-codex"]
    assert rows["tether"]["events_in_window"] >= 2
    assert rows["drends"]["active_claims"] == 1
    assert rows["drends"]["agents"] == ["nina"]


def test_blocked_questions_and_conflicts_aggregate(v2_client: TestClient) -> None:
    # tether: one blocked task and one unanswered question.
    post_event(
        v2_client,
        project="tether",
        task="GH-142",
        event_type="BLOCKED",
        summary="waiting on the SCARED-C export",
    )
    post_event(
        v2_client,
        project="tether",
        task="GH-143",
        event_type="QUESTION",
        summary="which horizon should we ship?",
        target_agent="nina",
    )
    # drends: leo-codex works a task nina owns — the per-project conflict rule.
    claim(v2_client, "nina", "drends", "GH-201")
    post_event(v2_client, project="drends", agent="leo-codex", task="GH-201")

    body = overview(v2_client)
    rows = by_project(body)
    assert rows["tether"]["blocked"] == 1
    assert rows["tether"]["open_questions"] == 1
    assert rows["tether"]["conflicts"] == 0
    assert rows["drends"]["conflicts"] == 1

    totals = body["totals"]
    assert totals["blocked"] == sum(r["blocked"] for r in body["projects"])
    assert totals["open_questions"] == sum(r["open_questions"] for r in body["projects"])
    assert totals["conflicts"] == sum(r["conflicts"] for r in body["projects"])
    assert totals["events_in_window"] == sum(r["events_in_window"] for r in body["projects"])
    assert totals["projects"] == len(body["projects"])


def test_busiest_projects_are_ordered_by_activity(v2_client: TestClient) -> None:
    for _ in range(3):
        post_event(v2_client, project="tether")
    post_event(v2_client, project="drends", task="GH-201")
    assert overview(v2_client)["busiest_projects"] == ["tether", "drends"]


# --------------------------------------------------------- the cross-project signal


def test_an_agent_split_across_projects_is_flagged(v2_client: TestClient) -> None:
    claim(v2_client, "leo-codex", "tether", "GH-142")
    claim(v2_client, "leo-codex", "drends", "GH-201")
    claim(v2_client, "nina", "drends", "GH-202")  # one project only

    body = overview(v2_client)
    overloaded = body["overloaded_agents"]
    assert [a["agent"] for a in overloaded] == ["leo-codex"]
    assert overloaded[0]["project_count"] == 2
    assert overloaded[0]["task_count"] == 2
    assert overloaded[0]["projects"] == {"drends": ["GH-201"], "tether": ["GH-142"]}

    assert body["agents_across_projects"]["nina"] == {"drends": ["GH-202"]}
    assert body["totals"]["overloaded_agents"] == 1


def test_suggested_actions_name_the_overloaded_agent(v2_client: TestClient) -> None:
    claim(v2_client, "leo-codex", "tether", "GH-142")
    claim(v2_client, "leo-codex", "drends", "GH-201")
    post_event(
        v2_client,
        project="tether",
        task="GH-143",
        event_type="BLOCKED",
        summary="waiting on the SCARED-C export",
    )

    actions = overview(v2_client)["suggested_actions"]
    assert actions, "an overloaded agent and a blocked task must produce actions"
    first = actions[0]
    assert "leo-codex" in first
    assert "2 projects" in first
    assert "GH-142" in first and "GH-201" in first
    assert any("unblock tether" in action for action in actions)


def test_a_single_project_agent_is_never_overloaded(v2_client: TestClient) -> None:
    claim(v2_client, "leo-codex", "tether", "GH-142")
    claim(v2_client, "leo-codex", "tether", "GH-143")  # two tasks, one project

    body = overview(v2_client)
    assert body["overloaded_agents"] == []
    assert body["agents_across_projects"] == {"leo-codex": {"tether": ["GH-142", "GH-143"]}}
    assert not any("consider releasing" in action for action in body["suggested_actions"])


def test_releasing_a_claim_clears_the_overload(v2_client: TestClient) -> None:
    claim(v2_client, "leo-codex", "tether", "GH-142")
    claim(v2_client, "leo-codex", "drends", "GH-201")
    assert overview(v2_client)["overloaded_agents"]

    released = v2_client.post(
        "/release", json={"agent": "leo-codex", "project": "drends", "task": "GH-201"}
    )
    assert released.status_code == 200, released.text
    assert overview(v2_client)["overloaded_agents"] == []


def test_the_window_narrows_the_activity_counts(v2_client: TestClient) -> None:
    post_event(v2_client, project="tether")
    body = overview(v2_client, window_hours=1)
    assert body["window_hours"] == 1
    assert by_project(body)["tether"]["events_in_window"] == 1
