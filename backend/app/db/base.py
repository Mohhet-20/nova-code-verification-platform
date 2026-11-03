from __future__ import annotations

import secrets
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


_id_lock = threading.Lock()
_last_id_ns = 0


def new_id() -> str:
    """32 hex characters: a nanosecond timestamp, strictly increasing within the process, then 64 random bits.

    Time-ordered so that rows created in the same clock tick still sort in creation order: every timeline query
    orders by ``(created_at, id)`` and relies on ``id`` to break ties (a random uuid4 made those orders arbitrary).
    """
    global _last_id_ns
    with _id_lock:
        _last_id_ns = max(time.time_ns(), _last_id_ns + 1)
        stamp = _last_id_ns
    return f"{stamp:016x}{secrets.token_hex(8)}"


def utcnow() -> datetime:
    return datetime.now(UTC)
