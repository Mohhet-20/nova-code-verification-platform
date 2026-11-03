"""Execution of a planned run. Only the checks in the plan can run; the test check goes through the Phase 05 restricted runner.

Cancellation takes effect between checks: a pytest process that is already running is bounded by the runner's own timeout and is not killed.
Partial results are always kept.
"""
from __future__ import annotations

import ast
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import CvBaseline, CvCheck, CvEvent, CvRun, Repository, TestExecutionRun
from app.security.paths import resolve_repository_root
from app.services.analysis.redaction import scrub
from app.services.repair import patch as patchlib
from app.services.repair import service as repair_service
from app.services.testing import execution
from app.services.verification import changes, gate, policy, regression

TERMINAL_CHECK = {"passed", "failed", "blocked", "error", "cancelled", "incomplete", "not_applicable"}


def event(db: Session, run: CvRun, kind: str, **detail: Any) -> None:
    db.add(CvEvent(run_id=run.id, repository_id=run.repository_id, kind=kind, detail=detail))


def checks_of(db: Session, run_id: str) -> list[CvCheck]:
    return list(db.scalars(select(CvCheck).where(CvCheck.run_id == run_id).order_by(CvCheck.ordinal)))


def check_dict(c: CvCheck) -> dict[str, Any]:
    return {"id": c.id, "check_id": c.check_id, "tier": c.tier, "required": c.required, "reason": c.reason, "confidence": c.confidence,
            "prerequisites": c.prerequisites, "status": c.status, "result": c.result, "started_at": c.started_at, "finished_at": c.finished_at}


def _scope(plan: dict[str, Any]) -> dict[str, Any]:
    t = plan.get("test_targets")
    return {"kind": "all"} if t is None else {"kind": "files", "files": sorted(t)}


def _reject_pending_exec(db: Session, run: CvRun, reason: str) -> None:
    if not run.execution_run_id:
        return
    ex = db.get(TestExecutionRun, run.execution_run_id)
    if ex is not None and ex.status == "pending":
        execution._finish_rejected(db, ex, reason)


def _do_syntax(cs: changes.ChangeSet) -> tuple[str, dict[str, Any]]:
    bad: list[dict[str, Any]] = []
    n = 0
    for f in cs.files:
        if f.is_python and f.new_text is not None:
            n += 1
            try:
                ast.parse(f.new_text, filename=f.path)
            except SyntaxError as exc:
                bad.append({"path": f.path, "line": exc.lineno, "message": scrub(str(exc.msg), 200)})
    if bad:
        return "failed", {"detail": f"{len(bad)} file(s) do not parse.", "errors": bad[:20], "files_checked": n}
    return "passed", {"detail": f"{n} Python file(s) parse.", "files_checked": n}


def _do_static(root: Any, cs: changes.ChangeSet) -> tuple[str, dict[str, Any]]:
    results = [patchlib.FileResult(f.path, "modify" if f.old_text is not None else "create", None, "", 0, 0, f.old_text, f.new_text or "")
               for f in cs.files if f.is_python and f.new_text is not None]
    res = repair_service.static_check(root, results)
    if not res.get("available"):
        return "error", {"detail": res.get("note") or "Static analysis could not run.", **res}
    n = len(res.get("new_findings", []))
    return "passed", {"detail": f"{n} new finding(s), {res.get('resolved_findings', 0)} resolved.", **res}


def _test_result(ex: TestExecutionRun) -> tuple[str, dict[str, Any]]:
    executed = (ex.tests_passed or 0) + (ex.tests_failed or 0) + (ex.tests_errors or 0)
    info = {"execution_run_id": ex.id, "execution_status": ex.status, "exit_code": ex.exit_code, "tests_collected": ex.tests_collected,
            "tests_passed": ex.tests_passed, "tests_failed": ex.tests_failed, "tests_errors": ex.tests_errors, "tests_skipped": ex.tests_skipped,
            "tests_executed": executed, "duration_ms": ex.duration_ms, "output_truncated": bool(ex.output_truncated),
            "failures": [{"test": f.get("test"), "message": scrub(str(f.get("message") or ""), 300)} for f in (ex.failures or [])[:20]],
            "reason": scrub(ex.reason or "", 300) or None}
    if ex.status == "passed":
        return "passed", {"detail": f"{executed} test(s) executed, none failed.", **info}
    if ex.status == "failed":
        return "failed", {"detail": f"{ex.tests_failed or 0} failed, {ex.tests_errors or 0} errored.", **info}
    if ex.status == "timed_out":
        return "incomplete", {"detail": "The test run timed out before finishing.", **info}
    return "error", {"detail": ex.reason or f"The test run ended as '{ex.status}'.", **info}


