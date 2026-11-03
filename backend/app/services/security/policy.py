"""Deterministic security policy evaluation: pass / fail / indeterminate. Insufficient evidence is never a pass."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import Repository, SecComponent, SecEvaluation, SecException, SecFinding, SecPolicy, SecScan
from app.security.auth import RequestContext
from app.services import audit
from app.services.security import priority
from app.services.security.errors import SecForbidden, SecInvalid

POLICY_ID = "nova.security.policy"
SEVERITIES = ("critical", "high", "medium", "low", "info")
CONFS = ("high", "medium", "low")
SCAN_TYPES = ("dependencies", "code", "secrets")
ADMIN_ROLES = {"owner"}
Sev = Literal["critical", "high", "medium", "low", "info"]
Conf = Literal["high", "medium", "low"]


class Prohibited(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ecosystem: Literal["PyPI", "npm"]
    name: str = Field(min_length=1, max_length=214)
    reason: str = Field("", max_length=200)


class SecurityRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fail_on_severity: Sev = "high"
    min_confidence: Conf = "medium"
    fail_on_advisory_severity: Sev = "high"
    unspecified_advisory_severity: Sev = "high"
    possible_advisories: Literal["indeterminate", "fail", "ignore"] = "indeterminate"
    fail_on_secret_confidence: Conf = "high"  # noqa: S105
    require_inventory: bool = True
    require_advisory_data: bool = True
    required_scan_types: list[Literal["dependencies", "code", "secrets"]] = Field(default_factory=lambda: list(SCAN_TYPES), max_length=3)
    prohibited_packages: list[Prohibited] = Field(default_factory=list, max_length=100)
    prohibited_licenses: list[str] = Field(default_factory=list, max_length=50)
    max_scan_age_hours: int = Field(168, ge=1, le=24 * 365)
    indeterminate_effect: Literal["block", "incomplete", "allow"] = "block"
    priority_weights: dict[str, Any] = Field(default_factory=dict)


def defaults() -> dict[str, Any]:
    return SecurityRules().model_dump()


def validate_rules(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SecInvalid("invalid_policy", "The security policy must be an object.")
    try:
        rules = SecurityRules.model_validate(raw)
        priority.validate_overrides(rules.priority_weights)
    except ValidationError as exc:
        errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()][:10]
        raise SecInvalid("invalid_policy", "The security policy is not valid.", errs) from None
    except ValueError as exc:
        raise SecInvalid("invalid_policy", str(exc)) from None
    return rules.model_dump()


def current(db: Session, repo_id: str) -> tuple[int, dict[str, Any], SecPolicy | None]:
    row = db.scalar(select(SecPolicy).where(SecPolicy.repository_id == repo_id).order_by(SecPolicy.version.desc()).limit(1))
    return (0, defaults(), None) if row is None else (row.version, validate_rules(row.rules), row)


def history(db: Session, repo_id: str) -> list[SecPolicy]:
    return list(db.scalars(select(SecPolicy).where(SecPolicy.repository_id == repo_id).order_by(SecPolicy.version.desc()).limit(50)))


def update(db: Session, ctx: RequestContext, repo: Repository, raw: Any, note: str) -> SecPolicy:
    if ctx.role not in ADMIN_ROLES:
        raise SecForbidden("policy_admin_required", "Only a workspace owner may change the security policy.")
    rules = validate_rules(raw)
    version = (db.scalar(select(func.max(SecPolicy.version)).where(SecPolicy.repository_id == repo.id)) or 0) + 1
    row = SecPolicy(repository_id=repo.id, version=version, rules=rules, note=(note or "")[:300], created_by=ctx.user.id)
    db.add(row)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="security.policy_updated", entity_type="sec_policy", entity_id=repo.id,
                 details={"repository_id": repo.id, "version": version})
    db.commit()
    return row


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def _ge(order: tuple[str, ...], a: str, b: str) -> bool:
    """Is ``a`` at least as severe/confident as ``b`` (earlier in ``order``)?"""
    return order.index(a) <= order.index(b)


def _matches(sel: dict[str, Any], f: SecFinding) -> bool:
    t, v = sel.get("type"), str(sel.get("value", ""))
    if t == "finding":
        return f.id == v or f.fingerprint == v
    if t == "rule":
        return f.rule_id == v
    if t == "package":
        return (f.component_key or "").lower() == v.lower()
    if t == "advisory":
        return v in (f.evidence.get("advisory_ids") or [])
    return False


def _check(cid: str, status: str, detail: str, findings: list[str] | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "detail": detail, "findings": findings or []}


def evaluate(db: Session, repo: Repository, scan: SecScan, rules: dict[str, Any], version: int, actor: str | None, now: datetime | None = None, persist: bool = True) -> dict[str, Any]:
    now = now or utcnow()
    r = SecurityRules.model_validate(rules)
    cov = scan.coverage or {}
    summ = scan.summary or {}
    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    skipped: list[str] = []
    findings = list(db.scalars(select(SecFinding).where(SecFinding.repository_id == repo.id, SecFinding.last_seen_scan_id == scan.id)))
    open_f = [f for f in findings if f.status == "open"]
    excluded: dict[str, int] = {}
    for f in findings:
        if f.status != "open":
            excluded[f.status] = excluded.get(f.status, 0) + 1
    exceptions = list(db.scalars(select(SecException).where(SecException.repository_id == repo.id, SecException.revoked_at.is_(None))))
    active = [e for e in exceptions if (_aware(e.expires_at) or now) > _aware(now)]
    expired = [e for e in exceptions if (_aware(e.expires_at) or now) <= _aware(now)]
    applied: list[dict[str, Any]] = []
    expired_hit: list[dict[str, Any]] = []
    causes: list[dict[str, Any]] = []

    def excepted(f: SecFinding) -> bool:
        for e in active:
            if _matches(e.selector, f):
                applied.append({"exception_id": e.id, "finding_id": f.id, "selector": e.selector, "reason": e.reason, "expires_at": _aware(e.expires_at).isoformat()})  # type: ignore[union-attr]
                return True
        for e in expired:
            if _matches(e.selector, f):
                expired_hit.append({"exception_id": e.id, "finding_id": f.id, "selector": e.selector, "expired_at": _aware(e.expires_at).isoformat()})  # type: ignore[union-attr]
        return False

    def cause(f: SecFinding, why: str) -> None:
        causes.append({"finding_id": f.id, "rule_id": f.rule_id, "severity": f.severity, "confidence": f.confidence, "title": f.title, "location": f.rel_path, "line": f.line,
                       "component": f.component_key, "why": why})

    # 1. the scan itself
    if scan.status != "completed":
        checks.append(_check("scan_complete", "indeterminate", f"The scan ended as '{scan.status}'; its results cannot support a decision."))
        missing.append("a completed scan")
    elif summ.get("state") != "complete":
        checks.append(_check("scan_complete", "indeterminate", "The scan is partial: " + "; ".join((summ.get("incomplete_reasons") or ["some inputs could not be examined"])[:3])))
        missing.extend(summ.get("incomplete_reasons") or ["partial scan"])
    else:
        checks.append(_check("scan_complete", "pass", "The scan completed and examined all supported inputs within its limits."))
    ran = set(scan.scan_types or [])
    absent = [t for t in r.required_scan_types if t not in ran]
    if absent:
        checks.append(_check("required_scans", "indeterminate", f"Required scan type(s) were not run: {', '.join(absent)}."))
        missing.extend(f"{t} scan" for t in absent)
        skipped.extend(f"checks that need the {t} scan" for t in absent)
    else:
        checks.append(_check("required_scans", "pass", "All required scan types were run."))
    age = (_aware(now) - _aware(scan.finished_at or scan.created_at)).total_seconds() / 3600  # type: ignore[operator]
    checks.append(_check("scan_fresh", "pass" if age <= r.max_scan_age_hours else "indeterminate",
                         f"Scan finished {age:.1f} h ago (limit {r.max_scan_age_hours} h)." if age <= r.max_scan_age_hours else f"The scan is {age:.0f} h old (limit {r.max_scan_age_hours} h)."))
    if age > r.max_scan_age_hours:
        missing.append("a recent scan")

    # 2. inventory and advisory data
    if "dependencies" not in ran:
        for flag, cid, what in ((r.require_inventory, "inventory", "a dependency inventory"), (r.require_advisory_data, "advisory_data", "advisory data")):
            if flag:
                checks.append(_check(cid, "indeterminate", f"The policy requires {what}, but this scan did not include the dependencies scan."))
                missing.append(what)
                skipped.append(f"{cid} (dependencies scan not run)")
    if "dependencies" in ran:
        inv = cov.get("inventory") or {}
        bad = [f for f in (inv.get("files") or []) if f["status"] in ("malformed", "unsupported", "too_large", "skipped")]
        if r.require_inventory:
            if scan.status == "completed" and not bad and not inv.get("truncated"):
                checks.append(_check("inventory", "pass", f"{summ.get('components', 0)} component(s) inventoried from {len(inv.get('files') or [])} manifest file(s)."))
            else:
                checks.append(_check("inventory", "indeterminate", "The dependency inventory is incomplete: " + (", ".join(f"{f['path']} ({f['status']})" for f in bad[:5]) or "scan limits reached") + "."))
                missing.append("a complete dependency inventory")
        adv = cov.get("advisories") or {}
        un = adv.get("unassessed", 0)
        if r.require_advisory_data:
            if adv.get("providers_ok", 0) == 0:
                checks.append(_check("advisory_data", "indeterminate", "No advisory source answered, so no component could be assessed. No result is not a safe result."))
                missing.append("advisory data")
            elif un:
                checks.append(_check("advisory_data", "indeterminate", f"{un} of {adv.get('components', 0)} component(s) could not be assessed (unpinned or unparseable versions, unsupported ecosystem)."))
                missing.append(f"{un} unassessed component(s)")
            else:
                checks.append(_check("advisory_data", "pass", f"All {adv.get('components', 0)} component(s) were assessed against {adv.get('providers_ok', 0)} source(s). Sources: {', '.join(adv.get('sources') or [])}."))
        dep = [f for f in open_f if f.kind == "dependency"]
        fail_ids: list[str] = []
        poss_ids: list[str] = []
        for f in dep:
            sev = f.severity
            if f.evidence.get("severity_source") == "unspecified":
                sev = r.unspecified_advisory_severity
            if f.evidence.get("match_status") == "possible":
                if r.possible_advisories != "ignore" and not excepted(f):
                    (fail_ids if r.possible_advisories == "fail" else poss_ids).append(f.id)
                    cause(f, "possible match: the installed version could not be evaluated against the advisory")
                continue
            if _ge(SEVERITIES, sev, r.fail_on_advisory_severity) and not excepted(f):
                fail_ids.append(f.id)
                cause(f, f"confirmed advisory match at {sev} severity (threshold {r.fail_on_advisory_severity})")
        checks.append(_check("dependency_advisories", "fail" if fail_ids else ("indeterminate" if poss_ids else "pass"),
                             (f"{len(fail_ids)} advisory match(es) at or above {r.fail_on_advisory_severity}." if fail_ids else
                              f"{len(poss_ids)} possible advisory match(es) could not be confirmed." if poss_ids else "No applicable advisory at or above the threshold in the queried sources (this is not a claim that the dependencies are safe)."),
                             fail_ids or poss_ids))
        if poss_ids and not fail_ids:
            missing.append("version information for possible advisory matches")
        comps = [{"ecosystem": c.ecosystem, "name": c.name, "license": c.license} for c in db.scalars(select(SecComponent).where(SecComponent.scan_id == scan.id))]
        banned = [c for c in comps if any(c["ecosystem"] == p.ecosystem and c["name"].lower() == p.name.lower().replace("_", "-") or c["name"].lower() == p.name.lower() and c["ecosystem"] == p.ecosystem for p in r.prohibited_packages)]
        if r.prohibited_packages:
            checks.append(_check("prohibited_packages", "fail" if banned else "pass", ("Prohibited package(s) present: " + ", ".join(f"{c['ecosystem']}:{c['name']}" for c in banned[:5])) if banned else "No prohibited package is in the inventory."))
            for c in banned:
                causes.append({"finding_id": None, "rule_id": "POLICY-PROHIBITED-PACKAGE", "severity": "high", "confidence": "high", "title": f"Prohibited package {c['name']}", "location": None, "line": None,
                               "component": f"{c['ecosystem']}:{c['name']}", "why": "listed in the policy"})
        if r.prohibited_licenses:
            lic_known = [c for c in comps if c.get("license")]
            lic_missing = [c for c in comps if not c.get("license")]
            bad_lic = [c for c in lic_known if c["license"] in r.prohibited_licenses]
            if bad_lic:
                checks.append(_check("licenses", "fail", "Prohibited license(s): " + ", ".join(f"{c['name']} ({c['license']})" for c in bad_lic[:5])))
            elif lic_missing:
                checks.append(_check("licenses", "indeterminate", f"{len(lic_missing)} component(s) have no license metadata in their manifests, so the license policy cannot be evaluated for them."))
                missing.append("license metadata")
            else:
                checks.append(_check("licenses", "pass", "No component carries a prohibited license."))
    else:
        skipped.append("dependency inventory and advisory checks (dependencies scan not run)")

    # 3. code and secrets
    if "code" in ran:
        bad_ids = []
        for f in (x for x in open_f if x.kind == "code"):
            if _ge(SEVERITIES, f.severity, r.fail_on_severity) and _ge(CONFS, f.confidence, r.min_confidence) and not excepted(f):
                bad_ids.append(f.id)
                cause(f, f"{f.severity} severity, {f.confidence} confidence (threshold {r.fail_on_severity}/{r.min_confidence})")
        below = [x for x in open_f if x.kind == "code" and x.id not in bad_ids and _ge(SEVERITIES, x.severity, r.fail_on_severity) and not _ge(CONFS, x.confidence, r.min_confidence)]
        checks.append(_check("code_findings", "fail" if bad_ids else "pass",
                             f"{len(bad_ids)} open code finding(s) at or above {r.fail_on_severity} severity with at least {r.min_confidence} confidence." if bad_ids else
                             "No open code finding meets the failure threshold." + (f" {len(below)} lower-confidence finding(s) at or above the severity threshold need review." if below else ""), bad_ids))
    else:
        skipped.append("code security checks (code scan not run)")
    if "secrets" in ran:
        bad_ids = []
        for f in (x for x in open_f if x.kind == "secret"):
            if _ge(CONFS, f.confidence, r.fail_on_secret_confidence) and not excepted(f):
                bad_ids.append(f.id)
                cause(f, f"unresolved {f.confidence}-confidence secret finding")
        checks.append(_check("secrets", "fail" if bad_ids else "pass", f"{len(bad_ids)} unresolved secret finding(s) with at least {r.fail_on_secret_confidence} confidence." if bad_ids else
                             "No unresolved secret finding meets the failure threshold (detection is best-effort and cannot find every credential).", bad_ids))
    else:
        skipped.append("secret checks (secrets scan not run)")

    statuses = [c["status"] for c in checks]
    status = "fail" if "fail" in statuses else ("indeterminate" if "indeterminate" in statuses else "pass")
    bad = [c for c in checks if c["status"] in ("fail", "indeterminate")]
    explanation = {"pass": "PASS: every evaluated check passed. Pass means the configured checks found nothing at or above the thresholds in the examined inputs; it is not a guarantee of security.",
                   "fail": "FAIL: " + " ".join(c["detail"] for c in bad if c["status"] == "fail")[:600],
                   "indeterminate": "INDETERMINATE: there is not enough evidence for a decision. " + " ".join(c["detail"] for c in bad)[:600]}[status]
    decision = {"policy_id": POLICY_ID, "policy_version": version, "status": status, "explanation": explanation, "checks": checks, "causes": causes[:200], "missing_data": sorted(set(missing)),
                "skipped_checks": skipped, "exceptions_applied": applied, "expired_exceptions": expired_hit, "excluded_by_triage": excluded, "evaluated_at": _aware(now).isoformat(),  # type: ignore[union-attr]
                "revision": {"head_sha": scan.head_sha, "dirty": scan.dirty}, "scan_id": scan.id, "indeterminate_effect": r.indeterminate_effect,
                "meaning": "Decision support: pass/fail applies to the configured checks over the examined inputs only. Indeterminate means evidence is missing, never success."}
    if persist:
        row = SecEvaluation(repository_id=repo.id, scan_id=scan.id, policy_version=version, status=status, decision=decision, head_sha=scan.head_sha, created_by=actor)
        db.add(row)
        db.flush()
        decision["evaluation_id"] = row.id
    return decision


def stale_after(scan: SecScan, hours: int) -> datetime:
    return (_aware(scan.finished_at or scan.created_at) or utcnow()) + timedelta(hours=hours)  # type: ignore[operator]
