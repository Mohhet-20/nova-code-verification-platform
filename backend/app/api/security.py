"""Security Intelligence: dependency inventory, advisory matching, security rules, secrets triage, policy gate, remediation and reports.
Scans only read files. Every route is repository-scoped (404 across tenants) and writes are audited."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db, get_session_factory
from app.models import (
    SecAdvisory,
    SecComponent,
    SecEvaluation,
    SecException,
    SecFinding,
    SecRemediation,
    SecScan,
    SecSuppression,
)
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.security import (
    AdvisoryImportBody,
    EvaluateBody,
    ExceptionBody,
    PolicyBody,
    ReportBody,
    RevokeBody,
    ScanBody,
    SuppressionBody,
    TriageBody,
)
from app.security.auth import RequestContext, get_context
from app.services import audit
from app.services import repositories as svc
from app.services.security import advisories as adv
from app.services.security import policy, priority, remediation, reports, scan, service
from app.services.security.errors import SecForbidden, SecInvalid, SecTooLarge

router = APIRouter(prefix="/repositories/{repository_id}/security", tags=["security"])
SEVERITIES = ("critical", "high", "medium", "low", "info")
SORTS = {"priority": None, "severity": None, "path": SecFinding.rel_path, "last_seen": SecFinding.last_seen_at}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@tracked("security_scan", "security_scan", model=SecScan, failed=("failed",))
def _job(scan_id: str) -> None:
    scan.run_job(get_session_factory(), get_settings(), scan_id)


@router.get("/overview")
def overview(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    last = service.latest_scan(db, repo.id, completed=False)
    done = service.latest_scan(db, repo.id)
    counts = dict(db.execute(select(SecFinding.status, func.count()).where(SecFinding.repository_id == repo.id).group_by(SecFinding.status)).all())
    sev = dict(db.execute(select(SecFinding.severity, func.count()).where(SecFinding.repository_id == repo.id, SecFinding.status == "open").group_by(SecFinding.severity)).all())
    ev = db.scalar(select(SecEvaluation).where(SecEvaluation.repository_id == repo.id).order_by(SecEvaluation.created_at.desc(), SecEvaluation.id.desc()).limit(1))
    version, _, _ = policy.current(db, repo.id)
    return jsonable_encoder({
        "latest_scan": service.scan_out(last) if last else None, "latest_completed_scan": service.scan_out(done) if done else None,
        "findings_by_status": {k: int(counts.get(k, 0)) for k in ("open", "resolved", "false_positive", "accepted_risk", "suppressed")},
        "open_by_severity": {k: int(sev.get(k, 0)) for k in SEVERITIES},
        "latest_evaluation": ({"id": ev.id, "status": ev.status, "policy_version": ev.policy_version, "scan_id": ev.scan_id, "created_at": ev.created_at, "explanation": (ev.decision or {}).get("explanation")} if ev else None),
        "policy_version": version, "policy_is_default": version == 0, "providers": adv.provider_status(settings), "can_write": ctx.role != "viewer", "can_admin": ctx.role == "owner",
        "notice": "No scan has been run yet." if last is None else None})


# ---------------------------------------------------------------- scans
@router.post("/scans", status_code=202)
def start_scan(repository_id: str, body: ScanBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    s = scan.start(db, ctx, settings, repo, list(body.scan_types), {"exclude_globs": body.exclude_globs, "include_dev": body.include_dev})
    background.add_task(_job, s.id)
    return jsonable_encoder(service.scan_out(s))


@router.get("/scans")
def list_scans(repository_id: str, status: str | None = Query(None, max_length=12), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
               ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(SecScan).where(SecScan.repository_id == repo.id)
    if status:
        stmt = stmt.where(SecScan.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(SecScan.created_at.desc(), SecScan.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(jsonable_encoder([service.scan_out(s) for s in rows]), total, page, page_size)


@router.get("/scans/{scan_id}")
def get_scan(repository_id: str, scan_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.scan_out(service.get_scan(db, repo.id, scan_id), detail=True))


@router.get("/scans/{scan_id}/components")
def list_components(repository_id: str, scan_id: str, ecosystem: str | None = Query(None, max_length=10), relationship: str | None = Query(None, max_length=12),
                    assessment: str | None = Query(None, max_length=20), q: str | None = Query(None, max_length=100), page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=200), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    s = service.get_scan(db, repo.id, scan_id)
    stmt = select(SecComponent).where(SecComponent.scan_id == s.id)
    if ecosystem:
        stmt = stmt.where(SecComponent.ecosystem == ecosystem)
    if relationship:
        stmt = stmt.where(SecComponent.relationship_kind == relationship)
    if q:
        stmt = stmt.where(SecComponent.name.like(_like(q), escape="\\"))
    rows = [c for c in db.scalars(stmt.order_by(SecComponent.ecosystem, SecComponent.name, SecComponent.version))]
    if assessment:
        rows = [c for c in rows if (c.assessment or {}).get("status") == assessment]
    total = len(rows)
    rows = rows[(page - 1) * page_size: page * page_size]
    items = [{"id": c.id, "ecosystem": c.ecosystem, "name": c.name, "version": c.version, "version_state": c.version_state, "relationship": c.relationship_kind, "scope": c.scope,
              "manifests": c.manifests, "declared": c.declared, "paths": c.paths, "license": c.license, "notes": c.notes, "assessment": c.assessment} for c in rows]
    return make_page(jsonable_encoder(items), total, page, page_size)


# ---------------------------------------------------------------- findings
@router.get("/findings")
def list_findings(repository_id: str, kind: str | None = Query(None, max_length=12), severity: str | None = Query(None, max_length=10), status: str | None = Query(None, max_length=16),
                  rule_id: str | None = Query(None, max_length=40), bucket: str | None = Query(None, max_length=2), path: str | None = Query(None, max_length=200),
                  q: str | None = Query(None, max_length=100), sort: str = Query("priority", max_length=12), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                  ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    if sort not in SORTS:
        raise SecInvalid("invalid_sort", f"sort must be one of {', '.join(SORTS)}.")
    stmt = select(SecFinding).where(SecFinding.repository_id == repo.id)
    for col, val in ((SecFinding.kind, kind), (SecFinding.severity, severity), (SecFinding.status, status), (SecFinding.rule_id, rule_id)):
        if val:
            stmt = stmt.where(col == val)
    if path:
        stmt = stmt.where(SecFinding.rel_path.like(_like(path), escape="\\"))
    if q:
        stmt = stmt.where(SecFinding.title.like(_like(q), escape="\\"))
    rows = list(db.scalars(stmt))
    if bucket:
        rows = [f for f in rows if (f.priority or {}).get("bucket") == bucket]
    order = {k: i for i, k in enumerate(SEVERITIES)}
    if sort == "priority":
        rows.sort(key=lambda f: (-(float((f.priority or {}).get("score", 0))), order.get(f.severity, 9), f.rel_path or "", f.line or 0, f.id))
    elif sort == "severity":
        rows.sort(key=lambda f: (order.get(f.severity, 9), -(float((f.priority or {}).get("score", 0))), f.rel_path or "", f.id))
    elif sort == "path":
        rows.sort(key=lambda f: (f.rel_path or "", f.line or 0, f.id))
    else:
        rows.sort(key=lambda f: (f.last_seen_at, f.id), reverse=True)
    total = len(rows)
    page_rows = rows[(page - 1) * page_size: page * page_size]
    return make_page(jsonable_encoder([service.finding_out(db, f) for f in page_rows]), total, page, page_size)


@router.get("/findings/{finding_id}")
def get_finding(repository_id: str, finding_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.finding_out(db, service.get_finding(db, repo.id, finding_id), detail=True))


@router.post("/findings/{finding_id}/triage")
def triage(repository_id: str, finding_id: str, body: TriageBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    f = service.triage(db, ctx, repo, service.get_finding(db, repo.id, finding_id), body.status, body.reason)
    return jsonable_encoder(service.finding_out(db, f, detail=True))


@router.get("/advisory-matches")
def advisory_matches(repository_id: str, match_status: str | None = Query(None, max_length=12), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    rows = [f for f in db.scalars(select(SecFinding).where(SecFinding.repository_id == repo.id, SecFinding.kind == "dependency").order_by(SecFinding.title))
            if not match_status or (f.evidence or {}).get("match_status") == match_status]
    total = len(rows)
    return make_page(jsonable_encoder([service.finding_out(db, f, detail=True) | {"title": f.title} for f in rows[(page - 1) * page_size: page * page_size]]), total, page, page_size)


# ---------------------------------------------------------------- suppressions & exceptions
@router.get("/suppressions")
def list_suppressions(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder([service.suppression_out(s) for s in db.scalars(select(SecSuppression).where(SecSuppression.repository_id == repo.id).order_by(SecSuppression.created_at.desc()).limit(200))])


@router.post("/suppressions", status_code=201)
def create_suppression(repository_id: str, body: SuppressionBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.suppression_out(service.suppress(db, ctx, repo, finding_id=body.finding_id, rule_id=body.rule_id, path_glob=body.path_glob, reason=body.reason, expires_at=body.expires_at)))


@router.post("/suppressions/{suppression_id}/revoke")
def revoke_suppression(repository_id: str, suppression_id: str, body: RevokeBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.suppression_out(service.revoke_suppression(db, ctx, repo, suppression_id, body.reason)))


@router.get("/exceptions")
def list_exceptions(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder([service.exception_out(x) for x in db.scalars(select(SecException).where(SecException.repository_id == repo.id).order_by(SecException.created_at.desc()).limit(200))])


@router.post("/exceptions", status_code=201)
def create_exception(repository_id: str, body: ExceptionBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.exception_out(service.add_exception(db, ctx, repo, body.selector, body.reason, body.expires_at)))


@router.post("/exceptions/{exception_id}/revoke")
def revoke_exception(repository_id: str, exception_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.exception_out(service.revoke_exception(db, ctx, repo, exception_id)))


# ---------------------------------------------------------------- policy
def _policy_out(db: Session, ctx: RequestContext, repo: Any) -> dict[str, Any]:
    v, rules, _ = policy.current(db, repo.id)
    return {"version": v, "is_default": v == 0, "rules": rules, "can_edit": ctx.role in policy.ADMIN_ROLES, "priority_model": priority.MODEL, "priority_defaults": priority.DEFAULT_WEIGHTS}


@router.get("/policy")
def get_policy(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    return jsonable_encoder(_policy_out(db, ctx, svc.get_repository(db, ctx, repository_id)))


@router.put("/policy")
def put_policy(repository_id: str, body: PolicyBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    policy.update(db, ctx, repo, body.rules, body.note)
    return jsonable_encoder(_policy_out(db, ctx, repo))


@router.get("/policy/history")
def policy_history(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder([{"version": p.version, "note": p.note, "created_by": p.created_by, "created_at": p.created_at, "rules": p.rules} for p in policy.history(db, repo.id)])


@router.post("/evaluate")
def evaluate(repository_id: str, body: EvaluateBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    ctx.require_write()
    s = service.get_scan(db, repo.id, body.scan_id) if body.scan_id else service.latest_scan(db, repo.id)
    if s is None:
        raise SecInvalid("no_scan", "Run a security scan before evaluating the policy.")
    v, rules, _ = policy.current(db, repo.id)
    d = policy.evaluate(db, repo, s, rules, v, ctx.user.id)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.policy_evaluated", entity_type="sec_evaluation", entity_id=d.get("evaluation_id") or s.id,
                 details={"repository_id": repo.id, "scan_id": s.id, "status": d["status"], "policy_version": v})
    db.commit()
    return jsonable_encoder(d)


@router.get("/evaluations")
def list_evaluations(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), ctx: RequestContext = Depends(get_context),
                     db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(SecEvaluation).where(SecEvaluation.repository_id == repo.id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(SecEvaluation.created_at.desc(), SecEvaluation.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(jsonable_encoder([{"id": e.id, "scan_id": e.scan_id, "status": e.status, "policy_version": e.policy_version, "head_sha": e.head_sha, "created_at": e.created_at, "decision": e.decision} for e in rows]), total, page, page_size)


# ---------------------------------------------------------------- reports
@router.post("/reports", status_code=201)
def create_report(repository_id: str, body: ReportBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    s = service.get_scan(db, repo.id, body.scan_id) if body.scan_id else service.latest_scan(db, repo.id, completed=False)
    if s is None:
        raise SecInvalid("no_scan", "Run a security scan before generating a report.")
    return jsonable_encoder(reports.out(reports.generate(db, ctx, settings, repo, s, body.format)))


@router.get("/reports/{report_id}")
def get_report(repository_id: str, report_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(reports.out(reports.get(db, repo.id, report_id), body=True))


@router.get("/reports/{report_id}/download")
def download_report(repository_id: str, report_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Response:
    repo = svc.get_repository(db, ctx, repository_id)
    r = reports.get(db, repo.id, report_id)
    mime = {"json": "application/json", "markdown": "text/markdown", "html": "text/html", "csv": "text/csv"}[r.format]
    ext = {"json": "json", "markdown": "md", "html": "html", "csv": "csv"}[r.format]
    headers = {"Content-Disposition": f'attachment; filename="security-report-{r.id}.{ext}"', "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
    return Response(r.body, media_type=mime + "; charset=utf-8", headers=headers)


# ---------------------------------------------------------------- remediation
@router.post("/findings/{finding_id}/remediations", status_code=201)
def propose_remediation(repository_id: str, finding_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(remediation.out(remediation.propose(db, ctx, settings, repo, service.get_finding(db, repo.id, finding_id))))


@router.get("/remediations")
def list_remediations(repository_id: str, finding_id: str | None = Query(None, max_length=64), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(SecRemediation).where(SecRemediation.repository_id == repo.id)
    if finding_id:
        stmt = stmt.where(SecRemediation.finding_id == finding_id)
    return jsonable_encoder([remediation.out(remediation.sync(db, repo, r), detail=False) for r in db.scalars(stmt.order_by(SecRemediation.created_at.desc()).limit(100))])


@router.get("/remediations/{remediation_id}")
def get_remediation(repository_id: str, remediation_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(remediation.out(remediation.sync(db, repo, remediation.get(db, repo.id, remediation_id))))


@router.post("/remediations/{remediation_id}/submit")
def submit_remediation(repository_id: str, remediation_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    r = remediation.get(db, repo.id, remediation_id)
    return jsonable_encoder(remediation.out(remediation.submit(db, ctx, settings, repo, r, service.get_finding(db, repo.id, r.finding_id))))


# ---------------------------------------------------------------- providers / advisory import
@router.get("/providers")
def providers(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    n = db.scalar(select(func.count()).select_from(SecAdvisory).where(SecAdvisory.workspace_id == repo.workspace_id)) or 0
    return {"providers": adv.provider_status(settings), "stored_advisories": int(n)}


@router.post("/advisories/import", status_code=201)
def import_advisories(repository_id: str, body: AdvisoryImportBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    if ctx.role != "owner":
        raise SecForbidden("owner_required", "Only a workspace owner can import advisory data.")
    import json as _json
    if len(_json.dumps(body.documents)) > settings.security_http_max_response_bytes:
        raise SecTooLarge("import_too_large", "The advisory import is too large.")
    recs, rejected = [], []
    for i, d in enumerate(body.documents):
        try:
            recs.extend(adv.normalize_osv(d, "imported", adv.utcnow()))
        except ValueError as exc:
            rejected.append({"index": i, "reason": str(exc)[:120]})
    n = adv.persist(db, repo.workspace_id, recs)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.advisories_imported", entity_type="sec_advisory", entity_id=repo.id,
                 details={"repository_id": repo.id, "records": n, "rejected": len(rejected)})
    db.commit()
    return {"imported": n, "rejected": rejected}
