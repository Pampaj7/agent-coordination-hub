"""GitHub ingestion: signature verification, webhook mapping, idempotency, polling.

No network and no credentials. The webhook secret is a local string, GitHub reads
are monkeypatched at the httpx layer exactly as in tests/test_github.py.

The contract these tests pin down:

* an unsigned or badly signed delivery never reaches the event log;
* the same delivery replayed (GitHub retries) produces exactly one event;
* ingested events are visibly *not* agent events — ``source="github"`` and a
  namespaced actor name.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_relay.api.routes_github_hooks import router as hooks_router
from agent_relay.config import Settings, get_settings
from agent_relay.db.models import Event
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.main import app
from agent_relay.services import github_ingest
from agent_relay.services.github import GitHubService
from tests.conftest import INTEGRATION_ENV

SECRET = "s3cr3t-webhook-key"
WEBHOOK_PATH = "/webhooks/github"
REPO = "https://github.com/acme/tether"
PROJECT = "tether"

GITHUB_ENV = (*INTEGRATION_ENV, "GITHUB_WEBHOOK_SECRET", "GITHUB_INGEST_PROJECT")


# ------------------------------------------------------------------- fixtures


def _ensure_router() -> None:
    """Mount the webhook router if the app has not already wired it."""
    if not any(getattr(route, "path", None) == WEBHOOK_PATH for route in app.routes):
        app.include_router(hooks_router)


def _relay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, secret: str | None
) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    for name in GITHUB_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GITHUB_REPO", "tether")
    monkeypatch.setenv("GITHUB_INGEST_PROJECT", PROJECT)
    if secret is not None:
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    _ensure_router()
    with TestClient(app) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def hook_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A relay with GitHub webhooks switched on."""
    yield from _relay(tmp_path, monkeypatch, secret=SECRET)


