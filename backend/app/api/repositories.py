from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db, get_session_factory
from app.models import IndexedFile, IndexSnapshot, Repository, Symbol
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.repositories import (
    FileOut,
    IndexRequest,
    RepositoryCreate,
    RepositoryDetail,
    RepositoryListItem,
    RepositorySummary,
    SnapshotOut,
)
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.intelligence.pipeline import PUBLISHED

router = APIRouter(prefix="/repositories", tags=["repositories"])


def _label(repo: Repository) -> str:
    return Path(repo.root_path).name or repo.name


def _detail(db: Session, repo: Repository) -> RepositoryDetail:
    snap = svc.latest_snapshot(db, repo.id)
    return RepositoryDetail(
        id=repo.id, name=repo.name, description=repo.description, root_path=repo.root_path,
        root_label=_label(repo), is_git=repo.is_git, git_branch=repo.git_branch,
        git_head=repo.git_head, git_dirty_count=repo.git_dirty_count,
        created_at=repo.created_at, updated_at=repo.updated_at,
        latest_snapshot=SnapshotOut.model_validate(snap) if snap else None)


@router.post("", response_model=RepositoryDetail, status_code=201)
def register(payload: RepositoryCreate, ctx: RequestContext = Depends(get_context),
             db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> RepositoryDetail:
    ctx.require_write()
    repo = svc.register_repository(db, ctx, settings, name=payload.name,
                                   root_path=payload.root_path, description=payload.description)
    return _detail(db, repo)


@router.get("", response_model=Page[RepositoryListItem])
def list_repositories(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                      ctx: RequestContext = Depends(get_context),
                      db: Session = Depends(get_db)) -> Page[RepositoryListItem]:
    where = Repository.workspace_id == ctx.workspace.id
    from app.security.authz import visible_repository_filter

    extra = visible_repository_filter(db, ctx)
    if extra is not None:
        where = where & extra
    total = db.scalar(select(func.count()).select_from(Repository).where(where)) or 0
    repos = db.scalars(select(Repository).where(where)
                       .order_by(Repository.created_at.desc(), Repository.id)
                       .offset((page - 1) * page_size).limit(page_size)).all()
    items = []
    for r in repos:
        snap = svc.latest_snapshot(db, r.id)
        items.append(RepositoryListItem(
            id=r.id, name=r.name, description=r.description, root_label=_label(r),
            is_git=r.is_git, git_branch=r.git_branch, created_at=r.created_at,
            latest_snapshot_status=snap.status if snap else None,
            latest_snapshot_at=snap.started_at if snap else None,
            files_indexed=snap.files_indexed if snap and snap.status in PUBLISHED else None))
    return make_page(items, total, page, page_size)


@router.get("/{repository_id}", response_model=RepositoryDetail)
def get_repository(repository_id: str, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db)) -> RepositoryDetail:
    return _detail(db, svc.get_repository(db, ctx, repository_id))


@tracked("indexing", "indexing", model=IndexSnapshot, id_arg=2, failed=("failed",))
def _index_job(workspace_id: str, user_id: str | None, snapshot_id: str) -> None:
    factory = get_session_factory()
    with factory() as session:
        svc.run_indexing(session, get_settings(), workspace_id, user_id, snapshot_id)


@router.post("/{repository_id}/index", response_model=SnapshotOut, status_code=202)
def start_indexing(repository_id: str, background: BackgroundTasks, body: IndexRequest | None = None,
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> SnapshotOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    snapshot = svc.begin_indexing(db, ctx, settings, repo, (body or IndexRequest()).mode)
    background.add_task(_index_job, ctx.workspace.id, ctx.user.id, snapshot.id)
    return SnapshotOut.model_validate(snapshot)


@router.get("/{repository_id}/snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(repository_id: str, snapshot_id: str,
                 ctx: RequestContext = Depends(get_context),
                 db: Session = Depends(get_db)) -> SnapshotOut:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = db.scalar(select(IndexSnapshot).where(IndexSnapshot.id == snapshot_id,
                                                 IndexSnapshot.repository_id == repo.id))
    if snap is None:
        raise NotFoundError("Snapshot not found.")
    return SnapshotOut.model_validate(snap)


@router.get("/{repository_id}/summary", response_model=RepositorySummary)
def summary(repository_id: str, ctx: RequestContext = Depends(get_context),
            db: Session = Depends(get_db)) -> RepositorySummary:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        return RepositorySummary(repository_id=repo.id, indexed=False, snapshot=None,
                                 top_languages=[], largest_files=[])
    langs = sorted(snap.language_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    largest = db.execute(select(IndexedFile.rel_path, IndexedFile.size_bytes)
                         .where(IndexedFile.snapshot_id == snap.id)
                         .order_by(IndexedFile.size_bytes.desc(), IndexedFile.rel_path)
                         .limit(5)).all()
    kinds = db.execute(select(Symbol.kind, func.count()).where(Symbol.snapshot_id == snap.id)
                       .group_by(Symbol.kind).order_by(func.count().desc(), Symbol.kind)).all()
    return RepositorySummary(repository_id=repo.id, indexed=True,
                             snapshot=SnapshotOut.model_validate(snap), top_languages=langs,
                             largest_files=[(p, s) for p, s in largest],
                             symbol_kinds=[(k, n) for k, n in kinds],
                             index_state="partial" if snap.status == "completed_with_warnings" else "ready")


@router.get("/{repository_id}/files", response_model=Page[FileOut])
def list_files(repository_id: str, page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200), q: str | None = Query(None, max_length=200),
               language: str | None = Query(None, max_length=40),
               analysis_status: str | None = Query(None, pattern="^(parsed|partial|failed|unavailable|unsupported)$"),
               change_status: str | None = Query(None, pattern="^(new|changed|unchanged)$"),
               ctx: RequestContext = Depends(get_context),
               db: Session = Depends(get_db)) -> Page[FileOut]:
    """File metadata only (path, language, size). File contents are never returned."""
    repo = svc.get_repository(db, ctx, repository_id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        return make_page([], 0, page, page_size)
    cond = [IndexedFile.snapshot_id == snap.id]
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond.append(IndexedFile.rel_path.ilike(f"%{escaped}%", escape="\\"))
    if language:
        cond.append(IndexedFile.language == language)
    if analysis_status:
        cond.append(IndexedFile.analysis_status == analysis_status)
    if change_status:
        cond.append(IndexedFile.change_status == change_status)
    total = db.scalar(select(func.count()).select_from(IndexedFile).where(*cond)) or 0
    rows = db.scalars(select(IndexedFile).where(*cond).order_by(IndexedFile.rel_path)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([FileOut.model_validate(r) for r in rows], total, page, page_size)
