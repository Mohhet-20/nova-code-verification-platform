"""Persistent shared review queue: listing with scope enforcement, assignment, claim/release and history."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    Membership,
    Repository,
    RepositoryOwnership,
    ReviewQueueHistory,
    ReviewQueueItem,
    TeamMember,
    User,
    WorkflowInstance,
)
from app.security.auth import RequestContext
from app.security.authz import Principal
from app.services.governance import resources as res_mod
from app.services.governance.common import emit

PRIORITIES = ("low", "normal", "high", "urgent")
_RANK = case({"urgent": 0, "high": 1, "normal": 2, "low": 3}, value=ReviewQueueItem.priority, else_=4)


def _hist(db: Session, item: ReviewQueueItem, action: str, actor: str | None, frm: str | None = None, to: str | None = None, note: str = "") -> None:
    db.add(ReviewQueueHistory(item_id=item.id, action=action, actor_user_id=actor, from_user_id=frm, to_user_id=to, note=note[:500]))


def ensure_item(db: Session, inst: WorkflowInstance, reason: str, actor: str | None) -> ReviewQueueItem:
    """Create (first submission) or reopen (resubmission) the queue item that mirrors a workflow instance."""
    item = db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.instance_id == inst.id))
    if item is None:
        item = ReviewQueueItem(workspace_id=inst.workspace_id, instance_id=inst.id, resource_type=inst.resource_type, resource_id=inst.resource_id, repository_id=inst.repository_id,
                               team_id=inst.team_id, title=inst.title, reason=reason[:500], priority=inst.priority, risk_level=inst.risk_level, created_by=actor)
        db.add(item)
        db.flush()
        _hist(db, item, "created", actor, note=reason)
    else:
        item.status, item.closed_at, item.reason, item.assignee_user_id = "open", None, reason[:500], None
        _hist(db, item, "reopened", actor, note=reason)
    return item


def close_item(db: Session, inst: WorkflowInstance, status: str, actor: str | None, note: str) -> None:
    item = db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.instance_id == inst.id))
    if item is not None and item.status == "open":
        item.status, item.closed_at = status, utcnow()
        _hist(db, item, "closed" if status == "done" else "cancelled", actor, note=note)


def _scope(p: Principal):  # type: ignore[no-untyped-def]
    """Rows the caller may see. Default deny: org-wide roles see all; others see their teams' items and items assigned to them."""
    if p.org_wide:
        return ReviewQueueItem.workspace_id == p.workspace_id
    teams = list(p.team_roles) or ["-"]
    owned_by_mine = select(RepositoryOwnership.repository_id).where(RepositoryOwnership.team_id.in_(teams))
    return (ReviewQueueItem.workspace_id == p.workspace_id) & (
        ReviewQueueItem.team_id.in_(teams) | (ReviewQueueItem.assignee_user_id == p.user_id) | ReviewQueueItem.repository_id.in_(owned_by_mine))


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def view(db: Session, it: ReviewQueueItem, res: res_mod.Resource | None, names: dict[str, str], inst: WorkflowInstance | None) -> dict[str, Any]:
    age = (utcnow() - _aware(it.created_at)).total_seconds()
    outstanding: list[str] = []
    if it.status == "open":
        outstanding.append("Claim or be assigned to this item." if not it.assignee_user_id else "Review and record a decision.")
        if inst is not None and inst.state != "IN_REVIEW":
            outstanding = [f"Workflow is {inst.state}."]
    return {"id": it.id, "instance_id": it.instance_id, "resource_type": it.resource_type, "resource_id": it.resource_id,
            "resource_available": res is not None, "resource_title": it.title if res is not None else "(resource unavailable)", "repository_id": it.repository_id,
            "team_id": it.team_id, "reason": it.reason, "priority": it.priority, "risk_level": it.risk_level, "status": it.status,
            "assignee_user_id": it.assignee_user_id, "assignee": names.get(it.assignee_user_id or ""), "created_at": it.created_at, "closed_at": it.closed_at,
            "age_seconds": int(age), "workflow_state": inst.state if inst else None, "outstanding_actions": outstanding,
            "overdue": None}  # no SLA scheduler exists, so "overdue" is deliberately never asserted