def run_job(session_factory: Any, settings: Settings, run_id: str) -> None:
    with session_factory() as db:
        try:
            _run(db, settings, run_id)
        except Exception as exc:  # noqa: BLE001 - a run must always reach a final state
            db.rollback()
            run = db.get(CvRun, run_id)
            if run is not None and run.status in ("queued", "running"):
                for c in checks_of(db, run.id):
                    if c.status in ("planned", "running"):
                        c.status, c.finished_at = "error", utcnow()
                        c.result = {**c.result, "detail": f"Unexpected {type(exc).__name__}; the check did not complete."}
                _reject_pending_exec(db, run, "The verification run failed unexpectedly before the tests started.")
                run.status, run.finished_at, run.reason = "error", utcnow(), f"Unexpected {type(exc).__name__} while verifying."
                run.gate_outcome = "BLOCKED"
                run.gate_decision = {"outcome": "BLOCKED", "reason": run.reason, "rules": [], "precedence": list(policy.PRECEDENCE), "meaning": run.reason}
                event(db, run, "run_error", error=type(exc).__name__)
                db.commit()


def _run(db: Session, settings: Settings, run_id: str) -> None:
    run = db.get(CvRun, run_id)
    if run is None or run.status != "queued":
        return
    repo = db.get(Repository, run.repository_id)
    assert repo is not None
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    run.status, run.started_at = "running", utcnow()
    event(db, run, "run_started", mode=run.mode)
    db.commit()
    flags: dict[str, Any] = {}
    cs: changes.ChangeSet | None = None
    if run.kind == "verify":
        spec = run.change_set.get("spec") or {}
        cs = changes.resolve(root, settings, spec["source"], base=spec.get("base"), head=spec.get("head"), paths=spec.get("paths"), patch=spec.get("patch"))
        if cs.fingerprint() != run.change_set.get("fingerprint"):
            flags["source_changed"] = True
            event(db, run, "source_changed_before_start")
    exec_run: TestExecutionRun | None = None
    for c in checks_of(db, run.id):
        if c.status in TERMINAL_CHECK:
            continue
        db.refresh(run)
        if run.cancel_requested:
            flags["cancelled"] = True
            c.status, c.finished_at = "cancelled", utcnow()
            event(db, run, "check_cancelled", check=c.check_id)
            continue
        if flags.get("source_changed"):
            c.status, c.finished_at = "blocked", utcnow()
            c.result = {"detail": "The source changed between the request and the start of the run; nothing was executed."}
            continue
        c.status, c.started_at = "running", utcnow()
        event(db, run, "check_started", check=c.check_id)
        db.commit()
        if c.check_id == "syntax":
            c.status, c.result = _do_syntax(cs)  # type: ignore[arg-type]
        elif c.check_id == "static_analysis":
            c.status, c.result = _do_static(root, cs)  # type: ignore[arg-type]
            run.static_result = {k: v for k, v in c.result.items() if k in ("available", "new_findings", "resolved_findings")}
        elif c.check_id == "tests":
            if not run.execution_run_id:
                c.status, c.result = "blocked", {"detail": "No test run could be created."}
            else:
                execution.run_execution(db, settings, run.execution_run_id)
                db.expire_all()
                exec_run = db.get(TestExecutionRun, run.execution_run_id)
                c.status, c.result = _test_result(exec_run)  # type: ignore[arg-type]
                fp, _, _ = changes.tree_fingerprint(root, settings)
                if fp != run.tree_fingerprint:
                    flags["source_changed"] = True
                    event(db, run, "source_changed_during_run")
        elif c.check_id == "coverage":
            exec_run = exec_run or (db.get(TestExecutionRun, run.execution_run_id) if run.execution_run_id else None)
            if exec_run is not None and exec_run.coverage_status == "measured":
                c.status, c.result = "passed", {"detail": f"Line coverage {exec_run.line_percent}%.", "line_percent": exec_run.line_percent}
            else:
                c.status = "unavailable"
                c.result = {"detail": (exec_run.coverage_error if exec_run else None) or "Coverage was not measured by the tool."}
        c.finished_at = utcnow()
        event(db, run, "check_finished", check=c.check_id, status=c.status)
        db.commit()
    db.refresh(run)
    if run.cancel_requested:
        flags["cancelled"] = True
        _reject_pending_exec(db, run, "Cancelled before the tests started.")
    finalize(db, settings, run, root, cs, flags)


