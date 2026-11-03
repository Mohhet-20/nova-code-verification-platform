"""Versioned approval workflows: definitions, instances, guarded transitions and approval decisions.

Invariants (all enforced here, on the server, regardless of what a client sends):

* Instances freeze their definition (``definition_snapshot``); editing a definition never changes a running instance.
* State changes happen only through ``transition`` (submit/resubmit/cancel/complete) or ``decide``. ``APPROVED`` is *derived*:
  it can only be reached when the final stage's quorum is met AND every blocking policy passes against persisted evidence.
* One decision per user per (instance, round, stage) - a database unique constraint, not just a check.
* Concurrent writers are serialised with ORM optimistic locking (``row_version``): a stale writer fails with 409 and rolls back.
* Every transition writes its workflow event + audit event in the same transaction as the state change.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    GovernancePolicy,
    PolicyEvaluation,
    ReviewQueueItem,
    Team,
    TeamMember,
    User,
    WorkflowDecision,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowInstance,
)
from app.models import RepositoryOwnership as Ownership
from app.observability import metrics as m
from app.security.auth import RequestContext
from app.security.authz import ORG_PERMISSIONS, ORG_ROLES, Principal, _record_denial
from app.services.governance import policies as pol_mod
from app.services.governance import queue as queue_mod
from app.services.governance import resources as res_mod
from app.services.governance.common import emit

TERMINAL = ("REJECTED", "COMPLETED", "CANCELLED")
LIVE = ("DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED")
DECISIONS = ("approve", "reject", "request_changes", "abstain")
ACTIONS = ("submit", "resubmit", "cancel", "complete")  # "approve" is deliberately NOT a transition action
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "submit": (("DRAFT",), "IN_REVIEW"), "resubmit": (("CHANGES_REQUESTED",), "IN_REVIEW"),
    "cancel": (("DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED"), "CANCELLED"), "complete": (("APPROVED",), "COMPLETED"),
}
DECIDER_ROLES = tuple(r for r in ORG_ROLES if "workflow.decide" in ORG_PERMISSIONS[r] or "*" in ORG_PERMISSIONS[r])


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageSpec(_S):
    name: str = Field(min_length=1, max_length=60)
    approver_roles: list[str] = Field(default_factory=list, max_length=6)
    approver_team_roles: list[str] = Field(default_factory=list, max_length=3)
    min_approvals: int = Field(1, ge=1, le=10)
    independent: bool = True

    @field_validator("approver_roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if r not in DECIDER_ROLES]
        if bad:
            raise ValueError(f"roles that cannot decide: {bad}; allowed {list(DECIDER_ROLES)}")
        return v

    @field_validator("approver_team_roles")
    @classmethod
    def _troles(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if r not in ("admin", "reviewer")]
        if bad:
            raise ValueError(f"team roles that cannot decide: {bad}; allowed ['admin', 'reviewer']")
        return v

    @model_validator(mode="after")
    def _some(self) -> StageSpec:
        if not self.approver_roles and not self.approver_team_roles:
            raise ValueError("a stage needs at least one approver role or team role")
        return self


class WorkflowSpec(_S):
    mode: Literal["sequential", "parallel"] = "sequential"
    stages: list[StageSpec] = Field(min_length=1, max_length=8)
    required_policy_keys: list[str] = Field(default_factory=list, max_length=20)
    allow_cancel: bool = True
    require_justification_on_approve: bool = False


def validate_spec(raw: Any) -> dict[str, Any]:
    try:
        return WorkflowSpec.model_validate(raw).model_dump()
    except ValidationError as e:
        raise ValidationFailedError("The workflow specification is not valid.", [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()]) from None


# ---------------------------------------------------------------------------------------------- definitions

def def_view(d: WorkflowDefinition) -> dict[str, Any]:
    return {"id": d.id, "key": d.key, "version": d.version, "name": d.name, "description": d.description, "resource_type": d.resource_type, "spec": d.spec,
            "active": d.active, "created_by": d.created_by, "created_at": d.created_at}


def create_definition(db: Session, ctx: RequestContext, p: Principal, *, key: str, name: str, description: str, resource_type: str, spec: Any) -> WorkflowDefinition:
    p.require("workflow.definition.manage")
    if resource_type not in res_mod.RESOURCE_TYPES:
        raise ValidationFailedError("Unsupported resource type.", {"allowed": list(res_mod.RESOURCE_TYPES)})
    clean_spec = validate_spec(spec)
    wanted = set(clean_spec.get("required_policy_keys") or [])
    known = set(db.scalars(select(GovernancePolicy.key).where(GovernancePolicy.workspace_id == ctx.workspace.id, GovernancePolicy.key.in_(wanted))).all()) if wanted else set()
    if wanted - known:
        raise ValidationFailedError("The workflow requires policies that do not exist.", {"unknown_policy_keys": sorted(wanted - known)})
    last = db.scalar(select(func.max(WorkflowDefinition.version)).where(WorkflowDefinition.workspace_id == ctx.workspace.id, WorkflowDefinition.key == key))
    prev = db.scalar(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == ctx.workspace.id, WorkflowDefinition.key == key, WorkflowDefinition.active.is_(True)))
    if prev is not None and prev.resource_type != resource_type:
        raise ValidationFailedError("A definition key cannot change its resource type; use a new key.")
    if prev is not None:
        prev.active = False
        db.flush()
    d = WorkflowDefinition(workspace_id=ctx.workspace.id, key=key, version=(last or 0) + 1, name=name.strip(), description=description.strip(), resource_type=resource_type,
                           spec=clean_spec, active=True, created_by=ctx.user.id)
    db.add(d)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("The definition was changed concurrently; retry.") from None
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="workflow.definition_created", entity_type="workflow_definition", entity_id=d.id,
         details={"key": key, "version": d.version, "resource_type": resource_type})
    db.commit()
    return d


def list_definitions(db: Session, workspace_id: str, *, resource_type: str | None, active_only: bool, offset: int, limit: int) -> tuple[list[WorkflowDefinition], int]:
    stmt = select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == workspace_id)
    if resource_type:
        stmt = stmt.where(WorkflowDefinition.resource_type == resource_type)
    if active_only:
        stmt = stmt.where(WorkflowDefinition.active.is_(True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    return list(db.scalars(stmt.order_by(WorkflowDefinition.key, WorkflowDefinition.version.desc()).offset(offset).limit(limit)).all()), total


DEFAULT_STAGE = {"name": "Independent review", "approver_roles": ["owner", "maintainer", "reviewer"], "approver_team_roles": ["admin", "reviewer"], "min_approvals": 1, "independent": True}
DEFAULT_DEFINITIONS = [
    {"key": f"{rt.replace('_', '-')}-review", "name": f"{label} approval", "resource_type": rt, "description": f"One independent approval before a {label.lower()} is accepted.",
     "spec": {"mode": "sequential", "stages": [DEFAULT_STAGE], "required_policy_keys": ["independent-approval"] + (["verification-passed"] if rt != "code_review" else [])}}
    for rt, label in (("code_review", "Code review"), ("repair_patch", "Repair patch"), ("refactor", "Refactoring"), ("change_request", "Change request"))
]


def seed_defaults(db: Session, ctx: RequestContext, p: Principal) -> dict[str, Any]:
    """Install the default policies and workflow definitions (idempotent: existing keys are left untouched)."""
    p.require("workflow.definition.manage")
    p.require("policy.manage")
    made_p, made_d = [], []
    for dp in pol_mod.DEFAULT_POLICIES:
        if db.scalar(select(GovernancePolicy.id).where(GovernancePolicy.workspace_id == ctx.workspace.id, GovernancePolicy.key == dp["key"])) is None:
            pol_mod.create_policy(db, ctx, p, dict(dp))
            made_p.append(dp["key"])
    for dd in DEFAULT_DEFINITIONS:
        if db.scalar(select(WorkflowDefinition.id).where(WorkflowDefinition.workspace_id == ctx.workspace.id, WorkflowDefinition.key == dd["key"])) is None:
            create_definition(db, ctx, p, **dd)
            made_d.append(dd["key"])
    return {"policies_created": made_p, "definitions_created": made_d}


# ---------------------------------------------------------------------------------------------- instances

def _event(db: Session, inst: WorkflowInstance, etype: str, actor: str | None, frm: str | None = None, to: str | None = None, data: dict[str, Any] | None = None) -> None:
    db.add(WorkflowEvent(instance_id=inst.id, workspace_id=inst.workspace_id, event_type=etype, from_state=frm, to_state=to, actor_user_id=actor, data=data or {}))


def _uname(db: Session, ids: set[str | None]) -> dict[str, str]:
    ids2 = {i for i in ids if i}
    return dict(db.execute(select(User.id, User.handle).where(User.id.in_(ids2))).all()) if ids2 else {}


def get_instance(db: Session, p: Principal, instance_id: str, *, for_update_check: bool = False) -> WorkflowInstance:
    inst = db.scalar(select(WorkflowInstance).where(WorkflowInstance.id == instance_id, WorkflowInstance.workspace_id == p.workspace_id))
    if inst is None or not _can_view(db, p, inst):
        raise NotFoundError("Workflow not found.")  # identical answer for missing and out-of-scope
    return inst


def _can_view(db: Session, p: Principal, inst: WorkflowInstance) -> bool:
    if p.org_wide or inst.requested_by == p.user_id or (inst.team_id and inst.team_id in p.team_roles):
        return True
    if db.scalar(select(WorkflowDecision.id).where(WorkflowDecision.instance_id == inst.id, WorkflowDecision.user_id == p.user_id).limit(1)):
        return True
    return db.scalar(select(ReviewQueueItem.id).where(ReviewQueueItem.instance_id == inst.id, ReviewQueueItem.assignee_user_id == p.user_id).limit(1)) is not None


def create_instance(db: Session, ctx: RequestContext, p: Principal, *, resource_type: str, resource_id: str, title: str | None, priority: str,
                    team_id: str | None, definition_key: str | None) -> WorkflowInstance:
    if resource_type not in res_mod.RESOURCE_TYPES:
        raise ValidationFailedError("Unsupported resource type.", {"allowed": list(res_mod.RESOURCE_TYPES)})
    if priority not in queue_mod.PRIORITIES:
        raise ValidationFailedError("Unknown priority.", {"allowed": list(queue_mod.PRIORITIES)})
    res = res_mod.resolve(db, ctx.workspace.id, resource_type, resource_id)
    if res is None or not p.can_see_repository(res.repository_id):
        raise NotFoundError("Resource not found.")
    owner = db.scalar(select(Ownership.team_id).where(Ownership.repository_id == res.repository_id))
    if team_id and owner and team_id != owner:
        raise ValidationFailedError("The repository is owned by a different team.")
    team = owner or team_id
    if team:
        t = db.scalar(select(Team).where(Team.id == team, Team.workspace_id == ctx.workspace.id))
        if t is None or not p.can_see_team(team):
            raise NotFoundError("Team not found.")
        if t.status != "active":
            raise ConflictError("The team is archived.")
    if not p.can("workflow.create", team):
        p.deny("workflow.create")
    stmt = select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == ctx.workspace.id, WorkflowDefinition.resource_type == resource_type, WorkflowDefinition.active.is_(True))
    if definition_key:
        stmt = stmt.where(WorkflowDefinition.key == definition_key)
    d = db.scalars(stmt.order_by(WorkflowDefinition.key).limit(1)).first()
    if d is None:
        raise ConflictError("No active workflow definition exists for this resource type. An organization administrator must install or create one first.")
    risk = res_mod.risk(db, res)
    inst = WorkflowInstance(workspace_id=ctx.workspace.id, definition_id=d.id, definition_version=d.version, definition_snapshot={"key": d.key, "name": d.name, "version": d.version, **d.spec},
                            resource_type=resource_type, resource_id=resource_id, repository_id=res.repository_id, team_id=team, title=(title or res.title).strip()[:200] or res.title,
                            state="DRAFT", stage_index=0, round=1, risk_level=risk["data"].get("level") if risk["state"] == "ok" else None, priority=priority,
                            requested_by=ctx.user.id, resource_author=res.author_user_id)
    db.add(inst)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("This resource already has a workflow in progress.") from None
    _event(db, inst, "created", ctx.user.id, None, "DRAFT", {"definition": d.key, "definition_version": d.version})
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="workflow.created", entity_type="workflow", entity_id=inst.id, team_id=team,
         details={"resource_type": resource_type, "resource_id": resource_id, "definition": d.key, "definition_version": d.version})
    db.commit()
    return inst


def _stages(inst: WorkflowInstance) -> list[dict[str, Any]]:
    return list(inst.definition_snapshot.get("stages", []))


def _approvals(db: Session, inst: WorkflowInstance, cache: dict[tuple[str, int, int], int] | None = None) -> dict[int, int]:
    if cache is not None:
        return {s: c for (iid, rnd, s), c in cache.items() if iid == inst.id and rnd == inst.round}
    rows = db.execute(select(WorkflowDecision.stage_index, func.count()).where(WorkflowDecision.instance_id == inst.id, WorkflowDecision.round == inst.round,
                                                                              WorkflowDecision.decision == "approve").group_by(WorkflowDecision.stage_index)).all()
    return {s: c for s, c in rows}


def _open_stages(db: Session, inst: WorkflowInstance, cache: dict[tuple[str, int, int], int] | None = None) -> list[int]:
    stages = _stages(inst)
    if inst.state != "IN_REVIEW":
        return []
    if inst.definition_snapshot.get("mode") == "parallel":
        appr = _approvals(db, inst, cache)
        return [i for i, s in enumerate(stages) if appr.get(i, 0) < s["min_approvals"]]
    return [inst.stage_index] if inst.stage_index < len(stages) else []


def eligible_for_stage(p: Principal, inst: WorkflowInstance, stage: dict[str, Any]) -> bool:
    org = p.role in stage["approver_roles"] and p.has_org("workflow.decide")
    team = inst.team_id is not None and p.team_roles.get(inst.team_id) in stage["approver_team_roles"] and p.has_team("workflow.decide", inst.team_id)
    # a team admin may decide only when the stage lists the 'admin' team role (it carries no decide permission by itself)
    team_admin = inst.team_id is not None and p.team_roles.get(inst.team_id) == "admin" and "admin" in stage["approver_team_roles"]
    return org or team or team_admin


def _independent_blocked(inst: WorkflowInstance, stage: dict[str, Any], user_id: str) -> bool:
    return bool(stage.get("independent", True)) and user_id in {inst.requested_by, inst.resource_author}


def _bump(inst: WorkflowInstance) -> None:
    inst.updated_at = utcnow()  # marks the row dirty so the optimistic-lock UPDATE (and row_version increment) always runs


def _ctx(db: Session, inst: WorkflowInstance, transition: str) -> pol_mod.EvalContext:
    res = res_mod.resolve(db, inst.workspace_id, inst.resource_type, inst.resource_id)
    if res is None:  # the governed resource vanished: evidence cannot exist, so nothing can be approved
        raise ConflictError("The governed resource is no longer available.")
    return pol_mod.EvalContext(db=db, workspace_id=inst.workspace_id, resource=res, instance=inst, transition=transition)


def _enforcing(pol_type: str, transition: str) -> bool:
    return pol_type == "prohibited_transition" or transition in ("approve", "complete")


def check_policies(db: Session, inst: WorkflowInstance, transition: str, actor: str | None) -> dict[str, Any]:
    """Evaluate policies for a transition WITHOUT persisting. ``blocked`` honours phase rules and the definition's required policies."""
    c = _ctx(db, inst, transition)
    out = pol_mod.evaluate_all(db, c, trigger=transition, actor=actor, persist=False)
    required = set(inst.definition_snapshot.get("required_policy_keys") or [])
    seen = {r["policy"]["key"] for r in out["results"]}
    blockers: list[str] = []
    for r in out["results"]:
        pol_type, key = r["policy"]["type"], r["policy"]["key"]
        must = key in required and transition in ("approve", "complete")
        if (r["blocking"] and _enforcing(pol_type, transition)) or (must and r["raw_status"] in ("fail", "unavailable")):
            blockers.append(key)
    for key in required - seen:  # a required policy that is missing/disabled/out of scope cannot be assumed to pass
        pol = db.scalar(select(GovernancePolicy).where(GovernancePolicy.workspace_id == inst.workspace_id, GovernancePolicy.key == key))
        if pol is None or not pol.enabled:
            if transition in ("approve", "complete"):
                blockers.append(key)
                out["results"].append({"policy": {"id": pol.id if pol else None, "key": key, "name": key, "version": pol.version if pol else 0, "type": pol.policy_type if pol else "unknown", "severity": "blocking"},
                                       "status": "unavailable", "raw_status": "unavailable", "blocking": True, "rules": [], "unmet": [f"Required policy '{key}' is missing or disabled."],
                                       "explanation": f"Cannot be evaluated: required policy '{key}' is missing or disabled.", "context": {}, "evaluated_at": c.now.isoformat()})
    out["blockers"] = sorted(set(blockers))
    out["blocked"] = bool(blockers)
    out["_ctx"] = c
    return out