def list_items(db: Session, p: Principal, *, status: str | None, team_id: str | None, repository_id: str | None, priority: str | None, risk_level: str | None,
               assignee: str | None, unassigned: bool, min_age_hours: float | None, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    stmt = select(ReviewQueueItem).where(_scope(p))
    if status:
        stmt = stmt.where(ReviewQueueItem.status == status)
    if team_id:
        stmt = stmt.where(ReviewQueueItem.team_id == team_id)
    if repository_id:
        stmt = stmt.where(ReviewQueueItem.repository_id == repository_id)
    if priority:
        stmt = stmt.where(ReviewQueueItem.priority == priority)
    if risk_level:
        stmt = stmt.where(ReviewQueueItem.risk_level == risk_level)
    if assignee:
        stmt = stmt.where(ReviewQueueItem.assignee_user_id == (p.user_id if assignee == "me" else assignee))
    if unassigned:
        stmt = stmt.where(ReviewQueueItem.assignee_user_id.is_(None))
    if min_age_hours is not None:
        from datetime import timedelta

        stmt = stmt.where(ReviewQueueItem.created_at <= utcnow() - timedelta(hours=min_age_hours))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # stable order: priority rank, oldest first, id as the final tiebreaker
    items = list(db.scalars(stmt.order_by(_RANK, ReviewQueueItem.created_at, ReviewQueueItem.id).offset(offset).limit(limit)).all())
    uids = {i.assignee_user_id for i in items if i.assignee_user_id}
    names = dict(db.execute(select(User.id, User.handle).where(User.id.in_(uids))).all()) if uids else {}
    insts = {i.id: i for i in db.scalars(select(WorkflowInstance).where(WorkflowInstance.id.in_([i.instance_id for i in items if i.instance_id]))).all()} if items else {}
    resolved = res_mod.resolve_many(db, p.workspace_id, {(i.resource_type, i.resource_id) for i in items})
    return [view(db, i, resolved.get((i.resource_type, i.resource_id)), names, insts.get(i.instance_id or "")) for i in items], total


def get_item(db: Session, p: Principal, item_id: str) -> ReviewQueueItem:
    it = db.scalar(select(ReviewQueueItem).where(ReviewQueueItem.id == item_id, _scope(p)))
    if it is None:
        raise NotFoundError("Queue item not found.")
    return it


def history(db: Session, p: Principal, item_id: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    get_item(db, p, item_id)
    stmt = select(ReviewQueueHistory).where(ReviewQueueHistory.item_id == item_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(ReviewQueueHistory.created_at, ReviewQueueHistory.id).offset(offset).limit(limit)).all()
    return [{"id": h.id, "action": h.action, "actor_user_id": h.actor_user_id, "from_user_id": h.from_user_id, "to_user_id": h.to_user_id, "note": h.note, "at": h.created_at} for h in rows], total


def _eligible_assignee(db: Session, workspace_id: str, item: ReviewQueueItem, user_id: str) -> None:
    m = db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.workspace_id == workspace_id, Membership.status == "active"))
    if m is None:
        raise ValidationFailedError("The assignee is not an active member of this organization.")
    if item.team_id and m.role not in ("owner", "maintainer", "auditor"):
        in_team = db.scalar(select(TeamMember.id).where(TeamMember.team_id == item.team_id, TeamMember.user_id == user_id, TeamMember.status == "active"))
        if in_team is None:
            raise ValidationFailedError("The assignee is not a member of the item's team.")
    if m.role in ("auditor", "viewer"):
        raise ValidationFailedError("This organization role cannot review work.")
    inst = db.get(WorkflowInstance, item.instance_id) if item.instance_id else None
    if inst is not None and user_id in {inst.requested_by, inst.resource_author}:
        raise ValidationFailedError("The requester or author cannot be assigned to review their own work.")


def _can_assign(p: Principal, item: ReviewQueueItem) -> bool:
    return p.can("queue.assign", item.team_id)


def assign(db: Session, ctx: RequestContext, p: Principal, item_id: str, user_id: str | None, note: str) -> ReviewQueueItem:
    it = get_item(db, p, item_id)
    if not _can_assign(p, it):
        p.deny("queue.assign")
    if it.status != "open":
        raise ConflictError("Only open queue items can be assigned.")
    old = it.assignee_user_id
    if user_id is not None:
        _eligible_assignee(db, ctx.workspace.id, it, user_id)
    if old == user_id:
        return it
    it.assignee_user_id = user_id
    _hist(db, it, "assigned" if old is None else ("released" if user_id is None else "reassigned"), ctx.user.id, old, user_id, note)
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="queue.assigned", entity_type="queue_item", entity_id=it.id, team_id=it.team_id, details={"from": old, "to": user_id})
    db.commit()
    return it


