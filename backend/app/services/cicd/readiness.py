"""Release-readiness: a transparent decision that *reuses* the quality gate and adds revision, freshness and policy criteria.

Outcomes and precedence (strongest first): NOT_READY > BLOCKED > INCOMPLETE > READY.
* NOT_READY - a required criterion definitively failed.
* BLOCKED - essential evidence is missing, stale, for another revision, or could not be produced.
* INCOMPLETE - configured non-blocking checks are unresolved (or the gate itself is only partly evidenced).
* READY - every required criterion has verified evidence for the exact revision and is satisfied.
Missing evidence can never be READY. READY is decision support, not a guarantee.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import ChangeRequest, CiReadinessPolicy, CiWorkflowRun, CvCheck, CvRun, Repository
from app.security.auth import RequestContext
from app.services import audit
from app.services.cicd.errors import CiForbidden, CiInvalid
from app.services.verification import policy as cv_policy
from app.services.verification import service as cv_service

PRECEDENCE = ("NOT_READY", "BLOCKED", "INCOMPLETE", "READY")
ADMIN_ROLES = {"owner"}
CHECK_IDS = ("syntax", "static_analysis", "tests", "coverage")
SEVERITIES = ("critical", "high", "medium", "low", "info")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadinessRules(_Strict):
    max_evidence_age_hours: int = Field(168, ge=1, le=24 * 365)
    required_checks: list[str] = Field(default_factory=lambda: ["syntax", "static_analysis", "tests"], max_length=8)
    optional_checks: list[str] = Field(default_factory=list, max_length=8)
    blocked_finding_severities: list[str] = Field(default_factory=lambda: ["critical", "high"], max_length=5)
    allow_unresolved_optional: bool = False
    require_migration_check: bool = False  # NOVA has no migration-check evidence source: requiring it yields BLOCKED
    require_deployment_checks: bool = False  # likewise: unit tests do not prove a deployment
    require_operational_health: bool = False  # opt in: no unresolved critical operational incident may be open
    require_security_scan: bool = False  # opt in: the security policy must PASS on a completed, clean-tree scan of the exact head


def defaults() -> dict[str, Any]:
    return ReadinessRules().model_dump()


def validate_rules(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CiInvalid("invalid_policy", "The readiness policy must be an object.")
    try:
        rules = ReadinessRules.model_validate(raw)
    except ValidationError as exc:
        errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()][:10]
        raise CiInvalid("invalid_policy", "The readiness policy is not valid.", errs) from None
    bad = [c for c in rules.required_checks + rules.optional_checks if c not in CHECK_IDS]
    bad += [s for s in rules.blocked_finding_severities if s not in SEVERITIES]
    if bad:
        raise CiInvalid("invalid_policy", f"Unknown check or severity: {', '.join(sorted(set(bad)))}.")
    if set(rules.required_checks) & set(rules.optional_checks):
        raise CiInvalid("invalid_policy", "A check cannot be both required and optional.")
    return rules.model_dump()


def current(db: Session, repo_id: str) -> tuple[int, dict[str, Any], CiReadinessPolicy | None]:
    row = db.scalar(select(CiReadinessPolicy).where(CiReadinessPolicy.repository_id == repo_id).order_by(CiReadinessPolicy.version.desc()).limit(1))
    return (0, defaults(), None) if row is None else (row.version, validate_rules(row.rules), row)


def history(db: Session, repo_id: str) -> list[CiReadinessPolicy]:
    return list(db.scalars(select(CiReadinessPolicy).where(CiReadinessPolicy.repository_id == repo_id).order_by(CiReadinessPolicy.version.desc()).limit(50)))


def update(db: Session, ctx: RequestContext, repo: Repository, raw: Any, note: str) -> CiReadinessPolicy:
    if ctx.role not in ADMIN_ROLES:
        raise CiForbidden("policy_admin_required", "Only a workspace owner may change the release-readiness policy.")
    rules = validate_rules(raw)
    version = (db.scalar(select(func.max(CiReadinessPolicy.version)).where(CiReadinessPolicy.repository_id == repo.id)) or 0) + 1
    row = CiReadinessPolicy(repository_id=repo.id, version=version, rules=rules, note=(note or "")[:300], created_by=ctx.user.id)
    db.add(row)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="ci.readiness_policy_updated", entity_type="ci_readiness_policy",
                 entity_id=repo.id, details={"repository_id": repo.id, "version": version})
    db.commit()
    return row


def _rule(rid: str, status: str, required: bool, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": rid, "status": status, "required": required, "detail": detail, "evidence": evidence or {}}


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def _severity(rule_id: str) -> str:
    try:
        from app.services.analysis.catalog import rule_index
        return rule_index()[rule_id][1].severity
    except Exception:  # noqa: BLE001
        return "unknown"


CHECK_STATUS = {"passed": "pass", "failed": "fail", "blocked": "blocked", "error": "blocked", "incomplete": "incomplete", "cancelled": "incomplete",
                "not_applicable": "blocked", "planned": "blocked", "running": "blocked"}
GATE_STATUS = {"PASS": "pass", "FAIL": "fail", "BLOCKED": "blocked", "INCOMPLETE": "incomplete"}


def evaluate(db: Session, settings: Any, repo: Repository, cr: ChangeRequest, wf: CiWorkflowRun, rules: dict[str, Any], version: int) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    run = db.get(CvRun, wf.cv_run_id) if wf.cv_run_id else None
    verified_head = (run.change_set or {}).get("head_sha") if run else None

    # 1. the evidence must be for the change request's *current* head (a new commit invalidates it)
    if wf.head_sha != cr.head_sha:
        out.append(_rule("revision_current", "blocked", True, f"This evidence is for {wf.head_sha[:8]}, but the change request is now at {cr.head_sha[:8]}. Verify the new revision.",
                         {"evidence_revision": wf.head_sha, "current_revision": cr.head_sha}))
    else:
        out.append(_rule("revision_current", "pass", True, f"Evidence applies to the current head {cr.head_sha[:8]}.", {"revision": cr.head_sha}))
    # 2. a finished verification run must exist and must describe exactly this revision
    if run is None or run.status != "completed":
        out.append(_rule("revision_verified", "blocked", True, "Verification has not finished, so there is no evidence." if run else "No verification run exists for this revision."))
    elif verified_head != wf.head_sha:
        out.append(_rule("revision_verified", "blocked", True, "The verification run did not examine this exact revision.", {"verified": verified_head, "expected": wf.head_sha}))
    else:
        out.append(_rule("revision_verified", "pass", True, f"Verification examined revision {wf.head_sha[:8]}.", {"cv_run_id": run.id}))
    if run is None or run.status != "completed":
        _security_rule(db, repo, cr, rules, out)
        _ops_rule(db, repo, rules, out)
        return _decide(out, wf, cr, version, rules, run)

    # 3. the quality gate, unchanged
    gs = GATE_STATUS.get(run.gate_outcome or "", "blocked")
    out.append(_rule("quality_gate", gs, True, f"The quality gate decided {run.gate_outcome or 'nothing'}: {run.reason or 'no reason recorded'}", {"cv_run_id": run.id, "gate": run.gate_outcome}))
    # 4. required / optional checks
    checks = {c.check_id: c for c in db.scalars(select(CvCheck).where(CvCheck.run_id == run.id))}
    for cid in rules["required_checks"]:
        c = checks.get(cid)
        if c is None:
            out.append(_rule(f"check:{cid}", "blocked", True, f"The required '{cid}' check was not part of this run."))
        else:
            out.append(_rule(f"check:{cid}", CHECK_STATUS.get(c.status, "blocked"), True, f"'{cid}': {c.status} - {str(c.result.get('detail') or '')[:200]}", {"check_status": c.status}))
    for cid in rules["optional_checks"]:
        c = checks.get(cid)
        if c is not None and c.status == "passed":
            out.append(_rule(f"check:{cid}", "pass", False, f"Optional '{cid}' passed."))
        elif rules["allow_unresolved_optional"]:
            out.append(_rule(f"check:{cid}", "n/a", False, f"Optional '{cid}' is unresolved ({c.status if c else 'not run'}); the policy allows that."))
        else:
            out.append(_rule(f"check:{cid}", "incomplete", True, f"Optional '{cid}' is unresolved ({c.status if c else 'not run'})."))
    # 5. disallowed new findings (severity comes rule catalogue; unknown severities are listed, never ignored silently)
    new = (run.static_result or {}).get("new_findings") or []
    sev = [(f.get("rule_id", "?"), _severity(str(f.get("rule_id", "")))) for f in new if isinstance(f, dict)]
    bad = [r for r, s in sev if s in rules["blocked_finding_severities"]]
    unknown = [r for r, s in sev if s == "unknown"]
    if (run.static_result or {}).get("available") is False:
        out.append(_rule("no_disallowed_findings", "blocked", True, "Static analysis was unavailable, so findings could not be assessed."))
    elif bad:
        out.append(_rule("no_disallowed_findings", "fail", True, f"{len(bad)} new finding(s) of a disallowed severity: {', '.join(sorted(set(bad)))}.", {"rules": sorted(set(bad))}))
    elif unknown:
        out.append(_rule("no_disallowed_findings", "incomplete", True, f"{len(unknown)} new finding(s) have no known severity: {', '.join(sorted(set(unknown)))}."))
    else:
        out.append(_rule("no_disallowed_findings", "pass", True, f"{len(sev)} new finding(s), none of a disallowed severity."))
    # 6. evidence age
    fin = _aware(run.finished_at)
    age_h = (utcnow().replace(tzinfo=UTC) - fin).total_seconds() / 3600 if fin else None
    if age_h is None or age_h > rules["max_evidence_age_hours"]:
        out.append(_rule("evidence_recent", "blocked", True, f"The evidence is {'of unknown age' if age_h is None else f'{age_h:.0f} h old'} (limit {rules['max_evidence_age_hours']} h).", {"age_hours": age_h}))
    else:
        out.append(_rule("evidence_recent", "pass", True, f"The evidence is {age_h:.1f} h old (limit {rules['max_evidence_age_hours']} h)."))
    # 7. compatible with the *current* quality-gate policy and unchanged source
    cv_version = cv_policy.current(db, repo.id)[0]
    out.append(_rule("policy_compatible", "pass" if run.policy_version == cv_version else "blocked", True,
                     f"The run used gate policy v{run.policy_version}; the current version is v{cv_version}.", {"run": run.policy_version, "current": cv_version}))
    fresh = cv_service.run_staleness(settings, repo, run)
    out.append(_rule("source_unchanged_since", "pass" if fresh["stale"] is False else "blocked" if fresh["stale"] else "incomplete", True,
                     "The checked-out source still matches what was verified." if fresh["stale"] is False else (fresh["reason"] or "The current source state could not be read.")))
    _security_rule(db, repo, cr, rules, out)
    _ops_rule(db, repo, rules, out)
    # 8. checks NOVA cannot evidence
    if rules["require_migration_check"]:
        out.append(_rule("migration_check", "blocked", True, "Policy requires a migration/packaging check, but NOVA has no evidence source for it."))
    if rules["require_deployment_checks"]:
        out.append(_rule("deployment_checks", "blocked", True, "Policy requires deployment checks; unit tests do not prove a deployment and NOVA runs none."))
    return _decide(out, wf, cr, version, rules, run)


def _security_rule(db: Session, repo: Repository, cr: ChangeRequest, rules: dict[str, Any], out: list[dict[str, Any]]) -> None:
    """Optional security gate. An indeterminate security decision is never treated as a pass: it maps through the security policy's explicit effect."""
    if not rules.get("require_security_scan"):
        return
    from app.models import SecScan
    from app.services.security import policy as sec_policy

    scan = db.scalar(select(SecScan).where(SecScan.repository_id == repo.id, SecScan.status == "completed", SecScan.head_sha == cr.head_sha, SecScan.dirty.is_(False))
                     .order_by(SecScan.created_at.desc(), SecScan.id.desc()).limit(1))
    if scan is None:
        out.append(_rule("security_policy", "blocked", True, f"No completed security scan of a clean working tree exists for revision {cr.head_sha[:8]}. Run a security scan on this revision."))
        return
    v, srules, _ = sec_policy.current(db, repo.id)
    d = sec_policy.evaluate(db, repo, scan, srules, v, None, persist=False)
    ev = {"scan_id": scan.id, "security_policy_version": v, "security_status": d["status"], "missing_data": d["missing_data"][:10]}
    if d["status"] == "pass":
        out.append(_rule("security_policy", "pass", True, f"Security policy v{v} passed on the scan of {cr.head_sha[:8]}.", ev))
    elif d["status"] == "fail":
        out.append(_rule("security_policy", "fail", True, d["explanation"][:300], ev))
    else:
        eff = d.get("indeterminate_effect", "block")
        if eff == "allow":
            out.append(_rule("security_policy", "n/a", False, "Security policy is INDETERMINATE; the policy explicitly allows that. " + d["explanation"][:200], ev))
        else:
            out.append(_rule("security_policy", "incomplete" if eff == "incomplete" else "blocked", True, d["explanation"][:300], ev))


