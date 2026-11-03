"""Idempotency keys for retryable external-effect writes. A key is scoped to (organization, user, route), expires, and is bound to the
request payload: reuse with a different payload is rejected. It is a replay guard, never an authentication credential."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NovaError, ValidationFailedError
from app.db.base import utcnow
from app.models import IdempotencyRecord

KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
TTL = timedelta(hours=24)


class KeyReused(NovaError):
    status_code = 422
    code = "idempotency_key_reused"


def request_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def begin(db: Session, workspace_id: str, user_id: str, scope: str, key: str | None, payload: Any) -> tuple[IdempotencyRecord | None, dict[str, Any] | None, int | None]:
    """Returns (record, replay_body, replay_status). With no key: (None, None, None). With a completed matching key: the stored response."""
    if key is None:
        return None, None, None
    if not KEY.match(key):
        raise ValidationFailedError("Idempotency-Key must be 8-80 characters of letters, digits and _ . : -")
    h = request_hash(payload)
    now = utcnow()
    rec = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.workspace_id == workspace_id, IdempotencyRecord.user_id == user_id,
                                                    IdempotencyRecord.scope == scope, IdempotencyRecord.key == key))
    if rec is not None:
        exp = rec.expires_at if rec.expires_at.tzinfo else rec.expires_at.replace(tzinfo=now.tzinfo)
        if exp <= now:
            db.delete(rec)
            db.flush()
            rec = None
    if rec is not None:
        if rec.request_hash != h:
            raise KeyReused("This Idempotency-Key was already used with a different request.")
        if rec.response_status is None:
            raise ConflictError("A request with this Idempotency-Key is still in progress.")
        return rec, rec.response_body, rec.response_status
    rec = IdempotencyRecord(workspace_id=workspace_id, user_id=user_id, scope=scope, key=key, request_hash=h, expires_at=now + TTL)
    db.add(rec)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A request with this Idempotency-Key is still in progress.") from None
    return rec, None, None


def complete(rec: IdempotencyRecord | None, status: int, body: dict[str, Any]) -> None:
    if rec is not None:
        rec.response_status, rec.response_body = status, body


def purge_expired(db: Session) -> int:
    n = 0
    for r in db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.expires_at <= utcnow())):
        db.delete(r)
        n += 1
    return n
