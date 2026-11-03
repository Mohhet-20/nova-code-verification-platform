"""AI-assisted refactoring: catalogue, requests, plans, previews, validation, approval, application and verification.

Requests and validation never modify repository files. ``apply`` is the only writer and needs an approved transformation plus ``confirm``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db, get_session_factory
from app.models import (
    AuditEvent,
    IndexSnapshot,
    RefactorTransformation,
    RefactorVerification,
    TestExecutionRun,
)
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.refactor import (
    ApplyBody,
    ApproveBody,
    CyclesBody,
    DuplicatesBody,
    ExtractBody,
    RejectBody,
    RenameBody,
    RunBody,
    SimplifyBody,
    TransformationDetail,
    TransformationOut,
    VerificationDetail,
)
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.refactor import duplicates, registry, verify
from app.services.refactor import service as rsvc
from app.services.refactor.errors import RefactorInvalid
from app.services.repair import patch as patchlib
from app.services.repair import verify as repair_verify
from app.services.testing import execution

router = APIRouter(prefix="/repositories/{repository_id}/refactors", tags=["refactor"])


def _out(t: RefactorTransformation, detail: bool = False) -> Any:
    cls = TransformationDetail if detail else TransformationOut
    o = cls.model_validate(t)
    findings = (t.plan or {}).get("findings", [])
    o.blockers = sum(1 for f in findings if f.get("severity") == "blocker")
    o.warnings = sum(1 for f in findings if f.get("severity") == "warning")
    if detail:
        o.has_diff = bool(t.diff_text)
    return o


def _get(db: Session, ctx: RequestContext, repository_id: str, tid: str) -> tuple[Any, RefactorTransformation]:
    repo = svc.get_repository(db, ctx, repository_id)
    return repo, rsvc.get_transformation(db, repo, tid)


@tracked("indexing", "indexing", model=IndexSnapshot, id_arg=2, failed=("failed",))
def _index_job(workspace_id: str, user_id: str | None, snapshot_id: str) -> None:
    with get_session_factory()() as session:
        svc.run_indexing(session, get_settings(), workspace_id, user_id, snapshot_id)


@tracked("refactor_verification", "refactor_verification")
def _vjob(run_id: str, verification_id: str) -> None:
    verify.run_job(get_session_factory(), get_settings(), run_id, verification_id)


@tracked("refactor_baseline", "refactor_verification")
def _bjob(run_id: str) -> None:
    with get_session_factory()() as session:
        execution.run_execution(session, get_settings(), run_id)


def _v_detail(db: Session, settings: Settings, repo: Any, v: RefactorVerification, evaluate: bool = True) -> VerificationDetail:
    if evaluate:
        verify.evaluate(db, None, settings, repo, v)
    out = VerificationDetail.model_validate(v)
    out.meaning = verify.MEANING[v.outcome]
    out.run = repair_verify.public_run(settings, db.get(TestExecutionRun, v.execution_run_id) if v.execution_run_id else None)
    return out


@router.get("/types")
def list_types(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    svc.get_repository(db, ctx, repository_id)
    return {"types": registry.catalog(), "support_levels": {"applicable": "A validated multi-file diff can be generated and, after approval, applied.",
                                                             "proposal_only": "Analysis or suggestions only. Nothing is ever applied automatically.",
                                                             "unsupported": "Listed so the answer is explicit; requests are rejected."}}


@router.post("/rename", status_code=201)
def request_rename(repository_id: str, body: RenameBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    t = rsvc.request_rename(db, ctx, settings, repo, type_id=body.type_id, rel_path=body.rel_path, name=body.name, new_name=body.new_name, function=body.function,
                            class_name=body.class_name, kind=body.kind)
    return _out(t, True)


@router.post("/extract-function", status_code=201)
def request_extract(repository_id: str, body: ExtractBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return _out(rsvc.request_extract(db, ctx, settings, repo, rel_path=body.rel_path, start_line=body.start_line, end_line=body.end_line, new_name=body.new_name), True)


@router.post("/duplicates", status_code=201)
def request_duplicates(repository_id: str, body: DuplicatesBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    opts = duplicates.DupOptions(min_lines=body.min_lines or settings.refactor_dup_min_lines, min_tokens=body.min_tokens or settings.refactor_dup_min_tokens,
                                 threshold=body.threshold or settings.refactor_dup_threshold, path_prefix=body.path_prefix, exclude_paths=body.exclude_paths,
                                 exclude_tests=body.exclude_tests, include_statement_sequences=body.include_statement_sequences, max_groups=body.max_groups)
    if body.languages:
        opts.languages = set(body.languages)
    if body.kinds:
        opts.kinds = set(body.kinds)
    return _out(rsvc.create_duplicates(db, ctx, settings, repo, opts), True)


@router.post("/simplify", status_code=201)
def request_simplify(repository_id: str, body: SimplifyBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return _out(rsvc.create_simplify(db, ctx, settings, repo, body.rel_path), True)


@router.post("/cycle-suggestions", status_code=201)
def request_cycles(repository_id: str, body: CyclesBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return _out(rsvc.create_cycle_suggestions(db, ctx, settings, repo, body.path_prefix), True)


@router.get("")
def list_refactors(repository_id: str, status: str | None = Query(None, max_length=24), type_id: str | None = Query(None, max_length=60),
                   page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db)) -> Page[TransformationOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(RefactorTransformation).where(RefactorTransformation.repository_id == repo.id)
    if status:
        stmt = stmt.where(RefactorTransformation.status == status)
    if type_id:
        stmt = stmt.where(RefactorTransformation.type_id == type_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(RefactorTransformation.created_at.desc(), RefactorTransformation.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_out(r) for r in rows], total, page, page_size)


@router.get("/{refactor_id}")
def get_refactor(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> TransformationDetail:
    return _out(_get(db, ctx, repository_id, refactor_id)[1], True)


@router.get("/{refactor_id}/plan")
def get_plan(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _get(db, ctx, repository_id, refactor_id)[1]
    return jsonable_encoder({"id": t.id, "status": t.status, "support_level": t.support_level, "plan": t.plan, "impact": t.impact})


@router.get("/{refactor_id}/diff")
def get_diff(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    t = _get(db, ctx, repository_id, refactor_id)[1]
    if not t.diff_text:
        return {"id": t.id, "status": t.status, "has_diff": False, "preview": {"files": []},
                "reason": "This request has no applicable diff (" + ("analysis only" if t.support_level == "proposal_only" else f"status {t.status}") + ")."}
    try:
        prev = patchlib.preview(t.diff_text, settings, settings.refactor_max_files)
    except patchlib.PatchError as exc:
        prev = {"error": exc.message, "files": []}
    return jsonable_encoder({"id": t.id, "status": t.status, "has_diff": True, "patch_sha256": t.patch_sha256, "preview": prev})


@router.get("/{refactor_id}/duplicates")
def get_duplicate_groups(repository_id: str, refactor_id: str, kind: str | None = Query(None, pattern="^(exact|renamed|structural)$"),
                         min_similarity: float = Query(0.0, ge=0, le=1), path: str = Query("", max_length=500), page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=100), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _get(db, ctx, repository_id, refactor_id)[1]
    if t.type_id != "analysis.duplicates":
        raise RefactorInvalid("wrong_type", "This request is not a duplicate analysis.")
    ex = (t.plan or {}).get("extra", {})
    groups = [g for g in ex.get("groups", []) if (not kind or g["kind"] == kind) and g["similarity"] >= min_similarity
              and (not path or any(m["rel_path"].startswith(path) for m in g["members"]))]
    start = (page - 1) * page_size
    return jsonable_encoder({"id": t.id, "total": len(groups), "page": page, "page_size": page_size, "groups": groups[start:start + page_size],
                             "stats": ex.get("stats", {}), "filters": ex.get("filters", {}), "limitations": ex.get("limitations", []), "index": (t.plan or {}).get("snapshot", {})})


@router.post("/{refactor_id}/validate")
def validate_refactor(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> TransformationDetail:
    ctx.require_write()
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return _out(rsvc.validate(db, ctx, settings, repo, t), True)


@router.post("/{refactor_id}/submit")
def submit_refactor(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return _out(rsvc.submit(db, ctx, settings, repo, t), True)


@router.post("/{refactor_id}/approve")
def approve_refactor(repository_id: str, refactor_id: str, body: ApproveBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return _out(rsvc.approve(db, ctx, settings, repo, t, body.acknowledged_warnings), True)


@router.post("/{refactor_id}/reject")
def reject_refactor(repository_id: str, refactor_id: str, body: RejectBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> TransformationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return _out(rsvc.reject(db, ctx, repo, t, body.reason), True)


@router.post("/{refactor_id}/close")
def close_refactor(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> TransformationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return _out(rsvc.close(db, ctx, repo, t), True)


@router.post("/{refactor_id}/apply")
def apply_refactor(repository_id: str, refactor_id: str, body: ApplyBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> TransformationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    t = rsvc.apply(db, ctx, settings, repo, t, body.confirm)
    snap_id = rsvc.schedule_reindex(db, ctx, settings, repo, t)
    if snap_id:
        background.add_task(_index_job, ctx.workspace.id, ctx.user.id, snap_id)
    return _out(t, True)


@router.get("/{refactor_id}/rollback")
def rollback(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return jsonable_encoder(rsvc.rollback_guidance(db, settings, repo, t))


@router.post("/{refactor_id}/baseline", status_code=202)
def start_baseline(repository_id: str, refactor_id: str, body: RunBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    run = rsvc.start_baseline(db, ctx, settings, repo, t, body.test_ids or None, body.coverage, body.confirm)
    background.add_task(_bjob, run.id)
    return jsonable_encoder({"execution_run_id": run.id, "status": run.status, "note": t.baseline_note})


@router.post("/{refactor_id}/verify/preview")
def preview_verification(repository_id: str, refactor_id: str, body: RunBody, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    return jsonable_encoder(verify.preview(db, repo, t, body.test_ids or None, body.scope))


@router.post("/{refactor_id}/verifications", status_code=202)
def start_verification(repository_id: str, refactor_id: str, body: RunBody, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                       db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> VerificationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    v = verify.start(db, ctx, settings, repo, t, body.test_ids or None, body.coverage, body.scope, body.confirm)
    if v.execution_run_id and v.outcome == "PENDING":
        background.add_task(_vjob, v.execution_run_id, v.id)
    return _v_detail(db, settings, repo, v, evaluate=False)


@router.get("/{refactor_id}/verifications")
def list_verifications(repository_id: str, refactor_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> list[VerificationDetail]:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    rows = db.scalars(select(RefactorVerification).where(RefactorVerification.transformation_id == t.id)
                      .order_by(RefactorVerification.created_at.desc(), RefactorVerification.id.desc()).limit(50))
    return [_v_detail(db, settings, repo, v) for v in rows]


@router.get("/{refactor_id}/verifications/{verification_id}")
def get_verification(repository_id: str, refactor_id: str, verification_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> VerificationDetail:
    repo, t = _get(db, ctx, repository_id, refactor_id)
    v = db.scalar(select(RefactorVerification).where(RefactorVerification.id == verification_id, RefactorVerification.transformation_id == t.id))
    if v is None:
        raise NotFoundError("Verification not found.")
    return _v_detail(db, settings, repo, v)


@router.get("/{refactor_id}/audit")
def refactor_audit(repository_id: str, refactor_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    _, t = _get(db, ctx, repository_id, refactor_id)
    ids = [t.id, *db.scalars(select(RefactorVerification.id).where(RefactorVerification.transformation_id == t.id))]
    stmt = select(AuditEvent).where(AuditEvent.workspace_id == ctx.workspace.id, AuditEvent.entity_id.in_(ids),
                                    AuditEvent.entity_type.in_(("refactor_transformation", "refactor_verification")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(AuditEvent.created_at, AuditEvent.id).offset((page - 1) * page_size).limit(page_size)).all()
    items = [{"id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "actor_user_id": r.actor_user_id,
              "details": r.details, "created_at": r.created_at} for r in rows]
    return make_page(jsonable_encoder(items), total, page, page_size)
