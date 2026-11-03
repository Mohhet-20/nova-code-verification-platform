"""Verification of an applied patch through the Phase 05 controlled test-execution boundary.

NOVA never runs a command from a request body: a verification selects *discovered* tests, and ``execution.begin_existing`` runs them in
the restricted pytest subprocess (timeout, minimal environment, bounded output). A patch is ``verified`` only when every required
criterion passes:

1. the run completed (not timed out, errored or rejected);
2. at least one selected test ran and passed;
3. no selected test failed or errored, and none was skipped;
4. when a failure is linked, the original failing test was part of the run and its failure did not recur (same fingerprint) or change;
5. when a baseline run exists and covers the test, no failure is new (a failing test that the baseline did not fail);
6. the patched Python files have no new correctness / security / reliability static findings (analysis unavailable = recorded, not blocking).
Anything failing is ``FAILED``; anything that could not be decided is ``INCOMPLETE``; a run that could not start is ``BLOCKED``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.db.base import utcnow
from app.models import RepairInvestigation, RepairPatch, RepairVerification, Repository, TestExecutionRun
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.debugging import ingest
from app.services.repair import evidence, selection
from app.services.repair import patch as patchlib
from app.services.repair import service as repair_service
from app.services.repair.errors import RepairConflict, RepairInvalid
from app.services.testing import execution

BLOCKING_CATEGORY_PREFIXES = ("PY-COR", "PY-SEC", "PY-REL")


def preview(db: Session, repo: Repository, inv: RepairInvestigation, p: RepairPatch, test_ids: list[str] | None, scope: str) -> dict[str, Any]:
    failure = evidence.load_sources(db, repo, inv)["failure"]
    if scope == "all":
        sel = {"targets": [{"target": "(all discovered tests)", "reason": "The whole discovered suite was requested.", "kind": "all"}], "notes": []}
    else:
        sel = selection.select_tests(db, repo, inv, [f["path"] for f in p.files], test_ids, failure)
    return {"executes": False, "requires": "Send the same request with confirm=true to start the run.", "patch_status": p.status, "selected": sel["targets"],
            "notes": sel["notes"], "baseline_execution_run_id": inv.baseline_execution_run_id or (failure.last_execution_run_id if failure else None),
            "execution": "Restricted pytest subprocess on this machine (timeout, minimal environment, bounded output). It is not a sandbox: "
                         "the repository's own test code runs with your privileges."}


def start(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: RepairInvestigation, p: RepairPatch,
          test_ids: list[str] | None, coverage: bool, scope: str, confirm: bool) -> RepairVerification:
    ctx.require_write()
    if not confirm:
        raise RepairInvalid("confirmation_required", "Verification runs the repository's tests. Send confirm=true to start it.")
    if p.status not in ("applied", "verification_failed", "verified"):
        raise RepairConflict("patch_not_applied", f"Only an applied patch can be verified (this one is '{p.status}'). Application and verification are separate steps.")
    failure = evidence.load_sources(db, repo, inv)["failure"]
    baseline = inv.baseline_execution_run_id or (failure.last_execution_run_id if failure else None)
    v = RepairVerification(patch_id=p.id, investigation_id=inv.id, repository_id=repo.id, baseline_execution_run_id=baseline,
                           requested_by=ctx.user.id, outcome="PENDING")
    blocked: str | None = None
    targets: list[str] | None = None
    try:
        if scope == "all":
            selection.known_ids(db, repo)
            v.selected_tests = [{"target": "(all discovered tests)", "reason": "The whole discovered suite was requested.", "kind": "all"}]
        else:
            sel = selection.select_tests(db, repo, inv, [f["path"] for f in p.files], test_ids, failure)
            v.selected_tests = sel["targets"]
            targets = [t["target"] for t in sel["targets"]]
            if not targets:
                blocked = "No discovered test is linked to the changed files. " + " ".join(sel["notes"])
    except RepairConflict as exc:
        blocked = exc.message
    run = None
    if blocked is None:
        try:
            run = execution.begin_existing(db, ctx, settings, repo, coverage, targets)
        except ConflictError as exc:
            if "already in progress" in exc.message:
                raise
            blocked = exc.message
    db.add(v)
    db.flush()
    if run is not None:
        v.execution_run_id = run.id
    else:
        v.outcome, v.outcome_reason, v.evaluated_at = "BLOCKED", scrub(blocked or "Verification could not start.", 600), utcnow()
        v.criteria = [{"id": "run_started", "status": "blocked", "detail": v.outcome_reason}]
        inv.verification_outcome = "BLOCKED"
        p.verification_summary = {"outcome": "BLOCKED", "verification_id": v.id, "at": utcnow().isoformat()}
    inv.verification_outcome = v.outcome
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="repair.verification_started" if run else "repair.verification_blocked",
                 entity_type="repair_verification", entity_id=v.id,
                 details={"repository_id": repo.id, "investigation_id": inv.id, "patch_id": p.id, "execution_run_id": run.id if run else None,
                          "targets": len(v.selected_tests)})
    db.commit()
    return v


def _covers(targets: list[dict[str, Any]], test_id: str) -> bool:
    for t in targets:
        tgt = t["target"]
        if t.get("kind") == "all" or test_id == tgt or test_id.startswith(tgt + "::") or test_id.split("[")[0] == tgt:
            return True
    return False


def _crit(out: list[dict[str, Any]], cid: str, status: str, detail: str) -> None:
    out.append({"id": cid, "status": status, "detail": scrub(detail, 400)})


def _candidate_files(name: str) -> set[str]:
    """Possible file paths for a JUnit-style 'pkg.module.Class::test' name."""
    parts = name.split("::")[0].split(".")
    return {"/".join(parts[:i]) + ".py" for i in range(len(parts), 0, -1)}


def _in_scope(name: str, selected: list[dict[str, Any]]) -> bool:
    if any(t.get("kind") == "all" for t in selected):
        return True
    files = _candidate_files(name)
    return any(t["target"].split("::")[0] in files for t in selected)


def _compare(run: TestExecutionRun, baseline: TestExecutionRun | None, selected: list[dict[str, Any]]) -> dict[str, Any]:
    cur = {str(f.get("test")) for f in (run.failures or [])}
    if baseline is None:
        return {"baseline_available": False, "note": "No baseline run is linked, so failures cannot be classed as new or pre-existing.",
                "current_failures": sorted(cur)[:30]}
    base = {str(f.get("test")) for f in (baseline.failures or [])}
    targets = (baseline.environment_profile or {}).get("targets")
    base_files = {t.split("::")[0] for t in targets} if targets else None
    new, pre, unknown = [], [], []
    for name in sorted(cur):
        if name in base:
            pre.append(name)
        elif base_files is None or _candidate_files(name) & base_files:
            new.append(name)
        else:
            unknown.append(name)
    fixed = sorted(n for n in base if n not in cur and _in_scope(n, selected))
    return {"baseline_available": True, "baseline_run_id": baseline.id, "baseline_status": baseline.status, "new_failures": new,
            "pre_existing_failures": pre, "failures_without_baseline_coverage": unknown, "no_longer_failing": fixed,
            "note": "A failure is 'new' only when the baseline run covered that test and did not fail it."}


compare_runs = _compare  # shared with the refactoring subsystem


def evaluate(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, v: RepairVerification) -> RepairVerification:
    """Decide the outcome once the linked run has finished. Idempotent."""
    if v.outcome != "PENDING" or v.execution_run_id is None:
        return v
    run = db.get(TestExecutionRun, v.execution_run_id)
    if run is None or run.status in ("pending", "running"):
        return v
    p = db.get(RepairPatch, v.patch_id)
    inv = db.get(RepairInvestigation, v.investigation_id)
    if p is None or inv is None:
        return v
    failure = evidence.load_sources(db, repo, inv)["failure"]
    crit: list[dict[str, Any]] = []
    # 1 run completed
    if run.status in ("passed", "failed"):
        _crit(crit, "run_completed", "pass", f"The run finished with status '{run.status}'.")
    elif run.status == "rejected":
        _crit(crit, "run_completed", "blocked", f"The run was rejected: {run.reason or 'no reason recorded'}.")
    else:
        _crit(crit, "run_completed", "incomplete", f"The run ended as '{run.status}': {run.reason or 'no result was produced'}.")
    finished = run.status in ("passed", "failed")
    if finished:
        ran = (run.tests_collected or 0) >= 1 and (run.tests_passed or 0) >= 1
        _crit(crit, "tests_executed", "pass" if ran else "incomplete",
              f"{run.tests_passed or 0} of {run.tests_collected or 0} collected test(s) passed." if ran else "No selected test is recorded as passed.")
        bad = (run.tests_failed or 0) + (run.tests_errors or 0)
        baseline = db.get(TestExecutionRun, v.baseline_execution_run_id) if v.baseline_execution_run_id else None
        if baseline is not None and baseline.repository_id != repo.id:
            baseline = None
        v.comparison = _compare(run, baseline, v.selected_tests)
        only_old = (bool(v.comparison.get("baseline_available")) and bad > 0 and not v.comparison["new_failures"]
                    and not v.comparison["failures_without_baseline_coverage"] and len(v.comparison["pre_existing_failures"]) >= bad)
        if bad == 0:
            _crit(crit, "no_failures", "pass", "No selected test failed or errored.")
        elif only_old:
            _crit(crit, "no_failures", "incomplete", f"{bad} failure(s) remain, all of which also failed in the baseline run; the patch added none, "
                                                     "but the selected tests are not all green.")
        else:
            _crit(crit, "no_failures", "fail", f"{bad} test(s) failed or errored.")
        _crit(crit, "no_skipped", "pass" if not run.tests_skipped else "incomplete",
              "No selected test was skipped." if not run.tests_skipped else f"{run.tests_skipped} selected test(s) were skipped, so their result is unknown.")
        if failure is not None and failure.test_id:
            persists = changed = False
            if run.failures:
                try:
                    for rep, _ in ingest.ingest_run(db, ctx, settings, repo, run.id):
                        if rep.id == failure.id:
                            persists = True
                        elif rep.test_id == failure.test_id:
                            changed = True
                except ConflictError:
                    pass
            if not _covers(v.selected_tests, failure.test_id):
                _crit(crit, "original_failure_resolved", "incomplete", "The original failing test was not part of this run.")
            elif persists:
                _crit(crit, "original_failure_resolved", "fail", "The original failure (same fingerprint) occurred again.")
            elif changed:
                _crit(crit, "original_failure_resolved", "fail", "The original test fails again with a different failure.")
            elif finished and (run.tests_passed or 0) >= 1:
                _crit(crit, "original_failure_resolved", "pass", "The original failing test was run and its failure did not recur.")
            else:
                _crit(crit, "original_failure_resolved", "incomplete", "The original failure could not be evaluated.")
        else:
            _crit(crit, "original_failure_resolved", "unavailable", "No failing test is linked to this investigation.")
        if v.comparison.get("baseline_available"):
            new = v.comparison["new_failures"]
            _crit(crit, "no_new_failures", "fail" if new else "pass", f"New failures compared with the baseline: {', '.join(new[:5])}." if new else "No failure is new compared with the baseline.")
        else:
            _crit(crit, "no_new_failures", "unavailable", v.comparison["note"])
    # 6 static check on the patched files as they are now
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    items: list[patchlib.FileResult] = []
    for f in p.files:
        if f["change_type"] != "modify" or not f["path"].endswith(".py"):
            continue
        try:
            cur = patchlib.read_text_file(patchlib.check_path(root, f["path"], settings), f["path"], settings)[0]
        except patchlib.PatchError:
            continue
        items.append(patchlib.FileResult(f["path"], "modify", f["base_sha256"], patchlib.sha256_text(cur), 0, 0, p.originals.get(f["path"]), cur))
    static = repair_service._static_check(root, items) if items else {"available": True, "new_findings": [], "resolved_findings": 0}
    v.static_check = static
    if not static["available"]:
        _crit(crit, "no_new_static_findings", "unavailable", static.get("note", "Static analysis was unavailable."))
    else:
        serious = [x for x in static["new_findings"] if x["rule_id"].startswith(BLOCKING_CATEGORY_PREFIXES)]
        _crit(crit, "no_new_static_findings", "fail" if serious else "pass",
              f"New correctness/security/reliability findings: {', '.join(x['rule_id'] for x in serious[:5])}." if serious else
              f"No new correctness/security/reliability findings ({static['resolved_findings']} finding(s) resolved).")
    statuses = {c["status"] for c in crit}
    if "fail" in statuses:
        outcome = "FAILED"
    elif "blocked" in statuses:
        outcome = "BLOCKED"
    elif "incomplete" in statuses:
        outcome = "INCOMPLETE"
    else:
        outcome = "PASSED"
    reasons = [c["detail"] for c in crit if c["status"] in ("fail", "blocked", "incomplete")]
    v.criteria = crit
    v.outcome = outcome
    v.outcome_reason = scrub(" ".join(reasons) if reasons else "Every required criterion passed. This shows the selected checks passed; it does not prove the change is correct.", 600)
    v.evaluated_at = utcnow()
    inv.verification_outcome = outcome
    p.verification_summary = {"outcome": outcome, "verification_id": v.id, "at": v.evaluated_at.isoformat(), "reason": v.outcome_reason[:300]}
    if outcome == "PASSED":
        repair_service.move_patch(p, "verified")
        if inv.status in ("applied", "verification_failed"):
            repair_service.move_inv(inv, "verified")
    elif outcome == "FAILED":
        repair_service.move_patch(p, "verification_failed")
        if inv.status in ("applied", "verification_failed"):
            repair_service.move_inv(inv, "verification_failed")
    actor = ctx.user.id if ctx else v.requested_by
    audit.record(db, workspace_id=repo.workspace_id, actor_user_id=actor, action="repair.verification_evaluated", entity_type="repair_verification",
                 entity_id=v.id, details={"repository_id": repo.id, "investigation_id": inv.id, "patch_id": p.id, "outcome": outcome,
                                          "criteria": {c["id"]: c["status"] for c in crit}})
    db.commit()
    return v


def run_job(session_factory: Any, settings: Settings, run_id: str, verification_id: str) -> None:
    """Background task: run the tests, then decide the outcome. The test run itself is the Phase 05 one."""
    with session_factory() as db:
        execution.run_execution(db, settings, run_id)
    with session_factory() as db:
        v = db.get(RepairVerification, verification_id)
        if v is not None:
            repo = db.get(Repository, v.repository_id)
            if repo is not None:
                evaluate(db, None, settings, repo, v)


def public_run(settings: Settings, run: TestExecutionRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    n = settings.repair_output_chars
    return {"id": run.id, "status": run.status, "reason": scrub(run.reason or "", 300) or None, "exit_code": run.exit_code,
            "command": [scrub(str(c), 200) for c in (run.command or [])][:20], "environment": {k: v for k, v in (run.environment_profile or {}).items()
                                                                                             if k in ("targets", "coverage_requested", "python_version")},
            "started_at": run.started_at, "finished_at": run.finished_at, "duration_ms": run.duration_ms,
            "tests_collected": run.tests_collected, "tests_passed": run.tests_passed, "tests_failed": run.tests_failed, "tests_errors": run.tests_errors,
            "tests_skipped": run.tests_skipped, "output_truncated": bool(run.output_truncated) or len(run.stdout_tail or "") > n,
            "stdout_tail": scrub((run.stdout_tail or "")[-n:], n), "stderr_tail": scrub((run.stderr_tail or "")[-n:], n),
            "failures": [{"test": f.get("test"), "message": scrub(str(f.get("message") or ""), 300)} for f in (run.failures or [])[:10]]}