def claim(db: Session, ctx: RequestContext, p: Principal, item_id: str) -> ReviewQueueItem:
    it = get_item(db, p, item_id)
    if not p.can("queue.claim", it.team_id):
        p.deny("queue.claim")
    if it.status != "open":
        raise ConflictError("Only open queue items can be claimed.")
    if it.assignee_user_id and it.assignee_user_id != ctx.user.id:
        raise ConflictError("The item is already assigned to someone else.")
    try:
        _eligible_assignee(db, ctx.workspace.id, it, ctx.user.id)
    except ValidationFailedError as e:
        raise ForbiddenError(str(e.message)) from None
    if it.assignee_user_id == ctx.user.id:
        return it
    it.assignee_user_id = ctx.user.id
    _hist(db, it, "claimed", ctx.user.id, None, ctx.user.id)
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="queue.claimed", entity_type="queue_item", entity_id=it.id, team_id=it.team_id)
    db.commit()
    return it


def release(db: Session, ctx: RequestContext, p: Principal, item_id: str) -> ReviewQueueItem:
    it = get_item(db, p, item_id)
    if it.assignee_user_id is None:
        raise ConflictError("The item is not assigned.")
    if it.assignee_user_id != ctx.user.id and not _can_assign(p, it):
        p.deny("queue.assign")
    old, it.assignee_user_id = it.assignee_user_id, None
    _hist(db, it, "released", ctx.user.id, old, None)
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="queue.released", entity_type="queue_item", entity_id=it.id, team_id=it.team_id)
    db.commit()
    return it


def enqueue(db: Session, ctx: RequestContext, p: Principal, *, resource_type: str, resource_id: str, reason: str, priority: str) -> ReviewQueueItem:
    """Manually queue an existing resource for review (without starting a workflow)."""
    if priority not in PRIORITIES:
        raise ValidationFailedError("Unknown priority.", {"allowed": list(PRIORITIES)})
    res = res_mod.resolve(db, ctx.workspace.id, resource_type, resource_id)
    if res is None or not p.can_see_repository(res.repository_id):
        raise NotFoundError("Resource not found.")
    owner = db.scalar(select(RepositoryOwnership.team_id).where(RepositoryOwnership.repository_id == res.repository_id))
    if not p.can("queue.enqueue", owner):
        p.deny("queue.enqueue")
    dup = db.scalar(select(ReviewQueueItem.id).where(ReviewQueueItem.workspace_id == ctx.workspace.id, ReviewQueueItem.resource_type == resource_type,
                                                      ReviewQueueItem.resource_id == resource_id, ReviewQueueItem.status == "open", ReviewQueueItem.instance_id.is_(None)))
    if dup:
        raise ConflictError("This resource is already waiting in the review queue.", {"item_id": dup})
    it = ReviewQueueItem(workspace_id=ctx.workspace.id, resource_type=resource_type, resource_id=resource_id, repository_id=res.repository_id, team_id=owner, title=res.title,
                         reason=(reason.strip() or "Queued for review")[:500], priority=priority, risk_level=res.risk_level, created_by=ctx.user.id)
    db.add(it)
    db.flush()
    _hist(db, it, "created", ctx.user.id, note=it.reason)
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="queue.enqueued", entity_type="queue_item", entity_id=it.id, team_id=owner, details={"resource_type": resource_type, "resource_id": resource_id})
    db.commit()
    return it


def counts(db: Session, p: Principal) -> dict[str, Any]:
    rows = dict(db.execute(select(ReviewQueueItem.status, func.count()).where(_scope(p)).group_by(ReviewQueueItem.status)).all())
    mine = db.scalar(select(func.count()).select_from(ReviewQueueItem).where(_scope(p), ReviewQueueItem.status == "open", ReviewQueueItem.assignee_user_id == p.user_id)) or 0
    unassigned = db.scalar(select(func.count()).select_from(ReviewQueueItem).where(_scope(p), ReviewQueueItem.status == "open", ReviewQueueItem.assignee_user_id.is_(None))) or 0
    _ = or_, Repository
    return {"by_status": rows, "assigned_to_me": mine, "unassigned": unassigned}
