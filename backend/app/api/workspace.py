from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import APP_VERSION, Settings, get_settings
from app.db.session import get_db
from app.models import AuditEvent, IndexSnapshot, Repository, Task
from app.schemas.system import AuditEventOut, Overview, WorkspaceContext
from app.security.auth import RequestContext, get_context
from app.services.intelligence.pipeline import PUBLISHED
from app.services.repositories import limits_from

router = APIRouter(tags=["workspace"])


@router.get("/workspace", response_model=WorkspaceContext)
def workspace_context(ctx: RequestContext = Depends(get_context),
                      settings: Settings = Depends(get_settings)) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=ctx.workspace.id, workspace_name=ctx.workspace.name,
        user_handle=ctx.user.handle, user_display_name=ctx.user.display_name, role=ctx.role,
        auth_mode="bearer-token" if settings.api_token else "local-trust",
        multi_user_isolation=False, ai_provider=settings.ai_provider,
        planner="deterministic-baseline", version=APP_VERSION, environment=settings.environment,
        limits=limits_from(settings).as_dict(),
        registration_restricted=bool(settings.registration_base_dirs),
    )


@router.get("/overview", response_model=Overview)
def overview(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Overview:
    ws = ctx.workspace.id
    repo_ids = select(Repository.id).where(Repository.workspace_id == ws)
    repos = db.scalar(select(func.count()).select_from(Repository)
                      .where(Repository.workspace_id == ws)) or 0
    # Latest completed snapshot per repository
    latest = (select(func.max(IndexSnapshot.started_at)).where(
        IndexSnapshot.repository_id.in_(repo_ids), IndexSnapshot.status.in_(PUBLISHED))
        .group_by(IndexSnapshot.repository_id))
    snaps = db.scalars(select(IndexSnapshot).where(
        IndexSnapshot.repository_id.in_(repo_ids), IndexSnapshot.status.in_(PUBLISHED),
        IndexSnapshot.started_at.in_(latest))).all()
    per_repo: dict[str, IndexSnapshot] = {}
    for s in snaps:
        cur = per_repo.get(s.repository_id)
        if cur is None or s.started_at > cur.started_at:
            per_repo[s.repository_id] = s
    status_rows = db.execute(select(Task.status, func.count()).where(Task.workspace_id == ws)
                             .group_by(Task.status)).all()
    events = db.scalars(select(AuditEvent).where(AuditEvent.workspace_id == ws)
                        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(8)).all()
    return Overview(
        repositories=repos, indexed_repositories=len(per_repo),
        indexed_files=sum(s.files_indexed for s in per_repo.values()),
        tasks_total=sum(c for _, c in status_rows),
        tasks_by_status={s: c for s, c in status_rows},
        recent_events=[AuditEventOut(id=e.id, action=e.action, entity_type=e.entity_type,
                                     entity_id=e.entity_id, details=e.details,
                                     created_at=e.created_at) for e in events],
    )
