"""Centralised, deny-by-default authorization for governance features.

Two layers, both evaluated on the server for every protected operation:

* **Organization role** (``Membership.role``) grants permissions from ``ORG_PERMISSIONS``.
* **Team role** (``TeamMember.role``) grants permissions only inside that team (``TEAM_PERMISSIONS``).

Org-wide *visibility* belongs to ``ORG_WIDE_VIEW`` roles; everybody else sees only resources of teams they belong to
(or resources they personally requested/decide/are assigned). Anything not listed here is denied.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, NotFoundError
from app.db.base import utcnow
from app.models import AuditEvent, RepositoryOwnership, Team, TeamMember
from app.security.auth import RequestContext

log = logging.getLogger("nova.authz")

ORG_ROLES = ("owner", "maintainer", "engineer", "reviewer", "auditor", "viewer")
TEAM_ROLES = ("admin", "member", "reviewer")
ALL = "*"

_VIEW = {"policy.view", "definition.view"}
ORG_PERMISSIONS: dict[str, set[str]] = {
    # Organization administrator
    "owner": {ALL},
    # Engineering lead: runs reviews/assignments, may decide and complete, may NOT change membership or blocking policy
    "maintainer": _VIEW | {"team.view_all", "queue.view_all", "queue.assign", "queue.claim", "queue.enqueue", "workflow.view_all", "workflow.create",
                           "workflow.submit", "workflow.decide", "workflow.cancel", "workflow.complete", "policy.manage_advisory", "audit.view_team", "team.create", "integration.view", "integration.link_issue"},
    "engineer": _VIEW | {"workflow.create", "workflow.submit", "workflow.cancel_own", "queue.enqueue"},
    "reviewer": _VIEW | {"workflow.decide", "queue.claim"},
    # Auditor: read-only everywhere, including the governance audit trail
    "auditor": _VIEW | {"team.view_all", "queue.view_all", "workflow.view_all", "audit.view", "integration.view"},
    "viewer": _VIEW,
}
ORG_WIDE_VIEW = {"owner", "maintainer", "auditor"}

TEAM_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"team.update", "team.members.manage", "ownership.assign", "queue.assign", "queue.claim", "workflow.create", "workflow.submit",
              "workflow.cancel_own", "queue.enqueue", "audit.view_team"},
    "member": {"workflow.create", "workflow.submit", "workflow.cancel_own", "queue.enqueue"},
    "reviewer": {"workflow.decide", "queue.claim"},
}



class Principal:
    """The acting user with their (cached) team memberships. Built per request from server-side data only."""

    def __init__(self, db: Session, ctx: RequestContext) -> None:
        self.db, self.ctx = db, ctx
        self.user_id, self.workspace_id, self.role = ctx.user.id, ctx.workspace.id, ctx.role
        rows = db.execute(select(TeamMember.team_id, TeamMember.role).join(Team, Team.id == TeamMember.team_id).where(
            TeamMember.user_id == self.user_id, TeamMember.status == "active", Team.workspace_id == self.workspace_id,
            Team.status == "active")).all()
        self.team_roles: dict[str, str] = {t: r for t, r in rows}

    # ---- queries ---------------------------------------------------------------------------------------------
    @property
    def org_wide(self) -> bool:
        return self.role in ORG_WIDE_VIEW

    def has_org(self, perm: str) -> bool:
        grants = ORG_PERMISSIONS.get(self.role, set())
        return ALL in grants or perm in grants

    def has_team(self, perm: str, team_id: str | None) -> bool:
        if team_id is None:
            return False
        return perm in TEAM_PERMISSIONS.get(self.team_roles.get(team_id, ""), set())

    def can(self, perm: str, team_id: str | None = None) -> bool:
        return self.has_org(perm) or self.has_team(perm, team_id)

    def can_see_team(self, team_id: str | None) -> bool:
        return team_id is not None and (self.org_wide or team_id in self.team_roles)

    def can_see_repository(self, repository_id: str) -> bool:
        owner = self.db.scalar(select(RepositoryOwnership.team_id).where(
            RepositoryOwnership.repository_id == repository_id, RepositoryOwnership.workspace_id == self.workspace_id))
        return owner is None or self.can_see_team(owner)  # unowned (legacy) repositories stay workspace-visible

    # ---- enforcement -----------------------------------------------------------------------------------------
    def require(self, perm: str, team_id: str | None = None) -> None:
        if not self.can(perm, team_id):
            self.deny(perm)

    def deny(self, perm: str) -> None:
        _record_denial(self.workspace_id, self.user_id, perm)
        raise ForbiddenError("You do not have permission to perform this action.")

    def require_visible_team(self, team_id: str | None) -> None:
        if not self.can_see_team(team_id):
            raise NotFoundError("Team not found.")  # never reveals that a hidden team exists


def principal(db: Session, ctx: RequestContext) -> Principal:
    return Principal(db, ctx)


def visible_repository_filter(db: Session, ctx: RequestContext):  # type: ignore[no-untyped-def]
    """SQLAlchemy condition limiting Repository rows to those the caller may see (None = no extra restriction)."""
    from app.models import Repository

    p = Principal(db, ctx)
    if p.org_wide:
        return None
    owned = select(RepositoryOwnership.repository_id).where(RepositoryOwnership.workspace_id == ctx.workspace.id)
    mine = select(RepositoryOwnership.repository_id).where(RepositoryOwnership.workspace_id == ctx.workspace.id,
                                                           RepositoryOwnership.team_id.in_(list(p.team_roles) or ["-"]))
    return (Repository.id.not_in(owned)) | (Repository.id.in_(mine))


def _record_denial(workspace_id: str, user_id: str, perm: str) -> None:
    """Best-effort audit of a denied attempt in its own transaction, de-duplicated per user+permission for 60 s."""
    try:
        from app.db.session import get_session_factory

        with get_session_factory()() as s:
            since = utcnow() - timedelta(seconds=60)
            recent = s.scalar(select(AuditEvent.id).where(
                AuditEvent.workspace_id == workspace_id, AuditEvent.actor_user_id == user_id, AuditEvent.action == "authz.denied",
                AuditEvent.entity_id == perm[:32], AuditEvent.created_at >= since).limit(1))
            if recent is None:
                s.add(AuditEvent(workspace_id=workspace_id, actor_user_id=user_id, action="authz.denied", entity_type="permission",
                                 entity_id=perm[:32], details={"outcome": "denied", "permission": perm}))
                s.commit()
    except Exception:  # noqa: BLE001 - auditing a denial must never change the denial itself
        log.warning("authz_denial_audit_failed")
