"""Engine + session lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agent_relay.config import Settings, get_settings, redact_db_url
from agent_relay.db.migrate import migrate

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _prepare_sqlite_path(db_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return
    raw = db_url[len(prefix) :]
    if not raw or raw == ":memory:":
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(db_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    _prepare_sqlite_path(db_url)
    engine = create_engine(db_url, connect_args=connect_args, future=True)

    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
            cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
            # WAL keeps concurrent agent writers from tripping over each other;
            # foreign_keys is off by default in SQLite and we want it on.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


def init_db(settings: Settings | None = None) -> Engine:
    """Create the engine (once) and ensure tables exist."""
    global _engine, _SessionFactory
    settings = settings or get_settings()
    if _engine is None:
        _engine = create_db_engine(settings.db_url)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        logger.info("database ready at %s", redact_db_url(settings.db_url))
    # create_all + additive ALTERs, so a v1 database keeps its history on upgrade.
    migrate(_engine)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session; commits are explicit in services."""
    if _SessionFactory is None:
        init_db()
    assert _SessionFactory is not None
    with _SessionFactory() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone session for scripts and background work."""
    if _SessionFactory is None:
        init_db()
    assert _SessionFactory is not None
    with _SessionFactory() as session:
        yield session
