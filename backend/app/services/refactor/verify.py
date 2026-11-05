"""Behavior-preservation *evidence* for an applied refactoring, controlled test runner.

A verification selects discovered tests (never an arbitrary command), runs them in the restricted pytest subprocess and compares the result
with a baseline run. ``PASSED`` means: the run completed, at least one selected test passed, none failed/errored/skipped, no failure is new
relative to the baseline, the files on disk still match what was applied, and static analysis found nothing serious that is new. It never
means the refactoring is proven equivalent: tests cover only what they cover.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.db.base import utcnow
from app.models import RefactorTransformation, RefactorVerification, Repository, TestExecutionRun
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.analysis.redaction import scrub
from app.services.refactor import service
from app.services.refactor.errors import RefactorConflict, RefactorInvalid
from app.services.repair import patch as patchlib
from app.services.repair import selection
from app.services.repair import service as repair_service
from app.services.repair import verify as repair_verify
from app.services.testing import execution

MEANING = {"PENDING": "The test run has not finished.", "PASSED": "The selected checks passed. This is evidence, not proof of equivalent behavior.",
           "FAILED": "A check failed or a failure is new compared with the baseline.", "INCOMPLETE": "Not every criterion could be decided.",
           "BLOCKED": "Verification could not run."}
NOT_PROVEN = ("Tests exercise only the paths they cover. Dynamic references, reflection, serialization formats and untested behavior are not verified, "
              "so the refactoring is not proven behavior-preserving.")


def implicit_baseline(db: Session, repo: Repository, t: RefactorTransformation) -> tuple[str | None, str]:
    if t.baseline_execution_run_id:
        return t.baseline_execution_run_id, t.baseline_note or "Explicit baseline."
    q = select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id, TestExecutionRun.status.in_(("passed", "failed")))
    if t.applied_at is not None:
        q = q.where(TestExecutionRun.started_at < t.applied_at)
    run = db.scalar(q.order_by(TestExecutionRun.started_at.desc()).limit(1))
    if run is None:
        return None, "No test run before the change exists, so new failures cannot be told apart from pre-existing ones."
    return run.id, "Implicit baseline: the latest finished run before the change was applied. It may not cover the same tests."


def preview(db: Session, repo: Repository, t: RefactorTransformation, test_ids: list[str] | None, scope: str) -> dict[str, Any]:
    if scope == "all":
        selected = [{"target": "(all discovered tests)", "reason": "The whole discovered suite was requested.", "kind": "all"}]
        notes: list[str] = []
    else:
        sel = selection.select_tests(db, repo, None, [f["path"] for f in t.files], test_ids, None)
        selected, notes = sel["targets"], sel["notes"]
    base_id, base_note = implicit_baseline(db, repo, t)
    return {"executes": False, "requires": "Send the same request with confirm=true to start the run.", "status": t.status, "selected": selected, "notes": notes,
            "baseline_execution_run_id": base_id, "baseline_note": base_note, "not_proven": NOT_PROVEN,
            "execution": "Restricted pytest subprocess on this machine (timeout, minimal environment, bounded output). It is not a sandbox: the repository's own test code runs with your privileges."}


def start(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation, test_ids: list[str] | None, coverage: bool,
          scope: str, confirm: bool) -> RefactorVerification:
    ctx.require_write()
    if not confirm:
        raise RefactorInvalid("confirmation_required", "Verification runs the repository's tests. Send confirm=true to start it.")
    if t.status not in service.APPLIED:
        raise RefactorConflict("not_applied", f"Only an applied refactoring can be verified (this one is '{t.status}'). Application and verification are separate steps.")
    base_id, base_note = implicit_baseline(db, repo, t)
    v = RefactorVerification(transformation_id=t.id, repository_id=repo.id, baseline_execution_run_id=base_id, requested_by=ctx.user.id, outcome="PENDING",
                             comparison={"baseline_note": base_note})
    blocked: str | None = None
    targets: list[str] | None = None
    try:
        if scope == "all":
            selection.known_ids(db, repo)
            v.selected_tests = [{"target": "(all discovered tests)", "reason": "The whole discovered suite was requested.", "kind": "all"}]
        else:
            sel = selection.select_tests(db, repo, None, [f["path"] for f in t.files], test_ids, None)
            v.selected_tests = sel["targets"]
            targets = [x["target"] for x in sel["targets"]]
            if not targets:
                blocked = "No discovered test is linked to the changed files. " + " ".join(sel["notes"])
    except Exception as exc:  # noqa: BLE001 - RepairConflict/RepairInvalid from the shared selector
        blocked = getattr(exc, "message", str(exc))
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
        t.verification_summary = {"outcome": "BLOCKED", "verification_id": v.id, "at": utcnow().isoformat()}
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="refactor.verification_started" if run else "refactor.verification_blocked",
                 entity_type="refactor_verification", entity_id=v.id,
                 details={"repository_id": repo.id, "transformation_id": t.id, "execution_run_id": run.id if run else None, "targets": len(v.selected_tests)})
    db.commit()
    return v


def evaluate(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, v: RefactorVerification) -> RefactorVerification:
    """Decide the outcome once the linked run has finished. Idempotent."""
    if v.outcome != "PENDING" or v.execution_run_id is None:
        return v
    run = db.get(TestExecutionRun, v.execution_run_id)
    if run is None or run.status in ("pending", "running"):
        return v
    t = db.get(RefactorTransformation, v.transformation_id)
    if t is None:
        return v
    crit: list[dict[str, Any]] = []
    c = repair_verify._crit
    if run.status in ("passed", "failed"):
        c(crit, "run_completed", "pass", f"The run finished with status '{run.status}'.")
    elif run.status == "rejected":
        c(crit, "run_completed", "blocked", f"The run was rejected: {run.reason or 'no reason recorded'}.")
    else:
        c(crit, "run_completed", "incomplete", f"The run ended as '{run.status}': {run.reason or 'no result was produced'}.")
    finished = run.status in ("passed", "failed")
    note = (v.comparison or {}).get("baseline_note", "")
    if finished:
        ran = (run.tests_collected or 0) >= 1 and (run.tests_passed or 0) >= 1
        c(crit, "tests_executed", "pass" if ran else "incomplete",
          f"{run.tests_passed or 0} of {run.tests_collected or 0} collected test(s) passed." if ran else "No selected test is recorded as passed.")
        bad = (run.tests_failed or 0) + (run.tests_errors or 0)
        baseline = db.get(TestExecutionRun, v.baseline_execution_run_id) if v.baseline_execution_run_id else None
        if baseline is not None and baseline.repository_id != repo.id:
            baseline = None
        cmp = repair_verify.compare_runs(run, baseline, v.selected_tests)
        cmp["baseline_note"] = note
        v.comparison = cmp
        only_old = (bool(cmp.get("baseline_available")) and bad > 0 and not cmp["new_failures"] and not cmp["failures_without_baseline_coverage"]
                    and len(cmp["pre_existing_failures"]) >= bad)
        if bad == 0:
            c(crit, "no_failures", "pass", "No selected test failed or errored.")
        elif only_old:
            c(crit, "no_failures", "incomplete", f"{bad} failure(s) remain, all of which also failed in the baseline; the refactoring added none, but the selected tests are not all green.")
        else:
            c(crit, "no_failures", "fail", f"{bad} test(s) failed or errored.")
        c(crit, "no_skipped", "pass" if not run.tests_skipped else "incomplete",
          "No selected test was skipped." if not run.tests_skipped else f"{run.tests_skipped} selected test(s) were skipped, so their result is unknown.")
        if cmp.get("baseline_available"):
            new = cmp["new_failures"]
            c(crit, "no_new_failures", "fail" if new else "pass", f"New failures compared with the baseline: {', '.join(new[:5])}." if new else "No failure is new compared with the baseline.")
        else:
            c(crit, "no_new_failures", "unavailable", cmp["note"])
    # files on disk must still be what was applied, otherwise the run says nothing about this change
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    items: list[patchlib.FileResult] = []
    drift: list[str] = []
    for f in t.files:
        try:
            cur = patchlib.read_text_file(patchlib.check_path(root, f["path"], settings), f["path"], settings)[0]
        except patchlib.PatchError:
            drift.append(f["path"])
            continue
        if patchlib.sha256_text(cur) != f.get("result_sha256"):
            drift.append(f["path"])
        if f["path"].endswith(".py"):
            items.append(patchlib.FileResult(f["path"], "modify", f["base_sha256"], patchlib.sha256_text(cur), 0, 0, t.originals.get(f["path"]), cur))
    c(crit, "applied_state_intact", "incomplete" if drift else "pass",
      f"These files differ from the applied result: {', '.join(drift[:5])}. The run may reflect other edits." if drift else "The changed files still match the applied result.")
    static = repair_service.static_check(root, items) if items else {"available": True, "new_findings": [], "resolved_findings": 0}
    v.static_check = static
    if not static["available"]:
        c(crit, "no_new_static_findings", "unavailable", static.get("note", "Static analysis was unavailable."))
    else:
        serious = [x for x in static["new_findings"] if x["rule_id"].startswith(repair_verify.BLOCKING_CATEGORY_PREFIXES)]
        c(crit, "no_new_static_findings", "fail" if serious else "pass",
          f"New correctness/security/reliability findings: {', '.join(x['rule_id'] for x in serious[:5])}." if serious else
          f"No new correctness/security/reliability findings ({static['resolved_findings']} finding(s) resolved).")
    c(crit, "behavior_equivalence", "unavailable", NOT_PROVEN)
    statuses = {x["status"] for x in crit}
    outcome = "FAILED" if "fail" in statuses else "BLOCKED" if "blocked" in statuses else "INCOMPLETE" if "incomplete" in statuses else "PASSED"
    reasons = [x["detail"] for x in crit if x["status"] in ("fail", "blocked", "incomplete")]
    v.criteria = crit
    v.outcome = outcome
    v.outcome_reason = scrub(" ".join(reasons) if reasons else "Every required criterion passed. " + NOT_PROVEN, 600)
    v.evaluated_at = utcnow()
    t.verification_summary = {"outcome": outcome, "verification_id": v.id, "at": v.evaluated_at.isoformat(), "reason": v.outcome_reason[:300]}
    if outcome == "PASSED":
        service.move(t, "verified")
    elif outcome == "FAILED":
        service.move(t, "verification_failed")
    actor = ctx.user.id if ctx else v.requested_by
    audit.record(db, workspace_id=repo.workspace_id, actor_user_id=actor, action="refactor.verification_evaluated", entity_type="refactor_verification", entity_id=v.id,
                 details={"repository_id": repo.id, "transformation_id": t.id, "outcome": outcome, "criteria": {x["id"]: x["status"] for x in crit}})
    db.commit()
    return v


def run_job(session_factory: Any, settings: Settings, run_id: str, verification_id: str) -> None:
    with session_factory() as db:
        execution.run_execution(db, settings, run_id)
    with session_factory() as db:
        v = db.get(RefactorVerification, verification_id)
        if v is not None:
            repo = db.get(Repository, v.repository_id)
            if repo is not None:
                evaluate(db, None, settings, repo, v)