def _ops_rule(db: Session, repo: Repository, rules: dict[str, Any], out: list[dict[str, Any]]) -> None:
    """Optional operational-health gate: fails while a critical operational incident is unresolved. Alerts are evaluated on request, so a pass means
    'no open critical incident is recorded', not 'the system is proven healthy'."""
    if not rules.get("require_operational_health"):
        return
    from app.models import OpsIncident

    open_crit = db.scalars(select(OpsIncident).where(OpsIncident.workspace_id == repo.workspace_id, OpsIncident.severity == "critical", OpsIncident.status != "resolved").limit(5)).all()
    if open_crit:
        out.append(_rule("operational_health", "fail", True, f"{len(open_crit)} critical operational incident(s) are unresolved.", {"incident_ids": [i.id for i in open_crit], "titles": [i.title for i in open_crit]}))
    else:
        out.append(_rule("operational_health", "pass", True, "No unresolved critical operational incident is recorded (alert evaluation is on request, so this is not proof of health)."))


def _decide(out: list[dict[str, Any]], wf: CiWorkflowRun, cr: ChangeRequest, version: int, rules: dict[str, Any], run: CvRun | None) -> dict[str, Any]:
    req = {r["status"] for r in out if r["required"]}
    if "fail" in req:
        outcome, why = "NOT_READY", "A required criterion failed."
    elif "blocked" in req:
        outcome, why = "BLOCKED", "Essential evidence is missing, stale or could not be produced."
    elif "incomplete" in req:
        outcome, why = "INCOMPLETE", "Some configured checks are unresolved."
    elif "pass" not in req:
        outcome, why = "BLOCKED", "No required criterion produced evidence."
    else:
        outcome, why = "READY", "Every required criterion has verified evidence for this exact revision."
    return {"outcome": outcome, "reason": why, "revision": wf.head_sha, "change_request_head": cr.head_sha, "policy_version": version, "policy": rules,
            "precedence": list(PRECEDENCE), "rules": out, "cv_run_id": run.id if run else None, "evaluated_at": utcnow().isoformat(),
            "meaning": ("READY is decision support for this revision under this policy. It is not a guarantee that the software is defect-free or suitable for every production environment."
                        if outcome == "READY" else why)}
