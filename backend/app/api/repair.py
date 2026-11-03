"""Repair investigations, patches and verification. Approval, application and verification are separate, explicit actions."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db, get_session_factory
from app.models import (
    AuditEvent,
    RepairInvestigation,
    RepairPatch,
    RepairVerification,
    Repository,
    TestExecutionRun,
)
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.repair import (
    ConfirmBody,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationOut,
    InvestigationStatusPatch,
    PatchCreate,
    PatchDetail,
    PatchOut,
    RejectBody,
    VerificationDetail,
    VerifyBody,
)
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.repair import patch as patchlib
from app.services.repair import service, verify

router = APIRouter(prefix="/repositories/{repository_id}", tags=["repair"])
MEANING = {
    "PENDING": "The verification run has not finished.",
    "PASSED": "Every required criterion passed. This shows the selected checks passed; it does not prove the change is correct.",
    "FAILED": "At least one required criterion failed. The patch stays applied; see the criteria and recovery guidance.",
    "INCOMPLETE": "The run could not show whether the patch is good (timeout, skipped or missing tests). The patch is not verified.",
    "BLOCKED": "Verification could not start. The patch is not verified.",
}


def _like(text: str) -> str:
    esc = "\\"
    return "%" + text.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_") + "%"


def _inv(db: Session, repo: Repository, inv_id: str) -> RepairInvestigation:
    return service.get_investigation(db, repo, inv_id)


def _out(db: Session, inv: RepairInvestigation, detail: bool = False) -> InvestigationOut:
    cls = InvestigationDetail if detail else InvestigationOut
    out = cls.model_validate(inv)
    out.patch_count = db.scalar(select(func.count()).select_from(RepairPatch).where(RepairPatch.investigation_id == inv.id)) or 0
    out.latest_patch_status = db.scalar(select(RepairPatch.status).where(RepairPatch.investigation_id == inv.id)
                                        .order_by(RepairPatch.created_at.desc(), RepairPatch.id.desc()).limit(1))
    return out


@tracked("repair_verification", "repair_verification")
def _vjob(run_id: str, verification_id: str) -> None:
    verify.run_job(get_session_factory(), get_settings(), run_id, verification_id)


def _v_detail(db: Session, settings: Settings, repo: Repository, v: RepairVerification, evaluate: bool = True) -> VerificationDetail:
    if evaluate:
        verify.evaluate(db, None, settings, repo, v)
    out = VerificationDetail.model_validate(v, from_attributes=True)
    out.meaning = MEANING[v.outcome]
    out.run = verify.public_run(settings, db.get(TestExecutionRun, v.execution_run_id) if v.execution_run_id else None)
    return out


# ------------------------------------------------------------------------------------------------ investigations

@router.post("/repairs", status_code=201)
def create_repair(repository_id: str, body: InvestigationCreate, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> InvestigationOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = service.create_investigation(db, ctx, settings, repo, title=body.title, description=body.description, severity=body.severity,
                                       failure_id=body.failure_id, finding_id=body.finding_id, review_finding_id=body.review_finding_id,
                                       debug_investigation_id=body.debug_investigation_id, hint_files=body.hint_files)
    return _out(db, inv)


@router.get("/repairs")
def list_repairs(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), q: str | None = Query(None, max_length=200),
                 status: list[Literal["open", "analyzed", "proposed", "patched", "applied", "verified", "verification_failed", "closed"]] | None = Query(None),
                 severity: list[Literal["critical", "high", "medium", "low", "info"]] | None = Query(None),
                 verification_outcome: list[Literal["PENDING", "PASSED", "FAILED", "INCOMPLETE", "BLOCKED"]] | None = Query(None),
                 source_kind: Literal["manual", "failure", "finding", "review_finding", "debug_investigation"] | None = None,
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[InvestigationOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    stmt = select(RepairInvestigation).where(RepairInvestigation.repository_id == repo.id)
    if status:
        stmt = stmt.where(RepairInvestigation.status.in_(status))
    if severity:
        stmt = stmt.where(RepairInvestigation.severity.in_(severity))
    if verification_outcome:
        stmt = stmt.where(RepairInvestigation.verification_outcome.in_(verification_outcome))
    if source_kind:
        stmt = stmt.where(RepairInvestigation.source_kind == source_kind)
    if q:
        stmt = stmt.where(RepairInvestigation.title.ilike(_like(q), escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(RepairInvestigation.created_at.desc(), RepairInvestigation.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_out(db, r) for r in rows], total, page, page_size)


@router.get("/repairs/{repair_id}")
def get_repair(repository_id: str, repair_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> InvestigationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return _out(db, _inv(db, repo, repair_id), True)  # type: ignore[return-value]


@router.patch("/repairs/{repair_id}")
def patch_repair(repository_id: str, repair_id: str, body: InvestigationStatusPatch, ctx: RequestContext = Depends(get_context),
                 db: Session = Depends(get_db)) -> InvestigationOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    return _out(db, service.set_investigation_status(db, ctx, repo, _inv(db, repo, repair_id), body.status))


@router.post("/repairs/{repair_id}/evidence")
def assemble_evidence(repository_id: str, repair_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    ev = service.refresh_evidence(db, ctx, settings, repo, inv)
    return jsonable_encoder({"repair_id": inv.id, "status": inv.status, "root_cause": inv.root_cause, "relevant_files": inv.relevant_files, "evidence": ev})


@router.post("/repairs/{repair_id}/proposal")
def make_proposal(repository_id: str, repair_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    doc = service.generate_proposal(db, ctx, settings, repo, inv)
    return jsonable_encoder({"repair_id": inv.id, "status": inv.status, "proposal": doc})


@router.get("/repairs/{repair_id}/audit")
def repair_audit(repository_id: str, repair_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[dict[str, Any]]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    ids = [inv.id]
    ids += list(db.scalars(select(RepairPatch.id).where(RepairPatch.investigation_id == inv.id)))
    ids += list(db.scalars(select(RepairVerification.id).where(RepairVerification.investigation_id == inv.id)))
    stmt = select(AuditEvent).where(AuditEvent.workspace_id == ctx.workspace.id, AuditEvent.entity_id.in_(ids),
                                    AuditEvent.entity_type.in_(("repair_investigation", "repair_patch", "repair_verification")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(AuditEvent.created_at, AuditEvent.id).offset((page - 1) * page_size).limit(page_size)).all()
    items = [{"id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "actor_user_id": r.actor_user_id,
              "details": r.details, "created_at": r.created_at} for r in rows]
    return make_page(jsonable_encoder(items), total, page, page_size)


# ------------------------------------------------------------------------------------------------ patches

def _patch_detail(p: RepairPatch, settings: Settings) -> PatchDetail:
    out = PatchDetail.model_validate(p, from_attributes=True)
    try:
        out.preview = patchlib.preview(p.diff_text, settings)
    except patchlib.PatchError as exc:
        out.preview = {"error": exc.message, "files": []}
    return out


@router.post("/repairs/{repair_id}/patches", status_code=201)
def create_patch(repository_id: str, repair_id: str, body: PatchCreate, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> PatchDetail:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    p = service.create_patch(db, ctx, settings, repo, inv, source=body.source, title=body.title, diff=body.diff, extra_files=body.extra_files,
                             rule_id=body.rule_id, rel_path=body.rel_path, line=body.line)
    return _patch_detail(p, settings)


@router.get("/repairs/{repair_id}/patches")
def list_patches(repository_id: str, repair_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> list[PatchOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    rows = db.scalars(select(RepairPatch).where(RepairPatch.investigation_id == inv.id).order_by(RepairPatch.created_at.desc(), RepairPatch.id.desc()).limit(100))
    return [PatchOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/repairs/{repair_id}/patches/{patch_id}")
def get_patch(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)) -> PatchDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.get_patch(db, repo, inv, patch_id), settings)


@router.post("/repairs/{repair_id}/patches/{patch_id}/validate")
def validate_patch(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> PatchDetail:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.validate(db, ctx, settings, repo, inv, service.get_patch(db, repo, inv, patch_id)), settings)


@router.post("/repairs/{repair_id}/patches/{patch_id}/submit")
def submit_patch(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> PatchDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.submit(db, ctx, settings, repo, inv, service.get_patch(db, repo, inv, patch_id)), settings)


@router.post("/repairs/{repair_id}/patches/{patch_id}/approve")
def approve_patch(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> PatchDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.approve(db, ctx, settings, repo, inv, service.get_patch(db, repo, inv, patch_id)), settings)


@router.post("/repairs/{repair_id}/patches/{patch_id}/reject")
def reject_patch(repository_id: str, repair_id: str, patch_id: str, body: RejectBody, ctx: RequestContext = Depends(get_context),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> PatchDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.reject(db, ctx, repo, inv, service.get_patch(db, repo, inv, patch_id), body.reason), settings)


@router.post("/repairs/{repair_id}/patches/{patch_id}/apply")
def apply_patch(repository_id: str, repair_id: str, patch_id: str, body: ConfirmBody, ctx: RequestContext = Depends(get_context),
                db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> PatchDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return _patch_detail(service.apply(db, ctx, settings, repo, inv, service.get_patch(db, repo, inv, patch_id), body.confirm), settings)


@router.get("/repairs/{repair_id}/patches/{patch_id}/rollback")
def rollback_guidance(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return jsonable_encoder(service.rollback_guidance(db, settings, repo, service.get_patch(db, repo, inv, patch_id)))


# ------------------------------------------------------------------------------------------------ verification

@router.post("/repairs/{repair_id}/patches/{patch_id}/verify/preview")
def preview_verification(repository_id: str, repair_id: str, patch_id: str, body: VerifyBody, ctx: RequestContext = Depends(get_context),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    return jsonable_encoder(verify.preview(db, repo, inv, service.get_patch(db, repo, inv, patch_id), body.test_ids or None, body.scope))


@router.post("/repairs/{repair_id}/patches/{patch_id}/verifications", status_code=202)
def start_verification(repository_id: str, repair_id: str, patch_id: str, body: VerifyBody, background: BackgroundTasks,
                       ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> VerificationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    p = service.get_patch(db, repo, inv, patch_id)
    v = verify.start(db, ctx, settings, repo, inv, p, body.test_ids or None, body.coverage, body.scope, body.confirm)
    if v.execution_run_id and v.outcome == "PENDING":
        background.add_task(_vjob, v.execution_run_id, v.id)
    return _v_detail(db, settings, repo, v, evaluate=False)


@router.get("/repairs/{repair_id}/patches/{patch_id}/verifications")
def list_verifications(repository_id: str, repair_id: str, patch_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> list[VerificationDetail]:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    p = service.get_patch(db, repo, inv, patch_id)
    rows = list(db.scalars(select(RepairVerification).where(RepairVerification.patch_id == p.id).order_by(RepairVerification.created_at.desc(),
                                                                                                          RepairVerification.id.desc()).limit(50)))
    return [_v_detail(db, settings, repo, v) for v in rows]


@router.get("/repairs/{repair_id}/patches/{patch_id}/verifications/{verification_id}")
def get_verification(repository_id: str, repair_id: str, patch_id: str, verification_id: str, ctx: RequestContext = Depends(get_context),
                     db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> VerificationDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    inv = _inv(db, repo, repair_id)
    p = service.get_patch(db, repo, inv, patch_id)
    v = db.scalar(select(RepairVerification).where(RepairVerification.id == verification_id, RepairVerification.patch_id == p.id))
    if v is None:
        raise NotFoundError("Verification not found.")
    return _v_detail(db, settings, repo, v)
