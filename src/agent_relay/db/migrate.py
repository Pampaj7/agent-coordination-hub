"""Additive schema migration.

A relay upgraded from v1 has real coordination history in it. Rather than pull in
Alembic — a whole migration framework for a three-person tool — we do the only kind
of change this project actually makes: add columns and tables that did not exist.

The rule is deliberately narrow, and enforced by that narrowness being the only
thing implemented: **nothing is ever dropped, renamed or retyped here.** If a change
ever needs more than this, that is the signal to adopt Alembic, not to extend this.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

from agent_relay.db.models import Base

logger = logging.getLogger(__name__)


def _column_ddl(column: object) -> str:
    """Render an ``ALTER TABLE ... ADD COLUMN`` type clause for one mapped column."""
    from sqlalchemy import Column

    assert isinstance(column, Column)
    ddl = f"{column.name} {column.type.compile()}"
    default = column.default
    # Only a literal scalar default can be inlined into DDL; a callable default
    # (like utcnow) is applied by the ORM on insert and has nothing to render here.
    if default is not None and getattr(default, "is_scalar", False):
        value = getattr(default, "arg", None)
        if isinstance(value, str):
            ddl += f" DEFAULT '{value}'"
        elif isinstance(value, bool):
            ddl += f" DEFAULT {int(value)}"
        elif isinstance(value, (int, float)):
            ddl += f" DEFAULT {value}"
    return ddl


def migrate(engine: Engine) -> list[str]:
    """Bring an existing database up to the current model definitions.

    Returns the list of changes applied, so startup can log them (and tests can
    assert on them). Safe to run on every boot; a current database is a no-op.
    """
    # New tables first — create_all never touches existing ones.
    Base.metadata.create_all(engine)

    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.default is None:
                    # Cannot add a NOT NULL column without a default to a table that
                    # already has rows. Loudly skip rather than corrupt a live DB.
                    logger.error(
                        "cannot add required column %s.%s automatically; "
                        "add it by hand or give it a default",
                        table.name,
                        column.name,
                    )
                    continue
                connection.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(column)}")
                )
                applied.append(f"{table.name}.{column.name}")

        # Backfill: rows written before `source` existed came from agents.
        if "events.source" in applied:
            connection.execute(text("UPDATE events SET source = 'agent' WHERE source IS NULL"))

    if applied:
        logger.info("schema migration added: %s", ", ".join(applied))
    return applied
