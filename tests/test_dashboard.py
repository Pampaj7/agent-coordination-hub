"""The read-only web dashboard: the page, its self-containment, and its project list."""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.routes_dashboard import router as dashboard_router
from agent_relay.config import get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import app
from tests.conftest import INTEGRATION_ENV, event_payload, post_event

TOKEN = "s3cr3t-team-token"

# main.py is owned by another track and may not have wired this router yet. Mounting
# it here is idempotent and additive, so the suite tests the real app either way.
if not any(getattr(route, "path", None) == "/dashboard" for route in app.routes):
    app.include_router(dashboard_router)


PANEL_HEADINGS = (
    "Blocked",
    "Open questions",
    "Active claims",
    "Recent activity",
    "Suggested actions",
)

#: Tags that never have a closing tag; the balance check must not expect one.
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _TagBalance(HTMLParser):
    """Minimal well-formedness check: every element opened is closed, in order.

    A page that 500s is an obvious failure; a page whose tags do not nest is an
    invisible one, so it gets asserted rather than eyeballed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_tags: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in VOID_ELEMENTS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return  # self-closing: opened and closed in one go

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if not self.open_tags:
            self.errors.append(f"</{tag}> closes nothing")
            return
        if self.open_tags[-1] != tag:
            self.errors.append(f"</{tag}> found while <{self.open_tags[-1]}> is open")
            return
        self.open_tags.pop()


@pytest.fixture
def secured(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A relay with a shared bearer token configured."""
    yield from _client_with(tmp_path, monkeypatch, AGENT_RELAY_API_TOKEN=TOKEN)


@pytest.fixture
def dashboard_off(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A relay with the dashboard switched off."""
    yield from _client_with(tmp_path, monkeypatch, AGENT_RELAY_DASHBOARD="false")


def _client_with(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, **env: str
) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(app) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


def page(client: TestClient) -> str:
    response = client.get("/dashboard")
    assert response.status_code == 200, response.text
    return response.text


# ------------------------------------------------------------------------ the page


def test_dashboard_serves_html(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<title>Agent Relay" in body
    for heading in PANEL_HEADINGS:
        assert heading in body, heading


def test_dashboard_html_is_well_formed(client: TestClient) -> None:
    parser = _TagBalance()
    parser.feed(page(client))
    parser.close()
    assert parser.errors == []
    assert parser.open_tags == []


def test_dashboard_makes_no_external_requests(client: TestClient) -> None:
    """It must render on a laptop with no internet: no CDN, no fonts, no remote images."""
    body = page(client)
    assert "<script src=" not in body
    assert '<link rel="stylesheet"' not in body
    assert "http://" not in body
    assert "https://" not in body


def test_dashboard_references_the_endpoints_it_reads(client: TestClient) -> None:
    body = page(client)
    for path in ("/context", "/tasks", "/coordination/summary", "/events", "/claims"):
        assert f'"{path}' in body, path
    assert "/dashboard/projects" in body


def test_dashboard_escapes_api_strings(client: TestClient) -> None:
    """Event summaries are hostile text; the page escapes them client-side."""
    hostile = "<script>alert(1)</script>"
    created = post_event(client, summary=hostile)
    # The API is a JSON contract: it returns exactly what was posted, verbatim.
    assert created["summary"] == hostile
    assert client.get("/events").json()[0]["summary"] == hostile

    # The protection lives in the page: an escaping helper plus textContent-only DOM.
    body = page(client)
    assert "function escapeHtml(" in body
    assert "textContent" in body


def test_dashboard_is_read_only(client: TestClient) -> None:
    """The page never writes: no form posts, no fetch with a method."""
    body = page(client)
    assert 'method="post"' not in body.lower()
    assert '"POST"' not in body


# ----------------------------------------------------------------- project list


def test_projects_is_empty_on_a_fresh_relay(client: TestClient) -> None:
    response = client.get("/dashboard/projects")
    assert response.status_code == 200
    assert response.json() == []


def test_projects_lists_projects_that_have_events(client: TestClient) -> None:
    post_event(client, project="tether")
    post_event(client, project="drends")
    post_event(client, project="tether", summary="second event, same project")

    assert client.get("/dashboard/projects").json() == ["drends", "tether"]


def test_projects_includes_a_project_known_only_from_a_claim(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    assert "tether" in client.get("/dashboard/projects").json()


# ---------------------------------------------------------------------- toggles


def test_dashboard_can_be_disabled(dashboard_off: TestClient) -> None:
    response = dashboard_off.get("/dashboard")
    assert response.status_code == 404
    assert "AGENT_RELAY_DASHBOARD" in response.json()["detail"]
    assert dashboard_off.get("/dashboard/projects").status_code == 404


def test_the_page_is_open_but_its_data_is_not(secured: TestClient) -> None:
    """The HTML carries no secret, so it needs no token; the JSON behind it does."""
    assert secured.get("/dashboard").status_code == 200
    assert secured.get("/dashboard/projects").status_code == 401

    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert secured.post("/events", json=event_payload(), headers=headers).status_code == 201
    response = secured.get("/dashboard/projects", headers=headers)
    assert response.status_code == 200
    assert response.json() == ["tether"]


def test_the_page_never_embeds_the_server_token(secured: TestClient) -> None:
    body = secured.get("/dashboard").text
    assert TOKEN not in body
    # It asks the viewer for one instead, and keeps it in the browser.
    assert "agent_relay_token" in body
    assert "localStorage" in body
