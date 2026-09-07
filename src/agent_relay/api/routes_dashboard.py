"""The read-only web dashboard.

Two routes and no framework. ``GET /dashboard`` hands back one self-contained HTML
file; the page then talks to the ordinary JSON API. Nothing here can change relay
state — the dashboard is a window onto the relay, not a second way to drive it.

Auth, honestly
--------------
The HTML page carries **no** ``AuthDep``: it contains no data, only markup, and a
401 on the page itself would just be a blank screen with no way to recover. Its
``fetch`` calls, however, hit the token-protected API and *will* 401 when
``AGENT_RELAY_API_TOKEN`` is set.

The tempting fix — templating the server's token into the page — is wrong: it would
hand the shared team secret to anyone who can reach the port, which is precisely the
population the token exists to keep out. So the page asks the viewer for the token
instead, keeps it in ``localStorage`` under ``agent_relay_token``, and sends it as a
bearer header. The secret then lives in the browser of someone who already knew it.

``/dashboard/projects`` is an API call rather than a page, so it *does* carry
``AuthDep`` and behaves like every other endpoint.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from agent_relay.api.deps import AuthDep, SessionDep, SettingsDep
from agent_relay.db.models import Event, Project

router = APIRouter(tags=["dashboard"])

#: Resolved relative to this module so it works from a source checkout and from an
#: installed wheel alike (hatchling ships everything under ``src/agent_relay``).
DASHBOARD_FILE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@lru_cache(maxsize=1)
def dashboard_html() -> str:
    """The page source, read once.

    Cached rather than re-read per request: the file is packaged with the code and
    cannot change while the process runs, and every open dashboard re-requests
    nothing but JSON afterwards. ``dashboard_html.cache_clear()`` is available for
    tests and for anyone hacking on the page with ``--reload``.
    """
    return DASHBOARD_FILE.read_text(encoding="utf-8")


def _require_enabled(enabled: bool) -> None:
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The dashboard is disabled on this relay (set AGENT_RELAY_DASHBOARD=true).",
        )


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(settings: SettingsDep) -> HTMLResponse:
    """Serve the dashboard page. Deliberately unauthenticated — see the module docstring."""
    _require_enabled(settings.dashboard_enabled)
    return HTMLResponse(content=dashboard_html())


@router.get("/dashboard/projects", response_model=list[str], dependencies=[AuthDep])
def dashboard_projects(session: SessionDep, settings: SettingsDep) -> list[str]:
    """Every project name the relay knows, for the header's project selector.

    Cheaper than deriving the list client-side from ``/tasks``, and it also surfaces
    a project whose tasks have all aged out. The ``projects`` registry is rebuilt
    from events, but a claim can exist without one, so both sources are unioned.
    """
    _require_enabled(settings.dashboard_enabled)
    registered = session.execute(select(Project.name)).scalars()
    from_events = session.execute(select(Event.project).distinct()).scalars()
    return sorted({*registered, *from_events})
