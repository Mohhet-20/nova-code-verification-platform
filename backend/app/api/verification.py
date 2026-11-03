"""Continuous verification: change preview, planned and executed runs, baselines, regression findings, quality gates and policy.

Starting a run is the only operation that executes tests and needs ``confirm``. Policy changes need the workspace owner role.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db, get_session_factory
from app.models import CvBaseline, CvEvent, CvRun
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.verification import BaselineBody, ChangeBody, LinkBody, PolicyBody, RunBody
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.verification import engine, policy
from app.services.verification import service as vsvc

router = APIRouter(prefix="/repositories/{repository_id}/verification", tags=["verification"])


@tracked("verification", "verification", model=CvRun, failed=("error",))
def _job(run_id: str) -> None:
    engine.run_job(get_session_factory(), get_settings(), run_id)


def _detail(db: Session, settings: Settings, repo: Any, run: CvRun, stale: bool = True) -> dict[str, Any]:
    out = vsvc.run_summary(run)
    out.update({"change_set": {k: v for k, v in (run.change_set or {}).items() if k != "spec"}, "plan": run.plan,
                "checks": [engine.check_dict(c) for c in engine.checks_of(db, run.id)], "policy_snapshot": run.policy_snapshot,
                "gate": run.gate_decision, "regression": run.regression, "coverage": run.coverage, "static": run.static_result})
    out["freshness"] = vsvc.run_staleness(settings, repo, run) if stale else {"stale": None, "reason": None}
    return jsonable_encoder(out)


@router.get("/overview")
def overview(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.overview(db, repo))


@router.get("/policy")
def get_policy(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    version, rules, row = policy.current(db, repo.id)
    return jsonable_encoder({"version": version, "is_default": row is None, "rules": rules, "can_edit": ctx.role in policy.POLICY_ADMIN_ROLES,
                             "precedence": list(policy.PRECEDENCE)})


@router.put("/policy")
def put_policy(repository_id: str, body: PolicyBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    row = policy.update(db, ctx, repo, body.rules, body.note)
    return jsonable_encoder({"version": row.version, "is_default": False, "rules": row.rules, "can_edit": True, "precedence": list(policy.PRECEDENCE)})


@router.get("/policy/history")
def policy_history(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder([{"version": p.version, "rules": p.rules, "note": p.note, "created_by": p.created_by, "created_at": p.created_at}
                             for p in policy.history(db, repo.id)])


@router.post("/changes/preview")
def preview(repository_id: str, body: ChangeBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.preview(db, settings, repo, source=body.source, mode=body.mode, base=body.base, head=body.head, paths=body.paths,
                                         patch=body.patch, coverage=body.coverage))


@router.post("/runs", status_code=202)
def start_run(repository_id: str, body: RunBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
              db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.start(db, ctx, settings, repo, source=body.source, mode=body.mode, base=body.base, head=body.head, paths=body.paths, patch=body.patch,
                     coverage=body.coverage, baseline_id=body.baseline_id, confirm=body.confirm, link=body.link.model_dump() if body.link else None)
    background.add_task(_job, run.id)
    return _detail(db, settings, repo, run, stale=False)


@router.get("/runs")
def list_runs(repository_id: str, status: str | None = Query(None, max_length=12), outcome: str | None = Query(None, max_length=12),
              kind: str | None = Query(None, max_length=12), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
              ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(CvRun).where(CvRun.repository_id == repo.id)
    if status:
        stmt = stmt.where(CvRun.status == status)
    if outcome:
        stmt = stmt.where(CvRun.gate_outcome == outcome.upper())
    if kind:
        stmt = stmt.where(CvRun.kind == kind)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(CvRun.created_at.desc(), CvRun.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page(jsonable_encoder([vsvc.run_summary(r) for r in rows]), total, page, page_size)


@router.get("/runs/{run_id}")
def get_run(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return _detail(db, settings, repo, vsvc.get_run(db, repo.id, run_id))


@router.get("/runs/{run_id}/checks")
def run_checks(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.get_run(db, repo.id, run_id)
    return jsonable_encoder([engine.check_dict(c) for c in engine.checks_of(db, run.id)])


@router.get("/runs/{run_id}/regressions")
def run_regressions(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.get_run(db, repo.id, run_id).regression or {"available": False})


@router.get("/runs/{run_id}/coverage")
def run_coverage(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.get_run(db, repo.id, run_id).coverage or {"status": "not_requested"})


@router.get("/runs/{run_id}/gate")
def run_gate(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.get_run(db, repo.id, run_id)
    gate = run.gate_decision or {"outcome": None, "reason": "The run has not finished, so there is no decision yet.", "rules": []}
    return jsonable_encoder({"run_id": run.id, "status": run.status, "outcome": run.gate_outcome, "policy_version": run.policy_version,
                             "policy_snapshot": run.policy_snapshot, "decision": gate, "freshness": vsvc.run_staleness(settings, repo, run)})


@router.get("/runs/{run_id}/events")
def run_events(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.get_run(db, repo.id, run_id)
    rows = db.scalars(select(CvEvent).where(CvEvent.run_id == run.id).order_by(CvEvent.created_at, CvEvent.id).limit(500))
    return jsonable_encoder([{"id": e.id, "kind": e.kind, "detail": e.detail, "created_at": e.created_at} for e in rows])


@router.post("/runs/{run_id}/cancel")
def cancel_run(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.cancel(db, ctx, repo, vsvc.get_run(db, repo.id, run_id), settings)
    return _detail(db, settings, repo, run, stale=False)


@router.get("/runs/{run_id}/compare")
def compare(repository_id: str, run_id: str, baseline_id: str = Query(..., max_length=64), ctx: RequestContext = Depends(get_context),
            db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.compare(db, settings, repo, vsvc.get_run(db, repo.id, run_id), baseline_id))


@router.put("/runs/{run_id}/link")
def link_run(repository_id: str, run_id: str, body: LinkBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.link_run(db, ctx, repo, vsvc.get_run(db, repo.id, run_id), body.model_dump())
    return _detail(db, settings, repo, run, stale=False)


@router.post("/baselines", status_code=202)
def create_baseline(repository_id: str, body: BaselineBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    run = vsvc.start_baseline(db, ctx, settings, repo, label=body.label, coverage=body.coverage, confirm=body.confirm)
    background.add_task(_job, run.id)
    return _detail(db, settings, repo, run, stale=False)


@router.get("/baselines")
def list_baselines(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    rows = db.scalars(select(CvBaseline).where(CvBaseline.repository_id == repo.id).order_by(CvBaseline.created_at.desc(), CvBaseline.id.desc()).limit(50))
    return jsonable_encoder([vsvc.baseline_out(b) for b in rows])


@router.get("/baselines/{baseline_id}")
def get_baseline(repository_id: str, baseline_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Any:
    repo = svc.get_repository(db, ctx, repository_id)
    return jsonable_encoder(vsvc.baseline_out(vsvc.get_baseline(db, repo.id, baseline_id)))