def _persist_eval(db: Session, out: dict[str, Any], trigger: str, actor: str | None) -> None:
    pol_mod.persist_results(db, out["_ctx"], [r for r in out["results"] if r["policy"].get("id")], trigger=trigger, actor=actor)


def _public(out: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in out.items() if k != "_ctx"}


def _blocked_error(out: dict[str, Any]) -> ConflictError:
    unmet = [u for r in out["results"] if r["policy"]["key"] in out["blockers"] for u in r.get("unmet", [])]
    return ConflictError("Blocked by governance policy: " + ("; ".join(unmet) if unmet else ", ".join(out["blockers"])) + ".", {"code": "policy_blocked", **_public(out)})


def _is_lead(p: Principal) -> bool:
    return p.has_org("workflow.complete")


def transition(db: Session, ctx: RequestContext, p: Principal, instance_id: str, action: str, note: str = "") -> WorkflowInstance:
    if action not in ACTIONS:
        raise ValidationFailedError("Unsupported transition.", {"allowed": list(ACTIONS), "note": "Approval happens through decisions, not transitions."})
    inst = get_instance(db, p, instance_id)
    allowed_from, to = TRANSITIONS[action]
    if inst.state not in allowed_from:
        m.GOV_TRANSITIONS.inc(action=action, outcome="invalid_state")
        raise ConflictError(f"Cannot {action} a workflow that is {inst.state}.", {"state": inst.state, "allowed_from": list(allowed_from)})
    requester = inst.requested_by == ctx.user.id
    if action in ("submit", "resubmit"):
        ok = requester or _is_lead(p)
    elif action == "cancel":
        if not inst.definition_snapshot.get("allow_cancel", True):
            raise ConflictError("This workflow definition does not allow cancellation.")
        ok = (requester and p.can("workflow.cancel_own", inst.team_id)) or p.has_org("workflow.cancel")
    else:  # complete
        ok = requester or _is_lead(p)
    if not ok:
        m.GOV_TRANSITIONS.inc(action=action, outcome="forbidden")
        p.deny(f"workflow.{action}")
    frm = inst.state
    sp = db.begin_nested()
    try:
        out = None
        if action in ("submit", "resubmit", "complete"):
            out = check_policies(db, inst, {"submit": "submit", "resubmit": "submit", "complete": "complete"}[action], ctx.user.id)
            if out["blocked"]:
                sp.rollback()
                _persist_eval(db, out, action, ctx.user.id)
                emit(db, workspace_id=inst.workspace_id, actor=ctx.user.id, action="workflow.transition_blocked", entity_type="workflow", entity_id=inst.id, team_id=inst.team_id,
                     outcome="blocked", details={"transition": action, "blockers": out["blockers"]})
                db.commit()
                m.GOV_TRANSITIONS.inc(action=action, outcome="policy_blocked")
                raise _blocked_error(out)
        now = utcnow()
        inst.state = to
        if action == "submit":
            inst.submitted_at, inst.stage_index = now, 0
            _event(db, inst, "state_changed", ctx.user.id, frm, "SUBMITTED")
            queue_mod.ensure_item(db, inst, f"Awaiting review: {inst.definition_snapshot.get('name', 'workflow')}", ctx.user.id)
        elif action == "resubmit":
            inst.round, inst.stage_index = inst.round + 1, 0
            queue_mod.ensure_item(db, inst, f"Resubmitted after requested changes (round {inst.round})", ctx.user.id)
        elif action == "complete":
            inst.completed_at = now
        elif action == "cancel":
            inst.completed_at = now
            queue_mod.close_item(db, inst, "cancelled", ctx.user.id, note or "workflow cancelled")
        if action == "complete":
            queue_mod.close_item(db, inst, "done", ctx.user.id, "workflow completed")  # no-op when already closed
        _bump(inst)
        _event(db, inst, "state_changed", ctx.user.id, "SUBMITTED" if action == "submit" else frm, to, {"action": action, "note": note[:500]})
        if out is not None:
            _persist_eval(db, out, action, ctx.user.id)
        emit(db, workspace_id=inst.workspace_id, actor=ctx.user.id, action=f"workflow.{action}", entity_type="workflow", entity_id=inst.id, team_id=inst.team_id,
             details={"from": frm, "to": to, "round": inst.round})
        db.flush()
        sp.commit()
        db.commit()
    except StaleDataError:
        db.rollback()
        m.GOV_TRANSITIONS.inc(action=action, outcome="conflict")
        raise ConflictError("The workflow was changed by someone else at the same time. Reload and try again.") from None
    except ConflictError:
        raise
    except Exception:
        db.rollback()
        m.GOV_TRANSITIONS.inc(action=action, outcome="error")
        raise
    m.GOV_TRANSITIONS.inc(action=action, outcome="ok")
    return inst


