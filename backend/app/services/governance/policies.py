"""Declarative governance policies: schemas, CRUD with versioning, and a deterministic evaluator.

Policies are data, never code: a ``policy_type`` selects a built-in evaluator and ``config`` is validated against that
type's pydantic schema (unknown fields rejected). Evaluation reads persisted evidence only. A rule whose evidence is missing
is ``unavailable`` - an explicit non-pass - and for blocking policies it blocks exactly like a failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    GovernancePolicy,
    GovernancePolicyVersion,
    PolicyEvaluation,
    Repository,
    Team,
    WorkflowDecision,
    WorkflowEvent,
    WorkflowInstance,
)
from app.observability import metrics as m
from app.security.auth import RequestContext
from app.security.authz import Principal
from app.services.governance import resources as res_mod
from app.services.governance.common import emit

logger = logging.getLogger("nova.governance")

SEVERITIES = ("informational", "warning", "blocking")
RiskLevel = Literal["low", "moderate", "high", "critical"]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequiredReviewers(_Cfg):
    min_reviewers: int = Field(1, ge=1, le=20)


class IndependentApprovals(_Cfg):
    min_approvals: int = Field(1, ge=1, le=20)


class VerificationRequired(_Cfg):
    allowed_outcomes: list[str] = Field(default_factory=lambda: ["PASS"], min_length=1, max_length=6)


class CodeReviewCompleted(_Cfg):
    pass


class MaxChangeRisk(_Cfg):
    max_level: RiskLevel = "high"


class SecurityChecksRequired(_Cfg):
    max_open_critical: int = Field(0, ge=0, le=1000)
    max_open_high: int = Field(0, ge=0, le=1000)


class EvidenceRequired(_Cfg):
    labels: list[str] = Field(min_length=1, max_length=20)


class ProhibitedTransition(_Cfg):
    transition: Literal["submit", "approve", "complete"]
    when_risk_at_least: RiskLevel | None = None


class ReleaseReadiness(_Cfg):
    allowed: list[str] = Field(default_factory=lambda: ["READY"], min_length=1, max_length=4)


class ExternalCiRequired(_Cfg):
    max_age_hours: int = Field(24, ge=1, le=720)
    sources: list[str] = Field(default_factory=list, max_length=10)


SCHEMAS: dict[str, type[_Cfg]] = {
    "required_reviewers": RequiredReviewers, "independent_approvals": IndependentApprovals, "verification_required": VerificationRequired,
    "code_review_completed": CodeReviewCompleted, "max_change_risk": MaxChangeRisk, "security_checks_required": SecurityChecksRequired,
    "evidence_required": EvidenceRequired, "prohibited_transition": ProhibitedTransition, "release_readiness": ReleaseReadiness, "external_ci_required": ExternalCiRequired,
}
DESCRIPTIONS = {
    "required_reviewers": "At least N distinct reviewers have approved.", "independent_approvals": "At least N approvals from people other than the requester and the resource author.",
    "verification_required": "Verification (quality gate / patch verification) has an allowed outcome for this revision.", "code_review_completed": "A code review has completed for this revision.",
    "max_change_risk": "The assessed change risk does not exceed a level.", "security_checks_required": "A completed security scan exists for this revision with no more than the allowed open critical/high findings.",
    "evidence_required": "Named evidence items have been attached to the workflow.", "prohibited_transition": "A workflow transition is forbidden (optionally only above a risk level).",
    "release_readiness": "The release-readiness decision for the change request is acceptable.",
    "external_ci_required": "Fresh, successful external CI evidence exists for this revision (missing, pending or stale evidence never passes).",
}


def validate_config(policy_type: str, config: Any) -> dict[str, Any]:
    schema = SCHEMAS.get(policy_type)
    if schema is None:
        raise ValidationFailedError("Unknown policy type.", {"allowed": sorted(SCHEMAS)})
    try:
        return schema.model_validate(config if config is not None else {}).model_dump()
    except ValidationError as e:
        raise ValidationFailedError("The policy configuration is not valid.", [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()]) from None


# ---------------------------------------------------------------------------------------------- CRUD

def snapshot(p: GovernancePolicy) -> dict[str, Any]:
    return {"key": p.key, "name": p.name, "description": p.description, "policy_type": p.policy_type, "resource_types": p.resource_types, "team_id": p.team_id,
            "repository_id": p.repository_id, "severity": p.severity, "enabled": p.enabled, "config": p.config, "effective_from": p.effective_from.isoformat() if p.effective_from else None}


def view(p: GovernancePolicy) -> dict[str, Any]:
    return {**snapshot(p), "id": p.id, "version": p.version, "created_by": p.created_by, "updated_by": p.updated_by, "created_at": p.created_at, "updated_at": p.updated_at}


def _need_manage(pr: Principal, severity: str) -> None:
    if severity == "blocking":
        pr.require("policy.manage")  # blocking policies: organization administrators only
    elif not (pr.has_org("policy.manage") or pr.has_org("policy.manage_advisory")):
        pr.deny("policy.manage_advisory")


def _check_scope(db: Session, ctx: RequestContext, team_id: str | None, repository_id: str | None, resource_types: list[str]) -> None:
    for rt in resource_types:
        if rt not in res_mod.RESOURCE_TYPES:
            raise ValidationFailedError("Unsupported resource type.", {"allowed": list(res_mod.RESOURCE_TYPES)})
    if team_id and db.scalar(select(Team.id).where(Team.id == team_id, Team.workspace_id == ctx.workspace.id)) is None:
        raise ValidationFailedError("Unknown team.")
    if repository_id and db.scalar(select(Repository.id).where(Repository.id == repository_id, Repository.workspace_id == ctx.workspace.id)) is None:
        raise ValidationFailedError("Unknown repository.")


def create_policy(db: Session, ctx: RequestContext, pr: Principal, data: dict[str, Any]) -> GovernancePolicy:
    if data["severity"] not in SEVERITIES:
        raise ValidationFailedError("Unknown severity.", {"allowed": list(SEVERITIES)})
    _need_manage(pr, data["severity"])
    cfg = validate_config(data["policy_type"], data.get("config"))
    _check_scope(db, ctx, data.get("team_id"), data.get("repository_id"), data.get("resource_types") or [])
    pol = GovernancePolicy(workspace_id=ctx.workspace.id, key=data["key"], name=data["name"].strip(), description=(data.get("description") or "").strip(), policy_type=data["policy_type"],
                           resource_types=list(data.get("resource_types") or []), team_id=data.get("team_id"), repository_id=data.get("repository_id"), severity=data["severity"],
                           enabled=bool(data.get("enabled", True)), config=cfg, effective_from=data.get("effective_from"), created_by=ctx.user.id, updated_by=ctx.user.id, version=1)
    db.add(pol)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("A policy with this key already exists.") from None
    db.add(GovernancePolicyVersion(policy_id=pol.id, version=1, snapshot=snapshot(pol), changed_by=ctx.user.id, note="created"))
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action="policy.created", entity_type="policy", entity_id=pol.id, details={"key": pol.key, "severity": pol.severity, "type": pol.policy_type})
    db.commit()
    return pol


def get_policy(db: Session, workspace_id: str, policy_id: str) -> GovernancePolicy:
    pol = db.scalar(select(GovernancePolicy).where(GovernancePolicy.id == policy_id, GovernancePolicy.workspace_id == workspace_id))
    if pol is None:
        raise NotFoundError("Policy not found.")
    return pol


def update_policy(db: Session, ctx: RequestContext, pr: Principal, policy_id: str, changes: dict[str, Any], note: str) -> GovernancePolicy:
    pol = get_policy(db, ctx.workspace.id, policy_id)
    new_sev = changes.get("severity", pol.severity)
    if new_sev not in SEVERITIES:
        raise ValidationFailedError("Unknown severity.", {"allowed": list(SEVERITIES)})
    # editing or disabling a blocking policy, or making a policy blocking, needs blocking-policy authority
    _need_manage(pr, "blocking" if "blocking" in (pol.severity, new_sev) else new_sev)
    before = snapshot(pol)
    for f in ("name", "description", "severity", "enabled", "resource_types", "team_id", "repository_id", "effective_from"):
        if f in changes and changes[f] is not None or (f == "effective_from" and f in changes):
            setattr(pol, f, changes[f].strip() if isinstance(changes[f], str) else changes[f])
    if "config" in changes and changes["config"] is not None:
        pol.config = validate_config(pol.policy_type, changes["config"])
    _check_scope(db, ctx, pol.team_id, pol.repository_id, pol.resource_types or [])
    after = snapshot(pol)
    if after == before:
        return pol
    pol.version += 1
    pol.updated_by = ctx.user.id
    db.add(GovernancePolicyVersion(policy_id=pol.id, version=pol.version, snapshot=snapshot(pol), changed_by=ctx.user.id, note=note[:500]))
    action = "policy.updated"
    if before["enabled"] != after["enabled"]:
        action = "policy.enabled" if after["enabled"] else "policy.disabled"
    emit(db, workspace_id=ctx.workspace.id, actor=ctx.user.id, action=action, entity_type="policy", entity_id=pol.id,
         details={"key": pol.key, "version": pol.version, "changed": sorted(k for k in after if after[k] != before[k]), "severity": pol.severity})
    db.commit()
    return pol


def list_policies(db: Session, workspace_id: str, *, policy_type: str | None, severity: str | None, enabled: bool | None, offset: int, limit: int) -> tuple[list[GovernancePolicy], int]:
    stmt = select(GovernancePolicy).where(GovernancePolicy.workspace_id == workspace_id)
    if policy_type:
        stmt = stmt.where(GovernancePolicy.policy_type == policy_type)
    if severity:
        stmt = stmt.where(GovernancePolicy.severity == severity)
    if enabled is not None:
        stmt = stmt.where(GovernancePolicy.enabled == enabled)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    return list(db.scalars(stmt.order_by(GovernancePolicy.key).offset(offset).limit(limit)).all()), total


# ---------------------------------------------------------------------------------------------- evaluation

@dataclass
class EvalContext:
    db: Session
    workspace_id: str
    resource: res_mod.Resource
    instance: WorkflowInstance | None = None
    transition: str | None = None
    now: datetime = field(default_factory=utcnow)
    _decisions: list[WorkflowDecision] | None = None
    _labels: set[str] | None = None

    def approvals(self) -> list[WorkflowDecision]:
        if self.instance is None:
            return []
        if self._decisions is None:
            self._decisions = list(self.db.scalars(select(WorkflowDecision).where(WorkflowDecision.instance_id == self.instance.id, WorkflowDecision.round == self.instance.round,
                                                                                  WorkflowDecision.decision == "approve")).all())
        return self._decisions

    def labels(self) -> set[str] | None:
        if self.instance is None:
            return None
        if self._labels is None:
            rows = self.db.scalars(select(WorkflowEvent.data).where(WorkflowEvent.instance_id == self.instance.id, WorkflowEvent.event_type == "evidence_added")).all()
            self._labels = {str(d.get("label")) for d in rows if isinstance(d, dict) and d.get("label")}
        return self._labels


def _rule(rule_id: str, status: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"rule_id": rule_id, "status": status, "detail": detail, "evidence": evidence or {}}


def _from_evidence(ev: dict[str, Any], rule_id: str, test: Any, pass_detail: str, fail_detail: str) -> dict[str, Any]:
    if ev["state"] != "ok":
        return _rule(rule_id, "unavailable", ev["detail"])
    return _rule(rule_id, "pass" if test(ev["data"]) else "fail", pass_detail if test(ev["data"]) else fail_detail, ev["data"])


def _eval_rules(pol: GovernancePolicy, c: EvalContext) -> list[dict[str, Any]]:
    cfg, r = pol.config, c.resource
    t = pol.policy_type
    if t == "required_reviewers":
        n = len({d.user_id for d in c.approvals()})
        return [_rule("reviewer_count", "pass" if n >= cfg["min_reviewers"] else "fail", f"{n} of {cfg['min_reviewers']} required reviewer approval(s) recorded.", {"approvals": n})]
    if t == "independent_approvals":
        excluded = {c.instance.requested_by if c.instance else None, c.instance.resource_author if c.instance else None} - {None}
        n = len({d.user_id for d in c.approvals() if d.user_id not in excluded})
        return [_rule("independent_approvals", "pass" if n >= cfg["min_approvals"] else "fail", f"{n} of {cfg['min_approvals']} required independent approval(s) recorded (the requester and resource author do not count).", {"independent_approvals": n})]
    if t == "verification_required":
        allowed = [a.upper() for a in cfg["allowed_outcomes"]]
        return [_from_evidence(res_mod.verification(c.db, r), "verification_outcome", lambda d: str(d.get("outcome", "")).upper() in allowed, "Verification outcome is acceptable.", f"Verification outcome is not one of {allowed}.")]
    if t == "code_review_completed":
        return [_from_evidence(res_mod.code_review(c.db, r), "code_review_completed", lambda d: True, "A code review has completed.", "")]
    if t == "max_change_risk":
        lim = res_mod.RISK_ORDER[cfg["max_level"]]
        return [_from_evidence(res_mod.risk(c.db, r), "change_risk", lambda d: res_mod.RISK_ORDER[d["level"]] <= lim, f"Change risk is within '{cfg['max_level']}'.", f"Change risk exceeds '{cfg['max_level']}'.")]
    if t == "security_checks_required":
        ev = res_mod.security(c.db, r)
        if ev["state"] == "ok" and ev["data"].get("state") != "complete":
            return [_rule("security_scan", "unavailable", "The latest security scan is incomplete, so it cannot be relied on.", ev["data"])]
        sev = (ev["data"].get("open_by_severity") or {}) if ev["state"] == "ok" else {}
        return [_from_evidence(ev, "security_scan", lambda d: int(sev.get("critical", 0)) <= cfg["max_open_critical"] and int(sev.get("high", 0)) <= cfg["max_open_high"],
                               "Open critical/high findings are within the limits.", f"Open findings exceed the limits (critical {sev.get('critical', 0)}/{cfg['max_open_critical']}, high {sev.get('high', 0)}/{cfg['max_open_high']}).")]
    if t == "evidence_required":
        have = c.labels()
        if have is None:
            return [_rule("evidence_items", "unavailable", "No workflow exists yet, so attached evidence cannot be checked.")]
        missing = [lab for lab in cfg["labels"] if lab not in have]
        return [_rule("evidence_items", "fail" if missing else "pass", ("Missing evidence: " + ", ".join(missing)) if missing else "All required evidence items are attached.", {"missing": missing})]
    if t == "prohibited_transition":
        if c.transition != cfg["transition"]:
            return [_rule("transition_allowed", "not_applicable", f"Applies to the '{cfg['transition']}' transition only.")]
        if not cfg.get("when_risk_at_least"):
            return [_rule("transition_allowed", "fail", f"The '{cfg['transition']}' transition is prohibited.")]
        ev = res_mod.risk(c.db, r)
        lim = res_mod.RISK_ORDER[cfg["when_risk_at_least"]]
        if ev["state"] != "ok":
            return [_rule("transition_allowed", "unavailable", "The transition is conditional on change risk, which cannot be determined: " + ev["detail"])]
        bad = res_mod.RISK_ORDER[ev["data"]["level"]] >= lim
        return [_rule("transition_allowed", "fail" if bad else "pass", f"Transition prohibited at risk '{ev['data']['level']}'." if bad else "Risk is below the prohibition threshold.", ev["data"])]
    if t == "release_readiness":
        allowed = [a.upper() for a in cfg["allowed"]]
        return [_from_evidence(res_mod.release_readiness(c.db, r), "release_readiness", lambda d: str(d.get("readiness", "")).upper() in allowed, "Release readiness is acceptable.", f"Release readiness is not one of {allowed}.")]
    if t == "external_ci_required":
        return [_from_evidence(res_mod.external_ci(c.db, r, cfg["max_age_hours"], cfg.get("sources") or None, c.now), "external_ci", lambda d: bool(d.get("passed")),
                               "External CI succeeded for this revision.", "An external CI run failed for this revision.")]
    return [_rule("known_type", "unavailable", f"Unknown policy type '{t}'; the policy cannot be evaluated.")]  # never silently ignored


def applies(pol: GovernancePolicy, c: EvalContext) -> bool:
    if not pol.enabled or (pol.effective_from is not None and _aware(pol.effective_from) > c.now):
        return False
    if pol.resource_types and c.resource.type not in pol.resource_types:
        return False
    if pol.repository_id and pol.repository_id != c.resource.repository_id:
        return False
    return not (pol.team_id and (c.instance is None or c.instance.team_id != pol.team_id))


def _aware(d: datetime) -> datetime:
    from datetime import UTC

    return d if d.tzinfo else d.replace(tzinfo=UTC)


def evaluate(pol: GovernancePolicy, c: EvalContext) -> dict[str, Any]:
    """Pure function of (policy row, persisted evidence, context). Same inputs give the same result."""
    rules = _eval_rules(pol, c) if applies(pol, c) else [_rule("applicable", "not_applicable", "The policy is disabled, not yet effective, or out of scope for this resource.")]
    statuses = {x["status"] for x in rules}
    raw = "fail" if "fail" in statuses else "unavailable" if "unavailable" in statuses else "pass" if "pass" in statuses else "not_applicable"
    status = raw
    if raw in ("fail", "unavailable") and pol.severity != "blocking":
        status = "warning"
    blocking = pol.severity == "blocking" and raw in ("fail", "unavailable")
    missing = [x["detail"] for x in rules if x["status"] in ("unavailable",)] + [x["detail"] for x in rules if x["status"] == "fail"]
    explain = {"pass": "All rules passed.", "not_applicable": rules[0]["detail"] if rules else "Not applicable.",
               "warning": "Not satisfied (non-blocking): " + "; ".join(missing), "fail": "Not satisfied: " + "; ".join(missing),
               "unavailable": "Cannot be evaluated: " + "; ".join(missing)}[status if status != "warning" else "warning"]
    return {"policy": {"id": pol.id, "key": pol.key, "name": pol.name, "version": pol.version, "type": pol.policy_type, "severity": pol.severity},
            "status": status, "raw_status": raw, "blocking": blocking, "rules": rules, "explanation": explain, "unmet": missing,
            "context": {"resource_type": c.resource.type, "resource_id": c.resource.id, "instance_id": c.instance.id if c.instance else None, "transition": c.transition},
            "evaluated_at": c.now.isoformat()}


def applicable_policies(db: Session, c: EvalContext, extra_keys: list[str] | None = None) -> list[GovernancePolicy]:
    pols = db.scalars(select(GovernancePolicy).where(GovernancePolicy.workspace_id == c.workspace_id, GovernancePolicy.enabled.is_(True)).order_by(GovernancePolicy.key)).all()
    return [p for p in pols if applies(p, c)]


def persist_results(db: Session, c: EvalContext, results: list[dict[str, Any]], *, trigger: str, actor: str | None) -> None:
    """Append evaluation records (kept with the policy version they were produced under)."""
    for r in results:
        pol = r["policy"]
        logger.info("policy_evaluated", extra={"event": "policy_evaluated", "policy_key": pol["key"], "policy_version": pol["version"], "policy_type": pol["type"], "status": r["status"],
                                               "blocking": r["blocking"], "trigger": trigger, "resource_type": c.resource.type, "resource_id": c.resource.id})
        db.add(PolicyEvaluation(workspace_id=c.workspace_id, policy_id=pol["id"], policy_key=pol["key"], policy_version=pol["version"], policy_type=pol["type"], severity=pol["severity"],
                                instance_id=c.instance.id if c.instance else None, resource_type=c.resource.type, resource_id=c.resource.id, status=r["status"],
                                blocking=r["blocking"], result=r, trigger=trigger, evaluated_by=actor, evaluated_at=c.now))


def evaluate_all(db: Session, c: EvalContext, *, trigger: str, actor: str | None, persist: bool = True) -> dict[str, Any]:
    """Evaluate every applicable policy, optionally record each result, and summarise whether anything blocks."""
    results = []
    for pol in applicable_policies(db, c):
        r = evaluate(pol, c)
        results.append(r)
        m.GOV_POLICY_EVALS.inc(policy_type=pol.policy_type, status=r["status"])
    if persist:
        persist_results(db, c, results, trigger=trigger, actor=actor)
    blocked = [r for r in results if r["blocking"]]
    return {"results": results, "blocked": bool(blocked), "blocking_policies": [r["policy"]["key"] for r in blocked], "evaluated_at": c.now.isoformat(), "count": len(results)}


DEFAULT_POLICIES: list[dict[str, Any]] = [
    {"key": "independent-approval", "name": "Independent approval", "policy_type": "independent_approvals", "severity": "blocking", "resource_types": [], "config": {"min_approvals": 1},
     "description": "At least one approval from someone other than the requester and the author."},
    {"key": "verification-passed", "name": "Verification must pass", "policy_type": "verification_required", "severity": "blocking", "resource_types": ["repair_patch", "refactor", "change_request"],
     "config": {"allowed_outcomes": ["PASS"]}, "description": "Verification evidence for this revision must have passed."},
    {"key": "code-review-completed", "name": "Code review completed", "policy_type": "code_review_completed", "severity": "warning", "resource_types": ["change_request"], "config": {},
     "description": "A completed code review should exist for the revision."},
    {"key": "risk-ceiling", "name": "Risk ceiling", "policy_type": "max_change_risk", "severity": "warning", "resource_types": ["code_review"], "config": {"max_level": "high"},
     "description": "Flags changes assessed above 'high' risk."},
]
