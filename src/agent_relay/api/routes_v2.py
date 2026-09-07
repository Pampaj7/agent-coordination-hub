"""V2 HTTP surface: experiment links, the cross-project overview, and A2A.

Same rule as ``api/routes.py``: routers stay thin — validate, call a service, shape
the response. The one piece of real logic that lives here is the JSON-RPC envelope
for A2A, and that is deliberate: envelope handling is transport, and
``services/a2a.py`` is kept transport-agnostic so it can be tested as plain dicts.

Four routers are exported so they can be wired (and secured) independently::

    experiments_router   GET  /experiments                 auth
    overview_router      GET  /coordination/overview       auth
    a2a_router           GET  /.well-known/agent.json      open — discovery must work
    a2a_rpc_router       POST /a2a                          auth
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent_relay.api.deps import AuthDep, GitHubDep, SessionDep, SettingsDep
from agent_relay.services import a2a as a2a_service
from agent_relay.services import experiments as experiment_service
from agent_relay.services import overview as overview_service
from agent_relay.services.experiments import ExperimentRun
from agent_relay.services.overview import RelayOverview

# JSON-RPC 2.0 reserved error codes. A2A carries its own errors in a 200 response,
# so an unknown method is *not* an HTTP failure — clients parse the envelope.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SUPPORTED_METHODS = ("message/send",)

experiments_router = APIRouter(dependencies=[AuthDep])
overview_router = APIRouter(dependencies=[AuthDep])
a2a_router = APIRouter()
a2a_rpc_router = APIRouter(dependencies=[AuthDep])


# --------------------------------------------------------------------- experiments


@experiments_router.get("/experiments", response_model=list[ExperimentRun], tags=["experiments"])
def get_experiments(
    session: SessionDep,
    settings: SettingsDep,
    project: str | None = None,
    limit: Annotated[int, Query(ge=1, le=experiment_service.MAX_LIMIT)] = (
        experiment_service.DEFAULT_LIMIT
    ),
) -> list[ExperimentRun]:
    """W&B / MLflow runs referenced by the event log, newest first.

    Link-only: nothing here talks to a tracker, so this endpoint answers at the same
    speed whether W&B is up, down, or was never configured.
    """
    return experiment_service.runs_for_project(session, project, limit=limit, settings=settings)


# ----------------------------------------------------------------------- overview


@overview_router.get("/coordination/overview", response_model=RelayOverview, tags=["coordination"])
def get_coordination_overview(
    session: SessionDep,
    settings: SettingsDep,
    github: GitHubDep,
    window_hours: Annotated[int, Query(ge=1, le=24 * 90)] | None = None,
    idle_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
) -> RelayOverview:
    """Every project at once: counts, agents split across projects, next steps."""
    return overview_service.build_overview(
        session,
        settings,
        window_hours=window_hours,
        idle_hours=idle_hours,
        github=github,
    )


# ---------------------------------------------------------------------------- a2a


@a2a_router.get("/.well-known/agent.json", tags=["a2a"])
def get_agent_card(settings: SettingsDep) -> dict[str, Any]:
    """A2A Agent Card. Intentionally unauthenticated: discovery precedes credentials.

    It contains only public facts — a name, a URL, and the list of skills — so
    serving it openly leaks nothing even on a relay that requires a token.
    """
    return a2a_service.agent_card(settings)


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    # HTTP 200 on purpose: a JSON-RPC error is a successful transport carrying a
    # protocol-level failure. Returning 500 would hide it from spec-compliant clients.
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


@a2a_rpc_router.post("/a2a", tags=["a2a"])
async def post_a2a(request: Request, session: SessionDep, settings: SettingsDep) -> JSONResponse:
    """JSON-RPC 2.0 entry point. Supports ``message/send`` and nothing else.

    Every failure path below produces a JSON-RPC error object; this endpoint never
    returns a bare 500, because a peer agent's only contract is the envelope.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any decoding failure is a JSON-RPC parse error
        return _rpc_error(None, PARSE_ERROR, "Parse error: request body is not valid JSON.")

    if not isinstance(body, dict):
        return _rpc_error(None, INVALID_REQUEST, "Invalid Request: expected a JSON-RPC object.")

    request_id = body.get("id") if isinstance(body.get("id"), (str, int)) else None
    method = body.get("method")
    if not isinstance(method, str) or body.get("jsonrpc") != "2.0":
        return _rpc_error(
            request_id,
            INVALID_REQUEST,
            "Invalid Request: 'jsonrpc' must be \"2.0\" and 'method' must be a string.",
        )

    if method not in SUPPORTED_METHODS:
        return _rpc_error(
            request_id,
            METHOD_NOT_FOUND,
            f"Method not found: {method}",
            {"supported": list(SUPPORTED_METHODS)},
        )

    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "Invalid params: expected an object.")

    try:
        # handle_message is synchronous (it uses the sync ORM session), so it runs in
        # the threadpool exactly like every other blocking route in this app.
        result = await run_in_threadpool(
            a2a_service.handle_message, session, params, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 - a peer agent gets -32603, never a 500
        return _rpc_error(request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
