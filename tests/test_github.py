"""GitHub linking. No credentials required: reads are mocked, links are pure functions."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import app
from agent_relay.services.github import GitHubService
from tests.conftest import INTEGRATION_ENV, event_payload

REPO = "https://github.com/acme/tether"


def service(**overrides: Any) -> GitHubService:
    return GitHubService(
        Settings(
            AGENT_RELAY_DB_URL="sqlite://",
            GITHUB_OWNER="acme",
            GITHUB_REPO="tether",
            **overrides,
        )
    )


def test_task_ids_become_issue_links() -> None:
    github = service()
    assert github.enabled is True
    assert github.repo_url == REPO
    assert github.issue_number("GH-142") == 142
    assert github.task_url("GH-142") == f"{REPO}/issues/142"
    assert github.branch_url("exp/temporal-ablation") == f"{REPO}/tree/exp/temporal-ablation"
    assert github.pr_url(17) == f"{REPO}/pull/17"


def test_non_issue_task_ids_are_left_alone() -> None:
    github = service()
    assert github.issue_number("refactor-loader") is None
    assert github.task_url("refactor-loader") is None
    assert github.task_url(None) is None
    assert github.issue_number("GH-142a") is None


def test_task_prefix_is_configurable() -> None:
    github = service(GITHUB_TASK_PREFIX="ISSUE-")
    assert github.issue_number("ISSUE-9") == 9
    assert github.issue_number("GH-9") is None


def test_artifacts_link_to_commits_and_pass_urls_through() -> None:
    github = service()
    assert github.artifact_url("82bd18f") == f"{REPO}/commit/82bd18f"
    assert github.artifact_url("commit 82bd18f") == f"{REPO}/commit/82bd18f"
    assert github.artifact_url("https://wandb.ai/run/1") == "https://wandb.ai/run/1"
    # A data file is not a commit; we do not guess a branch for it.
    assert github.artifact_url("runs/ablation_horizon.csv") is None


def test_everything_degrades_to_none_when_unconfigured() -> None:
    github = GitHubService(Settings(AGENT_RELAY_DB_URL="sqlite://"))
    assert github.enabled is False
    assert github.repo_url is None
    assert github.task_url("GH-142") is None
    assert github.links_for(task="GH-142", branch="main") == {}


@pytest.fixture
def gh_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GITHUB_REPO", "tether")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(app) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


def test_events_and_tasks_carry_github_links(gh_client: TestClient) -> None:
    created = gh_client.post("/events", json=event_payload()).json()
    assert created["github_url"] == f"{REPO}/issues/142"

    tasks = gh_client.get("/tasks").json()
    assert tasks[0]["github_url"] == f"{REPO}/issues/142"

    context = gh_client.get("/context", params={"project": "tether"}).json()
    assert context["github"]["repo"] == "acme/tether"
    assert context["github"]["task_links"]["GH-142"] == f"{REPO}/issues/142"


def test_github_endpoints_are_503_when_unconfigured(client: TestClient) -> None:
    assert client.get("/github/issues/142").status_code == 503
    assert client.get("/github/pulls").status_code == 503


def test_issue_metadata_is_fetched_and_trimmed(
    gh_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
        assert url == "https://api.github.com/repos/acme/tether/issues/142"
        return httpx.Response(
            200,
            json={
                "number": 142,
                "title": "Temporal ablation",
                "state": "open",
                "html_url": f"{REPO}/issues/142",
                "labels": [{"name": "experiment"}],
                "assignees": [{"login": "leonardo"}],
                "body": "a very long body we do not want to carry around",
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)
    body = gh_client.get("/github/issues/142").json()
    assert body == {
        "number": 142,
        "title": "Temporal ablation",
        "state": "open",
        "url": f"{REPO}/issues/142",
        "labels": ["experiment"],
        "assignees": ["leonardo"],
        "is_pull_request": False,
    }


def test_github_being_down_does_not_crash_the_relay(
    gh_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("github is unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "get", broken, raising=False)
    assert gh_client.get("/github/issues/142").status_code == 404
    assert gh_client.get("/github/pulls").json() == []
    # The relay itself is unaffected.
    assert gh_client.post("/events", json=event_payload()).status_code == 201


def test_open_pulls_are_trimmed(gh_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "number": 17,
                    "title": "Temporal ablation",
                    "head": {"ref": "exp/temporal-ablation"},
                    "base": {"ref": "main"},
                    "user": {"login": "leonardo"},
                    "draft": False,
                    "html_url": f"{REPO}/pull/17",
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)
    pulls = gh_client.get("/github/pulls").json()
    assert pulls == [
        {
            "number": 17,
            "title": "Temporal ablation",
            "branch": "exp/temporal-ablation",
            "base": "main",
            "author": "leonardo",
            "draft": False,
            "url": f"{REPO}/pull/17",
        }
    ]