def decide(db: Session, ctx: RequestContext, p: Principal, instance_id: str, decision: str, justification: str, stage: int | None = None) -> WorkflowInstance:
    if decision not in DECISIONS:
        raise ValidationFailedError("Unknown decision.", {"allowed": list(DECISIONS)})
    justification = (justification or "").strip()
    if len(justification) > 2000:
        raise ValidationFailedError("The justification is too long (2000 characters maximum).")
    inst = get_instance(db, p, instance_id)
    if inst.state != "IN_REVIEW":
        m.GOV_TRANSITIONS.inc(action=decision, outcome="invalid_state")
        raise ConflictError(f"Decisions can only be recorded while a workflow is IN_REVIEW (it is {inst.state}).", {"state": inst.state})
    stages = _stages(inst)
    open_idx = _open_stages(db, inst)
    mine = [i for i in open_idx if eligible_for_stage(p, inst, stages[i])]
    if stage is not None:
        if stage not in open_idx:
            raise ConflictError("That stage is not open for decisions.", {"open_stages": open_idx})
        mine = [i for i in mine if i == stage]
    if not mine:
        m.GOV_TRANSITIONS.inc(action=decision, outcome="ineligible")
        p.deny("workflow.decide")
    si = mine[0]
    st = stages[si]
    if _independent_blocked(inst, st, ctx.user.id):
        m.GOV_TRANSITIONS.inc(action=decision, outcome="self_review_blocked")
        _record_denial(inst.workspace_id, ctx.user.id, "self-review")
        raise ForbiddenError("Independent review is required: the requester or author cannot decide on their own work.")
    if decision in ("reject", "request_changes") and not justification:
        raise ValidationFailedError("A justification is required for this decision.")
    if decision == "approve" and inst.definition_snapshot.get("require_justification_on_approve") and not justification:
        raise ValidationFailedError("This workflow requires a justification for approvals.")
    dup = db.scalar(select(WorkflowDecision.id).where(WorkflowDecision.instance_id == inst.id, WorkflowDecision.round == inst.round, WorkflowDecision.stage_index == si, WorkflowDecision.user_id == ctx.user.id))
    if dup:
        raise ConflictError("You have already recorded a decision for this stage.")
    frm = inst.state
    sp = db.begin_nested()
    try:
        db.add(WorkflowDecision(instance_id=inst.id, round=inst.round, stage_index=si, user_id=ctx.user.id, decision=decision, justification=justification))
        db.flush()
        new_state, reason = frm, f"decision recorded: {decision}"
        out = None
        if decision == "reject":
            new_state = "REJECTED"
        elif decision == "request_changes":
            new_state = "CHANGES_REQUESTED"
        elif decision == "approve":
            appr = _approvals(db, inst)
            if appr.get(si, 0) >= st["min_approvals"]:
                if inst.definition_snapshot.get("mode") == "parallel":
                    final = all(appr.get(i, 0) >= s["min_approvals"] for i, s in enumerate(stages))
                else:
                    final = si >= len(stages) - 1
                    if not final:
                        inst.stage_index = si + 1
                        _event(db, inst, "stage_advanced", ctx.user.id, data={"from_stage": si, "to_stage": si + 1})
                if final:
                    out = check_policies(db, inst, "approve", ctx.user.id)
                    if out["blocked"]:
                        sp.rollback()
                        _persist_eval(db, out, "approve_blocked", ctx.user.id)
                        emit(db, workspace_id=inst.workspace_id, actor=ctx.user.id, action="workflow.approval_blocked", entity_type="workflow", entity_id=inst.id, team_id=inst.team_id,
                             outcome="blocked", details={"blockers": out["blockers"], "stage": si})
                        db.commit()
                        m.GOV_TRANSITIONS.inc(action="approve", outcome="policy_blocked")
                        raise _blocked_error(out)
                    new_state, reason = "APPROVED", "quorum met and policies passed"
        if new_state != frm:
            inst.state = new_state
            if new_state in TERMINAL:
                inst.completed_at = utcnow()
            if new_state in ("REJECTED", "CHANGES_REQUESTED", "APPROVED"):  # review work is finished once a verdict exists
                queue_mod.close_item(db, inst, "done", ctx.user.id, f"{decision}: {justification[:200]}")
            _event(db, inst, "state_changed", ctx.user.id, frm, new_state, {"reason": reason})
        _event(db, inst, "decision_recorded", ctx.user.id, data={"decision": decision, "stage": si, "round": inst.round, "justification": justification[:500]})
        if out is not None:
            _persist_eval(db, out, "approve", ctx.user.id)
        _bump(inst)
        emit(db, workspace_id=inst.workspace_id, actor=ctx.user.id, action=f"workflow.decision.{decision}", entity_type="workflow", entity_id=inst.id, team_id=inst.team_id,
             details={"stage": si, "round": inst.round, "from": frm, "to": new_state, "justification_given": bool(justification)})
        db.flush()
        sp.commit()
        db.commit()
    except StaleDataError:
        db.rollback()
        m.GOV_TRANSITIONS.inc(action=decision, outcome="conflict")
        raise ConflictError("The workflow was changed by someone else at the same time. Reload and try again.") from None
    except IntegrityError:
        db.rollback()
        m.GOV_TRANSITIONS.inc(action=decision, outcome="duplicate")
        raise ConflictError("You have already recorded a decision for this stage.") from None
    except (ConflictError, ForbiddenError, ValidationFailedError):
        raise
    except Exception:
        db.rollback()
        m.GOV_TRANSITIONS.inc(action=decision, outcome="error")
        raise
    if inst.state in TERMINAL and inst.submitted_at is not None:
        m.GOV_WORKFLOW_SECONDS.observe(max(0.0, (utcnow() - _aw(inst.submitted_at)).total_seconds()), outcome=inst.state.lower())
    m.GOV_TRANSITIONS.inc(action=decision, outcome="ok")
    return inst


