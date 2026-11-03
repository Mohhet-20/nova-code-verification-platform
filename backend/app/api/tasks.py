from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Repository, Task
from app.schemas.common import Page, make_page
from app.schemas.tasks import PlanOut, PlanUpdate, TaskCreate, TaskDetail, TaskOut
from app.security.auth import RequestContext, get_context
from app.services import tasks as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _repo_name(db: Session, task: Task) -> str:
    return db.scalar(select(Repository.name).where(Repository.id == task.repository_id)) or ""


def _out(db: Session, task: Task) -> TaskOut:
    return TaskOut(id=task.id, repository_id=task.repository_id,
                   repository_name=_repo_name(db, task), title=task.title,
                   problem_statement=task.problem_statement, objective=task.objective,
                   scope=task.scope, acceptance_criteria=task.acceptance_criteria,
                   status=task.status, created_at=task.created_at, updated_at=task.updated_at)


def _detail(db: Session, task: Task) -> TaskDetail:
    plan = None
    if task.plan is not None:
        p = task.plan
        plan = PlanOut(generator=p.generator, generator_version=p.generator_version,
                       revision=p.revision, edited_by_user=p.edited_by_user,
                       snapshot_id=p.snapshot_id, updated_at=p.updated_at, content=p.content)
    return TaskDetail(**_out(db, task).model_dump(), plan=plan)


@router.post("", response_model=TaskDetail, status_code=201)
def create_task(payload: TaskCreate, ctx: RequestContext = Depends(get_context),
                db: Session = Depends(get_db)) -> TaskDetail:
    ctx.require_write()
    return _detail(db, svc.create_task(db, ctx, payload))


@router.get("", response_model=Page[TaskOut])
def list_tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
               repository_id: str | None = Query(None, max_length=32),
               ctx: RequestContext = Depends(get_context),
               db: Session = Depends(get_db)) -> Page[TaskOut]:
    cond = [Task.workspace_id == ctx.workspace.id]
    if repository_id:
        cond.append(Task.repository_id == repository_id)
    total = db.scalar(select(func.count()).select_from(Task).where(*cond)) or 0
    rows = db.scalars(select(Task).where(*cond).order_by(Task.created_at.desc(), Task.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([_out(db, t) for t in rows], total, page, page_size)


@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: str, ctx: RequestContext = Depends(get_context),
             db: Session = Depends(get_db)) -> TaskDetail:
    return _detail(db, svc.get_task(db, ctx, task_id))


@router.put("/{task_id}/plan", response_model=TaskDetail)
def edit_plan(task_id: str, payload: PlanUpdate, ctx: RequestContext = Depends(get_context),
              db: Session = Depends(get_db)) -> TaskDetail:
    ctx.require_write()
    task = svc.get_task(db, ctx, task_id)
    return _detail(db, svc.update_plan(db, ctx, task, payload.content))


@router.post("/{task_id}/plan/regenerate", response_model=TaskDetail)
def regenerate(task_id: str, ctx: RequestContext = Depends(get_context),
               db: Session = Depends(get_db)) -> TaskDetail:
    ctx.require_write()
    task = svc.get_task(db, ctx, task_id)
    return _detail(db, svc.regenerate_plan(db, ctx, task))
