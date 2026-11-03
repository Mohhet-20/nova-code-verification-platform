"""CI/CD integration: provider connections, change requests, revision-bound verification workflows, readiness, reports, status reporting and the
authenticated GitHub webhook. Webhooks never start test execution; an authorized user (or a later policy) starts verification."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db, get_session_factory
from app.models import ChangeRequest, CiConnection, CiReport, CiStatusReport, CiWorkflowRun, Repository
from app.observability.jobs import tracked
from app.schemas.cicd import ConnectionBody, PolicyBody, ReportBody, SendStatusBody, StartBody
from app.schemas.common import Page, make_page
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.cicd import readiness, reports, service, webhook
from app.services.cicd.errors import CiInvalid, CiNotFound, CiTooLarge
from app.services.verification import engine
from app.services.verification import service as cv_service

router = APIRouter(prefix="/repositories/{repository_id}/ci", tags=["ci"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@tracked("cicd", "cicd", model=CiWorkflowRun, id_arg=1, failed=("error",))
def _job(repo_id: str, wf_id: str, cv_run_id: str) -> None:
    factory, settings = get_session_factory(), get_settings()
    engine.run_job(factory, settings, cv_run_id)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        if repo is not None:
            service.finalize(db, settings, repo, wf_id)


@router.get("/overview")
def overview(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    conns = list(db.scalars(select(CiConnection).where(CiConnection.repository_id == repo.id).order_by(CiConnection.created_at)))
    counts = dict(db.execute(select(ChangeRequest.state, func.count()).where(ChangeRequest.repository_id == repo.id).group_by(ChangeRequest.state)).all())
    version, rules, _ = readiness.current(db, repo.id)
    return jsonable_encoder({"connections": [service.connection_out(c) for c in conns], "change_requests": {k: int(counts.get(k, 0)) for k in ("open", "closed", "merged")},
                             "readiness_policy_version": version, "readiness_policy_is_default": version == 0, "can_admin": ctx.role in readiness.ADMIN_ROLES})


@router.get("/connections")
def list_connections(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder([service.connection_out(c) for c in db.scalars(select(CiConnection).where(CiConnection.repository_id == repo.id).order_by(CiConnection.created_at))])


@router.post("/connections", status_code=201)
def save_connection(repository_id: str, body: ConnectionBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    c = service.upsert_connection(db, ctx, repo, provider=body.provider, external_repo=body.external_repo.strip(), default_branch=body.default_branch.strip(),
                                  enabled_events=body.enabled_events, token_env=body.token_env.strip(), webhook_secret_env=body.webhook_secret_env.strip(),
                                  status_context=body.status_context.strip(), enabled=body.enabled, settings=settings)
    return jsonable_encoder(service.connection_out(c))


@router.post("/connections/{connection_id}/validate")
def validate_connection(repository_id: str, connection_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                        settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.connection_out(service.validate_connection(db, ctx, settings, repo, service.get_connection(db, repo.id, connection_id))))


@router.post("/connections/{connection_id}/sync")
def sync_connection(repository_id: str, connection_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return service.sync_local(db, ctx, settings, repo, service.get_connection(db, repo.id, connection_id))


@router.get("/readiness-policy")
def get_readiness_policy(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    v, rules, _ = readiness.current(db, repo.id)
    return {"version": v, "is_default": v == 0, "rules": rules, "can_edit": ctx.role in readiness.ADMIN_ROLES, "precedence": list(readiness.PRECEDENCE)}


@router.put("/readiness-policy")
def put_readiness_policy(repository_id: str, body: PolicyBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    row = readiness.update(db, ctx, repo, body.rules, body.note)
    return {"version": row.version, "is_default": False, "rules": row.rules, "can_edit": True, "precedence": list(readiness.PRECEDENCE)}


@router.get("/change-requests")
def list_change_requests(repository_id: str, state: str | None = Query(None, max_length=10), q: str | None = Query(None, max_length=100), page: int = Query(1, ge=1),
                         page_size: int = Query(25, ge=1, le=100), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(ChangeRequest).where(ChangeRequest.repository_id == repo.id)
    if state:
        stmt = stmt.where(ChangeRequest.state == state)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(ChangeRequest.title.like(like, escape="\\") | ChangeRequest.source_branch.like(like, escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(ChangeRequest.updated_at.desc(), ChangeRequest.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(jsonable_encoder([service.cr_out(db, r) for r in rows]), total, page, page_size)


@router.get("/change-requests/{cr_id}")
def get_change_request(repository_id: str, cr_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.cr_out(db, service.get_cr(db, repo.id, cr_id), detail=True))


@router.post("/change-requests/{cr_id}/fetch-files")
def fetch_files(repository_id: str, cr_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(service.cr_out(db, service.fetch_provider_files(db, ctx, settings, repo, service.get_cr(db, repo.id, cr_id)), detail=True))


@router.post("/change-requests/{cr_id}/workflow-runs", status_code=202)
def start_workflow(repository_id: str, cr_id: str, body: StartBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    cr = service.get_cr(db, repo.id, cr_id)
    wf, reused = service.start_workflow(db, ctx, settings, repo, cr, mode=body.mode, confirm=body.confirm, force=body.force)
    if not reused and wf.cv_run_id:
        background.add_task(_job, repo.id, wf.id, wf.cv_run_id)
    return jsonable_encoder({**service.wf_out(db, wf), "reused": reused})


@router.get("/change-requests/{cr_id}/workflow-runs")
def list_cr_workflows(repository_id: str, cr_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    cr = service.get_cr(db, repo.id, cr_id)
    rows = db.scalars(select(CiWorkflowRun).where(CiWorkflowRun.change_request_id == cr.id).order_by(CiWorkflowRun.created_at.desc(), CiWorkflowRun.id.desc()).limit(50))
    return jsonable_encoder([service.wf_out(db, w) for w in rows])


@router.get("/workflow-runs")
def list_workflows(repository_id: str, status: str | None = Query(None, max_length=12), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(CiWorkflowRun).where(CiWorkflowRun.repository_id == repo.id)
    if status:
        stmt = stmt.where(CiWorkflowRun.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(CiWorkflowRun.created_at.desc(), CiWorkflowRun.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(jsonable_encoder([service.wf_out(db, w) for w in rows]), total, page, page_size)


@router.get("/workflow-runs/{wf_id}")
def get_workflow(repository_id: str, wf_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    wf = service.refresh(db, settings, repo, service.get_wf(db, repo.id, wf_id))
    out = service.wf_out(db, wf)
    out["checks"] = []
    if wf.cv_run_id:
        out["checks"] = [engine.check_dict(c) for c in engine.checks_of(db, wf.cv_run_id)]
    return jsonable_encoder(out)


@router.get("/workflow-runs/{wf_id}/readiness")
def get_readiness(repository_id: str, wf_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    wf = service.refresh(db, settings, repo, service.get_wf(db, repo.id, wf_id))
    if wf.status in ("queued", "running", "awaiting") or wf.cv_run_id is None:
        return {"workflow_run_id": wf.id, "outcome": None, "decision": None, "reason": f"No decision: the workflow is {wf.status}. Readiness is never assumed.", "stale": False}
    decision = service.evaluate(db, settings, repo, wf)  # always current: age, policy version and source state are re-checked
    return jsonable_encoder({"workflow_run_id": wf.id, "outcome": decision["outcome"], "decision": decision, "stale": service.wf_out(db, wf)["stale"]})


@router.post("/workflow-runs/{wf_id}/evaluate")
def evaluate_workflow(repository_id: str, wf_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    ctx.require_write()
    return get_readiness(repository_id, wf_id, ctx, db, settings)


@router.post("/workflow-runs/{wf_id}/cancel")
def cancel_workflow(repository_id: str, wf_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    wf = service.get_wf(db, repo.id, wf_id)
    if wf.status == "awaiting":
        ctx.require_write()
        wf.status = "cancelled"
        db.commit()
    elif wf.cv_run_id:
        cv_service.cancel(db, ctx, repo, cv_service.get_run(db, repo.id, wf.cv_run_id), settings)
        service.refresh(db, settings, repo, wf)
    return jsonable_encoder(service.wf_out(db, wf))


@router.get("/change-requests/{cr_id}/review-report")
def review_report(repository_id: str, cr_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    cr = service.get_cr(db, repo.id, cr_id)
    wf = service.latest_wf(db, cr, head_only=True) or service.latest_wf(db, cr)
    if wf is not None:
        service.refresh(db, settings, repo, wf)
    body = reports._sanitize(reports.collect(db, settings, repo, cr, wf, "review"), repo.root_path)
    return jsonable_encoder(body)


@router.post("/change-requests/{cr_id}/reports", status_code=201)
def generate_report(repository_id: str, cr_id: str, body: ReportBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    cr = service.get_cr(db, repo.id, cr_id)
    wf = service.get_wf(db, repo.id, body.workflow_run_id) if body.workflow_run_id else (service.latest_wf(db, cr, head_only=True) or service.latest_wf(db, cr))
    if wf is not None and wf.change_request_id != cr.id:
        raise CiNotFound("workflow_not_found", "Workflow run not found.")
    return jsonable_encoder(reports.report_out(reports.generate(db, ctx, settings, repo, cr, wf, body.kind), full=True))


@router.get("/reports")
def list_reports(repository_id: str, change_request_id: str | None = Query(None, max_length=64), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(CiReport).where(CiReport.repository_id == repo.id)
    if change_request_id:
        stmt = stmt.where(CiReport.change_request_id == change_request_id)
    return jsonable_encoder([reports.report_out(r) for r in db.scalars(stmt.order_by(CiReport.created_at.desc(), CiReport.id.desc()).limit(50))])


def _report(db: Session, repo_id: str, rid: str) -> CiReport:
    r = db.scalar(select(CiReport).where(CiReport.id == rid, CiReport.repository_id == repo_id))
    if r is None:
        raise CiNotFound("report_not_found", "Report not found.")
    return r


@router.get("/reports/{report_id}")
def get_report(repository_id: str, report_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(reports.report_out(_report(db, repo.id, report_id), full=True))


@router.get("/reports/{report_id}/markdown", response_class=PlainTextResponse)
def get_report_markdown(repository_id: str, report_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return PlainTextResponse(_report(db, repo.id, report_id).markdown, media_type="text/markdown; charset=utf-8", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/workflow-runs/{wf_id}/status-reports")
def list_status_reports(repository_id: str, wf_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    wf = service.get_wf(db, repo.id, wf_id)
    return jsonable_encoder([service.report_out(r) for r in db.scalars(select(CiStatusReport).where(CiStatusReport.workflow_run_id == wf.id).order_by(CiStatusReport.created_at))])


@router.post("/workflow-runs/{wf_id}/status-reports", status_code=201)
def send_status(repository_id: str, wf_id: str, body: SendStatusBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    ctx.require_write()
    if not body.confirm:
        raise CiInvalid("confirmation_required", "Reporting a status writes to the external provider. Send confirm=true.")
    wf = service.refresh(db, settings, repo, service.get_wf(db, repo.id, wf_id))
    cr = db.get(ChangeRequest, wf.change_request_id)
    conn = db.get(CiConnection, cr.connection_id) if cr and cr.connection_id else None
    if cr is None or conn is None or conn.provider == "local":
        raise CiInvalid("no_provider", "This change request has no external provider connection to report to.")
    return jsonable_encoder(service.report_out(service.report_status(db, settings, repo, conn, cr, wf)))


@router.post("/status-reports/{report_id}/retry")
def retry_status(repository_id: str, report_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    rep = db.scalar(select(CiStatusReport).where(CiStatusReport.id == report_id, CiStatusReport.repository_id == repo.id))
    if rep is None:
        raise CiNotFound("status_report_not_found", "Status report not found.")
    return jsonable_encoder(service.report_out(service.retry_report(db, ctx, settings, repo, rep)))


@webhook_router.post("/github/{connection_id}")
async def github_webhook(connection_id: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.ci_webhook_max_bytes:
        raise CiTooLarge("payload_too_large", "The webhook body exceeds the size limit.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.ci_webhook_max_bytes:
            raise CiTooLarge("payload_too_large", "The webhook body exceeds the size limit.")
        chunks.append(chunk)
    return webhook.receive(db, settings, connection_id, dict(request.headers), b"".join(chunks))
