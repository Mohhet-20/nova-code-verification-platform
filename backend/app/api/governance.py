"""Governance API: organization members, teams, ownership, policies, workflows, review queue and governance audit.

Every handler builds a server-side ``Principal`` from the authenticated identity; no role, team or approval claim is read
from the request. Out-of-scope resources answer 404, in-scope but unauthorized operations answer 403.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.db.session import get_db
from app.models import (
    AuditEvent,
    GovernancePolicyVersion,
    PolicyEvaluation,
    ReviewQueueItem,
    Team,
    User,
    WorkflowInstance,
)
from app.observability import metrics as m
from app.schemas import governance as S
from app.schemas.common import Page, make_page
from app.security.auth import RequestContext, get_context
from app.security.authz import ORG_PERMISSIONS, ORG_ROLES, TEAM_PERMISSIONS, Principal
from app.services.governance import members as members_svc
from app.services.governance import policies as pol
from app.services.governance import queue as queue_svc
from app.services.governance import resources as res_mod
from app.services.governance import teams as teams_svc
from app.services.governance import workflows as wf
from app.services.governance.common import paginate

router = APIRouter(tags=["governance"])
Dict = dict[str, Any]


def P(ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Principal:  # noqa: N802 - FastAPI dependency
    return Principal(db, ctx)


def _page(items: list[Any], total: int, page: int, size: int) -> Page[Any]:
    return make_page(items, total, page, max(1, min(size, 100)))


# ---------------------------------------------------------------------------------------------- identity & members
@router.get("/governance/me")
def me(p: Principal = Depends(P), ctx: RequestContext = Depends(get_context)) -> Dict:
    """The caller's effective role and permissions. For UI display only - every operation is re-checked server-side."""
    perms = ORG_PERMISSIONS.get(p.role, set())
    return {"user_id": p.user_id, "handle": ctx.user.handle, "display_name": ctx.user.display_name, "org_role": p.role, "org_wide_visibility": p.org_wide,
            "org_permissions": sorted(perms), "teams": [{"team_id": t, "role": r, "permissions": sorted(TEAM_PERMISSIONS[r])} for t, r in p.team_roles.items()],
            "org_roles": list(ORG_ROLES), "auth_kind": ctx.auth_kind}