def finalize(db: Session, settings: Settings, run: CvRun, root: Any, cs: changes.ChangeSet | None, flags: dict[str, Any]) -> None:
    exec_run = db.get(TestExecutionRun, run.execution_run_id) if run.execution_run_id else None
    plan = run.plan or {}
    baseline = db.get(CvBaseline, run.baseline_id) if run.baseline_id else None
    scope = _scope(plan)
    compat = regression.compatibility(baseline, regression.config_hash(root, exec_run), scope) if run.kind == "verify" else {"compatible": False, "problems": []}
    changed_paths = [f["path"] for f in (run.change_set or {}).get("files", [])]
    coverage_requested = any(c.check_id == "coverage" for c in checks_of(db, run.id))
    run.regression = regression.classify(db, settings, run, exec_run, baseline if run.kind == "verify" else None, compat, scope)
    run.coverage = regression.coverage_summary(db, run, exec_run, baseline if run.kind == "verify" else None, compat, changed_paths, coverage_requested)
    cks = [check_dict(c) for c in checks_of(db, run.id)]
    run.gate_decision = gate.evaluate(run.policy_snapshot, cks, run.regression, run.coverage, run.static_result or {}, plan, compat, flags)
    run.gate_outcome = run.gate_decision["outcome"]
    run.status = "cancelled" if flags.get("cancelled") else "completed"
    run.finished_at = utcnow()
    run.reason = run.gate_decision["reason"]
    if run.kind == "baseline":
        _record_baseline(db, run, exec_run, root, scope)
    event(db, run, "run_finished", status=run.status, gate=run.gate_outcome)
    db.commit()


def _record_baseline(db: Session, run: CvRun, exec_run: TestExecutionRun | None, root: Any, scope: dict[str, Any]) -> None:
    if exec_run is None or exec_run.status not in ("passed", "failed") or not (exec_run.tests_collected or 0):
        run.reason = "No usable test result was produced, so no baseline was recorded."
        return
    for old in db.scalars(select(CvBaseline).where(CvBaseline.repository_id == run.repository_id, CvBaseline.status == "valid")):
        old.status = "superseded"
    b = CvBaseline(repository_id=run.repository_id, label=(run.change_set or {}).get("label", ""), run_id=run.id, execution_run_id=exec_run.id,
                   head_sha=run.head_sha, tree_fingerprint=run.tree_fingerprint, config_hash=regression.config_hash(root, exec_run), scope=scope,
                   failing=sorted({str(f.get("test")) for f in (exec_run.failures or [])}),
                   totals={"collected": exec_run.tests_collected, "passed": exec_run.tests_passed, "failed": exec_run.tests_failed,
                           "errors": exec_run.tests_errors, "skipped": exec_run.tests_skipped},
                   coverage=({"status": "measured", "line_percent": exec_run.line_percent} if exec_run.coverage_status == "measured" else {"status": exec_run.coverage_status}),
                   created_by=run.requested_by)
    db.add(b)
    db.flush()
    b.integrity_sha256 = regression.baseline_digest(b)
    run.baseline_id = b.id
    event(db, run, "baseline_recorded", baseline_id=b.id)
