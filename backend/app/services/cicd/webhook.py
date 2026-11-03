"""Authenticated provider webhooks. Order matters: size limit -> connection -> signature over the raw bytes -> structure -> repository identity
-> delivery de-duplication -> state change. Nothing in a payload selects a command, a path or a URL; deliveries never run tests."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import CiConnection, CiWebhookDelivery, CiWorkflowRun, Repository
from app.services import audit
from app.services.cicd import providers, service
from app.services.cicd.errors import CiForbidden, CiInvalid, CiNotFound

EVENT_NAME = re.compile(r"^[a-z_]{1,40}$")


def receive(db: Session, settings: Settings, connection_id: str, headers: dict[str, str], raw: bytes) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in headers.items()}
    conn = db.get(CiConnection, connection_id)
    if conn is None or conn.provider == "local":
        raise CiNotFound("connection_not_found", "Webhook endpoint not found.")
    if not conn.enabled:
        raise CiForbidden("connection_disabled", "This connection is disabled.")
    prov = service.provider_for(settings, conn.provider)
    prov.verify_webhook(providers.resolve_secret(conn.webhook_secret_env), headers, raw)  # constant-time compare; raises before anything is parsed
    delivery = headers.get("x-github-delivery", "")
    event = headers.get("x-github-event", "")
    if not providers.DELIVERY.match(delivery) or not EVENT_NAME.match(event):
        raise CiInvalid("headers_invalid", "The delivery or event header is missing or malformed.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CiInvalid("payload_invalid", "The webhook body is not valid JSON.") from None
    norm = prov.normalize_webhook(headers, payload)
    if norm.repository.lower() != conn.external_repo.lower():
        raise CiForbidden("repository_mismatch", "The delivery is for a different repository than the one this connection is configured for.")
    repo = db.get(Repository, conn.repository_id)
    assert repo is not None
    d = CiWebhookDelivery(connection_id=conn.id, delivery_id=delivery, event=event, action=norm.action, outcome="accepted")
    db.add(d)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True, "outcome": "duplicate", "event": event, "message": "This delivery was already processed; nothing changed."}
    result: dict[str, Any] = {"accepted": True, "duplicate": False, "event": event, "action": norm.action}
    if norm.kind == "ping":
        result.update(outcome="ping", message="pong")
    elif norm.kind == "ignored" or "pull_request" not in (conn.enabled_events or []):
        d.outcome = "ignored"
        result.update(outcome="ignored", ignored=True, message=norm.note or "pull_request events are not enabled for this connection.")
    else:
        assert norm.change_request is not None
        cr, outcome = service.upsert_cr(db, repo, conn, norm.change_request, settings, conn.provider)
        d.change_request_id = cr.id
        if outcome == "stale_event":
            d.outcome = "stale_event"
            result.update(outcome="stale_event", ignored=True, message="The event is older than the state already recorded and was ignored.")
        else:
            wf = service.ensure_awaiting(db, repo, cr) if cr.state == "open" else None
            if cr.state != "open":
                for w in db.scalars(select(CiWorkflowRun).where(CiWorkflowRun.change_request_id == cr.id, CiWorkflowRun.status == "awaiting")):
                    w.status = "cancelled"
            result.update(outcome=outcome, change_request_id=cr.id, change_request_outcome=outcome, head_sha=cr.head_sha, workflow_run_id=wf.id if wf else None)
            audit.record(db, workspace_id=repo_workspace(db, repo), actor_user_id=None, action="ci.webhook_accepted", entity_type="change_request", entity_id=cr.id,
                         details={"provider": conn.provider, "event": event, "action": norm.action, "outcome": outcome, "head_sha": cr.head_sha})
    conn.last_sync_at = utcnow()
    db.commit()
    return result


def repo_workspace(db: Session, repo: Repository) -> str:
    return repo.workspace_id

