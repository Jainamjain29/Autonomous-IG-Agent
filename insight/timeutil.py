"""UTC helpers and the UTCDateTime column type.

SQLite has no timezone-aware timestamp type and silently drops tzinfo, so every
datetime is normalized to UTC on write and comes back tagged as UTC on read.
Naive datetimes are rejected on write: we never guess which zone they meant.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

UTC = timezone.utc


def utcnow():
    return datetime.now(UTC)


def to_utc(value):
    """Aware datetime -> same instant in UTC. Naive datetimes raise ValueError."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime {value!r}: pass a timezone-aware datetime")
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = to_utc(value)
        # SQLite would drop tzinfo anyway; store naive UTC so the text is unambiguous.
        # Postgres keeps the aware value in a timestamptz column.
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
