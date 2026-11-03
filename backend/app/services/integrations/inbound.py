"""Inbound provider webhooks (authenticated by signature only).

Order: size (API layer) -> uniform-404 lookup -> enabled -> rate limit -> signature over raw bytes -> JSON/structure -> repository mapping ->
delivery de-duplication -> persist a minimal summary -> process. Payloads are untrusted data: nothing in them selects a path, command, URL
or approval. CI events become *evidence rows* only; they never change a workflow, approval or release state. Raw payloads are not stored or logged.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError, NovaError
from app.db.base import utcnow
from app.models import CiEvidenceRun, IntegrationEvent, IntegrationInstallation, IntegrationSecret
from app.observability import metrics as m
from app.services.integrations import secrets as sec
from app.services.integrations.registry import (
    InboundAuthError,
    InboundEvent,
    InvalidInbound,
    Unsupported,
    get_adapter,
)

log = logging.getLogger("nova.integrations")
RATE_LIMIT_PER_MINUTE = 300
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


class InboundError(NovaError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code = status, code


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MINUTE:
            return False
        q.append(now)
        return True


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()


def _webhook_secret(db: Session, settings: Settings, inst: IntegrationInstallation) -> str | None:
    if not inst.webhook_secret_id:
        return None
    row = db.get(IntegrationSecret, inst.webhook_secret_id)
    if row is None or row.revoked_at is not None or row.workspace_id != inst.workspace_id:
        return None
    try:
        return sec.decrypt(settings, row)
    except (sec.SecretsUnavailable, sec.SecretMissing):
        return None


def receive(db: Session, settings: Settings, installation_id: str, headers: dict[str, str], raw: bytes) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in headers.items()}
    inst = db.get(IntegrationInstallation, installation_id)
    if inst is None or inst.removed_at is not None:
        raise NotFoundError("Webhook endpoint not found.")
    itype = inst.integration_type
    try:
        adapter = get_adapter(itype)
    except Unsupported:
        raise NotFoundError("Webhook endpoint not found.") from None
    if "inbound_events" not in adapter.capabilities:
        raise NotFoundError("Webhook endpoint not found.")
    if not inst.enabled:
        m.INT_INBOUND.inc(integration_type=itype, outcome="disabled")
        raise InboundError(403, "integration_disabled", "This integration is disabled.")
    if not _rate_ok(inst.id):
        m.INT_INBOUND.inc(integration_type=itype, outcome="rate_limited")
        raise InboundError(429, "rate_limited", "Too many deliveries; retry later.")
    try:
        adapter.verify_inbound(_webhook_secret(db, settings, inst), headers, raw)
    except InboundAuthError as exc:
        m.INT_INBOUND.inc(integration_type=itype, outcome="auth_failed")
        log.warning("integration_inbound_rejected", extra={"event": "integration_inbound_rejected", "installation_id": inst.id, "category": exc.category})
        raise InboundError(401, exc.category, "The delivery could not be authenticated.") from None
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        m.INT_INBOUND.inc(integration_type=itype, outcome="invalid")
        raise InboundError(415, "unsupported_content_type", "Content-Type must be application/json.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        m.INT_INBOUND.inc(integration_type=itype, outcome="invalid")
        raise InboundError(422, "payload_invalid", "The body is not valid JSON.") from None
    try:
        ev = adapter.normalize_inbound(headers, payload)
    except InvalidInbound as exc:
        m.INT_INBOUND.inc(integration_type=itype, outcome="invalid")
        raise InboundError(422, exc.category, "The payload is not valid for this integration.") from None
    row = IntegrationEvent(workspace_id=inst.workspace_id, installation_id=inst.id, delivery_id=ev.delivery_id, event_type=ev.event_type[:60], action=ev.action[:60],
                           external_ref=ev.external_ref[:200], normalized=ev.summary, status="accepted", attempts=1)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        m.INT_INBOUND.inc(integration_type=itype, outcome="duplicate")
        return {"accepted": True, "duplicate": True, "outcome": "duplicate", "message": "This delivery was already received; nothing changed."}
    try:
        outcome = process(db, inst, row, ev)
    except Exception:  # noqa: BLE001 - recorded with a category; the delivery stays reprocessable
        log.exception("integration_inbound_processing_failed", extra={"event": "integration_inbound_processing_failed", "installation_id": inst.id})
        row.status, row.error_category = "failed", "processing_error"
        m.INT_INBOUND.inc(integration_type=itype, outcome="failed")
        return {"accepted": True, "duplicate": False, "outcome": "failed", "message": "The delivery was recorded but processing failed."}
    m.INT_INBOUND.inc(integration_type=itype, outcome=outcome)
    return {"accepted": True, "duplicate": False, "outcome": outcome, "event": ev.event_type}


def process(db: Session, inst: IntegrationInstallation, row: IntegrationEvent, ev: InboundEvent) -> str:
    row.processed_at = utcnow()
    if ev.event_type == "ping" or ev.ignored:
        row.status = "ignored" if ev.ignored else "processed"
        return "ignored" if ev.ignored else "ping"
    repo_id = None
    for mp in inst.config.get("repositories", []):
        if mp["external"].lower() == ev.external_repo.lower():
            repo_id = mp["repository_id"]
    if repo_id is None:
        row.status, row.error_category = "ignored", "repository_not_mapped"
        return "ignored"
    row.repository_id = repo_id
    if ev.ci_run is not None:
        record_evidence(db, inst, repo_id, row.id, ev.ci_run)
        inst.last_sync_at = utcnow()
    row.status = "processed"
    return "processed"


def _dt(v: Any):  # type: ignore[no-untyped-def]
    from datetime import datetime
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_evidence(db: Session, inst: IntegrationInstallation, repository_id: str, event_id: str, run: dict[str, Any]) -> CiEvidenceRun:
    """Upsert one external run. A terminal status is never downgraded to pending by a late/out-of-order event."""
    existing = db.scalar(select(CiEvidenceRun).where(CiEvidenceRun.installation_id == inst.id, CiEvidenceRun.source == run["source"],
                                                     CiEvidenceRun.external_run_id == run["external_run_id"]))
    if existing is None:
        existing = CiEvidenceRun(workspace_id=inst.workspace_id, installation_id=inst.id, repository_id=repository_id, source=run["source"], external_run_id=run["external_run_id"],
                                 head_sha=run["head_sha"], status=run["status"], event_id=event_id)
        db.add(existing)
    elif existing.head_sha != run["head_sha"]:
        pass  # a run's commit never changes; ignore a conflicting claim
    elif not (existing.status in ("success", "failed", "cancelled") and run["status"] == "pending"):
        existing.status = run["status"]
        existing.event_id = event_id
    existing.name = run.get("name", "")[:120] or existing.name
    existing.external_url = run.get("url") or existing.external_url
    existing.started_at = _dt(run.get("started_at")) or existing.started_at
    existing.completed_at = _dt(run.get("completed_at")) or existing.completed_at
    existing.updated_at = utcnow()
    db.flush()
    from app.services.integrations import outbox  # lazy import: outbox depends on governance
    outbox.publish(db, inst.workspace_id, "ci.run_recorded", "ci_run", existing.id, f"ci.run_recorded:{existing.id}:{existing.status}")
    return existing