def _aw(d):  # type: ignore[no-untyped-def]
    from datetime import UTC

    return d if d.tzinfo else d.replace(tzinfo=UTC)


def add_evidence(db: Session, ctx: RequestContext, p: Principal, instance_id: str, label: str, note: str) -> dict[str, Any]:
    """Attach a named evidence attestation. It is attributed and recorded; policies treat it as an attestation, not as a verified result."""
    inst = get_instance(db, p, instance_id)
    if not (inst.requested_by == ctx.user.id or _is_lead(p)):
        p.deny("workflow.evidence")
    if inst.state in TERMINAL:
        raise ConflictError("The workflow is finished.")
    label = label.strip()
    if not label or len(label) > 80:
        raise ValidationFailedError("An evidence label of 1-80 characters is required.")
    n = db.scalar(select(func.count()).select_from(WorkflowEvent).where(WorkflowEvent.instance_id == inst.id, WorkflowEvent.event_type == "evidence_added")) or 0
    if n >= 50:
        raise ConflictError("The maximum number of evidence items has been reached.")
    _event(db, inst, "evidence_added", ctx.user.id, data={"label": label, "note": note.strip()[:500]})
    emit(db, workspace_id=inst.workspace_id, actor=ctx.user.id, action="workflow.evidence_added", entity_type="workflow", entity_id=inst.id, team_id=inst.team_id, details={"label": label})
    db.commit()
    return {"label": label, "note": note.strip()[:500], "attested_by": ctx.user.id}


