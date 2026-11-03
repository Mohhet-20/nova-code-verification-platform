"""Failure ingestion, localization, investigations and approved verification."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.db.session import get_db, get_session_factory
from app.models import (
    CoverageFile,
    FailureFrame,
    FailureOccurrence,
    FailureReport,
    Finding,
    Investigation,
    LocalizationCandidate,
    Repository,
    TestExecutionRun,
    VerificationRun,
)
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.debugging import (
    FailureDetail,
    FailureOut,
    FailurePatch,
    FrameOut,
    IngestOut,
    IngestRequest,
    InvestigationDetail,
    InvestigationOut,
    InvestigationPatch,
    VerificationRequest,
)
from app.security.auth import RequestContext, get_context
from app.services import audit
from app.services import repositories as svc
from app.services.analysis.model import OPEN_STATUSES
from app.services.analysis.redaction import scrub
from app.services.debugging import ingest, investigate, localize, verify
from app.services.testing import execution

router = APIRouter(prefix="/repositories/{repository_id}", tags=["debugging"])
SORTS = {"last_seen": FailureReport.last_seen_at, "first_seen": FailureReport.first_seen_at,
         "occurrences": FailureReport.occurrence_count, "category": FailureReport.category}


def _like(text: str) -> str:
    esc = "\\"
    return "%" + text.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_") + "%"


def _failure(db: Session, repo: Repository, failure_id: str) -> FailureReport:
    f = db.scalar(select(FailureReport).where(FailureReport.id == failure_id, FailureReport.repository_id == repo.id))
    if f is None:
        raise NotFoundError("Failure not found.")
    return f


def _inv(db: Session, repo: Repository, inv_id: str) -> Investigation:
    i = db.scalar(select(Investigation).where(Investigation.id == inv_id, Investigation.repository_id == repo.id))
    if i is None:
        raise NotFoundError("Investigation not found.")
    return i


def _brief(db: Session, f: FailureReport) -> FailureOut:
    out = FailureOut.model_validate(f)
    fr = db.scalar(select(FailureFrame).where(FailureFrame.failure_id == f.id, FailureFrame.scope == "repository",
                                              FailureFrame.is_test.is_(False)).order_by(FailureFrame.idx.desc()).limit(1))
    cand = db.scalar(select(LocalizationCandidate).where(LocalizationCandidate.failure_id == f.id, LocalizationCandidate.rank == 1))
    if cand is not None:
        out.top_location = cand.rel_path
    elif fr is not None:
        out.top_location = f"{fr.rel_path}:{fr.line}" if fr.line else fr.rel_path
    inv = db.scalar(select(Investigation).where(Investigation.failure_id == f.id).order_by(Investigation.created_at.desc()).limit(1))
    if inv is not None:
        out.investigation_status, out.investigation_id = inv.status, inv.id
    return out


# ------------------------------------------------------------------------------------------------ failures

@router.post("/failures/ingest", response_model=IngestOut, status_code=201)
def ingest_failures(repository_id: str, body: IngestRequest, ctx: RequestContext = Depends(get_context),
                    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> IngestOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    if body.source == "execution_run":
        pairs = ingest.ingest_run(db, ctx, settings, repo, body.execution_run_id or "")
    else:
        pairs = ingest.ingest_text(db, ctx, settings, repo, body.source, body.text or "", body.test_id)
    return IngestOut(items=[_brief(db, f) for f, _ in pairs], created=sum(1 for _, c in pairs if c), recurred=sum(1 for _, c in pairs if not c))


@router.get("/failures", response_model=Page[FailureOut])
def list_failures(
        repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
        status: list[Literal["open", "investigating", "resolved", "ignored"]] | None = Query(None),
        category: list[Literal["assertion", "exception", "collection_error", "test_error", "unknown"]] | None = Query(None),
        q: str | None = Query(None, max_length=200), path: str | None = Query(None, max_length=300),
        sort: Literal["last_seen", "first_seen", "occurrences", "category"] = Query("last_seen"),
        order: Literal["asc", "desc"] = Query("desc"),
        ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[FailureOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    cond: list[Any] = [FailureReport.repository_id == repo.id]
    if status:
        cond.append(FailureReport.status.in_(status))
    if category:
        cond.append(FailureReport.category.in_(category))
    if q:
        like = _like(q)
        cond.append(or_(FailureReport.summary.ilike(like, escape="\\"), FailureReport.test_id.ilike(like, escape="\\"),
                        FailureReport.exception_type.ilike(like, escape="\\")))
    if path:
        like = _like(path)
        cond.append(FailureReport.id.in_(select(FailureFrame.failure_id).where(FailureFrame.rel_path.ilike(like, escape="\\"))))
    col = SORTS[sort]
    total = db.scalar(select(func.count()).select_from(FailureReport).where(*cond)) or 0
    rows = db.scalars(select(FailureReport).where(*cond).order_by(col.asc() if order == "asc" else col.desc(), FailureReport.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_brief(db, f) for f in rows], total, page, page_size)


@router.get("/failures/{failure_id}", response_model=FailureDetail)
def get_failure(repository_id: str, failure_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> FailureDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    f = _failure(db, repo, failure_id)
    frames = list(db.scalars(select(FailureFrame).where(FailureFrame.failure_id == f.id).order_by(FailureFrame.idx)))
    occ = db.execute(select(FailureOccurrence.execution_run_id, FailureOccurrence.seen_at, FailureOccurrence.source, TestExecutionRun.status)
                     .outerjoin(TestExecutionRun, TestExecutionRun.id == FailureOccurrence.execution_run_id)
                     .where(FailureOccurrence.failure_id == f.id).order_by(FailureOccurrence.seen_at.desc()).limit(20)).all()
    paths = sorted({fr.rel_path for fr in frames if fr.rel_path})
    findings = [{"id": x.id, "rule_id": x.rule_id, "title": x.title, "severity": x.severity, "rel_path": x.rel_path, "start_line": x.start_line,
                 "end_line": x.end_line} for x in db.scalars(select(Finding).where(
                     Finding.repository_id == repo.id, Finding.status.in_(OPEN_STATUSES), Finding.rel_path.in_(paths or [""]))
                     .order_by(Finding.rel_path, Finding.start_line).limit(25))]
    cov_run = db.scalar(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id, TestExecutionRun.coverage_status == "measured")
                        .order_by(TestExecutionRun.finished_at.desc(), TestExecutionRun.id.desc()).limit(1))
    coverage: dict[str, Any] = {"available": cov_run is not None, "run_id": cov_run.id if cov_run else None, "files": []}
    if cov_run is not None:
        for cf in db.scalars(select(CoverageFile).where(CoverageFile.execution_run_id == cov_run.id, CoverageFile.rel_path.in_(paths or [""]))):
            coverage["files"].append({"rel_path": cf.rel_path, "line_percent": cf.line_percent, "missing_lines": cf.missing_lines})
    else:
        coverage["note"] = "No measured coverage exists; nothing is estimated."
    latest = svc.latest_snapshot(db, repo.id, completed_only=True)
    freshness = {"failure_snapshot_id": f.index_snapshot_id, "latest_snapshot_id": latest.id if latest else None,
                 "stale": bool(latest and f.index_snapshot_id and f.index_snapshot_id != latest.id),
                 "note": "The repository was re-indexed after this failure was ingested; locations may have moved."
                 if latest and f.index_snapshot_id and f.index_snapshot_id != latest.id else "The index has not changed since ingestion."}
    base = _brief(db, f)
    return FailureDetail(**base.model_dump(), message=f.message, fingerprint=f.fingerprint, warnings=f.warnings,
                         frames_truncated=f.frames_truncated, revision=f.revision, revision_known=f.revision is not None,
                         index_snapshot_id=f.index_snapshot_id, frames=[FrameOut.model_validate(x) for x in frames],
                         occurrences=[{"execution_run_id": r, "seen_at": s, "source": so, "run_status": st} for r, s, so, st in occ],
                         related_findings=findings, coverage=coverage, freshness=freshness, localization_meta=f.localization_meta)


@router.patch("/failures/{failure_id}", response_model=FailureOut)
def patch_failure(repository_id: str, failure_id: str, body: FailurePatch, ctx: RequestContext = Depends(get_context),
                  db: Session = Depends(get_db)) -> FailureOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    f = _failure(db, repo, failure_id)
    f.status = body.status
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="failures.status_changed",
                 entity_type="failure_report", entity_id=f.id, details={"repository_id": repo.id, "status": body.status})
    db.commit()
    return _brief(db, f)


@router.get("/failures/{failure_id}/localization")
def get_localization(repository_id: str, failure_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    f = _failure(db, repo, failure_id)
    return {"failure_id": f.id, **localize.run_localization(db, settings, repo, f)}


@router.get("/failures/{failure_id}/related")
def related(repository_id: str, failure_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    f = _failure(db, repo, failure_id)
    mine = list(db.scalars(select(FailureFrame).where(FailureFrame.failure_id == f.id, FailureFrame.scope == "repository")))
    paths = sorted({x.rel_path for x in mine if x.rel_path})
    syms = sorted({x.symbol_stable_id for x in mine if x.symbol_stable_id})
    rows = db.execute(select(FailureReport, FailureFrame.rel_path, FailureFrame.symbol_stable_id).join(FailureFrame, FailureFrame.failure_id == FailureReport.id)
                      .where(FailureReport.repository_id == repo.id, FailureReport.id != f.id, FailureFrame.scope == "repository",
                             FailureFrame.rel_path.in_(paths or [""]))).all()
    by: dict[str, dict[str, Any]] = {}
    for rep, p, s in rows:
        e = by.setdefault(rep.id, {"failure": _brief(db, rep).model_dump(mode="json"), "shared_files": set(), "shared_symbols": set()})
        e["shared_files"].add(p)
        if s and s in syms:
            e["shared_symbols"].add(s)
    items = [{"failure": e["failure"], "shared_files": sorted(e["shared_files"]), "shared_symbol_count": len(e["shared_symbols"]),
              "relationship": "shared_symbol" if e["shared_symbols"] else "shared_file"} for e in by.values()]
    items.sort(key=lambda e: (-e["shared_symbol_count"], -len(e["shared_files"]), e["failure"]["id"]))
    top = db.scalar(select(LocalizationCandidate).where(LocalizationCandidate.failure_id == f.id).order_by(LocalizationCandidate.rank).limit(1))
    imp = investigate.impact(db, settings, repo, top.rel_path, top.symbol_stable_id) if top else None
    return {"failure_id": f.id, "related_failures": items[:25], "impact": imp,
            "note": "Relationships are shared repository frames; they suggest, but do not prove, a common cause."}


# ------------------------------------------------------------------------------------------------ investigations

@router.post("/failures/{failure_id}/investigations", response_model=InvestigationDetail, status_code=201)
def create_investigation(repository_id: str, failure_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                         settings: Settings = Depends(get_settings)) -> InvestigationDetail:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    f = _failure(db, repo, failure_id)
    return _inv_detail(db, investigate.create(db, ctx, settings, repo, f))


def _inv_out(db: Session, i: Investigation) -> InvestigationOut:
    out = InvestigationOut.model_validate(i)
    out.failure_summary = db.scalar(select(FailureReport.summary).where(FailureReport.id == i.failure_id))
    return out


def _vr_dict(db: Session, vr: VerificationRun) -> dict[str, Any]:
    run = db.get(TestExecutionRun, vr.execution_run_id) if vr.execution_run_id else None
    base = db.get(TestExecutionRun, vr.baseline_execution_run_id) if vr.baseline_execution_run_id else None
    return {"id": vr.id, "outcome": vr.outcome, "meaning": verify.MEANING.get(vr.outcome, ""), "reason": vr.outcome_reason,
            "test_ids": vr.test_ids, "created_at": vr.created_at, "evaluated_at": vr.evaluated_at,
            "original_run": verify.summarize_run(base), "verification_run": verify.summarize_run(run),
            "baseline_available": base is not None}


def _inv_detail(db: Session, i: Investigation, settings: Settings | None = None, repo: Repository | None = None) -> InvestigationDetail:
    vrs = list(db.scalars(select(VerificationRun).where(VerificationRun.investigation_id == i.id).order_by(VerificationRun.created_at.desc()).limit(20)))
    if settings is not None and repo is not None:
        for vr in vrs:
            verify.evaluate(db, None, settings, repo, vr)
    return InvestigationDetail(**_inv_out(db, i).model_dump(), content=i.content, notes=i.notes, verifications=[_vr_dict(db, v) for v in vrs])


@router.get("/investigations", response_model=Page[InvestigationOut])
def list_investigations(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                        status: list[Literal["open", "in_review", "closed"]] | None = Query(None),
                        failure_id: str | None = Query(None, max_length=32),
                        ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[InvestigationOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    cond: list[Any] = [Investigation.repository_id == repo.id]
    if status:
        cond.append(Investigation.status.in_(status))
    if failure_id:
        cond.append(Investigation.failure_id == failure_id)
    total = db.scalar(select(func.count()).select_from(Investigation).where(*cond)) or 0
    rows = db.scalars(select(Investigation).where(*cond).order_by(Investigation.updated_at.desc(), Investigation.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_inv_out(db, r) for r in rows], total, page, page_size)


@router.get("/investigations/{investigation_id}", response_model=InvestigationDetail)
def get_investigation(repository_id: str, investigation_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> InvestigationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return _inv_detail(db, _inv(db, repo, investigation_id), settings, repo)


@router.patch("/investigations/{investigation_id}", response_model=InvestigationDetail)
def patch_investigation(repository_id: str, investigation_id: str, body: InvestigationPatch, ctx: RequestContext = Depends(get_context),
                        db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> InvestigationDetail:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    i = _inv(db, repo, investigation_id)
    if i.status == "closed" and body.status in (None, "closed") and body.resolution is not None:
        raise ConflictError("A closed investigation's resolution cannot be changed; reopen it first.")
    if body.notes is not None:
        i.notes = scrub(body.notes, 4000)
    if body.status == "closed" and body.resolution is None and i.resolution is None:
        raise ConflictError("Choose a resolution when closing an investigation (it is your classification, not NOVA's finding).")
    if body.resolution is not None:
        i.resolution = body.resolution
    if body.status is not None:
        i.status = body.status
        f = db.get(FailureReport, i.failure_id)
        if f is not None:
            if body.status == "closed":
                f.status = "ignored" if i.resolution in ("wont_fix", "duplicate") else "resolved"
            elif f.status in ("resolved", "ignored", "open"):
                f.status = "investigating"
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="investigations.updated",
                 entity_type="investigation", entity_id=i.id,
                 details={"repository_id": repo.id, "status": body.status, "has_notes": body.notes is not None, "resolution": body.resolution})
    db.commit()
    return _inv_detail(db, i, settings, repo)


# ------------------------------------------------------------------------------------------------ verification

@tracked("debug_verification", "repair_verification")
def _job(vr_id: str) -> None:
    with get_session_factory()() as session:
        vr = session.get(VerificationRun, vr_id)
        if vr is None or vr.execution_run_id is None:
            return
        settings = get_settings()
        execution.run_execution(session, settings, vr.execution_run_id)
        repo = session.get(Repository, vr.repository_id)
        if repo is not None:
            verify.evaluate(session, None, settings, repo, vr)


@router.post("/investigations/{investigation_id}/verification", response_model=None)
def request_verification(repository_id: str, investigation_id: str, body: VerificationRequest, background: BackgroundTasks,
                         ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                         settings: Settings = Depends(get_settings)) -> Any:
    """Without ``confirm`` this only previews the execution request. With ``confirm`` it approves and starts a run through
    the Phase 05 workflow (discovered tests only, restricted subprocess, one active run per repository)."""
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, investigation_id)
    failure = db.get(FailureReport, inv.failure_id)
    assert failure is not None  # FK guarantees it
    if not body.confirm:
        return JSONResponse(verify.preview(db, repo, inv, failure, body.test_ids, body.coverage) | {"investigation_id": inv.id}, status_code=200,
                            headers={"Cache-Control": "no-store"})
    vr = verify.start(db, ctx, settings, repo, inv, failure, body.test_ids, body.coverage)
    background.add_task(_job, vr.id)
    return JSONResponse(jsonable_encoder(_vr_dict(db, vr) | {"started": True}), status_code=202, headers={"Cache-Control": "no-store"})


@router.get("/investigations/{investigation_id}/verifications")
def list_verifications(repository_id: str, investigation_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, investigation_id)
    vrs = list(db.scalars(select(VerificationRun).where(VerificationRun.investigation_id == inv.id).order_by(VerificationRun.created_at.desc()).limit(50)))
    for vr in vrs:
        verify.evaluate(db, None, settings, repo, vr)
    return {"items": [_vr_dict(db, v) for v in vrs], "outcomes": list(verify.OUTCOMES)}
