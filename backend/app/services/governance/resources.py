"""Resolution of governed resources and the evidence NOVA already holds about them.

Only resource types that exist in this codebase are supported. Evidence is always read from persisted records; nothing
here accepts a client-supplied result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChangeRequest,
    CiEvidenceRun,
    CiWorkflowRun,
    CodeReview,
    CvRun,
    RefactorTransformation,
    RepairPatch,
    Repository,
    SecScan,
)

RESOURCE_TYPES = ("code_review", "repair_patch", "refactor", "change_request")
RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


@dataclass
class Resource:
    type: str
    id: str
    repository_id: str
    title: str
    head_sha: str | None
    risk_level: str | None
    author_user_id: str | None


def resolve(db: Session, workspace_id: str, rtype: str, rid: str) -> Resource | None:
    """The resource, only if its repository belongs to ``workspace_id`` (cross-tenant ids resolve to None)."""
    if rtype == "code_review":
        r = db.scalar(select(CodeReview).join(Repository, Repository.id == CodeReview.repository_id).where(CodeReview.id == rid, Repository.workspace_id == workspace_id))
        return Resource(rtype, r.id, r.repository_id, f"Code review ({r.mode})"[:200], r.head_sha, (r.risk_category or None), r.created_by) if r else None
    if rtype == "repair_patch":
        r = db.scalar(select(RepairPatch).join(Repository, Repository.id == RepairPatch.repository_id).where(RepairPatch.id == rid, Repository.workspace_id == workspace_id))
        return Resource(rtype, r.id, r.repository_id, r.title[:200], r.base_head, None, r.created_by) if r else None
    if rtype == "refactor":
        r = db.scalar(select(RefactorTransformation).join(Repository, Repository.id == RefactorTransformation.repository_id).where(RefactorTransformation.id == rid, Repository.workspace_id == workspace_id))
        return Resource(rtype, r.id, r.repository_id, r.title[:200], None, None, r.created_by) if r else None
    if rtype == "change_request":
        r = db.scalar(select(ChangeRequest).join(Repository, Repository.id == ChangeRequest.repository_id).where(ChangeRequest.id == rid, Repository.workspace_id == workspace_id))
        return Resource(rtype, r.id, r.repository_id, r.title[:200], r.head_sha, None, None) if r else None
    return None


def resolve_many(db: Session, workspace_id: str, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], Resource]:
    """Bounded batch lookup (one query per resource type) for list views."""
    out: dict[tuple[str, str], Resource] = {}
    for rtype in RESOURCE_TYPES:
        ids = [i for t, i in pairs if t == rtype]
        for rid in ids:  # ids per page <= 100; each is a primary-key lookup
            r = resolve(db, workspace_id, rtype, rid)
            if r is not None:
                out[(rtype, rid)] = r
    return out


# ---------------------------------------------------------------------------------------------- evidence
# Each returns {"state": "ok"|"unavailable", "detail": str, "data": {...}}; "unavailable" means the evidence does not exist (never "pass").

def _na(detail: str) -> dict[str, Any]:
    return {"state": "unavailable", "detail": detail, "data": {}}


def verification(db: Session, res: Resource) -> dict[str, Any]:
    if res.type in ("repair_patch", "refactor"):
        model = RepairPatch if res.type == "repair_patch" else RefactorTransformation
        summary = db.scalar(select(model.verification_summary).where(model.id == res.id)) or {}
        if not summary.get("outcome"):
            return _na("No verification has been run for this resource.")
        return {"state": "ok", "detail": f"Verification outcome {summary['outcome']}.", "data": {"outcome": summary["outcome"], "at": summary.get("at"), "verification_id": summary.get("verification_id")}}
    stmt = select(CvRun).where(CvRun.repository_id == res.repository_id, CvRun.status == "completed", CvRun.gate_outcome.is_not(None))
    if res.head_sha:
        stmt = stmt.where(CvRun.head_sha == res.head_sha)  # evidence must be for THIS revision, otherwise it is stale/absent
    run = db.scalars(stmt.order_by(CvRun.created_at.desc()).limit(1)).first()
    if run is None:
        return _na("No completed continuous-verification run exists for this revision." if res.head_sha else "No completed continuous-verification run exists.")
    return {"state": "ok", "detail": f"Quality gate {run.gate_outcome}.", "data": {"outcome": run.gate_outcome, "run_id": run.id, "head_sha": run.head_sha}}


def security(db: Session, res: Resource) -> dict[str, Any]:
    stmt = select(SecScan).where(SecScan.repository_id == res.repository_id, SecScan.status == "completed")
    if res.head_sha:
        stmt = stmt.where(SecScan.head_sha == res.head_sha)
    scan = db.scalars(stmt.order_by(SecScan.created_at.desc()).limit(1)).first()
    if scan is None:
        return _na("No completed security scan exists for this revision." if res.head_sha else "No completed security scan exists.")
    summ = scan.summary or {}
    return {"state": "ok", "detail": f"Security scan {scan.id} ({summ.get('state', 'unknown')}).", "data": {"scan_id": scan.id, "state": summ.get("state"), "open_by_severity": summ.get("open_by_severity") or {}}}


def code_review(db: Session, res: Resource) -> dict[str, Any]:
    if res.type == "code_review":
        r = db.get(CodeReview, res.id)
        done = r is not None and r.status in ("completed", "completed_with_warnings")
        return {"state": "ok" if done else "unavailable", "detail": f"Code review status {r.status if r else 'missing'}.", "data": {"review_id": res.id, "status": r.status if r else None, "risk_category": r.risk_category if r else None}}
    if res.type == "change_request" and res.head_sha:
        r = db.scalars(select(CodeReview).where(CodeReview.repository_id == res.repository_id, CodeReview.head_sha == res.head_sha,
                                                CodeReview.status.in_(("completed", "completed_with_warnings"))).order_by(CodeReview.created_at.desc()).limit(1)).first()
        if r is None:
            return _na("No completed code review exists for this revision.")
        return {"state": "ok", "detail": "A completed code review exists for this revision.", "data": {"review_id": r.id, "status": r.status, "risk_category": r.risk_category}}
    return _na("Code-review evidence is not available for this resource type.")


def risk(db: Session, res: Resource) -> dict[str, Any]:
    ev = code_review(db, res)
    level = (ev["data"].get("risk_category") or "").lower() if ev["state"] == "ok" else ""
    if level not in RISK_ORDER:
        return _na("No change-risk assessment is available for this resource.")
    return {"state": "ok", "detail": f"Assessed change risk: {level}.", "data": {"level": level, "review_id": ev["data"].get("review_id")}}


def release_readiness(db: Session, res: Resource) -> dict[str, Any]:
    if res.type != "change_request":
        return _na("Release readiness is only evaluated for change requests.")
    q = select(CiWorkflowRun).where(CiWorkflowRun.change_request_id == res.id, CiWorkflowRun.readiness.is_not(None))
    if res.head_sha:
        q = q.where(CiWorkflowRun.head_sha == res.head_sha)
    wf = db.scalars(q.order_by(CiWorkflowRun.created_at.desc()).limit(1)).first()
    if wf is None:
        return _na("No release-readiness decision exists for this revision.")
    return {"state": "ok", "detail": f"Release readiness {wf.readiness}.", "data": {"readiness": wf.readiness, "workflow_run_id": wf.id}}


def external_ci(db: Session, res: Resource, max_age_hours: int, sources: list[str] | None, now: Any = None) -> dict[str, Any]:
    """External CI evidence for THIS commit. Absent, pending, cancelled, unknown or stale evidence is "unavailable" (never a pass);
    any failed run is ok-with-failure so the policy reports a failure. Only a fresh success for every recorded run passes."""
    from datetime import UTC, timedelta

    from app.db.base import utcnow
    now = now or utcnow()
    if not res.head_sha:
        return _na("This resource has no commit, so external CI evidence cannot be matched.")
    q = select(CiEvidenceRun).where(CiEvidenceRun.repository_id == res.repository_id, CiEvidenceRun.head_sha == res.head_sha).order_by(CiEvidenceRun.received_at.desc(), CiEvidenceRun.id)
    runs = [r for r in db.scalars(q) if not sources or r.source in sources]
    if not runs:
        return _na("No external CI run has been recorded for this commit.")
    latest: dict[tuple[str, str], CiEvidenceRun] = {}
    for r in runs:
        latest.setdefault((r.source, r.name or r.external_run_id), r)
    def aware(d):  # noqa: ANN001, ANN202
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    statuses: dict[str, int] = {}
    stale = 0
    for r in latest.values():
        st = r.status
        if st == "success" and aware(r.completed_at or r.received_at) < aware(now) - timedelta(hours=max_age_hours):
            st, stale = "stale", stale + 1
        statuses[st] = statuses.get(st, 0) + 1
    data = {"runs": len(latest), "by_status": statuses, "stale": stale}
    if statuses.get("failed"):
        return {"state": "ok", "detail": "An external CI run failed for this commit.", "data": {**data, "passed": False}}
    if len(latest) == statuses.get("success", 0):
        return {"state": "ok", "detail": "All external CI runs for this commit succeeded.", "data": {**data, "passed": True}}
    reason = "stale" if statuses.get("stale") else "still pending" if statuses.get("pending") else "cancelled or of unknown status"
    return _na(f"External CI evidence is not conclusive ({reason}).")