# ---------------------------------------------------------------------------------------------- reads

def _batch(db: Session, insts: list[WorkflowInstance]) -> tuple[dict[str, str], dict[tuple[str, int, int], int]]:
    """Names and approval counts for a page of instances in two queries (no per-row lookups)."""
    names = _uname(db, {i.requested_by for i in insts} | {i.resource_author for i in insts})
    ids = [i.id for i in insts]
    rows = db.execute(select(WorkflowDecision.instance_id, WorkflowDecision.round, WorkflowDecision.stage_index, func.count()).where(
        WorkflowDecision.instance_id.in_(ids), WorkflowDecision.decision == "approve").group_by(WorkflowDecision.instance_id, WorkflowDecision.round, WorkflowDecision.stage_index)).all() if ids else []
    return names, {(i, r, s): c for i, r, s, c in rows}


def view(db: Session, p: Principal, inst: WorkflowInstance, *, detail: bool = False,
         batch: tuple[dict[str, str], dict[tuple[str, int, int], int]] | None = None) -> dict[str, Any]:
    names, cache = batch if batch is not None else _batch(db, [inst])
    stages = _stages(inst)
    appr = _approvals(db, inst, cache) if inst.state != "DRAFT" else {}
    open_idx = _open_stages(db, inst, cache)
    stage_view = [{"index": i, "name": s["name"], "min_approvals": s["min_approvals"], "approvals": appr.get(i, 0), "independent": s.get("independent", True),
                   "approver_roles": s["approver_roles"], "approver_team_roles": s["approver_team_roles"], "open": i in open_idx,
                   "satisfied": appr.get(i, 0) >= s["min_approvals"]} for i, s in enumerate(stages)]
    outstanding: list[str] = []
    if inst.state == "DRAFT":
        outstanding.append("Submit the workflow for review.")
    elif inst.state == "IN_REVIEW":
        for i in open_idx:
            s = stages[i]
            outstanding.append(f"Stage '{s['name']}' needs {max(0, s['min_approvals'] - appr.get(i, 0))} more approval(s) from: "
                               + ", ".join(s["approver_roles"] + [f"team {r}" for r in s["approver_team_roles"]]) + (" (independent of the requester and author)." if s.get("independent", True) else "."))
    elif inst.state == "CHANGES_REQUESTED":
        outstanding.append("The requester must address the requested changes and resubmit.")
    elif inst.state == "APPROVED":
        outstanding.append("Approved. Complete the workflow to close it.")
    body = {"id": inst.id, "state": inst.state, "round": inst.round, "stage_index": inst.stage_index, "resource_type": inst.resource_type, "resource_id": inst.resource_id,
            "repository_id": inst.repository_id, "team_id": inst.team_id, "title": inst.title, "priority": inst.priority, "risk_level": inst.risk_level,
            "requested_by": inst.requested_by, "requested_by_handle": names.get(inst.requested_by or ""), "definition_key": inst.definition_snapshot.get("key"),
            "definition_version": inst.definition_version, "mode": inst.definition_snapshot.get("mode"), "stages": stage_view, "outstanding_actions": outstanding,
            "submitted_at": inst.submitted_at, "completed_at": inst.completed_at, "created_at": inst.created_at, "updated_at": inst.updated_at}
    if detail:
        decs = db.scalars(select(WorkflowDecision).where(WorkflowDecision.instance_id == inst.id).order_by(WorkflowDecision.created_at, WorkflowDecision.id).limit(200)).all()
        unames = _uname(db, {d.user_id for d in decs})
        body["decisions"] = [{"id": d.id, "round": d.round, "stage_index": d.stage_index, "user_id": d.user_id, "user_handle": unames.get(d.user_id or ""), "decision": d.decision,
                              "justification": d.justification, "created_at": d.created_at} for d in decs]
        latest = db.execute(select(PolicyEvaluation).where(PolicyEvaluation.instance_id == inst.id).order_by(PolicyEvaluation.evaluated_at.desc(), PolicyEvaluation.id.desc()).limit(60)).scalars().all()
        seen: dict[str, PolicyEvaluation] = {}
        for e in latest:
            seen.setdefault(e.policy_key, e)
        body["policy_evaluations"] = [{"id": e.id, "policy_key": e.policy_key, "policy_version": e.policy_version, "status": e.status, "blocking": e.blocking, "trigger": e.trigger,
                                       "explanation": e.result.get("explanation"), "evaluated_at": e.evaluated_at} for e in seen.values()]
        body["resource_available"] = res_mod.resolve(db, inst.workspace_id, inst.resource_type, inst.resource_id) is not None
    _ = Team, TeamMember
    return body


