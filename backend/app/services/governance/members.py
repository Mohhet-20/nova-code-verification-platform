"""Organization membership: members, org roles and per-user API tokens."""
from __future__ import annotations

import re
import secrets
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import Membership, TeamMember, User, UserToken
from app.security.auth import USER_TOKEN_PREFIX, RequestContext, hash_token
from app.security.authz import ORG_ROLES, Principal
from app.services.governance.common import emit

HANDLE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def _new_token() -> str:
    return USER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _issue(db: Session, ctx: RequestContext, user_id: str, label: str) -> str:
    plain = _new_token()
    db.add(UserToken(workspace_id=ctx.workspace.id, user_id=user_id, token_hash=hash_token(plain), label=label[:80], created_by=ctx.user.id))
    return plain


def _active_owner_count(db: Session, workspace_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Membership).where(
        Membership.workspace_id == workspace_id, Membership.role == "owner", Membership.status == "active")) or 0


def _check_role(role: str) -> None:
    if role not in ORG_ROLES:
        raise ValidationFailedError("Unknown organization role.", {"allowed": list(ORG_ROLES)})


def view(m: Membership, u: User) -> dict[str, Any]:
    return {"user_id": u.id, "handle": u.handle, "display_name": u.display_name, "role": m.role, "status": m.status,
            "joined_at": m.created_at, "removed_at": m.removed_at}


def list_members(db: Session, p: Principal, *, q: str | None, include_removed: bool, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    # Everyone in the organization can see the directory of active members' names (needed to pick reviewers); removed members only to admins/auditors.
    stmt = select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.workspace_id == p.workspace_id)
    if not (include_removed and (p.has_org("org.members.manage") or p.has_org("audit.view"))):
        stmt = stmt.where(Membership.status == "active")
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.handle).like(like), func.lower(User.display_name).like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(User.handle).offset(offset).limit(limit)).all()
    return [view(m, u) for m, u in rows], total


def create_member(db: Session, ctx: RequestContext, p: Principal, *, handle: str, display_name: str, role: str) -> tuple[dict[str, Any], str]:
    p.require("org.members.manage")
    handle = handle.strip().lower()
    if not HANDLE.match(handle):
        raise ValidationFailedError("Handle must be 3-40 characters: lowercase letters, digits, '.', '_' or '-'.")
    _check_role(role)
    user = db.scalar(select(User).where(User.handle == handle))
    if user is None:
        user = User(handle=handle, display_name=display_name.strip()[:120] or handle)
        db.add(user)
        db.flush()
        membership = None
    else:
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.workspace_id == ctx.workspace.id))
        if membership is None or membership.status == "active":
            raise ConflictError("That handle is not available.")  # no hint about other organizations
    if membership is None:
        membership = Membership(workspace_id=ctx.workspace.id, user_id=user.id, role=role, status="active")
        db.add(membership)
    else:
        membership.role, membership.status, membership.removed_at = role, "active", None
    plain = _issue(db, ctx, user.id, "initial")
    db.flush()
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="member.added", entity_type="user", entity_id=user.id, details={"handle": handle, "role": role})
    db.commit()
    return view(membership, user), plain


def _target(db: Session, workspace_id: str, user_id: str) -> tuple[Membership, User]:
    row = db.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user_id)).first()
    if row is None:
        raise NotFoundError("Member not found.")
    return row[0], row[1]


def change_role(db: Session, ctx: RequestContext, p: Principal, user_id: str, role: str) -> dict[str, Any]:
    p.require("org.members.manage")
    _check_role(role)
    m, u = _target(db, ctx.workspace.id, user_id)
    if m.status != "active":
        raise ConflictError("The member has been removed.")
    if m.role == "owner" and role != "owner" and _active_owner_count(db, ctx.workspace.id) <= 1:
        raise ConflictError("The last organization administrator cannot be demoted. Promote another administrator first.")
    old, m.role = m.role, role
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="member.role_changed", entity_type="user", entity_id=user_id, details={"from": old, "to": role})
    db.commit()
    return view(m, u)


def remove_member(db: Session, ctx: RequestContext, p: Principal, user_id: str) -> dict[str, Any]:
    p.require("org.members.manage")
    m, u = _target(db, ctx.workspace.id, user_id)
    if m.status != "active":
        raise ConflictError("The member has already been removed.")
    if m.role == "owner" and _active_owner_count(db, ctx.workspace.id) <= 1:
        raise ConflictError("The last organization administrator cannot be removed. Promote another administrator first.")
    now = utcnow()
    m.status, m.removed_at = "removed", now
    db.execute(update(UserToken).where(UserToken.user_id == user_id, UserToken.workspace_id == ctx.workspace.id, UserToken.revoked_at.is_(None)).values(revoked_at=now))
    teams = db.execute(update(TeamMember).where(TeamMember.user_id == user_id, TeamMember.status == "active").values(status="removed", removed_at=now)).rowcount
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="member.removed", entity_type="user", entity_id=user_id, details={"handle": u.handle, "team_memberships_ended": teams})
    db.commit()
    return view(m, u)


def rotate_token(db: Session, ctx: RequestContext, p: Principal, user_id: str) -> str:
    """Revoke every active token of the member and issue one new token (shown once). Admins or the member themselves."""
    if user_id != ctx.user.id:
        p.require("org.members.manage")
    m, _ = _target(db, ctx.workspace.id, user_id)
    if m.status != "active":
        raise ConflictError("The member has been removed.")
    db.execute(update(UserToken).where(UserToken.user_id == user_id, UserToken.workspace_id == ctx.workspace.id, UserToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    plain = _issue(db, ctx, user_id, "rotated")
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="member.token_rotated", entity_type="user", entity_id=user_id)
    db.commit()
    return plain
