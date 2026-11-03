"""Teams, team membership and repository ownership."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    Membership,
    Repository,
    RepositoryOwnership,
    ReviewQueueItem,
    Team,
    TeamMember,
    User,
    WorkflowInstance,
)
from app.security.auth import RequestContext
from app.security.authz import TEAM_ROLES, Principal
from app.services.governance.common import emit

OPEN_STATES = ("DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED")


def team_view(t: Team, members: int | None = None, my_role: str | None = None) -> dict[str, Any]:
    return {"id": t.id, "name": t.name, "description": t.description, "status": t.status, "created_at": t.created_at,
            "updated_at": t.updated_at, "archived_at": t.archived_at, "member_count": members, "my_role": my_role}


def get_team(db: Session, p: Principal, team_id: str, *, allow_archived: bool = True) -> Team:
    t = db.scalar(select(Team).where(Team.id == team_id, Team.workspace_id == p.workspace_id))
    if t is None or not p.can_see_team(team_id):
        raise NotFoundError("Team not found.")  # hidden and nonexistent teams are indistinguishable
    if t.status == "archived" and not allow_archived:
        raise ConflictError("The team is archived.")
    return t


def list_teams(db: Session, p: Principal, *, q: str | None, include_archived: bool, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Team).where(Team.workspace_id == p.workspace_id)
    if not p.org_wide:
        stmt = stmt.where(Team.id.in_(list(p.team_roles) or ["-"]))
    if not include_archived:
        stmt = stmt.where(Team.status == "active")
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Team.name).like(like), func.lower(Team.description).like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    teams = db.scalars(stmt.order_by(Team.name, Team.id).offset(offset).limit(limit)).all()
    counts = dict(db.execute(select(TeamMember.team_id, func.count()).where(
        TeamMember.team_id.in_([t.id for t in teams]), TeamMember.status == "active").group_by(TeamMember.team_id)).all()) if teams else {}
    return [team_view(t, counts.get(t.id, 0), p.team_roles.get(t.id)) for t in teams], total


def create_team(db: Session, ctx: RequestContext, p: Principal, *, name: str, description: str) -> Team:
    p.require("team.create")
    name = name.strip()
    if not name:
        raise ValidationFailedError("A team name is required.")
    t = Team(workspace_id=ctx.workspace.id, name=name, description=description.strip(), created_by=ctx.user.id)
    db.add(t)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A team with this name already exists.") from None
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.created", entity_type="team", entity_id=t.id, team_id=t.id, details={"name": name})
    db.commit()
    return t


def update_team(db: Session, ctx: RequestContext, p: Principal, team_id: str, *, name: str | None, description: str | None) -> Team:
    t = get_team(db, p, team_id, allow_archived=False)
    p.require("team.update", team_id)
    changes: dict[str, Any] = {}
    if name is not None and name.strip() != t.name:
        if not name.strip():
            raise ValidationFailedError("A team name is required.")
        changes["name"] = {"from": t.name, "to": name.strip()}
        t.name = name.strip()
    if description is not None and description.strip() != t.description:
        changes["description_changed"] = True
        t.description = description.strip()
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A team with this name already exists.") from None
    if changes:
        emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.updated", entity_type="team", entity_id=t.id, team_id=t.id, details=changes)
    db.commit()
    return t


def archive_team(db: Session, ctx: RequestContext, p: Principal, team_id: str) -> Team:
    t = get_team(db, p, team_id)
    p.require("team.archive", team_id)
    if t.status == "archived":
        raise ConflictError("The team is already archived.")
    owned = db.scalar(select(func.count()).select_from(RepositoryOwnership).where(RepositoryOwnership.team_id == team_id)) or 0
    live = db.scalar(select(func.count()).select_from(WorkflowInstance).where(WorkflowInstance.team_id == team_id, WorkflowInstance.state.in_(OPEN_STATES))) or 0
    if owned or live:
        raise ConflictError("Reassign the team's repositories and finish or cancel its open workflows before archiving it.",
                            {"owned_repositories": owned, "open_workflows": live})
    t.status, t.archived_at = "archived", utcnow()
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.archived", entity_type="team", entity_id=t.id, team_id=t.id, details={"name": t.name})
    db.commit()
    return t


# ---------------------------------------------------------------------------------------------- members

def member_view(m: TeamMember, u: User) -> dict[str, Any]:
    return {"user_id": u.id, "handle": u.handle, "display_name": u.display_name, "role": m.role, "status": m.status,
            "joined_at": m.created_at, "removed_at": m.removed_at, "added_by": m.added_by}


def list_members(db: Session, p: Principal, team_id: str, *, include_removed: bool, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    get_team(db, p, team_id)
    stmt = select(TeamMember, User).join(User, User.id == TeamMember.user_id).where(TeamMember.team_id == team_id)
    if not include_removed:
        stmt = stmt.where(TeamMember.status == "active")
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(User.handle).offset(offset).limit(limit)).all()
    return [member_view(m, u) for m, u in rows], total


def _active_admin_count(db: Session, team_id: str) -> int:
    return db.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id, TeamMember.role == "admin", TeamMember.status == "active")) or 0


def add_member(db: Session, ctx: RequestContext, p: Principal, team_id: str, *, user_id: str, role: str) -> dict[str, Any]:
    t = get_team(db, p, team_id, allow_archived=False)
    p.require("team.members.manage", team_id)
    if role not in TEAM_ROLES:
        raise ValidationFailedError("Unknown team role.", {"allowed": list(TEAM_ROLES)})
    # the user must be an ACTIVE member of THIS organization (blocks cross-tenant assignment by id)
    org = db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.workspace_id == ctx.workspace.id, Membership.status == "active"))
    user = db.get(User, user_id) if org else None
    if org is None or user is None:
        raise NotFoundError("User not found in this organization.")
    existing = db.scalar(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id))
    if existing is not None and existing.status == "active":
        raise ConflictError("The user is already a member of this team.")
    if existing is None:
        existing = TeamMember(team_id=team_id, user_id=user_id, role=role, status="active", added_by=ctx.user.id)
        db.add(existing)
    else:
        existing.role, existing.status, existing.removed_at, existing.added_by = role, "active", None, ctx.user.id
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("The user is already a member of this team.") from None
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.member_added", entity_type="team", entity_id=t.id, team_id=t.id, details={"user_id": user_id, "role": role})
    db.commit()
    return member_view(existing, user)


def change_member_role(db: Session, ctx: RequestContext, p: Principal, team_id: str, user_id: str, role: str) -> dict[str, Any]:
    get_team(db, p, team_id, allow_archived=False)
    p.require("team.members.manage", team_id)
    if role not in TEAM_ROLES:
        raise ValidationFailedError("Unknown team role.", {"allowed": list(TEAM_ROLES)})
    row = db.execute(select(TeamMember, User).join(User, User.id == TeamMember.user_id).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id, TeamMember.status == "active")).first()
    if row is None:
        raise NotFoundError("Team member not found.")
    m, u = row
    if m.role == "admin" and role != "admin" and _active_admin_count(db, team_id) <= 1:
        raise ConflictError("A team must keep at least one team administrator.")
    old, m.role = m.role, role
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.member_role_changed", entity_type="team", entity_id=team_id, team_id=team_id, details={"user_id": user_id, "from": old, "to": role})
    db.commit()
    return member_view(m, u)


def remove_member(db: Session, ctx: RequestContext, p: Principal, team_id: str, user_id: str) -> dict[str, Any]:
    get_team(db, p, team_id)
    p.require("team.members.manage", team_id)
    row = db.execute(select(TeamMember, User).join(User, User.id == TeamMember.user_id).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id, TeamMember.status == "active")).first()
    if row is None:
        raise NotFoundError("Team member not found.")
    m, u = row
    if m.role == "admin" and _active_admin_count(db, team_id) <= 1:
        raise ConflictError("A team must keep at least one team administrator.")
    m.status, m.removed_at = "removed", utcnow()
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="team.member_removed", entity_type="team", entity_id=team_id, team_id=team_id, details={"user_id": user_id})
    db.commit()
    return member_view(m, u)


# ---------------------------------------------------------------------------------------------- ownership

def ownership_view(db: Session, o: RepositoryOwnership, repo: Repository) -> dict[str, Any]:
    team = db.get(Team, o.team_id)
    acct = db.get(User, o.accountable_user_id) if o.accountable_user_id else None
    return {"repository_id": repo.id, "repository_name": repo.name, "team_id": o.team_id, "team_name": team.name if team else None,
            "accountable_user_id": o.accountable_user_id, "accountable_handle": acct.handle if acct else None, "assigned_at": o.assigned_at, "assigned_by": o.assigned_by}


def assign_repository(db: Session, ctx: RequestContext, p: Principal, team_id: str, repository_id: str, accountable_user_id: str | None) -> dict[str, Any]:
    t = get_team(db, p, team_id, allow_archived=False)
    repo = db.scalar(select(Repository).where(Repository.id == repository_id, Repository.workspace_id == ctx.workspace.id))
    if repo is None or not p.can_see_repository(repository_id):
        raise NotFoundError("Repository not found.")
    p.require("ownership.assign", team_id)
    cur = db.get(RepositoryOwnership, repository_id)
    if cur is not None and cur.team_id != team_id:
        # taking a repository away from another team is an organization-level decision
        if not p.has_org("ownership.assign"):
            p.deny("ownership.assign")
    if accountable_user_id is not None:
        ok = db.scalar(select(TeamMember.id).where(TeamMember.team_id == team_id, TeamMember.user_id == accountable_user_id, TeamMember.status == "active"))
        if ok is None:
            raise ValidationFailedError("The accountable owner must be an active member of the owning team.")
    before = cur.team_id if cur else None
    if cur is None:
        cur = RepositoryOwnership(repository_id=repository_id, workspace_id=ctx.workspace.id, team_id=team_id, accountable_user_id=accountable_user_id, assigned_by=ctx.user.id)
        db.add(cur)
    else:
        cur.team_id, cur.accountable_user_id, cur.assigned_by, cur.assigned_at = team_id, accountable_user_id, ctx.user.id, utcnow()
    db.flush()
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="ownership.assigned", entity_type="repository", entity_id=repository_id, team_id=team_id,
         details={"from_team_id": before, "to_team_id": team_id, "accountable_user_id": accountable_user_id})
    db.commit()
    _ = t
    return ownership_view(db, cur, repo)


def release_repository(db: Session, ctx: RequestContext, p: Principal, team_id: str, repository_id: str) -> None:
    get_team(db, p, team_id)
    p.require("ownership.assign", team_id)
    cur = db.get(RepositoryOwnership, repository_id)
    if cur is None or cur.team_id != team_id or cur.workspace_id != ctx.workspace.id:
        raise NotFoundError("Ownership record not found.")
    db.delete(cur)
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="ownership.released", entity_type="repository", entity_id=repository_id, team_id=team_id)
    db.commit()


def owned_repositories(db: Session, p: Principal, team_id: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    get_team(db, p, team_id)
    stmt = select(RepositoryOwnership, Repository).join(Repository, Repository.id == RepositoryOwnership.repository_id).where(RepositoryOwnership.team_id == team_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(Repository.name).offset(offset).limit(limit)).all()
    return [ownership_view(db, o, r) for o, r in rows], total


def team_work(db: Session, p: Principal, team_id: str) -> dict[str, Any]:
    """Counts of team-owned engineering work (aggregated; no per-item queries)."""
    get_team(db, p, team_id)
    states = dict(db.execute(select(WorkflowInstance.state, func.count()).where(WorkflowInstance.team_id == team_id).group_by(WorkflowInstance.state)).all())
    queue = dict(db.execute(select(ReviewQueueItem.status, func.count()).where(ReviewQueueItem.team_id == team_id).group_by(ReviewQueueItem.status)).all())
    unassigned = db.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.team_id == team_id, ReviewQueueItem.status == "open", ReviewQueueItem.assignee_user_id.is_(None))) or 0
    repos = db.scalar(select(func.count()).select_from(RepositoryOwnership).where(RepositoryOwnership.team_id == team_id)) or 0
    return {"team_id": team_id, "owned_repositories": repos, "workflows_by_state": states, "queue_by_status": queue, "queue_unassigned": unassigned,
            "outstanding_approvals": states.get("IN_REVIEW", 0)}