def list_instances(db: Session, p: Principal, *, state: str | None, team_id: str | None, repository_id: str | None, mine: bool, pending_for_me: bool, resource_type: str | None,
                   offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    stmt = select(WorkflowInstance).where(WorkflowInstance.workspace_id == p.workspace_id)
    if not p.org_wide:
        teams = list(p.team_roles) or ["-"]
        decided = select(WorkflowDecision.instance_id).where(WorkflowDecision.user_id == p.user_id)
        assigned = select(ReviewQueueItem.instance_id).where(ReviewQueueItem.assignee_user_id == p.user_id, ReviewQueueItem.instance_id.is_not(None))
        stmt = stmt.where(WorkflowInstance.team_id.in_(teams) | (WorkflowInstance.requested_by == p.user_id) | WorkflowInstance.id.in_(decided) | WorkflowInstance.id.in_(assigned))
    if state:
        stmt = stmt.where(WorkflowInstance.state == state)
    if team_id:
        stmt = stmt.where(WorkflowInstance.team_id == team_id)
    if repository_id:
        stmt = stmt.where(WorkflowInstance.repository_id == repository_id)
    if resource_type:
        stmt = stmt.where(WorkflowInstance.resource_type == resource_type)
    if mine:
        stmt = stmt.where(WorkflowInstance.requested_by == p.user_id)
    if pending_for_me:
        cands = db.scalars(stmt.where(WorkflowInstance.state == "IN_REVIEW").order_by(WorkflowInstance.created_at, WorkflowInstance.id).limit(500)).all()
        decided_ids = {(d.instance_id, d.round, d.stage_index) for d in db.scalars(select(WorkflowDecision).where(WorkflowDecision.user_id == p.user_id, WorkflowDecision.instance_id.in_([c.id for c in cands]))).all()} if cands else set()
        pending = []
        _, cache = _batch(db, list(cands))
        for inst in cands:
            stages = _stages(inst)
            for i in _open_stages(db, inst, cache):
                if eligible_for_stage(p, inst, stages[i]) and not _independent_blocked(inst, stages[i], p.user_id) and (inst.id, inst.round, i) not in decided_ids:
                    pending.append(inst)
                    break
        total = len(pending)
        page = pending[offset:offset + limit]
        b = _batch(db, page)
        return [view(db, p, i, batch=b) for i in page], total
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(WorkflowInstance.created_at.desc(), WorkflowInstance.id).offset(offset).limit(limit)).all()
    b = _batch(db, list(rows))
    return [view(db, p, i, batch=b) for i in rows], total


def events(db: Session, p: Principal, instance_id: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    inst = get_instance(db, p, instance_id)
    stmt = select(WorkflowEvent).where(WorkflowEvent.instance_id == inst.id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(WorkflowEvent.created_at, WorkflowEvent.id).offset(offset).limit(limit)).all()
    names = _uname(db, {e.actor_user_id for e in rows})
    return [{"id": e.id, "event_type": e.event_type, "from_state": e.from_state, "to_state": e.to_state, "actor_user_id": e.actor_user_id, "actor_handle": names.get(e.actor_user_id or ""),
             "data": e.data, "at": e.created_at} for e in rows], total


def policy_status(db: Session, p: Principal, instance_id: str) -> dict[str, Any]:
    """Read-only preview of what would block approval/completion right now (nothing is recorded)."""
    inst = get_instance(db, p, instance_id)
    try:
        out = check_policies(db, inst, "complete" if inst.state == "APPROVED" else "approve", p.user_id)
    except ConflictError as e:
        return {"available": False, "reason": e.message, "results": [], "blocked": True, "blockers": []}
    return {"available": True, **_public(out)}
