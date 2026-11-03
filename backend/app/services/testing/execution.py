"""Approved execution of tests, persistence of results, and coverage / gap queries."""
from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    TooManyRequestsError,
    UnsafePathError,
    ValidationFailedError,
)
from app.db.base import utcnow
from app.models import CoverageFile, Repository, TestCandidate, TestExecutionRun, TestFile
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root, safe_join
from app.services import audit
from app.services.testing import artifacts, facts, runner, service
from app.services.testing.discovery import EXCLUDED_BASENAMES

INI = "[pytest]\npython_files = test_*.py\naddopts =\ntestpaths =\n"


def recover_interrupted_executions(db: Session) -> int:
    result = db.execute(update(TestExecutionRun).where(TestExecutionRun.status.in_(("pending", "running"))).values(
        status="error", finished_at=utcnow(), reason="Interrupted: the backend stopped before the run finished."))
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _limit_concurrency(db: Session, settings: Settings) -> None:
    active = db.scalar(select(func.count()).select_from(TestExecutionRun).where(
        TestExecutionRun.status.in_(("pending", "running")))) or 0
    if active >= settings.test_max_concurrent_runs:
        raise TooManyRequestsError("Too many test runs are in progress. Try again shortly.")


def _create(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, **fields: Any) -> TestExecutionRun:
    active = db.scalar(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id,
                                                      TestExecutionRun.status.in_(("pending", "running"))))
    if active is not None:
        raise ConflictError("A test run is already in progress for this repository.", {"run_id": active.id})
    _limit_concurrency(db, settings)
    snap = service.published_snapshot(db, repo)
    run = TestExecutionRun(repository_id=repo.id, index_snapshot_id=snap.id, status="pending", requested_by=ctx.user.id, **fields)
    db.add(run)
    try:
        db.flush()
        _, rel = artifacts.run_dir(settings, repo.id, "executions", run.id)
        run.artifact_dir = rel
        audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.execution_requested",
                     entity_type="test_execution_run", entity_id=run.id, details={"repository_id": repo.id, "kind": fields["kind"]})
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id,
                                                          TestExecutionRun.status.in_(("pending", "running"))))
        raise ConflictError("A test run is already in progress for this repository.",
                            {"run_id": active.id if active else None}) from None
    return run


def begin_generated(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, gen_run_id: str,
                    candidate_ids: list[str] | None, coverage: bool) -> TestExecutionRun:
    gen = service.get_run(db, repo.id, gen_run_id)
    cands = service.candidates_of(db, gen.id)
    approved = [c for c in cands if c.status == "approved" and (candidate_ids is None or c.id in candidate_ids)]
    if candidate_ids is not None:
        unknown = set(candidate_ids) - {c.id for c in cands}
        if unknown:
            raise NotFoundError("One or more candidates do not belong to this generation run.")
    if not approved:
        raise ConflictError("Nothing to run: approve at least one validated candidate first. Generated tests are never run automatically.")
    for c in approved:
        staged = artifacts.resolve_rel(settings, c.staged_path)
        if not staged.is_file() or artifacts.sha256_file(staged) != c.approved_sha256:
            raise ConflictError("A staged file differs from the version that was approved; review and approve it again.",
                                {"candidate_id": c.id})
    return _create(db, ctx, settings, repo, kind="generated", generation_run_id=gen.id, candidate_ids=[c.id for c in approved],
                   source_hashes={c.target_path: next((t["sha256"] for t in gen.targets if t["path"] == c.target_path), "")
                                  for c in approved} | {c.staged_path: c.approved_sha256 or "" for c in approved},
                   coverage_status="not_requested" if not coverage else "pending",
                   environment_profile={"coverage_requested": coverage})


def begin_existing(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, coverage: bool,
                   targets: list[str] | None = None) -> TestExecutionRun:
    """``targets`` (already validated against discovery by the caller) narrows the run to specific files or test ids."""
    d = service.latest_discovery(db, repo.id)
    if d is None:
        raise ConflictError("Run test discovery first so NOVA knows which test files belong to the suite.")
    files = [f.rel_path for f in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id))
             if f.parse_status == "ok" and f.test_count > 0]
    if not files:
        raise ConflictError("Discovery found no runnable test files.")
    chosen = {t.split("::")[0] for t in targets} if targets else set(files)
    return _create(db, ctx, settings, repo, kind="existing", candidate_ids=[],
                   source_hashes={f.rel_path: f.sha256 for f in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id))
                                  if f.rel_path in chosen},
                   coverage_status="pending" if coverage else "not_requested",
                   environment_profile={"coverage_requested": coverage, **({"targets": targets} if targets else {})})


