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
from agent_relay.config import get_settings
from agent_relay.db.session import init_db

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
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Relay",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(public_router)
    app.include_router(router)
    return app


app = create_app()
