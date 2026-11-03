"""Regression and coverage intelligence. Everything here is computed from recorded runs; nothing is guessed.

* A failure is **new** only when a compatible, untampered baseline covered the test's file and did not fail it.
* **Pre-existing**: the baseline failed it too.  **Resolved**: failed in the baseline, in scope now, and passing now.
* **Unclassified**: no compatible baseline (or the baseline did not cover the test) - treated conservatively by the gate.
* **Flaky** needs repeated evidence: the same test failed in this run and passed in another completed run on a byte-identical source state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import CoverageFile, CvBaseline, CvRun, TestExecutionRun
from app.services.analysis.redaction import scrub

CONFIG_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")


def config_hash(root: Path, run: TestExecutionRun | None) -> str:
    h = hashlib.sha256()
    for name in CONFIG_FILES:
        p = root / name
        h.update(name.encode())
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_size < 500_000:
                h.update(hashlib.sha256(p.read_bytes()).digest())
        except OSError:
            h.update(b"unreadable")
    env = (run.environment_profile or {}) if run else {}
    tv = (run.tool_versions or {}) if run else {}
    h.update(json.dumps([env.get("python_version"), tv.get("python"), tv.get("pytest")], sort_keys=True).encode())
    return h.hexdigest()


def baseline_digest(b: CvBaseline) -> str:
    payload = {"repo": b.repository_id, "run": b.run_id, "exec": b.execution_run_id, "head": b.head_sha, "tree": b.tree_fingerprint,
               "config": b.config_hash, "scope": b.scope, "failing": sorted(b.failing or []), "totals": b.totals, "coverage": b.coverage}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _files_of(name: str) -> set[str]:
    parts = name.split("::")[0].split(".")
    return {"/".join(parts[:i]) + ".py" for i in range(len(parts), 0, -1)}


def _scope_covers(scope: dict[str, Any], name: str) -> bool:
    if scope.get("kind") == "all":
        return True
    files = set(scope.get("files") or [])
    return bool(_files_of(name) & files)


def compatibility(b: CvBaseline | None, config: str, run_scope: dict[str, Any]) -> dict[str, Any]:
    if b is None:
        return {"compatible": False, "problems": ["No baseline is linked to this run."], "baseline_id": None}
    problems: list[str] = []
    if b.status != "valid":
        problems.append("The baseline has been superseded.")
    if baseline_digest(b) != b.integrity_sha256:
        problems.append("The baseline record no longer matches its integrity hash (it was modified after it was recorded); it is not used.")
    if (b.totals or {}).get("collected") in (None, 0):
        problems.append("The baseline run collected no tests.")
    if b.config_hash != config:
        problems.append("The test configuration or tool versions differ from the baseline's.")
    if b.scope.get("kind") != "all":
        if run_scope.get("kind") == "all":
            problems.append("The baseline covered only some test files, but this run covers all of them.")
        else:
            missing = sorted(set(run_scope.get("files") or []) - set(b.scope.get("files") or []))
            if missing:
                problems.append(f"The baseline did not run {len(missing)} of this run's test file(s) (e.g. {missing[0]}).")
    return {"compatible": not problems, "problems": problems, "baseline_id": b.id, "head_sha": b.head_sha, "recorded_at": b.created_at.isoformat() if b.created_at else None}


def _flaky_evidence(db: Session, settings: Any, run: CvRun, name: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(CvRun).where(CvRun.repository_id == run.repository_id, CvRun.id != run.id, CvRun.status == "completed",
                                          CvRun.tree_fingerprint == run.tree_fingerprint, CvRun.created_at <= run.created_at)
                      .order_by(CvRun.created_at.desc()).limit(settings.cv_flaky_lookback))
    ev: list[dict[str, Any]] = []
    for prior in rows:
        reg = prior.regression or {}
        if "failing_tests" not in reg:
            continue
        if _scope_covers(reg.get("scope") or {}, name) and name not in set(reg["failing_tests"]):
            ev.append({"run_id": prior.id, "outcome": "passed", "at": prior.finished_at.isoformat() if prior.finished_at else None})
    return ev


def classify(db: Session, settings: Any, run: CvRun, exec_run: TestExecutionRun | None, baseline: CvBaseline | None, compat: dict[str, Any],
             run_scope: dict[str, Any]) -> dict[str, Any]:
    if exec_run is None or exec_run.status not in ("passed", "failed"):
        return {"available": False, "reason": "No completed test run, so failures cannot be classified.", "failing_tests": [], "scope": run_scope,
                "baseline": compat, "items": [], "counts": {}}
    cur = {str(f.get("test")): scrub(str(f.get("message") or ""), 300) for f in (exec_run.failures or [])}
    usable = baseline is not None and compat.get("compatible")
    base_failing = set(baseline.failing or []) if usable and baseline else set()
    items: list[dict[str, Any]] = []
    for name in sorted(cur):
        flaky = _flaky_evidence(db, settings, run, name)
        if flaky:
            cat, why = "flaky", f"Failed in this run but passed in {len(flaky)} other run(s) on an identical source state."
        elif not usable:
            cat, why = "unclassified", "No compatible baseline, so this failure cannot be called new or pre-existing."
        elif name in base_failing:
            cat, why = "pre_existing", "The baseline run failed this test too."
        elif baseline and _scope_covers(baseline.scope or {}, name):
            cat, why = "new", "The baseline covered this test and it did not fail there."
        else:
            cat, why = "unclassified", "The baseline did not cover this test."
        items.append({"test": name, "category": cat, "explanation": why, "message": cur[name],
                      "evidence": {"baseline_id": baseline.id if baseline else None, "flaky_runs": flaky[:5]}})
    resolved = []
    if usable:
        for name in sorted(base_failing - set(cur)):
            if _scope_covers(run_scope, name):
                resolved.append({"test": name, "category": "resolved", "explanation": "Failed in the baseline, ran in this run's scope and did not fail.",
                                 "message": "", "evidence": {"baseline_id": baseline.id if baseline else None}})
    counts = {c: sum(1 for i in items if i["category"] == c) for c in ("new", "pre_existing", "flaky", "unclassified")}
    counts["resolved"] = len(resolved)
    return {"available": True, "failing_tests": sorted(cur), "scope": run_scope, "baseline": compat, "items": items + resolved, "counts": counts,
            "basis": "A failure is 'new' only when a compatible baseline covered the test. A resolved failure is only reported when the test ran in this run's scope."}


def coverage_summary(db: Session, run: CvRun, exec_run: TestExecutionRun | None, baseline: CvBaseline | None, compat: dict[str, Any],
                     changed_paths: list[str], requested: bool) -> dict[str, Any]:
    if not requested:
        return {"status": "not_requested"}
    if exec_run is None:
        return {"status": "unavailable", "reason": "No test run."}
    if exec_run.coverage_status != "measured":
        return {"status": exec_run.coverage_status or "unavailable", "reason": exec_run.coverage_error or "Coverage was not measured by the tool."}
    rows = {c.rel_path: c for c in db.scalars(select(CoverageFile).where(CoverageFile.execution_run_id == exec_run.id))}
    changed = [{"rel_path": p, "line_percent": round(rows[p].line_percent, 1), "statements": rows[p].statements, "covered": rows[p].covered,
                "missing_line_ranges": rows[p].missing_lines[:20]} for p in changed_paths if p in rows]
    out: dict[str, Any] = {"status": "measured", "tool": exec_run.coverage_tool, "line_percent": exec_run.line_percent,
                           "lines_total": exec_run.lines_total, "lines_covered": exec_run.lines_covered, "branch_percent": exec_run.branch_percent,
                           "changed_files": changed, "changed_files_not_measured": [p for p in changed_paths if p.endswith(".py") and p not in rows][:50],
                           "measured_scope": (exec_run.coverage_source or [])[:50], "basis": "Measured by coverage.py during this run's test selection only."}
    bc = (baseline.coverage or {}) if baseline else {}
    if baseline and compat.get("compatible") and bc.get("status") == "measured" and bc.get("line_percent") is not None and exec_run.line_percent is not None:
        out["baseline_line_percent"] = bc["line_percent"]
        out["delta_percent"] = round(exec_run.line_percent - bc["line_percent"], 2)
    elif baseline:
        out["baseline_note"] = "No comparable baseline coverage."
    return out


def stamp() -> str:
    return utcnow().isoformat()
