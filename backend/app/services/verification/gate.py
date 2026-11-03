"""Quality-gate evaluation.

Outcomes: ``PASS`` / ``FAIL`` / ``BLOCKED`` / ``INCOMPLETE``.  Precedence (strongest first): FAIL > BLOCKED > INCOMPLETE > PASS.

* FAIL: a required criterion definitively failed (a failing test, a new static finding over the limit, coverage below a threshold, ...).
* BLOCKED: required evidence could not be produced (nothing to run, tool unavailable, prerequisite or baseline missing, runner error).
* INCOMPLETE: evidence was only partly produced (cancelled, timed out, source changed during the run, changed files without a verifier,
  or nothing applicable at all).
* PASS: every required criterion has verified evidence and satisfies the policy, and at least one required criterion ran.
Missing evidence therefore can never produce PASS.
"""
from __future__ import annotations

from typing import Any

from app.services.verification.policy import PRECEDENCE

CHECK_TO_RULE = {"passed": "pass", "failed": "fail", "blocked": "blocked", "error": "blocked", "unavailable": "blocked",
                 "cancelled": "incomplete", "incomplete": "incomplete", "planned": "incomplete", "running": "incomplete", "skipped": "incomplete",
                 "not_applicable": "n/a"}


def _rule(rid: str, status: str, required: bool, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": rid, "status": status, "required": required, "detail": detail, "evidence": evidence or {}}


def evaluate(rules: dict[str, Any], checks: list[dict[str, Any]], regression: dict[str, Any], coverage: dict[str, Any], static: dict[str, Any],
             plan: dict[str, Any], baseline_compat: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    by = {c["check_id"]: c for c in checks}

    # 1. every planned check, by its requirement level
    for c in checks:
        st = CHECK_TO_RULE.get(c["status"], "incomplete")
        if c["check_id"] == "tests" and c["status"] == "failed" and rules["tests"]["allow_preexisting_failures"]:
            counts = regression.get("counts", {})
            if counts.get("new", 0) == 0 and counts.get("unclassified", 0) == 0 and counts.get("flaky", 0) == 0 and counts.get("pre_existing", 0) > 0:
                st = "pass"
        detail = (c.get("result") or {}).get("detail") or c.get("reason") or ""
        out.append(_rule(f"check:{c['check_id']}", st, bool(c["required"]), detail, {"check_status": c["status"]}))

    # 2. tests: minimum executed, skipped limit
    tests = by.get("tests")
    if tests and tests["status"] in ("passed", "failed"):
        executed = (tests.get("result") or {}).get("tests_executed", 0)
        need = rules["tests"]["min_tests_executed"]
        out.append(_rule("tests_executed", "pass" if executed >= need else "blocked", True,
                         f"{executed} test(s) executed (policy minimum {need}).", {"executed": executed, "minimum": need}))
        ms = rules["tests"]["max_skipped"]
        if ms is not None:
            skipped = (tests.get("result") or {}).get("tests_skipped", 0)
            out.append(_rule("max_skipped", "pass" if skipped <= ms else "fail", True, f"{skipped} skipped (limit {ms}).", {"skipped": skipped}))

    # 3. no new failures (classification)
    if regression.get("available"):
        counts = regression.get("counts", {})
        bad = counts.get("new", 0) + counts.get("flaky", 0) + counts.get("unclassified", 0)
        pre = counts.get("pre_existing", 0)
        if bad:
            out.append(_rule("no_new_failures", "fail", True,
                             f"{counts.get('new', 0)} new, {counts.get('flaky', 0)} flaky, {counts.get('unclassified', 0)} unclassified failure(s).", counts))
        elif pre and not rules["tests"]["allow_preexisting_failures"]:
            out.append(_rule("no_new_failures", "fail", True, f"{pre} pre-existing failure(s) and the policy does not allow them.", counts))
        else:
            out.append(_rule("no_new_failures", "pass", True, "No new, flaky or unclassified failure.", counts))

    # 4. static analysis limit
    if rules["static"]["enforce"] and by.get("static_analysis", {}).get("status") == "passed":
        n = len(static.get("new_findings", []))
        lim = rules["static"]["max_new_findings"]
        out.append(_rule("static_new_findings", "pass" if n <= lim else "fail", True, f"{n} new finding(s) (limit {lim}).", {"new": n, "limit": lim}))

    # 5. coverage thresholds
    cmin, cdrop = rules["coverage"]["min_line_percent"], rules["coverage"]["max_drop_percent"]
    if cmin is not None or cdrop is not None:
        if coverage.get("status") != "measured":
            out.append(_rule("coverage", "blocked", True, f"Coverage is required by policy but was not measured ({coverage.get('status')}).", {}))
        else:
            if cmin is not None:
                v = coverage.get("line_percent")
                out.append(_rule("coverage_min", "pass" if v is not None and v >= cmin else "fail", True, f"Line coverage {v}% (minimum {cmin}%).", {"value": v}))
            if cdrop is not None:
                d = coverage.get("delta_percent")
                if d is None:
                    out.append(_rule("coverage_drop", "blocked", True, "A coverage drop limit is set but there is no comparable baseline coverage.", {}))
                else:
                    out.append(_rule("coverage_drop", "pass" if -d <= cdrop else "fail", True, f"Coverage changed by {d} points (largest allowed drop {cdrop}).", {"delta": d}))

    # 6. baseline requirement
    if rules["baseline"]["required"]:
        ok = baseline_compat.get("compatible")
        out.append(_rule("baseline", "pass" if ok else "blocked", True,
                         "A compatible baseline is linked." if ok else " ".join(baseline_compat.get("problems") or ["No baseline."]), baseline_compat))

    # 7. every changed file needs a verifier
    unv = plan.get("unverified_paths") or []
    if unv:
        out.append(_rule("changed_files_verified", "incomplete", True, f"{len(unv)} changed file(s) have no verifier in NOVA (e.g. {unv[0]}).", {"paths": unv[:20]}))

    # 8. source stability
    if flags.get("source_changed") and rules["require_unchanged_tree"]:
        out.append(_rule("source_unchanged", "incomplete", True, "The working tree changed while verification ran; results do not describe a single state.", {}))
    if flags.get("cancelled"):
        out.append(_rule("not_cancelled", "incomplete", True, "The run was cancelled before every check finished.", {}))

    required = [r for r in out if r["required"]]
    statuses = {r["status"] for r in required}
    if "fail" in statuses:
        outcome, why = "FAIL", "A required criterion failed."
    elif "blocked" in statuses:
        outcome, why = "BLOCKED", "Required evidence could not be produced."
    elif "incomplete" in statuses:
        outcome, why = "INCOMPLETE", "Required evidence is only partly available."
    elif "pass" not in statuses:
        outcome, why = "INCOMPLETE", "No required check produced evidence (nothing applicable to this change set)."
    else:
        outcome, why = "PASS", "Every required criterion has verified evidence and satisfies the policy."
    return {"outcome": outcome, "reason": why, "precedence": list(PRECEDENCE), "rules": out,
            "meaning": ("PASS means the selected checks passed under this policy for this source state. It is evidence, not proof of correctness."
                        if outcome == "PASS" else why)}
