"""Small SQLAlchemy type helpers."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import Dialect, Text
from sqlalchemy.types import DateTime, TypeDecorator


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Always store naive UTC, always return timezone-aware UTC.

    SQLite has no native timezone support, so a plain ``DateTime(timezone=True)``
    silently hands back naive datetimes on read, which then explode when compared
    with ``datetime.now(dt.UTC)``. This decorator keeps both sides honest.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC)


class JSONText(TypeDecorator[Any]):
    """JSON stored as TEXT.

    We keep this explicit rather than using ``sqlalchemy.JSON`` so the columns are
    readable with plain ``sqlite3`` from a terminal, which matters for a tool whose
    whole point is transparency.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