@router.get("/org/members")
def list_members(q: str | None = Query(None, max_length=80), include_removed: bool = False, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                 p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = members_svc.list_members(db, p, q=q, include_removed=include_removed, offset=off, limit=lim)
    return _page(items, total, page, page_size)


@router.post("/org/members", status_code=201)
def create_member(body: S.MemberCreate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    member, token = members_svc.create_member(db, ctx, p, handle=body.handle, display_name=body.display_name, role=body.role)
    return {"member": member, "token": token, "token_notice": "Store this token now; it cannot be shown again."}


@router.put("/org/members/{user_id}/role")
def change_member_role(user_id: str, body: S.RoleChange, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return members_svc.change_role(db, ctx, p, user_id, body.role)


@router.post("/org/members/{user_id}/remove")
def remove_member(user_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return members_svc.remove_member(db, ctx, p, user_id)


@router.post("/org/members/{user_id}/token")
def rotate_token(user_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return {"token": members_svc.rotate_token(db, ctx, p, user_id), "token_notice": "Previous tokens were revoked. Store this token now; it cannot be shown again."}


# ---------------------------------------------------------------------------------------------- teams
@router.get("/teams")
def list_teams(q: str | None = Query(None, max_length=80), include_archived: bool = False, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
               p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = teams_svc.list_teams(db, p, q=q, include_archived=include_archived, offset=off, limit=lim)
    return _page(items, total, page, page_size)


@router.post("/teams", status_code=201)
def create_team(body: S.TeamCreate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.team_view(teams_svc.create_team(db, ctx, p, name=body.name, description=body.description), 0, None)


@router.get("/teams/{team_id}")
def get_team(team_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    t = teams_svc.get_team(db, p, team_id)
    n = db.scalar(select(func.count()).select_from(teams_svc.TeamMember).where(teams_svc.TeamMember.team_id == team_id, teams_svc.TeamMember.status == "active")) or 0
    return teams_svc.team_view(t, n, p.team_roles.get(team_id))


@router.put("/teams/{team_id}")
def update_team(team_id: str, body: S.TeamUpdate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.team_view(teams_svc.update_team(db, ctx, p, team_id, name=body.name, description=body.description), None, p.team_roles.get(team_id))


@router.post("/teams/{team_id}/archive")
def archive_team(team_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.team_view(teams_svc.archive_team(db, ctx, p, team_id), None, None)


@router.get("/teams/{team_id}/members")
def list_team_members(team_id: str, include_removed: bool = False, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                      p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = teams_svc.list_members(db, p, team_id, include_removed=include_removed, offset=off, limit=lim)
    return _page(items, total, page, page_size)


@router.post("/teams/{team_id}/members", status_code=201)
def add_team_member(team_id: str, body: S.TeamMemberAdd, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.add_member(db, ctx, p, team_id, user_id=body.user_id, role=body.role)


@router.put("/teams/{team_id}/members/{user_id}")
def change_team_member(team_id: str, user_id: str, body: S.RoleChange, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.change_member_role(db, ctx, p, team_id, user_id, body.role)


@router.post("/teams/{team_id}/members/{user_id}/remove")
def remove_team_member(team_id: str, user_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.remove_member(db, ctx, p, team_id, user_id)


@router.get("/teams/{team_id}/ownership")
def team_ownership(team_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = teams_svc.owned_repositories(db, p, team_id, off, lim)
    return _page(items, total, page, page_size)


@router.put("/teams/{team_id}/ownership")
def assign_ownership(team_id: str, body: S.OwnershipAssign, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.assign_repository(db, ctx, p, team_id, body.repository_id, body.accountable_user_id)


@router.post("/teams/{team_id}/ownership/{repository_id}/release", status_code=204)
def release_ownership(team_id: str, repository_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> None:
    teams_svc.release_repository(db, ctx, p, team_id, repository_id)


@router.get("/teams/{team_id}/work")
def team_work(team_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    return teams_svc.team_work(db, p, team_id)


# ---------------------------------------------------------------------------------------------- policies
@router.get("/governance/policy-types")
def policy_types(_: Principal = Depends(P)) -> list[Dict]:
    return [{"type": t, "description": pol.DESCRIPTIONS[t], "config_schema": s.model_json_schema()} for t, s in sorted(pol.SCHEMAS.items())]


@router.get("/governance/policies")
def list_policies(policy_type: str | None = Query(None, max_length=40), severity: str | None = Query(None, max_length=14), enabled: bool | None = None,
                  page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("policy.view")
    off, lim = paginate(page, page_size)
    rows, total = pol.list_policies(db, p.workspace_id, policy_type=policy_type, severity=severity, enabled=enabled, offset=off, limit=lim)
    return _page([pol.view(r) for r in rows], total, page, page_size)


@router.post("/governance/policies", status_code=201)
def create_policy(body: S.PolicyCreate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return pol.view(pol.create_policy(db, ctx, p, body.model_dump()))


@router.post("/governance/seed-defaults")
def seed_defaults(p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return wf.seed_defaults(db, ctx, p)


@router.get("/governance/policies/{policy_id}")
def get_policy(policy_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    p.require("policy.view")
    return pol.view(pol.get_policy(db, p.workspace_id, policy_id))


@router.put("/governance/policies/{policy_id}")
def update_policy(policy_id: str, body: S.PolicyUpdate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    changes = body.model_dump(exclude_unset=True)
    note = changes.pop("note", "")
    return pol.view(pol.update_policy(db, ctx, p, policy_id, changes, note))


@router.get("/governance/policies/{policy_id}/versions")
def policy_versions(policy_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("policy.view")
    pol.get_policy(db, p.workspace_id, policy_id)
    off, lim = paginate(page, page_size)
    stmt = select(GovernancePolicyVersion).where(GovernancePolicyVersion.policy_id == policy_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(GovernancePolicyVersion.version.desc()).offset(off).limit(lim)).all()
    return _page([{"version": r.version, "snapshot": r.snapshot, "changed_by": r.changed_by, "note": r.note, "at": r.created_at} for r in rows], total, page, page_size)


def _eval_view(e: PolicyEvaluation) -> Dict:
    return {"id": e.id, "policy_id": e.policy_id, "policy_key": e.policy_key, "policy_version": e.policy_version, "policy_type": e.policy_type, "severity": e.severity,
            "instance_id": e.instance_id, "resource_type": e.resource_type, "resource_id": e.resource_id, "status": e.status, "blocking": e.blocking, "trigger": e.trigger,
            "result": e.result, "evaluated_by": e.evaluated_by, "evaluated_at": e.evaluated_at}


@router.post("/governance/evaluations", status_code=201)
def evaluate_now(body: S.EvaluationRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    """Evaluate every applicable policy against stored evidence. The request carries identifiers only; results are never client-supplied."""
    p.require("policy.view")
    res = res_mod.resolve(db, ctx.workspace.id, body.resource_type, body.resource_id)
    if res is None or not p.can_see_repository(res.repository_id):
        raise NotFoundError("Resource not found.")
    inst = None
    if body.instance_id:
        inst = wf.get_instance(db, p, body.instance_id)
        if (inst.resource_type, inst.resource_id) != (res.type, res.id):
            raise ValidationFailedError("The workflow does not govern this resource.")
    c = pol.EvalContext(db=db, workspace_id=ctx.workspace.id, resource=res, instance=inst, transition=None)
    out = pol.evaluate_all(db, c, trigger="manual", actor=ctx.user.id, persist=True)
    db.commit()
    return out


@router.get("/governance/evaluations")
def list_evaluations(resource_type: str | None = Query(None, max_length=30), resource_id: str | None = Query(None, max_length=32), instance_id: str | None = Query(None, max_length=32),
                     policy_key: str | None = Query(None, max_length=60), status: str | None = Query(None, max_length=14), page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("policy.view")
    stmt = select(PolicyEvaluation).where(PolicyEvaluation.workspace_id == p.workspace_id)
    if instance_id:
        wf.get_instance(db, p, instance_id)  # scope check
        stmt = stmt.where(PolicyEvaluation.instance_id == instance_id)
    elif not p.org_wide:
        # without org-wide visibility an evaluation list must be anchored to a workflow or resource the caller can see
        if not (resource_type and resource_id):
            raise ValidationFailedError("Provide instance_id, or resource_type and resource_id.")
        res = res_mod.resolve(db, p.workspace_id, resource_type, resource_id)
        if res is None or not p.can_see_repository(res.repository_id):
            raise NotFoundError("Resource not found.")
    if resource_type:
        stmt = stmt.where(PolicyEvaluation.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(PolicyEvaluation.resource_id == resource_id)
    if policy_key:
        stmt = stmt.where(PolicyEvaluation.policy_key == policy_key)
    if status:
        stmt = stmt.where(PolicyEvaluation.status == status)
    off, lim = paginate(page, page_size)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(PolicyEvaluation.evaluated_at.desc(), PolicyEvaluation.id).offset(off).limit(lim)).all()
    return _page([_eval_view(r) for r in rows], total, page, page_size)


# ---------------------------------------------------------------------------------------------- workflows
@router.get("/workflows/definitions")
def list_definitions(resource_type: str | None = Query(None, max_length=30), active_only: bool = True, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                     p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    p.require("definition.view")
    off, lim = paginate(page, page_size)
    rows, total = wf.list_definitions(db, p.workspace_id, resource_type=resource_type, active_only=active_only, offset=off, limit=lim)
    return _page([wf.def_view(r) for r in rows], total, page, page_size)


@router.post("/workflows/definitions", status_code=201)
def create_definition(body: S.DefinitionCreate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return wf.def_view(wf.create_definition(db, ctx, p, key=body.key, name=body.name, description=body.description, resource_type=body.resource_type, spec=body.spec))


@router.get("/workflows/instances")
def list_instances(state: str | None = Query(None, max_length=20), team_id: str | None = Query(None, max_length=32), repository_id: str | None = Query(None, max_length=32),
                   resource_type: str | None = Query(None, max_length=30), mine: bool = False, pending_for_me: bool = False, page: int = Query(1, ge=1),
                   page_size: int = Query(25, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = wf.list_instances(db, p, state=state, team_id=team_id, repository_id=repository_id, mine=mine, pending_for_me=pending_for_me, resource_type=resource_type, offset=off, limit=lim)
    return _page(items, total, page, page_size)


@router.post("/workflows/instances", status_code=201)
def create_instance(body: S.InstanceCreate, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    inst = wf.create_instance(db, ctx, p, resource_type=body.resource_type, resource_id=body.resource_id, title=body.title, priority=body.priority, team_id=body.team_id, definition_key=body.definition_key)
    return wf.view(db, p, inst, detail=True)


@router.get("/workflows/instances/{instance_id}")
def get_instance(instance_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    return wf.view(db, p, wf.get_instance(db, p, instance_id), detail=True)


@router.get("/workflows/instances/{instance_id}/events")
def instance_events(instance_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = wf.events(db, p, instance_id, off, lim)
    return _page(items, total, page, page_size)


@router.get("/workflows/instances/{instance_id}/policy-status")
def instance_policy_status(instance_id: str, p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    return wf.policy_status(db, p, instance_id)


@router.post("/workflows/instances/{instance_id}/transitions")
def transition(instance_id: str, body: S.TransitionRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return wf.view(db, p, wf.transition(db, ctx, p, instance_id, body.action, body.note), detail=True)


@router.post("/workflows/instances/{instance_id}/decisions", status_code=201)
def decide(instance_id: str, body: S.DecisionRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return wf.view(db, p, wf.decide(db, ctx, p, instance_id, body.decision, body.justification, body.stage), detail=True)


@router.post("/workflows/instances/{instance_id}/evidence", status_code=201)
def add_evidence(instance_id: str, body: S.EvidenceRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return wf.add_evidence(db, ctx, p, instance_id, body.label, body.note)


# ---------------------------------------------------------------------------------------------- review queue
@router.get("/review-queue")
def list_queue(status: str | None = Query("open", pattern="^(open|done|cancelled)?$"), team_id: str | None = Query(None, max_length=32), repository_id: str | None = Query(None, max_length=32),
               priority: str | None = Query(None, pattern="^(low|normal|high|urgent)$"), risk_level: str | None = Query(None, max_length=12), assignee: str | None = Query(None, max_length=32),
               unassigned: bool = False, min_age_hours: float | None = Query(None, ge=0, le=24 * 365), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
               p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = queue_svc.list_items(db, p, status=status or None, team_id=team_id, repository_id=repository_id, priority=priority, risk_level=risk_level, assignee=assignee,
                                        unassigned=unassigned, min_age_hours=min_age_hours, offset=off, limit=lim)
    return _page(items, total, page, page_size)


@router.get("/review-queue/summary")
def queue_summary(p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    return queue_svc.counts(db, p)


@router.post("/review-queue", status_code=201)
def enqueue(body: S.EnqueueRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    it = queue_svc.enqueue(db, ctx, p, resource_type=body.resource_type, resource_id=body.resource_id, reason=body.reason, priority=body.priority)
    return {"id": it.id, "status": it.status, "team_id": it.team_id}


def _qview(db: Session, p: Principal, it: ReviewQueueItem) -> Dict:
    names = dict(db.execute(select(User.id, User.handle).where(User.id == it.assignee_user_id)).all()) if it.assignee_user_id else {}
    inst = db.get(WorkflowInstance, it.instance_id) if it.instance_id else None
    res = res_mod.resolve(db, p.workspace_id, it.resource_type, it.resource_id)
    return queue_svc.view(db, it, res, names, inst)


@router.post("/review-queue/{item_id}/assign")
def assign(item_id: str, body: S.AssignRequest, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return _qview(db, p, queue_svc.assign(db, ctx, p, item_id, body.user_id, body.note))


@router.post("/review-queue/{item_id}/claim")
def claim(item_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return _qview(db, p, queue_svc.claim(db, ctx, p, item_id))


@router.post("/review-queue/{item_id}/release")
def release(item_id: str, p: Principal = Depends(P), ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Dict:
    return _qview(db, p, queue_svc.release(db, ctx, p, item_id))


@router.get("/review-queue/{item_id}/history")
def queue_history(item_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    off, lim = paginate(page, page_size)
    items, total = queue_svc.history(db, p, item_id, off, lim)
    return _page(items, total, page, page_size)


# ---------------------------------------------------------------------------------------------- audit, overview, health
GOV_PREFIXES = ("team.", "member.", "ownership.", "policy.", "workflow.", "queue.", "authz.", "governance.")


@router.get("/governance/audit")
def governance_audit(action_prefix: str | None = Query(None, max_length=40), entity_type: str | None = Query(None, max_length=40), actor_user_id: str | None = Query(None, max_length=32),
                     team_id: str | None = Query(None, max_length=32), since: datetime | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                     p: Principal = Depends(P), db: Session = Depends(get_db)) -> Page[Any]:
    """Read-only governance history. There is intentionally no create/update/delete endpoint for audit records."""
    if not p.has_org("audit.view"):
        # team administrators may read the history of their own team only
        if not team_id or not p.has_team("audit.view_team", team_id):
            p.deny("audit.view")
    stmt = select(AuditEvent).where(AuditEvent.workspace_id == p.workspace_id)
    stmt = stmt.where(or_(*[AuditEvent.action.like(f"{x}%") for x in GOV_PREFIXES]))
    if action_prefix:
        stmt = stmt.where(AuditEvent.action.like(action_prefix.replace("%", "").replace("_", "") + "%"))
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if actor_user_id:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
    if team_id:
        stmt = stmt.where(AuditEvent.details["team_id"].as_string() == team_id)
    if since:
        stmt = stmt.where(AuditEvent.created_at >= since)
    off, lim = paginate(page, page_size)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id).offset(off).limit(lim)).all()
    names = dict(db.execute(select(User.id, User.handle).where(User.id.in_({r.actor_user_id for r in rows if r.actor_user_id}))).all()) if rows else {}
    return _page([{"id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "actor_user_id": r.actor_user_id,
                   "actor_handle": names.get(r.actor_user_id or ""), "outcome": (r.details or {}).get("outcome"), "details": r.details, "at": r.created_at} for r in rows], total, page, page_size)


@router.get("/governance/overview")
def overview(p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    """Role-appropriate dashboard counts (aggregate queries only; scoped to what the caller may see)."""
    pending, pending_total = wf.list_instances(db, p, state=None, team_id=None, repository_id=None, mine=False, pending_for_me=True, resource_type=None, offset=0, limit=5)
    q = queue_svc.counts(db, p)
    teams_n = db.scalar(select(func.count()).select_from(Team).where(Team.workspace_id == p.workspace_id, Team.status == "active",
                                                                       *([] if p.org_wide else [Team.id.in_(list(p.team_roles) or ["-"])]))) or 0
    mine_open = db.scalar(select(func.count()).select_from(WorkflowInstance).where(WorkflowInstance.workspace_id == p.workspace_id, WorkflowInstance.requested_by == p.user_id,
                                                                                    WorkflowInstance.state.in_(wf.LIVE))) or 0
    if p.org_wide:
        m.GOV_QUEUE_DEPTH.set(float(q["by_status"].get("open", 0)))
        m.GOV_PENDING.set(float(db.scalar(select(func.count()).select_from(WorkflowInstance).where(WorkflowInstance.workspace_id == p.workspace_id, WorkflowInstance.state == "IN_REVIEW")) or 0))
    return {"teams": teams_n, "pending_my_approval": pending_total, "pending_preview": pending, "queue": q, "my_open_requests": mine_open, "org_wide": p.org_wide, "role": p.role}


@router.get("/governance/health")
def governance_health(p: Principal = Depends(P), db: Session = Depends(get_db)) -> Dict:
    if not p.org_wide:
        p.deny("governance.health")
    from app.models import GovernancePolicy, Membership, WorkflowDefinition

    owners = db.scalar(select(func.count()).select_from(Membership).where(Membership.workspace_id == p.workspace_id, Membership.role == "owner", Membership.status == "active")) or 0
    pols = db.scalar(select(func.count()).select_from(GovernancePolicy).where(GovernancePolicy.workspace_id == p.workspace_id, GovernancePolicy.enabled.is_(True))) or 0
    defs = db.scalar(select(func.count()).select_from(WorkflowDefinition).where(WorkflowDefinition.workspace_id == p.workspace_id, WorkflowDefinition.active.is_(True))) or 0
    triggers: int | None = None
    if db.get_bind().dialect.name == "sqlite":
        triggers = db.execute(text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%'")).scalar()
    checks = [{"name": "active_organization_administrator", "ok": owners >= 1, "detail": f"{owners} active administrator(s)."},
              {"name": "policies_configured", "ok": pols >= 1, "detail": f"{pols} enabled policy(ies); with none, workflows have no policy gate."},
              {"name": "workflow_definitions", "ok": defs >= 1, "detail": f"{defs} active definition(s); with none, workflows cannot be created."},
              {"name": "audit_append_only_triggers", "ok": (triggers is None) or triggers >= 12,
               "detail": "Not applicable (non-SQLite database: use equivalent triggers or privileges)." if triggers is None else f"{triggers} append-only triggers present (12 expected)."}]
    return {"healthy": all(c["ok"] for c in checks), "checks": checks, "scheduler": "not_implemented",
            "limits": "No scheduler exists: nothing escalates, expires or is marked overdue automatically."}
