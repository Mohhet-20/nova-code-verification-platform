from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, TooManyRequestsError
from app.models import IndexedFile, IndexSnapshot, Repository
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit, git
from app.services.indexer import IndexLimits
from app.services.intelligence.pipeline import PUBLISHED

logger = logging.getLogger("nova.repositories")


def limits_from(settings: Settings) -> IndexLimits:
    return IndexLimits(settings.index_max_files, settings.index_max_depth,
                       settings.index_max_file_bytes, settings.index_max_total_bytes)


def get_repository(db: Session, ctx: RequestContext, repository_id: str) -> Repository:
    repo = db.scalar(select(Repository).where(
        Repository.id == repository_id, Repository.workspace_id == ctx.workspace.id))
    if repo is None:
        raise NotFoundError("Repository not found.")
    from app.security.authz import Principal

    if not Principal(db, ctx).can_see_repository(repo.id):  # team-owned repositories are hidden from non-members (same answer as "missing")
        raise NotFoundError("Repository not found.")
    return repo


def register_repository(db: Session, ctx: RequestContext, settings: Settings, *, name: str,
                        root_path: str, description: str) -> Repository:
    root = resolve_repository_root(root_path, settings.registration_base_dirs)
    existing = db.scalar(select(Repository).where(
        Repository.workspace_id == ctx.workspace.id, Repository.root_path == str(root)))
    if existing is not None:
        raise ConflictError("This directory is already registered.", {"repository_id": existing.id})
    info = git.inspect_repository(root, settings.git_timeout_seconds, settings.git_max_output_bytes)
    repo = Repository(workspace_id=ctx.workspace.id, name=name, description=description,
                      root_path=str(root), is_git=info.is_git, git_branch=info.branch,
                      git_head=info.head, git_dirty_count=info.dirty_count)
    db.add(repo)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id,
                 action="repository.registered", entity_type="repository", entity_id=repo.id,
                 details={"name": name, "is_git": info.is_git})
    db.commit()
    return repo


def latest_snapshot(db: Session, repository_id: str, *, completed_only: bool = False
                    ) -> IndexSnapshot | None:
    q = select(IndexSnapshot).where(IndexSnapshot.repository_id == repository_id)
    if completed_only:
        q = q.where(IndexSnapshot.status.in_(PUBLISHED))
    return db.scalar(q.order_by(IndexSnapshot.started_at.desc(), IndexSnapshot.id.desc()).limit(1))


def begin_indexing(db: Session, ctx: RequestContext, settings: Settings,
                   repo: Repository, mode: str = "incremental") -> IndexSnapshot:
    running = db.scalar(select(IndexSnapshot).where(
        IndexSnapshot.repository_id == repo.id, IndexSnapshot.status == "running"))
    if running is not None:
        raise ConflictError("Indexing is already in progress for this repository.",
                            {"snapshot_id": running.id})
    # Admission: bound concurrent indexing runs process-wide (an unbounded fan-out of scans exhausts CPU/IO). Per-repo conflicts win above.
    active = db.scalar(select(func.count()).select_from(IndexSnapshot).where(IndexSnapshot.status == "running")) or 0
    if active >= settings.index_max_concurrent_runs:
        raise TooManyRequestsError("Too many indexing runs are in progress. Try again shortly.",
                                   {"max_concurrent_runs": settings.index_max_concurrent_runs})
    snapshot = IndexSnapshot(repository_id=repo.id, status="running", mode=mode,
                             limits=limits_from(settings).as_dict())
    db.add(snapshot)
    try:
        db.flush()
        audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id,
                     action="index.started", entity_type="snapshot", entity_id=snapshot.id,
                     details={"repository_id": repo.id, "mode": mode})
        db.commit()
    except IntegrityError:  # lost a race with a concurrent request: the partial unique index fired
        db.rollback()
        raise ConflictError("Indexing is already in progress for this repository.") from None
    return snapshot


def run_indexing(db: Session, settings: Settings, workspace_id: str, actor_user_id: str | None,
                 snapshot_id: str) -> None:
    """Execute the indexing run for ``snapshot_id`` (see ``intelligence.pipeline``)."""
    from app.services.intelligence.pipeline import run_index

    run_index(db, settings, workspace_id, actor_user_id, snapshot_id)


def file_count_for(db: Session, snapshot_id: str) -> int:
    return db.scalar(select(func.count()).select_from(IndexedFile)
                     .where(IndexedFile.snapshot_id == snapshot_id)) or 0