def run_execution(db: Session, settings: Settings, run_id: str) -> None:
    run = db.get(TestExecutionRun, run_id)
    if run is None or run.status != "pending":
        return
    repo = db.get(Repository, run.repository_id)
    run.status, run.started_at = "running", utcnow()
    db.commit()
    try:
        _run(db, settings, run, repo)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - the run must always reach a final state
        db.rollback()
        run = db.get(TestExecutionRun, run_id)
        if run is not None:
            run.status, run.finished_at = "error", utcnow()
            run.reason = f"Unexpected {type(exc).__name__} while running tests."
            db.commit()


def _run(db: Session, settings: Settings, run: TestExecutionRun, repo: Repository) -> None:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    run_path, _ = artifacts.run_dir(settings, repo.id, "executions", run.id)
    run_path.mkdir(parents=True, exist_ok=True)
    want_cov = bool(run.environment_profile.get("coverage_requested"))
    if run.kind == "generated":
        cands = list(db.scalars(select(TestCandidate).where(TestCandidate.id.in_(run.candidate_ids),
                                                            TestCandidate.repository_id == repo.id)))
        targets: list[str] = []
        roots: list[Path] = []
        includes: list[Path] = []
        for c in cands:
            staged = artifacts.resolve_rel(settings, c.staged_path)
            if artifacts.sha256_file(staged) != c.approved_sha256:
                _finish_rejected(db, run, "A staged file changed after approval; nothing was run.")
                return
            targets.append(str(staged))
            ir = safe_join(root, c.import_root) if c.import_root else root
            if ir not in roots:
                roots.append(ir)
            includes.append(safe_join(root, c.target_path))
        (run_path / "pytest.ini").write_text(INI, encoding="utf-8")
        spec = runner.RunSpec("generated", root, run_path, run_path, run_path, targets, roots or [root], want_cov,
                              coverage_include=includes, pytest_ini=run_path / "pytest.ini")
    else:
        d = service.latest_discovery(db, repo.id)
        files = list(run.environment_profile.get("targets") or []) or [
            f.rel_path for f in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id))  # type: ignore[union-attr]
            if f.parse_status == "ok" and f.test_count > 0]
        roots = [root] + ([root / "src"] if (root / "src").is_dir() else [])
        spec = runner.RunSpec("existing", root, run_path, root, root, files, roots, want_cov, coverage_source=root)
    res = runner.execute(spec, settings)
    apply_result(db, run, res)


def _finish_rejected(db: Session, run: TestExecutionRun, reason: str) -> None:
    run.status, run.reason, run.finished_at = "rejected", reason, utcnow()
    db.commit()


def apply_result(db: Session, run: TestExecutionRun, res: runner.RunResult) -> None:
    run.status, run.reason, run.exit_code = res.status, res.reason, res.exit_code
    if res.started_at:
        run.started_at = datetime.fromtimestamp(res.started_at, UTC)
    run.finished_at = datetime.fromtimestamp(res.finished_at, UTC) if res.finished_at else utcnow()
    run.duration_ms = int((res.finished_at - res.started_at) * 1000) if res.started_at and res.finished_at else None
    run.command, run.environment_profile = res.command, {**run.environment_profile, **res.environment_profile}
    run.tests_collected, run.tests_passed, run.tests_failed = res.collected, res.passed, res.failed
    run.tests_errors, run.tests_skipped, run.failures = res.errors, res.skipped, res.failures
    run.stdout_tail, run.stderr_tail, run.output_truncated = res.stdout, res.stderr, res.truncated
    run.tool_versions = res.tool_versions
    if run.coverage_status == "pending":
        run.coverage_status = res.coverage_status
    elif res.coverage_status != "not_requested":
        run.coverage_status = res.coverage_status
    run.coverage_error, run.coverage_tool, run.coverage_branch = res.coverage_error, res.coverage_tool, res.coverage_status == "measured"
    if res.unmeasured_targets:
        run.environment_profile = {**run.environment_profile, "unmeasured_targets": res.unmeasured_targets}
    if res.coverage_status == "measured":
        t = res.coverage_totals
        run.lines_total, run.lines_covered, run.line_percent = t["lines_total"], t["lines_covered"], t["line_percent"]
        run.branches_total, run.branches_covered, run.branch_percent = t["branches_total"], t["branches_covered"], t["branch_percent"]
        run.coverage_source = [f["rel_path"] for f in res.coverage_files][:200]
        db.execute(delete(CoverageFile).where(CoverageFile.execution_run_id == run.id))
        for f in res.coverage_files:
            db.add(CoverageFile(execution_run_id=run.id, repository_id=run.repository_id, **f))
    db.commit()


def get_exec_run(db: Session, repo_id: str, run_id: str) -> TestExecutionRun:
    run = db.scalar(select(TestExecutionRun).where(TestExecutionRun.id == run_id, TestExecutionRun.repository_id == repo_id))
    if run is None:
        raise NotFoundError("Test run not found.")
    return run


