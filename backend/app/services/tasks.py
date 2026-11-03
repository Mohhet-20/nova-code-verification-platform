from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import IndexedFile, Repository, Task, TaskPlan
from app.schemas.tasks import PlanContent, TaskCreate
from app.security.auth import RequestContext
from app.services import audit
from app.services.planner import InventoryFile, PlannerInput
from app.services.providers import get_plan_provider
from app.services.repositories import get_repository, latest_snapshot


def get_task(db: Session, ctx: RequestContext, task_id: str) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.workspace_id == ctx.workspace.id))
    if task is None:
        raise NotFoundError("Task not found.")
    return task


def _generate(db: Session, task: Task, repo: Repository) -> tuple[PlanContent, str | None, str, str]:
    snap = latest_snapshot(db, repo.id, completed_only=True)
    files: list[InventoryFile] = []
    if snap is not None:
        rows = db.execute(select(IndexedFile.rel_path, IndexedFile.language)
                          .where(IndexedFile.snapshot_id == snap.id)).all()
        files = [InventoryFile(r[0], r[1]) for r in rows]
    provider = get_plan_provider()
    content = provider.generate(PlannerInput(
        title=task.title, problem_statement=task.problem_statement, objective=task.objective,
        scope=task.scope, acceptance_criteria=task.acceptance_criteria, files=files,
        indexed=snap is not None))
    return content, snap.id if snap else None, provider.name, provider.version


def create_task(db: Session, ctx: RequestContext, payload: TaskCreate) -> Task:
    repo = get_repository(db, ctx, payload.repository_id)
    task = Task(workspace_id=ctx.workspace.id, repository_id=repo.id, created_by=ctx.user.id,
                title=payload.title, problem_statement=payload.problem_statement,
                objective=payload.objective, scope=payload.scope,
                acceptance_criteria=payload.acceptance_criteria, status="planned")
    db.add(task)
    db.flush()
    content, snap_id, name, version = _generate(db, task, repo)
    task.plan = TaskPlan(generator=name, generator_version=version, snapshot_id=snap_id,
                         content=content.model_dump(mode="json"))
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id,
                 action="task.created", entity_type="task", entity_id=task.id,
                 details={"repository_id": repo.id, "title": task.title})
    db.commit()
    return task


def regenerate_plan(db: Session, ctx: RequestContext, task: Task) -> Task:
    repo = get_repository(db, ctx, task.repository_id)
    content, snap_id, name, version = _generate(db, task, repo)
    plan = task.plan
    if plan is None:
        plan = task.plan = TaskPlan(generator=name, generator_version=version, content={})
    plan.generator, plan.generator_version, plan.snapshot_id = name, version, snap_id
    plan.content = content.model_dump(mode="json")
    plan.edited_by_user = False
    plan.revision = (plan.revision or 0) + 1
    task.status = "planned"
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id,
                 action="plan.regenerated", entity_type="task", entity_id=task.id,
                 details={"revision": plan.revision})
    db.commit()
    return task


def update_plan(db: Session, ctx: RequestContext, task: Task, content: PlanContent) -> Task:
    plan = task.plan
    if plan is None:
        raise NotFoundError("Task has no plan.")
    plan.content = content.model_dump(mode="json")
    plan.edited_by_user = True
    plan.revision = (plan.revision or 0) + 1
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id,
                 action="plan.edited", entity_type="task", entity_id=task.id,
                 details={"revision": plan.revision})
    db.commit()
    return task
