"""Triage, suppressions, exceptions, lookups and serialisers for the security workspace. Everything is repository-scoped and audited."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import Repository, SecEvent, SecException, SecFinding, SecScan, SecSuppression
from app.security.auth import RequestContext
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.security.errors import SecConflict, SecForbidden, SecInvalid, SecNotFound
from app.services.security.scan import _aware, event

USER_STATUSES = ("open", "false_positive", "accepted_risk")
SELECTOR_TYPES = ("finding", "advisory", "package", "rule")
MAX_EXPIRY_DAYS = 365


def _audit(db: Session, ctx: RequestContext, action: str, etype: str, eid: str, **details: Any) -> None:
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action=action, entity_type=etype, entity_id=eid, details=details)


def _reason(text: str, minimum: int = 8) -> str:
    t = (text or "").strip()
    if len(t) < minimum:
        raise SecInvalid("reason_required", f"A reason of at least {minimum} characters is required.")
    return scrub(t, 500)


def _future(expires_at: datetime | None, required: bool) -> datetime | None:
    if expires_at is None:
        if required:
            raise SecInvalid("expiry_required", "An expiry date is required.")
        return None
    e = _aware(expires_at)
    now = datetime.now(UTC)
    assert e is not None
    if e <= now:
        raise SecInvalid("expiry_in_past", "The expiry must be in the future.")
    if (e - now).days > MAX_EXPIRY_DAYS:
        raise SecInvalid("expiry_too_far", f"The expiry may be at most {MAX_EXPIRY_DAYS} days away.")
    return e


# ------------------------------------------------------------------------------------------------ lookups
def get_scan(db: Session, repo_id: str, scan_id: str) -> SecScan:
    s = db.scalar(select(SecScan).where(SecScan.id == scan_id, SecScan.repository_id == repo_id))
    if s is None:
        raise SecNotFound("scan_not_found", "Security scan not found.")
    return s


def get_finding(db: Session, repo_id: str, fid: str) -> SecFinding:
    f = db.scalar(select(SecFinding).where(SecFinding.id == fid, SecFinding.repository_id == repo_id))
    if f is None:
        raise SecNotFound("finding_not_found", "Security finding not found.")
    return f


def latest_scan(db: Session, repo_id: str, completed: bool = True) -> SecScan | None:
    stmt = select(SecScan).where(SecScan.repository_id == repo_id)
    if completed:
        stmt = stmt.where(SecScan.status == "completed")
    return db.scalar(stmt.order_by(SecScan.created_at.desc(), SecScan.id.desc()).limit(1))


# ------------------------------------------------------------------------------------------------ serialisers
def scan_out(s: SecScan, detail: bool = False) -> dict[str, Any]:
    out = {"id": s.id, "status": s.status, "scan_types": s.scan_types, "options": s.options, "head_sha": s.head_sha, "dirty": s.dirty, "summary": s.summary or {}, "error": s.error,
           "created_at": s.created_at, "started_at": s.started_at, "finished_at": s.finished_at, "inventory_digest": s.inventory_digest, "source_digest": s.source_digest}
    if detail:
        out |= {"coverage": s.coverage or {}, "tool_versions": s.tool_versions or {}}
    return out


def finding_out(db: Session, f: SecFinding, detail: bool = False) -> dict[str, Any]:
    out = {"id": f.id, "kind": f.kind, "rule_id": f.rule_id, "category": f.category, "title": f.title, "severity": f.severity, "confidence": f.confidence, "evidence_quality": f.evidence_quality,
           "rel_path": f.rel_path, "line": f.line, "status": f.status, "priority": f.priority, "component_key": f.component_key, "first_seen_at": f.first_seen_at, "last_seen_at": f.last_seen_at,
           "match_status": (f.evidence or {}).get("match_status"), "preview": (f.evidence or {}).get("preview")}
    if detail:
        evs = db.scalars(select(SecEvent).where(SecEvent.entity_type == "finding", SecEvent.entity_id == f.id).order_by(SecEvent.created_at, SecEvent.id)).all()
        out |= {"description": f.description, "end_line": f.end_line, "scope": f.scope, "evidence": f.evidence, "remediation": f.remediation, "references": f.references,
                "resolution_reason": f.resolution_reason, "suppression_id": f.suppression_id, "first_seen_scan_id": f.first_seen_scan_id, "last_seen_scan_id": f.last_seen_scan_id,
                "events": [{"action": e.action, "from_status": e.from_status, "to_status": e.to_status, "reason": e.reason, "actor_user_id": e.actor_user_id, "created_at": e.created_at,
                            "details": e.details} for e in evs]}
    return out


def suppression_out(s: SecSuppression) -> dict[str, Any]:
    now = datetime.now(UTC)
    exp = _aware(s.expires_at)
    state = "revoked" if s.revoked_at else "expired" if exp and exp <= now else "active"
    return {"id": s.id, "kind": s.kind, "fingerprint": s.fingerprint, "rule_id": s.rule_id, "path_glob": s.path_glob, "reason": s.reason, "created_by": s.created_by, "created_at": s.created_at,
            "expires_at": s.expires_at, "state": state, "revoked_at": s.revoked_at, "revoked_reason": s.revoked_reason}


def exception_out(x: SecException) -> dict[str, Any]:
    exp = _aware(x.expires_at)
    state = "revoked" if x.revoked_at else "expired" if exp and exp <= datetime.now(UTC) else "active"
    return {"id": x.id, "selector": x.selector, "reason": x.reason, "expires_at": x.expires_at, "state": state, "created_by": x.created_by, "created_at": x.created_at, "revoked_at": x.revoked_at}


# ------------------------------------------------------------------------------------------------ triage
def triage(db: Session, ctx: RequestContext, repo: Repository, f: SecFinding, status: str, reason: str) -> SecFinding:
    ctx.require_write()
    if status not in USER_STATUSES:
        raise SecInvalid("invalid_status", f"Status must be one of {', '.join(USER_STATUSES)}.")
    if f.status in ("resolved", "suppressed") and status != "open":
        raise SecConflict("invalid_transition", f"A {f.status} finding is managed by scans and suppressions; it cannot be triaged to '{status}'.")
    if f.status == "suppressed" and status == "open":
        raise SecConflict("invalid_transition", "Revoke the suppression instead.")
    if f.status == "resolved" and status == "open":
        raise SecConflict("invalid_transition", "A resolved finding reopens automatically if a later complete scan detects it again.")
    if f.status == status:
        raise SecConflict("no_change", f"The finding is already '{status}'.")
    r = _reason(reason) if status != "open" else scrub((reason or "").strip(), 500)
    old = f.status
    f.status, f.resolution_reason = status, (r or None)
    event(db, repo.id, "finding", f.id, "triaged", frm=old, to=status, reason=r, actor=ctx.user.id)
    _audit(db, ctx, "security.finding_triaged", "sec_finding", f.id, repository_id=repo.id, from_status=old, to_status=status, rule_id=f.rule_id)
    db.commit()
    return f


def suppress(db: Session, ctx: RequestContext, repo: Repository, *, finding_id: str | None, rule_id: str | None, path_glob: str | None, reason: str, expires_at: datetime | None) -> SecSuppression:
    ctx.require_write()
    r = _reason(reason)
    exp = _future(expires_at, required=False)
    if finding_id:
        f = get_finding(db, repo.id, finding_id)
        row = SecSuppression(repository_id=repo.id, kind="finding", fingerprint=f.fingerprint, rule_id=f.rule_id, reason=r, created_by=ctx.user.id, expires_at=exp)
    else:
        if not rule_id or not path_glob:
            raise SecInvalid("scope_required", "A suppression needs a finding id, or both a rule id and a path glob.")
        g = path_glob.strip()
        if g in ("*", "**", "**/*") or g.startswith("/") or ".." in g.split("/") or len(g) > 300:
            raise SecInvalid("scope_too_broad", "The path glob must be a relative, specific pattern (not '*' or '**').")
        row = SecSuppression(repository_id=repo.id, kind="rule_in_path", rule_id=rule_id.strip()[:40], path_glob=g, reason=r, created_by=ctx.user.id, expires_at=exp)
    db.add(row)
    db.flush()
    if row.kind == "finding":
        f = get_finding(db, repo.id, finding_id or "")
        if f.status == "open":
            event(db, repo.id, "finding", f.id, "suppressed", frm="open", to="suppressed", reason=r, actor=ctx.user.id, details={"suppression_id": row.id})
            f.status, f.suppression_id, f.resolution_reason = "suppressed", row.id, f"suppressed: {r}"[:300]
    event(db, repo.id, "suppression", row.id, "created", reason=r, actor=ctx.user.id, details={"kind": row.kind, "rule_id": row.rule_id, "path_glob": row.path_glob})
    _audit(db, ctx, "security.suppression_created", "sec_suppression", row.id, repository_id=repo.id, kind=row.kind, rule_id=row.rule_id, path_glob=row.path_glob, expires_at=str(exp) if exp else None)
    db.commit()
    return row


def revoke_suppression(db: Session, ctx: RequestContext, repo: Repository, sid: str, reason: str) -> SecSuppression:
    ctx.require_write()
    s = db.scalar(select(SecSuppression).where(SecSuppression.id == sid, SecSuppression.repository_id == repo.id))
    if s is None:
        raise SecNotFound("suppression_not_found", "Suppression not found.")
    if s.revoked_at:
        raise SecConflict("already_revoked", "The suppression is already revoked.")
    s.revoked_at, s.revoked_by, s.revoked_reason = utcnow(), ctx.user.id, _reason(reason, 4)
    for f in db.scalars(select(SecFinding).where(SecFinding.repository_id == repo.id, SecFinding.suppression_id == s.id, SecFinding.status == "suppressed")):
        f.status, f.suppression_id, f.resolution_reason = "open", None, None
        event(db, repo.id, "finding", f.id, "unsuppressed", frm="suppressed", to="open", reason="the suppression was revoked", actor=ctx.user.id)
    event(db, repo.id, "suppression", s.id, "revoked", reason=s.revoked_reason, actor=ctx.user.id)
    _audit(db, ctx, "security.suppression_revoked", "sec_suppression", s.id, repository_id=repo.id)
    db.commit()
    return s


def add_exception(db: Session, ctx: RequestContext, repo: Repository, selector: dict[str, Any], reason: str, expires_at: datetime) -> SecException:
    if ctx.role != "owner":
        raise SecForbidden("owner_required", "Only a workspace owner can grant a policy exception.")
    t, v = selector.get("type"), selector.get("value")
    if t not in SELECTOR_TYPES or not isinstance(v, str) or not v.strip() or len(v) > 200:
        raise SecInvalid("invalid_selector", f"The selector needs a type ({', '.join(SELECTOR_TYPES)}) and a value.")
    r = _reason(reason)
    exp = _future(expires_at, required=True)
    x = SecException(repository_id=repo.id, selector={"type": t, "value": v.strip()}, reason=r, expires_at=exp, created_by=ctx.user.id)
    db.add(x)
    db.flush()
    event(db, repo.id, "exception", x.id, "created", reason=r, actor=ctx.user.id, details={"selector": x.selector, "expires_at": str(exp)})
    _audit(db, ctx, "security.exception_created", "sec_exception", x.id, repository_id=repo.id, selector=x.selector, expires_at=str(exp))
    db.commit()
    return x


def revoke_exception(db: Session, ctx: RequestContext, repo: Repository, xid: str) -> SecException:
    if ctx.role != "owner":
        raise SecForbidden("owner_required", "Only a workspace owner can revoke a policy exception.")
    x = db.scalar(select(SecException).where(SecException.id == xid, SecException.repository_id == repo.id))
    if x is None:
        raise SecNotFound("exception_not_found", "Exception not found.")
    if x.revoked_at:
        raise SecConflict("already_revoked", "The exception is already revoked.")
    x.revoked_at, x.revoked_by = utcnow(), ctx.user.id
    event(db, repo.id, "exception", x.id, "revoked", actor=ctx.user.id)
    _audit(db, ctx, "security.exception_revoked", "sec_exception", x.id, repository_id=repo.id)
    db.commit()
    return x