def latest_measured(db: Session, repo_id: str) -> TestExecutionRun | None:
    return db.scalar(select(TestExecutionRun).where(
        TestExecutionRun.repository_id == repo_id, TestExecutionRun.coverage_status == "measured")
        .order_by(TestExecutionRun.finished_at.desc(), TestExecutionRun.id.desc()).limit(1))


# ------------------------------------------------------------------------------------------------ gaps

def compute_gaps(db: Session, settings: Settings, repo: Repository, *, path: str | None, include_private: bool,
                 limit_files: int = 2000) -> dict[str, Any]:
    """Static recommendations (labelled as such) enriched with measured evidence when a measured run exists."""
    snap = service.published_snapshot(db, repo)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    files = service.python_files(db, snap.id)
    disc = service.latest_discovery(db, repo.id)
    tests_for: dict[str, list[str]] = {}
    if disc is not None:
        for tf in db.scalars(select(TestFile).where(TestFile.discovery_id == disc.id)):
            for a in tf.associated_sources:
                if a.get("via") != "import":
                    continue  # a matching file name is not evidence that behaviour is tested
                tests_for.setdefault(a["path"], []).append(tf.rel_path)
    run = latest_measured(db, repo.id)
    cov: dict[str, CoverageFile] = {}
    if run is not None:
        cov = {c.rel_path: c for c in db.scalars(select(CoverageFile).where(CoverageFile.execution_run_id == run.id))}
    items: list[dict[str, Any]] = []
    sources = [p for p in sorted(files) if p.rsplit("/", 1)[-1] not in EXCLUDED_BASENAMES
               and not service.is_test_path(db, repo.id, p) and (path is None or path.lower() in p.lower())][:limit_files]
    for rel in sources:
        try:
            p = safe_join(root, rel)
            if not p.is_file() or p.is_symlink() or p.stat().st_size > settings.test_max_file_bytes:
                continue
            tree = facts.parse_source(p.read_text(encoding="utf-8", errors="replace"))
        except (UnsafePathError, OSError):
            continue
        if tree is None:
            continue
        cf = cov.get(rel)
        missing: set[int] = set()
        if cf is not None:
            for a, b in cf.missing_lines:
                missing.update(range(a, b + 1))
        for f in facts.analyze_module(tree):
            if not include_private and not f.public:
                continue
            reasons: list[str] = []
            if f.complexity >= 5:
                reasons.append(f"cyclomatic complexity {f.complexity}")
            if f.guards:
                reasons.append(f"{len(f.guards)} input-validation guard(s)")
            elif f.raises:
                reasons.append(f"{len(f.raises)} raise statement(s)")
            if f.handlers:
                reasons.append(f"{f.handlers} exception handler(s)")
            if f.boundary_compares:
                reasons.append("comparisons against constants (boundary-sensitive)")
            if f.loops:
                reasons.append(f"{f.loops} loop(s)")
            measured = None
            if cf is not None:
                body = set(range(f.start_line, f.end_line + 1))
                unc = sorted(missing & body)
                measured = {"run_id": run.id, "missing_line_count": len(unc), "missing_lines": unc[:40]}  # type: ignore[union-attr]
                if unc:
                    reasons.insert(0, f"{len(unc)} line(s) not executed in the measured run")
            elif run is not None:
                measured = {"run_id": run.id, "missing_line_count": None, "missing_lines": [],
                            "note": "this file was not part of the measured scope"}
            has_tests = bool(tests_for.get(rel))
            if not has_tests:
                reasons.append("no discovered test imports this module" if disc else "tests have not been discovered")
            if not reasons:
                continue
            score = (measured["missing_line_count"] or 0) * 3 if measured and measured.get("missing_line_count") else 0
            score += f.complexity + (3 if not has_tests else 0) + len(f.guards)
            items.append({
                "rel_path": rel, "symbol": f.qualname, "kind": f.kind, "start_line": f.start_line, "end_line": f.end_line,
                "complexity": f.complexity, "reasons": reasons, "has_associated_tests": has_tests if disc else None,
                "associated_tests": sorted(set(tests_for.get(rel, [])))[:5], "measured": measured,
                "evidence": "measured+static" if measured and measured.get("missing_line_count") else "static",
                "confidence": "medium" if measured and measured.get("missing_line_count") else "low",
                "limitations": "Static recommendation: it shows likely untested behaviour, not proof of missing coverage."
                if not (measured and measured.get("missing_line_count")) else
                "Missing lines are measured; whether they matter is a judgement.", "score": score})
    items.sort(key=lambda i: (-i["score"], i["rel_path"], i["start_line"]))
    return {"items": items, "coverage_run_id": run.id if run else None, "discovery_id": disc.id if disc else None}


def _unused(_: Any) -> None:  # keep imports referenced for type checkers
    _ = (ast, hashlib, ValidationFailedError)