@pytest.fixture
def unconfigured_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The same relay with no webhook secret: the feature is off."""
    yield from _relay(tmp_path, monkeypatch, secret=None)


# -------------------------------------------------------------------- helpers


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(
    client: TestClient,
    event_name: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
    signature: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event_name,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    header_value = sign(body) if signature is None else signature
    if header_value:
        headers["X-Hub-Signature-256"] = header_value
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


def issue_payload(action: str = "opened", number: int = 142) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "Temporal ablation regresses EPE",
            "state": "open" if action != "closed" else "closed",
            "html_url": f"{REPO}/issues/{number}",
            "labels": [{"name": "experiment"}, {"name": "bug"}],
        },
        "sender": {"login": "leonardo"},
    }


def pull_payload(
    action: str = "opened", *, merged: bool = False, number: int = 17, title: str | None = None
) -> dict[str, Any]:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "title": title or "Add H=8 horizon",
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
            "draft": False,
            "html_url": f"{REPO}/pull/{number}",
            "head": {"ref": "exp/temporal-ablation"},
            "base": {"ref": "main"},
        },
        "sender": {"login": "leonardo"},
    }


def push_payload(commits: int = 7, ref: str = "refs/heads/exp/temporal-ablation") -> dict[str, Any]:
    return {
        "ref": ref,
        "deleted": False,
        "compare": f"{REPO}/compare/aaa...bbb",
        "commits": [
            {"id": f"{i:040x}", "message": f"commit {i}", "url": f"{REPO}/commit/{i:040x}"}
            for i in range(commits)
        ],
        "pusher": {"name": "leonardo"},
        "sender": {"login": "leonardo"},
    }


# ------------------------------------------------------------------ signature


def test_correct_signature_verifies() -> None:
    body = b'{"zen": "Non-blocking is better than blocking."}'
    assert github_ingest.verify_signature(SECRET, body, sign(body)) is True


def test_wrong_secret_and_tampered_body_are_rejected() -> None:
    body = b'{"action": "opened"}'
    assert github_ingest.verify_signature("other-secret", body, sign(body)) is False
    assert github_ingest.verify_signature(SECRET, b'{"action": "closed"}', sign(body)) is False


def test_missing_and_malformed_signature_headers_are_rejected() -> None:
    body = b"{}"
    digest = sign(body).removeprefix("sha256=")
    for header in (
        None,
        "",
        digest,  # no scheme
        f"sha1={digest}",  # wrong algorithm
        "sha256=",  # empty digest
        "sha256=not-hex-at-all",
        f"sha256={digest[:-1]}",  # truncated
        f"sha256={digest}ff",  # over-long
        "sha256=über-hex",  # non-ascii would blow up a naive compare
    ):
        assert github_ingest.verify_signature(SECRET, body, header) is False


def test_a_near_miss_signature_still_fails() -> None:
    """One flipped hex character must fail — compare_digest is exact, not fuzzy."""
    body = b'{"action": "opened"}'
    digest = sign(body).removeprefix("sha256=")
    flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    assert flipped != digest
    assert github_ingest.verify_signature(SECRET, body, f"sha256={flipped}") is False
    assert github_ingest.verify_signature(SECRET, body, f"sha256={digest}") is True


# -------------------------------------------------------------------- endpoint


def test_webhook_is_503_when_no_secret_is_configured(unconfigured_client: TestClient) -> None:
    response = deliver(unconfigured_client, "issues", issue_payload())
    assert response.status_code == 503
    assert "GITHUB_WEBHOOK_SECRET" in response.json()["detail"]


def test_webhook_is_401_on_a_bad_or_missing_signature(hook_client: TestClient) -> None:
    assert (
        deliver(hook_client, "issues", issue_payload(), signature="sha256=deadbeef").status_code
        == 401
    )
    assert deliver(hook_client, "issues", issue_payload(), signature="").status_code == 401
    # Nothing was written.
    assert hook_client.get("/events").json() == []


def test_webhook_does_not_require_the_relay_bearer_token(hook_client: TestClient) -> None:
    """GitHub cannot send our token; the HMAC is the authentication."""
    assert not any(
        getattr(route, "path", None) == WEBHOOK_PATH and getattr(route, "dependencies", [])
        for route in app.routes
    )
    assert deliver(
        hook_client, "ping", {"zen": "Anything added dilutes everything else."}
    ).json() == {
        "status": "pong",
        "event": None,
    }


def test_good_signature_is_accepted(hook_client: TestClient) -> None:
    response = deliver(hook_client, "issues", issue_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "created"


# --------------------------------------------------------------------- mapping


def test_issue_opened_becomes_an_update_on_the_task(hook_client: TestClient) -> None:
    event = deliver(hook_client, "issues", issue_payload()).json()["event"]
    assert event["event_type"] == "UPDATE"
    assert event["task"] == "GH-142"
    assert event["summary"] == "Issue GH-142 opened: Temporal ablation regresses EPE"
    assert event["artifacts"] == [f"{REPO}/issues/142"]
    assert event["details"]["labels"] == ["experiment", "bug"]
    assert event["details"]["state"] == "open"
    assert event["metadata"] == {
        "github_event": "issues",
        "action": "opened",
        "delivery": "delivery-1",
    }
    assert event["github_url"] == f"{REPO}/issues/142"


def test_issue_labelled_is_not_worth_recording(hook_client: TestClient) -> None:
    body = deliver(hook_client, "issues", issue_payload(action="labeled")).json()
    assert body == {"status": "ignored", "event": None}
    assert hook_client.get("/events").json() == []


def test_pull_request_opened_is_an_update_with_the_branch(hook_client: TestClient) -> None:
    event = deliver(hook_client, "pull_request", pull_payload()).json()["event"]
    assert event["event_type"] == "UPDATE"
    assert event["branch"] == "exp/temporal-ablation"
    assert event["task"] == "GH-17"
    assert event["summary"] == "PR #17 opened: Add H=8 horizon"
    assert event["artifacts"] == [f"{REPO}/pull/17"]
    assert event["details"]["merged"] is False


def test_merged_pull_request_is_a_decision(hook_client: TestClient) -> None:
    payload = pull_payload("closed", merged=True)
    event = deliver(hook_client, "pull_request", payload).json()["event"]
    assert event["event_type"] == "DECISION"
    assert "merged into main" in event["summary"]
    assert event["details"]["merged"] is True
    assert event["branch"] == "exp/temporal-ablation"


def test_closed_unmerged_pull_request_is_only_an_update(hook_client: TestClient) -> None:
    event = deliver(hook_client, "pull_request", pull_payload("closed")).json()["event"]
    assert event["event_type"] == "UPDATE"
    assert "closed without merging" in event["summary"]


def test_pull_request_referencing_a_task_lands_on_that_task(hook_client: TestClient) -> None:
    payload = pull_payload(title="Fix GH-142: clamp the horizon")
    event = deliver(hook_client, "pull_request", payload).json()["event"]
    # The PR belongs to the issue it closes, not to its own number.
    assert event["task"] == "GH-142"


def test_push_summarises_commits_and_caps_artifacts(hook_client: TestClient) -> None:
    event = deliver(hook_client, "push", push_payload(commits=7)).json()["event"]
    assert event["event_type"] == "UPDATE"
    assert event["branch"] == "exp/temporal-ablation"
    assert event["summary"].startswith("7 commits pushed to exp/temporal-ablation")
    assert event["details"]["commits"] == 7
    assert len(event["artifacts"]) == github_ingest.MAX_COMMIT_LINKS + 1
    assert event["artifacts"][-1] == "…and 2 more commits"


def test_empty_pushes_and_branch_deletions_are_skipped(hook_client: TestClient) -> None:
    empty = push_payload(commits=0)
    assert deliver(hook_client, "push", empty, delivery="d-empty").json()["status"] == "ignored"

    deletion = push_payload(commits=2)
    deletion["deleted"] = True
    assert deliver(hook_client, "push", deletion, delivery="d-del").json()["status"] == "ignored"

    tag = push_payload(commits=2, ref="refs/tags/v1.0.0")
    assert deliver(hook_client, "push", tag, delivery="d-tag").json()["status"] == "ignored"
    assert hook_client.get("/events").json() == []


def test_issue_comment_is_recorded_and_truncated(hook_client: TestClient) -> None:
    payload = {
        "action": "created",
        "issue": {"number": 142, "title": "Temporal ablation", "html_url": f"{REPO}/issues/142"},
        "comment": {
            "body": "x" * 900,
            "html_url": f"{REPO}/issues/142#issuecomment-1",
            "user": {"login": "sofia"},
        },
        "sender": {"login": "sofia"},
    }
    event = deliver(hook_client, "issue_comment", payload).json()["event"]
    assert event["task"] == "GH-142"
    assert event["summary"].startswith("Comment on GH-142 by sofia: ")
    assert len(event["summary"]) < 400
    assert len(event["details"]["comment"]) == github_ingest.MAX_COMMENT_CHARS


def test_unknown_event_types_are_ignored(hook_client: TestClient) -> None:
    body = deliver(hook_client, "star", {"action": "created"}).json()
    assert body == {"status": "ignored", "event": None}
    assert hook_client.get("/events").json() == []


def test_a_nonsense_payload_never_500s(hook_client: TestClient) -> None:
    assert deliver(hook_client, "issues", {"action": "opened"}, delivery="d-1").json()[
        "status"
    ] == ("ignored")
    assert deliver(hook_client, "push", {"ref": 12}, delivery="d-2").json()["status"] == "ignored"

    body = b"not json at all"
    response = hook_client.post(
        WEBHOOK_PATH,
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "d-3",
            "X-Hub-Signature-256": sign(body),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


# ----------------------------------------------------------------- idempotency


def test_replayed_delivery_creates_exactly_one_event(hook_client: TestClient) -> None:
    """GitHub retries deliveries. One push must never become three events."""
    first = deliver(hook_client, "issues", issue_payload(), delivery="retry-me")
    second = deliver(hook_client, "issues", issue_payload(), delivery="retry-me")
    third = deliver(hook_client, "issues", issue_payload(), delivery="retry-me")

    assert first.json()["status"] == "created"
    assert second.json() == {"status": "duplicate", "event": None}
    assert third.json() == {"status": "duplicate", "event": None}
    assert len(hook_client.get("/events").json()) == 1


def test_a_different_delivery_of_the_same_activity_is_a_new_event(hook_client: TestClient) -> None:
    deliver(hook_client, "issues", issue_payload(), delivery="a")
    deliver(hook_client, "issues", issue_payload(action="closed"), delivery="b")
    assert len(hook_client.get("/events").json()) == 2


def test_the_ledger_dedupes_even_without_a_delivery_header(hook_client: TestClient) -> None:
    body = json.dumps(issue_payload()).encode()
    headers = {"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)}
    assert hook_client.post(WEBHOOK_PATH, content=body, headers=headers).json()["status"] == (
        "created"
    )
    assert hook_client.post(WEBHOOK_PATH, content=body, headers=headers).json()["status"] == (
        "duplicate"
    )
    assert len(hook_client.get("/events").json()) == 1


# ------------------------------------------------------------------ provenance


def test_ingested_events_are_marked_as_github_and_cannot_pose_as_an_agent(
    hook_client: TestClient,
) -> None:
    event = deliver(hook_client, "issues", issue_payload()).json()["event"]
    assert event["agent"] == "github:leonardo"
    assert event["agent"].startswith(github_ingest.AGENT_PREFIX)
    # ":" is what makes the namespace safe: no relay agent name contains one.
    assert ":" in event["agent"]

    with session_scope() as session:
        row = session.get(Event, event["id"])
        assert row is not None
        assert row.source == "github"


def test_an_actorless_payload_is_still_namespaced(hook_client: TestClient) -> None:
    payload = issue_payload()
    payload.pop("sender")
    event = deliver(hook_client, "issues", payload).json()["event"]
    assert event["agent"] == "github:unknown"


def test_ingested_events_show_up_in_events_and_context(hook_client: TestClient) -> None:
    deliver(hook_client, "issues", issue_payload(), delivery="a")
    deliver(hook_client, "pull_request", pull_payload("closed", merged=True), delivery="b")

    events = hook_client.get("/events", params={"project": PROJECT}).json()
    assert {e["event_type"] for e in events} == {"UPDATE", "DECISION"}

    context = hook_client.get("/context", params={"project": PROJECT}).json()
    assert any(u["task"] == "GH-142" for u in context["recent_updates"])
    assert any("merged into main" in d["summary"] for d in context["recent_decisions"])
    assert "github:leonardo" in {a["agent"] for a in context["active_agents"]}


# --------------------------------------------------------------------- polling


def _pull_json(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR number {number}",
        "head": {"ref": f"exp/branch-{number}"},
        "base": {"ref": "main"},
        "user": {"login": "leonardo"},
        "draft": False,
        "html_url": f"{REPO}/pull/{number}",
    }


def test_poll_once_returns_nothing_when_github_is_unconfigured(hook_client: TestClient) -> None:
    bare = Settings(AGENT_RELAY_DB_URL="sqlite://")
    github = GitHubService(bare)
    with session_scope() as session:
        assert asyncio.run(github_ingest.poll_once(session, github, bare)) == []


def test_poll_once_creates_one_event_per_new_open_pull(
    hook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_pull_json(17), _pull_json(18)],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)
    settings = get_settings()
    github = GitHubService(settings)

    with session_scope() as session:
        created = asyncio.run(github_ingest.poll_once(session, github, settings))
        assert len(created) == 2
        assert [e.source for e in created] == ["github", "github"]
        assert created[0].agent == "github:leonardo"
        assert created[0].task == "GH-17"
        assert created[0].branch == "exp/branch-17"
        assert "Open PR #17" in created[0].summary

        # A second tick re-reads the same open PRs and must announce nothing.
        assert asyncio.run(github_ingest.poll_once(session, github, settings)) == []

    assert len(hook_client.get("/events", params={"project": PROJECT}).json()) == 2


def test_poll_once_degrades_to_empty_when_github_is_unreachable(
    hook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("github is unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "get", broken, raising=False)
    settings = get_settings()
    with session_scope() as session:
        assert (
            asyncio.run(github_ingest.poll_once(session, GitHubService(settings), settings)) == []
        )


# --------------------------------------------------------------------- project


def test_project_resolution_prefers_the_explicit_name_then_config_then_repo() -> None:
    configured = Settings(
        AGENT_RELAY_DB_URL="sqlite://",
        GITHUB_OWNER="acme",
        GITHUB_REPO="tether",
        GITHUB_INGEST_PROJECT="drends",
    )
    assert github_ingest.resolve_project(configured, "explicit") == "explicit"
    assert github_ingest.resolve_project(configured) == "drends"

    repo_only = Settings(AGENT_RELAY_DB_URL="sqlite://", GITHUB_OWNER="acme", GITHUB_REPO="tether")
    assert github_ingest.resolve_project(repo_only) == "tether"

    bare = Settings(AGENT_RELAY_DB_URL="sqlite://")
    assert github_ingest.resolve_project(bare) is None
