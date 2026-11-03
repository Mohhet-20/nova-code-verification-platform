"""Durable integration jobs (SQLite-backed; no Redis/Kafka).

Lifecycle: ``queued`` -> ``running`` (leased) -> ``succeeded`` | ``queued`` again with backoff | ``dead`` after ``max_attempts``. A worker that dies
leaves a ``running`` row whose lease expires; ``run_due`` re-claims it, so jobs survive restarts. Payloads are identifiers only (``input_ref``);
credentials are resolved at execution time and never stored. Operations are a closed, compiled-in set. Polling is bounded: nothing reschedules
itself; a recurring check must be enqueued by an authorized caller or the optional periodic loop (one job per installation, de-duplicated while live).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.db.base import utcnow
from app.models import IntegrationInstallation, IntegrationJob
from app.observability import metrics as m
from app.services.integrations import service as svc
from app.services.integrations.netguard import HttpClient

OPERATIONS = ("connection_check",)


def enqueue(db: Session, *, workspace_id: str, actor: str | None, operation: str, installation_id: str | None, input_ref: dict[str, Any] | None = None, max_attempts: int = 3) -> IntegrationJob:
    if operation not in OPERATIONS:
        raise ConflictError("Unknown job operation.")
    job = IntegrationJob(workspace_id=workspace_id, installation_id=installation_id, operation=operation, input_ref=input_ref or {}, dedupe_key=f"{operation}:{installation_id}",
                         max_attempts=max_attempts, correlation_id=uuid.uuid4().hex[:16], created_by=actor, scheduled_at=utcnow())
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("An identical job is already queued or running.") from None
    return job


def _aware(d):  # type: ignore[no-untyped-def]
    from datetime import UTC
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def run_due(db: Session, settings: Settings, *, http: HttpClient | None = None, now=None, limit: int = 20) -> dict[str, int]:  # type: ignore[no-untyped-def]
    now = now or utcnow()
    rows = list(db.scalars(select(IntegrationJob).where(or_(and_(IntegrationJob.state == "queued", IntegrationJob.scheduled_at <= now),
                                                           and_(IntegrationJob.state == "running", IntegrationJob.lease_until <= now))).order_by(IntegrationJob.scheduled_at).limit(limit)))
    for j in rows:
        j.state, j.lease_until, j.started_at = "running", now + timedelta(seconds=settings.webhook_lease_seconds), now
    db.commit()
    out = {"claimed": len(rows), "succeeded": 0, "requeued": 0, "dead": 0}
    for j in rows:
        j.attempts += 1
        cat: str | None = None
        try:
            if j.operation == "connection_check":
                inst = db.get(IntegrationInstallation, j.installation_id) if j.installation_id else None
                if inst is None or inst.removed_at is not None or inst.workspace_id != j.workspace_id:
                    cat = "installation_gone"
                elif not inst.enabled:
                    cat = "installation_disabled"
                else:
                    res = svc.test(db, settings, None, inst, http)
                    cat = None if res["ok"] else (res["category"] or "failed")
        except Exception:  # noqa: BLE001
            cat = "internal_error"
        permanent = cat in ("installation_gone", "installation_disabled")
        if cat is None:
            j.state, j.finished_at, j.error_category, j.lease_until = "succeeded", now, None, None
            out["succeeded"] += 1
        elif permanent or j.attempts >= j.max_attempts:
            j.state, j.finished_at, j.error_category, j.lease_until = "dead", now, cat, None
            out["dead"] += 1
        else:
            j.state, j.error_category, j.lease_until = "queued", cat, None
            j.scheduled_at = now + timedelta(seconds=min(settings.webhook_backoff_max_seconds, settings.webhook_backoff_base_seconds * 2 ** (j.attempts - 1)))
            out["requeued"] += 1
        m.INT_JOBS.inc(operation=j.operation, result="ok" if cat is None else ("dead" if j.state == "dead" else "retry"))
        db.commit()
    return out


def stats(db: Session, now=None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    now = now or utcnow()
    counts = {s: 0 for s in ("queued", "running", "dead")}
    oldest = None
    for j in db.scalars(select(IntegrationJob).where(IntegrationJob.state.in_(tuple(counts)))):
        counts[j.state] += 1
        if j.state == "queued" and _aware(j.scheduled_at) <= _aware(now) and (oldest is None or _aware(j.scheduled_at) < oldest):
            oldest = _aware(j.scheduled_at)
    age = max(0.0, (_aware(now) - oldest).total_seconds()) if oldest else None
    return {**counts, "oldest_due_age_seconds": age, "stuck": bool(age is not None and age > 900)}


def serialize(j: IntegrationJob) -> dict[str, Any]:
    return {"id": j.id, "operation": j.operation, "installation_id": j.installation_id, "state": j.state, "attempts": j.attempts, "max_attempts": j.max_attempts,
            "scheduled_at": j.scheduled_at, "started_at": j.started_at, "finished_at": j.finished_at, "error_category": j.error_category, "correlation_id": j.correlation_id}
