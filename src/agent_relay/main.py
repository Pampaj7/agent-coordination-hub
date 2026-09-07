"""ASGI application.

Run with either::

    agent-relay serve
    uvicorn agent_relay.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_relay import __version__
from agent_relay.api.routes import public_router, router
from agent_relay.api.routes_coordinator import router as coordinator_router
from agent_relay.api.routes_dashboard import router as dashboard_router
from agent_relay.api.routes_github_hooks import router as github_hooks_router
from agent_relay.api.routes_inbox import router as inbox_router
from agent_relay.api.routes_presence import router as presence_router
from agent_relay.api.routes_priorities import router as priorities_router
from agent_relay.api.routes_slack_hooks import router as slack_hooks_router
from agent_relay.api.routes_v2 import (
    a2a_router,
    a2a_rpc_router,
    experiments_router,
    overview_router,
)
from agent_relay.config import get_settings
from agent_relay.db.session import init_db
from agent_relay.services.scheduler import Scheduler

logger = logging.getLogger(__name__)

DESCRIPTION = """
Lightweight coordination layer for a small team of humans running multiple coding and
research agents.

* **GitHub** is the source of truth for code, issues, PRs and artifacts.
* **Slack** is the human-readable event bus.
* **This relay** holds the structured coordination state: events, task claims, context.

It does not replace any of them.
"""


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db(settings)
    logger.info("agent-relay %s ready | %s", __version__, settings.public_summary())

    # Background jobs (sweeper / GitHub polling / status posts). Each is opt-in and
    # isolated; see services/scheduler.py.
    scheduler = Scheduler(settings)
    app.state.scheduler = scheduler
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Relay",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(public_router)
    app.include_router(router)
    # --- V2 ---
    app.include_router(presence_router)
    app.include_router(coordinator_router)
    app.include_router(inbox_router)
    app.include_router(priorities_router)
    app.include_router(github_hooks_router)
    app.include_router(slack_hooks_router)
    app.include_router(dashboard_router)
    app.include_router(experiments_router)
    app.include_router(overview_router)
    app.include_router(a2a_router)
    app.include_router(a2a_rpc_router)
    return app


app = create_app()
