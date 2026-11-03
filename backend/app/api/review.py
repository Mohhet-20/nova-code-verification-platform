"""Code review: change acquisition, risk, impact, findings and test recommendations."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.base import utcnow
from app.db.session import get_db, get_session_factory
from app.models import CodeReview, Repository, ReviewChange, ReviewChangedSymbol, ReviewFinding
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.review import (
    ChangedSymbolOut,
    ChangeOut,
    FindingOut,
    FindingPatch,
    ReviewCreate,
    ReviewDetail,
    ReviewOut,
)
from app.security.auth import RequestContext, get_context
from app.services import audit
from app.services import repositories as svc
from app.services.analysis.redaction import scrub
from app.services.review import gitops, pipeline

router = APIRouter(prefix="/repositories/{repository_id}", tags=["review"])
SEV_ORDER = case({"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}, value=ReviewFinding.severity, else_=5)
CONF_ORDER = case({"high": 0, "medium": 1, "low": 2}, value=ReviewFinding.confidence, else_=3)


def _like(text: str) -> str:
    esc = "\\"
    return "%" + text.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_") + "%"


def _review(db: Session, repo: Repository, review_id: str) -> CodeReview:
    r = db.scalar(select(CodeReview).where(CodeReview.id == review_id, CodeReview.repository_id == repo.id))
    if r is None:
        raise NotFoundError("Review not found.")
    return r


@tracked("code_review", "code_review")
def _job(review_id: str) -> None:
    with get_session_factory()() as session:
        pipeline.run_review(session, get_settings(), review_id)


@router.post("/reviews/preview")
def preview_review(repository_id: str, body: ReviewCreate, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """List the files a review would analyse. Reads git only; stores and executes nothing."""
    repo = svc.get_repository(db, ctx, repository_id)
    paths = gitops.validate_paths(body.paths)
    return pipeline.preview(db, settings, repo, body.mode, body.base, body.head, paths)


@router.post("/reviews", status_code=202)
def create_review(repository_id: str, body: ReviewCreate, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                  db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ReviewOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    paths = gitops.validate_paths(body.paths)
    review = pipeline.begin(db, ctx, settings, repo, body.mode, body.base, body.head, paths)
    background.add_task(_job, review.id)
    return ReviewOut.model_validate(review)


@router.get("/reviews")
def list_reviews(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                 status: list[Literal["pending", "running", "completed", "completed_with_warnings", "failed"]] | None = Query(None),
                 mode: Literal["revisions", "staged", "unstaged", "working_tree"] | None = None,
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[ReviewOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    q = select(CodeReview).where(CodeReview.repository_id == repo.id)
    if status:
        q = q.where(CodeReview.status.in_(status))
    if mode:
        q = q.where(CodeReview.mode == mode)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(CodeReview.created_at.desc(), CodeReview.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([ReviewOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/reviews/{review_id}")
def get_review(repository_id: str, review_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> ReviewDetail:
    repo = svc.get_repository(db, ctx, repository_id)
    return ReviewDetail.model_validate(_review(db, repo, review_id))


@router.get("/reviews/{review_id}/summary")
def get_summary(repository_id: str, review_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    return jsonable_encoder({"review": ReviewOut.model_validate(r), "summary": r.summary, "stages": r.stages, "ready": r.status not in ("pending", "running")})


@router.get("/reviews/{review_id}/changes")
def list_changes(repository_id: str, review_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                 change_type: list[Literal["added", "modified", "deleted", "renamed", "unsupported"]] | None = Query(None),
                 q: str | None = Query(None, max_length=200), ctx: RequestContext = Depends(get_context),
                 db: Session = Depends(get_db)) -> Page[ChangeOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    stmt = select(ReviewChange).where(ReviewChange.review_id == r.id)
    if change_type:
        stmt = stmt.where(ReviewChange.change_type.in_(change_type))
    if q:
        stmt = stmt.where(ReviewChange.rel_path.ilike(_like(q), escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(ReviewChange.rel_path).offset((page - 1) * page_size).limit(page_size)).all()
    syms: dict[str, list[ChangedSymbolOut]] = {}
    if rows:
        for s in db.scalars(select(ReviewChangedSymbol).where(ReviewChangedSymbol.change_id.in_([x.id for x in rows]))
                            .order_by(ReviewChangedSymbol.start_line, ReviewChangedSymbol.qualified_name)):
            syms.setdefault(s.change_id, []).append(ChangedSymbolOut.model_validate(s))
    items = []
    for row in rows:
        out = ChangeOut.model_validate(row)
        out.symbols = syms.get(row.id, [])
        items.append(out)
    return make_page(items, total, page, page_size)


@router.get("/reviews/{review_id}/findings")
def list_findings(repository_id: str, review_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  severity: list[Literal["critical", "high", "medium", "low", "info"]] | None = Query(None),
                  category: list[Literal["correctness_defect", "security", "reliability", "maintainability", "performance", "testing_gap",
                                         "informational"]] | None = Query(None),
                  confidence: list[Literal["high", "medium", "low"]] | None = Query(None),
                  status: list[Literal["open", "accepted", "dismissed", "follow_up"]] | None = Query(None),
                  path: str | None = Query(None, max_length=300), actionable: bool | None = None,
                  sort: Literal["severity", "path", "confidence"] = Query("severity"),
                  ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[FindingOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    stmt = select(ReviewFinding).where(ReviewFinding.review_id == r.id)
    for col, vals in ((ReviewFinding.severity, severity), (ReviewFinding.category, category), (ReviewFinding.confidence, confidence),
                      (ReviewFinding.status, status)):
        if vals:
            stmt = stmt.where(col.in_(vals))
    if path:
        stmt = stmt.where(ReviewFinding.rel_path.ilike(_like(path), escape="\\"))
    if actionable is not None:
        stmt = stmt.where(ReviewFinding.actionable == actionable)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {"severity": (SEV_ORDER, CONF_ORDER, ReviewFinding.rel_path, ReviewFinding.start_line, ReviewFinding.id),
             "path": (ReviewFinding.rel_path, ReviewFinding.start_line, SEV_ORDER, ReviewFinding.id),
             "confidence": (CONF_ORDER, SEV_ORDER, ReviewFinding.rel_path, ReviewFinding.id)}[sort]
    rows = db.scalars(stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([FindingOut.model_validate(x) for x in rows], total, page, page_size)


@router.patch("/reviews/{review_id}/findings/{finding_id}")
def patch_finding(repository_id: str, review_id: str, finding_id: str, body: FindingPatch, ctx: RequestContext = Depends(get_context),
                  db: Session = Depends(get_db)) -> FindingOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    f = db.scalar(select(ReviewFinding).where(ReviewFinding.id == finding_id, ReviewFinding.review_id == r.id))
    if f is None:
        raise NotFoundError("Finding not found.")
    f.status = body.status
    f.rationale = scrub(body.rationale.strip(), 500) if body.rationale and body.rationale.strip() else None
    f.status_changed_by, f.status_changed_at = ctx.user.id, utcnow()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="review.finding_status", entity_type="review_finding",
                 entity_id=f.id, details={"repository_id": repo.id, "review_id": r.id, "status": body.status})
    db.commit()
    return FindingOut.model_validate(f)


def _ready(r: CodeReview) -> bool:
    return r.status not in ("pending", "running")


@router.get("/reviews/{review_id}/risk")
def get_risk(repository_id: str, review_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    if not _ready(r) or not r.risk:
        return {"review_id": r.id, "status": r.status, "ready": _ready(r), "state": "pending" if not _ready(r) else "unknown", "score": None,
                "category": "unknown", "factors": [], "reason": "The review has not produced a risk assessment." if _ready(r) else "The review is still running."}
    return {"review_id": r.id, "status": r.status, "ready": True, **r.risk}


@router.get("/reviews/{review_id}/impact")
def get_impact(repository_id: str, review_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    return {"review_id": r.id, "status": r.status, "ready": _ready(r), "index_stale": r.index_stale, "index_notes": r.index_notes, **(r.impact or {})}


@router.get("/reviews/{review_id}/test-recommendations")
def get_recommendations(repository_id: str, review_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                        priority: Literal["high", "medium", "low"] | None = None, ctx: RequestContext = Depends(get_context),
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = svc.get_repository(db, ctx, repository_id)
    r = _review(db, repo, review_id)
    items = [x for x in (r.recommendations or []) if priority is None or x["priority"] == priority]
    start = (page - 1) * page_size
    pg = make_page(items[start:start + page_size], len(items), page, page_size)
    return {**pg.model_dump(), "review_id": r.id, "ready": _ready(r),
            "note": "Recommendations are suggestions derived from the change. No test was generated or executed."}
