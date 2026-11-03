"""Reliability events and the incident lifecycle: open -> acknowledged -> investigating -> resolved -> reopened.

Incidents are deduplicated by a deterministic fingerprint. History is append-only (ops_events); nothing is deleted by lifecycle changes.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import OpsEvent, OpsIncident
from app.observability import context as ctx
from app.observability.errors import OpsConflict, OpsNotFound
from app.observability.redact import clean, clean_str
from app.security.auth import RequestContext
from app.services import audit

SEVERITIES = ("info", "warning", "critical")
TRANSITIONS = {
    "open": {"acknowledged", "investigating", "resolved"},
    "reopened": {"acknowledged", "investigating", "resolved"},
    "acknowledged": {"investigating", "resolved"},
    "investigating": {"resolved"},
    "resolved": {"reopened"},
}
ACTION_STATUS = {"acknowledge": "acknowledged", "investigate": "investigating", "resolve": "resolved", "reopen": "reopened"}


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def record_event(db: Session, workspace_id: str, event_type: str, *, incident_id: str | None = None, severity: str = "info", component: str = "platform",
                 summary: str = "", evidence: dict[str, Any] | None = None, occurred_at: datetime | None = None, actor: str | None = None, note: str | None = None) -> OpsEvent:
    ev = OpsEvent(workspace_id=workspace_id, incident_id=incident_id, event_type=event_type[:40], severity=severity if severity in SEVERITIES else "info",
                  component=component[:40], occurred_at=occurred_at or utcnow(), detected_at=utcnow(), request_id=ctx.REQUEST_ID.get(), trace_id=ctx.TRACE_ID.get(),
                  summary=clean_str(summary, 500), evidence=clean(evidence or {}, 500), actor_user_id=actor, note=clean_str(note, 1000) if note else None)
    db.add(ev)
    return ev


def raise_incident(db: Session, workspace_id: str, *, fp: str, source: str, severity: str, component: str, title: str, summary: str,
                   evidence: dict[str, Any], rule_id: str | None = None, rule_version: int | None = None) -> tuple[OpsIncident, str]:
    """Create or update the incident for this fingerprint. Returns (incident, transition) where transition is opened | reopened | recurred."""
    inc = db.scalar(select(OpsIncident).where(OpsIncident.workspace_id == workspace_id, OpsIncident.fingerprint == fp))
    now = utcnow()
    safe_ev = clean(evidence, 500)
    if inc is None:
        inc = OpsIncident(workspace_id=workspace_id, fingerprint=fp, source=source, rule_id=rule_id, rule_version=rule_version, severity=severity, component=component[:40],
                          title=clean_str(title, 200), summary=clean_str(summary, 1000), evidence=safe_ev, correlation_id=ctx.REQUEST_ID.get(), first_seen_at=now, last_seen_at=now)
        db.add(inc)
        db.flush()
        record_event(db, workspace_id, "incident_opened", incident_id=inc.id, severity=severity, component=component, summary=title, evidence=safe_ev)
        return inc, "opened"
    inc.last_seen_at, inc.occurrences, inc.evidence, inc.severity, inc.summary = now, inc.occurrences + 1, safe_ev, severity, clean_str(summary, 1000)
    if rule_version is not None:
        inc.rule_version = rule_version
    if inc.status == "resolved":
        inc.status, inc.resolved_at = "reopened", None
        record_event(db, workspace_id, "incident_reopened", incident_id=inc.id, severity=severity, component=component, summary=f"Recurrence: {title}", evidence=safe_ev)
        return inc, "reopened"
    return inc, "recurred"


def get_incident(db: Session, ctx_: RequestContext, incident_id: str) -> OpsIncident:
    inc = db.get(OpsIncident, incident_id)
    if inc is None or inc.workspace_id != ctx_.workspace.id:
        raise OpsNotFound("incident_not_found", "Incident not found.")
    return inc


def transition(db: Session, ctx_: RequestContext, incident_id: str, action: str, note: str | None = None) -> OpsIncident:
    ctx_.require_write()
    inc = get_incident(db, ctx_, incident_id)
    target = ACTION_STATUS[action]
    if target not in TRANSITIONS.get(inc.status, set()):
        raise OpsConflict("invalid_transition", f"Cannot {action} an incident that is {inc.status}.", {"status": inc.status, "allowed": sorted(TRANSITIONS.get(inc.status, set()))})
    if target == "resolved" and not (note and note.strip()):
        raise OpsConflict("note_required", "A resolution note is required.")
    now = utcnow()
    inc.status = target
    if target == "acknowledged":
        inc.acknowledged_by, inc.acknowledged_at = ctx_.user.id, now
    elif target == "resolved":
        inc.resolved_at, inc.resolution_note = now, clean_str(note or "", 1000)
    elif target == "reopened":
        inc.resolved_at = None
    record_event(db, inc.workspace_id, f"incident_{target}", incident_id=inc.id, severity=inc.severity, component=inc.component, summary=inc.title, actor=ctx_.user.id, note=note)
    audit.record(db, workspace_id=inc.workspace_id, actor_user_id=ctx_.user.id, action=f"ops.incident.{action}", entity_type="ops_incident", entity_id=inc.id, details={"status": target})
    db.commit()
    return inc


def auto_resolve(db: Session, workspace_id: str, fp: str, reason: str) -> OpsIncident | None:
    """Close an incident whose condition has cleared. Only untouched (open/reopened) incidents are closed; acknowledged or investigated ones
    stay for a human to resolve."""
    inc = db.scalar(select(OpsIncident).where(OpsIncident.workspace_id == workspace_id, OpsIncident.fingerprint == fp))
    if inc is None or inc.status not in ("open", "reopened"):
        return None
    inc.status, inc.resolved_at, inc.resolution_note = "resolved", utcnow(), clean_str(reason, 1000)
    record_event(db, workspace_id, "incident_resolved", incident_id=inc.id, severity=inc.severity, component=inc.component, summary=inc.title, note=reason)
    return inc
