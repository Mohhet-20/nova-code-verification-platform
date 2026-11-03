"""Reliable outbound webhooks: transactional outbox -> per-subscription deliveries -> signed, bounded, retried HTTP POSTs.

Guarantees (and non-guarantees):
* AT-LEAST-ONCE. A crash after a receiver accepted a request but before we recorded it causes a retry; receivers must de-duplicate on
  ``X-Nova-Event-Id``. There is no exactly-once claim.
* The event row is written in the same transaction as the change that caused it (see ``governance.common.emit``); delivery happens later
  in a worker, so user requests are never blocked by a slow receiver.
* A delivery is claimed with a lease. If the process dies mid-attempt the lease expires and ``run_due`` re-claims it (recovery after restart).
* Transient failures (timeouts, connection errors, 408/425/429/5xx) retry with exponential backoff up to ``webhook_max_attempts``; permanent
  failures (blocked destination, other 4xx, redirects) stop immediately. Responses are stored only as a short sanitized excerpt.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    IntegrationSecret,
    OutboxEvent,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookSubscription,
)
from app.observability import metrics as m
from app.services.analysis.redaction import scrub
from app.services.governance.common import emit
from app.services.integrations import secrets as sec
from app.services.integrations.netguard import HttpClient, HttpError, SafeHttpClient

log = logging.getLogger("nova.integrations")
SCHEMA_VERSION = "1"
ALLOWED_EVENTS = ("workflow.created", "workflow.state_changed", "workflow.decision_recorded", "integration.health_changed", "ci.run_recorded")
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _client(settings: Settings) -> HttpClient:
    return SafeHttpClient(allow_private=settings.integration_allow_private_destinations, timeout=settings.integration_http_timeout_seconds, max_bytes=65_536)


# ---- publish (transactional outbox) ------------------------------------------------------------------------------------------------------
def publish(db: Session, workspace_id: str, event_type: str, resource_type: str, resource_id: str, dedupe_key: str) -> OutboxEvent | None:
    """Persist a logical event IF an active subscription wants it. Runs in the caller's transaction; the envelope holds only a resource reference."""
    if event_type not in ALLOWED_EVENTS:
        return None
    subs = db.scalars(select(WebhookSubscription).where(WebhookSubscription.workspace_id == workspace_id, WebhookSubscription.active.is_(True), WebhookSubscription.removed_at.is_(None)))
    if not any(event_type in (s.events or []) for s in subs):
        return None
    ev = OutboxEvent(workspace_id=workspace_id, event_type=event_type, resource_type=resource_type, resource_id=resource_id[:32], dedupe_key=dedupe_key[:120], payload={})
    try:
        with db.begin_nested():
            db.add(ev)
            db.flush()
    except IntegrityError:
        return None
    fan_out(db, ev)
    return ev


def fan_out(db: Session, ev: OutboxEvent) -> int:
    n = 0
    for s in db.scalars(select(WebhookSubscription).where(WebhookSubscription.workspace_id == ev.workspace_id, WebhookSubscription.active.is_(True), WebhookSubscription.removed_at.is_(None))):
        if ev.event_type in (s.events or []):
            db.add(WebhookDelivery(workspace_id=ev.workspace_id, subscription_id=s.id, event_id=ev.id, state="pending", next_attempt_at=utcnow()))
            n += 1
    ev.fanned_out_at = utcnow()
    db.flush()
    return n


_AUDIT_MAP = (("workflow.decision.", "workflow.decision_recorded"), ("workflow.created", "workflow.created"))


def publish_from_audit(db: Session, workspace_id: str, action: str, entity_type: str, entity_id: str, outcome: str) -> None:
    """Called by the governance audit writer. Never raises: an outbox problem must not break the governed operation."""
    if outcome != "success" or not action.startswith("workflow."):
        return
    ev_type = next((t for p, t in _AUDIT_MAP if action.startswith(p)), None)
    if ev_type is None and action in ("workflow.submit", "workflow.cancel", "workflow.complete", "workflow.reopen", "workflow.withdraw"):
        ev_type = "workflow.state_changed"
    if ev_type is None:
        return
    try:
        publish(db, workspace_id, ev_type, "workflow", entity_id, f"{ev_type}:{entity_id}:{action}:{int(time.time() * 1000)}")
    except Exception:  # noqa: BLE001
        log.exception("outbox_publish_failed", extra={"event": "outbox_publish_failed", "event_type": ev_type})


# ---- envelope & signing ------------------------------------------------------------------------------------------------------------------
def envelope(ev: OutboxEvent) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "event_id": ev.id, "type": ev.event_type, "created_at": ev.created_at.isoformat(),
            "organization_id": ev.workspace_id, "resource": {"type": ev.resource_type, "id": ev.resource_id}}


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, header: str, body: bytes, tolerance: int, now: float | None = None) -> bool:
    """Receiver-side reference verifier (also used by tests and documented for consumers)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, t, body), header)


def backoff_seconds(settings: Settings, attempts: int) -> float:
    return min(settings.webhook_backoff_max_seconds, settings.webhook_backoff_base_seconds * (2 ** max(0, attempts - 1)))


def _excerpt(body: bytes) -> str:
    return str(scrub(body[:400].decode("utf-8", "replace")))[:300]


# ---- delivery ----------------------------------------------------------------------------------------------------------------------------
def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def attempt(db: Session, settings: Settings, d: WebhookDelivery, http: HttpClient | None = None, now: datetime | None = None) -> str:
    """One delivery attempt. Returns the new state. Assumes the caller has claimed ``d`` (state in_progress)."""
    now = now or utcnow()
    sub = db.get(WebhookSubscription, d.subscription_id)
    ev = db.get(OutboxEvent, d.event_id)
    d.attempts += 1
    d.last_attempt_at = now
    status_code: int | None = None
    cat: str | None = None
    excerpt = ""
    transient = False
    t0 = time.monotonic()
    if sub is None or ev is None or sub.removed_at is not None or not sub.active:
        cat, transient = "subscription_inactive", False
    else:
        try:
            row = db.get(IntegrationSecret, sub.secret_id) if sub.secret_id else None
            if row is None or row.revoked_at is not None:
                raise sec.SecretMissing("no secret")
            secret = sec.decrypt(settings, row)
        except sec.SecretsUnavailable:
            secret, cat, transient = "", "secrets_unavailable", True
        except sec.SecretMissing:
            secret, cat, transient = "", "secret_missing", False
        if cat is None:
            body = json.dumps(envelope(ev), separators=(",", ":"), sort_keys=True).encode()
            ts = int(time.time())
            headers = {"Content-Type": "application/json", "User-Agent": "nova-webhooks/1", "X-Nova-Signature": sign(secret, ts, body), "X-Nova-Event-Id": ev.id,
                       "X-Nova-Event-Type": ev.event_type, "X-Nova-Delivery-Id": d.id, "X-Nova-Delivery-Attempt": str(d.attempts)}
            try:
                r = (http or _client(settings)).request("POST", sub.url, headers=headers, body=body)
                status_code, excerpt = r.status, _excerpt(r.body)
                if 200 <= r.status < 300:
                    cat = None
                elif r.status in TRANSIENT_STATUS:
                    cat, transient = f"http_{r.status}", True
                else:
                    cat = f"http_{r.status}"
            except HttpError as exc:
                cat = exc.category
                transient = exc.category in ("timeout", "connection_failed", "tls_error")
    dur = time.monotonic() - t0
    ok = cat is None
    db.add(WebhookDeliveryAttempt(delivery_id=d.id, attempt_no=d.attempts, started_at=now, duration_ms=int(dur * 1000), status_code=status_code, error_category=cat, response_excerpt=excerpt))
    d.last_status_code, d.last_error_category, d.lease_until = status_code, cat, None
    result = "success" if ok else ("retry" if transient and d.attempts < settings.webhook_max_attempts else "failed")
    m.WEBHOOK_ATTEMPTS.inc(result=result)
    m.WEBHOOK_LATENCY.observe(dur, result=result)
    if ok:
        d.state, d.completed_at = "succeeded", now
        if sub:
            sub.consecutive_failures, sub.last_delivery_at = 0, now
    elif result == "retry":
        d.state, d.next_attempt_at = "retrying", now + timedelta(seconds=backoff_seconds(settings, d.attempts))
    else:
        d.state, d.completed_at = "failed", now
        if sub:
            sub.consecutive_failures += 1
            sub.last_delivery_at = now
            if sub.consecutive_failures >= settings.webhook_disable_after_failures:
                sub.active, sub.disabled_reason = False, "too_many_failures"
                emit(db, workspace_id=sub.workspace_id, actor=None, action="webhook.subscription_auto_disabled", entity_type="webhook_subscription", entity_id=sub.id, outcome="failure")
    db.flush()
    return d.state


def claim_due(db: Session, settings: Settings, now: datetime, limit: int) -> list[WebhookDelivery]:
    rows = list(db.scalars(select(WebhookDelivery).where(or_(
        and_(WebhookDelivery.state.in_(("pending", "retrying")), WebhookDelivery.next_attempt_at <= now),
        and_(WebhookDelivery.state == "in_progress", WebhookDelivery.lease_until <= now))).order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id).limit(limit)))
    for d in rows:
        d.state, d.lease_until = "in_progress", now + timedelta(seconds=settings.webhook_lease_seconds)
    db.flush()
    return rows


def run_due(db: Session, settings: Settings, *, http: HttpClient | None = None, now: datetime | None = None, limit: int = 25) -> dict[str, int]:
    """Process due deliveries (and re-claim any whose lease expired, e.g. after a crash). Safe to call repeatedly."""
    now = now or utcnow()
    for ev in db.scalars(select(OutboxEvent).where(OutboxEvent.fanned_out_at.is_(None)).limit(100)):  # events written by a path that skipped fan-out
        fan_out(db, ev)
    claimed = claim_due(db, settings, now, limit)
    db.commit()  # make the claim durable before any network I/O
    out = {"claimed": len(claimed), "succeeded": 0, "retrying": 0, "failed": 0}
    for d in claimed:
        state = attempt(db, settings, d, http, now)
        db.commit()
        out["succeeded" if state == "succeeded" else "retrying" if state == "retrying" else "failed"] += 1
    stats = queue_stats(db, now)
    m.INT_QUEUE_DEPTH.set(stats["pending"] + stats["retrying"])
    m.INT_OLDEST_AGE.set(stats["oldest_due_age_seconds"] or 0)
    return out


def queue_stats(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    counts = {s: 0 for s in ("pending", "in_progress", "retrying", "failed")}
    oldest: datetime | None = None
    for d in db.scalars(select(WebhookDelivery).where(WebhookDelivery.state.in_(tuple(counts)))):
        counts[d.state] += 1
        if d.state in ("pending", "retrying") and _aware(d.next_attempt_at) <= _aware(now) and (oldest is None or _aware(d.next_attempt_at) < oldest):
            oldest = _aware(d.next_attempt_at)
    age = max(0.0, (_aware(now) - oldest).total_seconds()) if oldest else None
    return {**counts, "oldest_due_age_seconds": age, "stuck": bool(age is not None and age > 900)}


def replay(db: Session, actor: str, d: WebhookDelivery) -> WebhookDelivery:
    """Admin replay of a finished delivery: resets it to pending for a fresh bounded cycle. History of attempts is kept."""
    if d.state not in ("failed", "succeeded"):
        raise ConflictError("Only finished deliveries can be replayed.")
    sub = db.get(WebhookSubscription, d.subscription_id)
    if sub is None or sub.removed_at is not None or not sub.active:
        raise ConflictError("The subscription is inactive; re-enable it before replaying.")
    d.state, d.attempts, d.next_attempt_at, d.replayed_by, d.completed_at, d.last_error_category = "pending", 0, utcnow(), actor, None, None
    emit(db, workspace_id=d.workspace_id, actor=actor, action="webhook.delivery_replayed", entity_type="webhook_delivery", entity_id=d.id)
    return d


# ---- subscriptions -----------------------------------------------------------------------------------------------------------------------
def validate_subscription_url(settings: Settings, url: str) -> None:
    from app.observability.notify import Blocked, validate_destination
    try:
        validate_destination(url, settings.integration_allow_private_destinations)
    except Blocked as exc:
        raise ValidationFailedError("The webhook URL is not an acceptable destination.", {"reason": str(exc)}) from None


def create_subscription(db: Session, settings: Settings, *, workspace_id: str, actor: str, name: str, url: str, events: list[str]) -> tuple[WebhookSubscription, str]:
    bad = [e for e in events if e not in ALLOWED_EVENTS]
    if bad or not events:
        raise ValidationFailedError("Unsupported event types.", {"allowed": list(ALLOWED_EVENTS), "rejected": bad[:5]})
    validate_subscription_url(settings, url)
    sub = WebhookSubscription(workspace_id=workspace_id, name=name, url=url, events=sorted(set(events)), created_by=actor)
    db.add(sub)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A subscription with this name already exists.") from None
    plain = sec.new_webhook_secret()
    try:
        sub.secret_id = sec.store(db, settings, workspace_id=workspace_id, owner_type="subscription", owner_id=sub.id, plaintext=plain, actor=actor).id
    except sec.SecretsUnavailable as exc:
        from app.services.integrations.service import SecretsUnavailableError
        raise SecretsUnavailableError(exc.reason) from None
    emit(db, workspace_id=workspace_id, actor=actor, action="webhook.subscription_created", entity_type="webhook_subscription", entity_id=sub.id, details={"events": sub.events})
    return sub, plain


def get_subscription(db: Session, workspace_id: str, sub_id: str) -> WebhookSubscription:
    s = db.get(WebhookSubscription, sub_id)
    if s is None or s.workspace_id != workspace_id or s.removed_at is not None:
        raise NotFoundError("Webhook subscription not found.")
    return s


def rotate_secret(db: Session, settings: Settings, actor: str, sub: WebhookSubscription) -> str:
    plain = sec.new_webhook_secret()
    try:
        sub.secret_id = sec.store(db, settings, workspace_id=sub.workspace_id, owner_type="subscription", owner_id=sub.id, plaintext=plain, actor=actor).id
    except sec.SecretsUnavailable as exc:
        from app.services.integrations.service import SecretsUnavailableError
        raise SecretsUnavailableError(exc.reason) from None
    emit(db, workspace_id=sub.workspace_id, actor=actor, action="webhook.secret_rotated", entity_type="webhook_subscription", entity_id=sub.id)
    return plain


def serialize_subscription(s: WebhookSubscription) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "url": s.url, "events": s.events, "active": s.active, "disabled_reason": s.disabled_reason,
            "consecutive_failures": s.consecutive_failures, "last_delivery_at": s.last_delivery_at, "created_by": s.created_by, "created_at": s.created_at,
            "has_secret": bool(s.secret_id)}
