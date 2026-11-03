"""Starting, cancelling, linking and describing verification runs and baselines."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.db.base import utcnow
from app.models import (
    CodeReview,
    CvBaseline,
    CvEvent,
    CvRun,
    Investigation,
    RefactorTransformation,
    RepairInvestigation,
    Repository,
)
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services.testing import execution
from app.services.verification import changes, engine, planner, policy, regression
from app.services.verification.errors import CvConflict, CvInvalid, CvNotFound

LINK_TYPES = {"repair": RepairInvestigation, "refactor": RefactorTransformation, "review": CodeReview, "debugging": Investigation}


def get_run(db: Session, repo_id: str, run_id: str) -> CvRun:
    run = db.scalar(select(CvRun).where(CvRun.id == run_id, CvRun.repository_id == repo_id))
    if run is None:
        raise CvNotFound("run_not_found", "Verification run not found.")
    return run


def get_baseline(db: Session, repo_id: str, baseline_id: str) -> CvBaseline:
    b = db.scalar(select(CvBaseline).where(CvBaseline.id == baseline_id, CvBaseline.repository_id == repo_id))
    if b is None:
        raise CvNotFound("baseline_not_found", "Baseline not found.")
    return b


def latest_baseline(db: Session, repo_id: str) -> CvBaseline | None:
    return db.scalar(select(CvBaseline).where(CvBaseline.repository_id == repo_id, CvBaseline.status == "valid")
                     .order_by(CvBaseline.created_at.desc(), CvBaseline.id.desc()).limit(1))


def recover_interrupted_runs(db: Session) -> int:
    n = 0
    for run in db.scalars(select(CvRun).where(CvRun.status.in_(("queued", "running")))):
        run.status, run.finished_at, run.reason = "error", utcnow(), "Interrupted: the backend stopped before verification finished."
        run.gate_outcome = "BLOCKED"
        run.gate_decision = {"outcome": "BLOCKED", "reason": run.reason, "rules": [], "precedence": list(policy.PRECEDENCE), "meaning": run.reason}
        n += 1
    db.commit()
    return n


def preview(db: Session, settings: Settings, repo: Repository, *, source: str, mode: str | None, base: str | None, head: str | None,
            paths: list[str] | None, patch: str | None, coverage: bool) -> dict[str, Any]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    _, rules, _ = policy.current(db, repo.id)
    cs = changes.resolve(root, settings, source, base=base, head=head, paths=paths, patch=patch)
    pl = planner.plan(db, settings, repo, root, cs, mode or rules["default_mode"], rules, coverage)
    return {"executes": False, "change_set": cs.public(), "plan": pl.public(), "policy_version": policy.current(db, repo.id)[0],
            "requires": "Send the same request to POST /verification/runs with confirm=true to start it."}


def start(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, *, source: str, mode: str | None, base: str | None,
          head: str | None, paths: list[str] | None, patch: str | None, coverage: bool, baseline_id: str | None, confirm: bool,
          link: dict[str, str] | None) -> CvRun:
    ctx.require_write()
    if not confirm:
        raise CvInvalid("confirmation_required", "A verification run executes the repository's tests. Send confirm=true to start it.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    version, rules, _ = policy.current(db, repo.id)
    cs = changes.resolve(root, settings, source, base=base, head=head, paths=paths, patch=patch)
    pl = planner.plan(db, settings, repo, root, cs, mode or rules["default_mode"], rules, coverage)
    fp, fp_warn, git_head = changes.tree_fingerprint(root, settings)
    baseline: CvBaseline | None
    implicit = False
    if baseline_id:
        baseline = get_baseline(db, repo.id, baseline_id)
    else:
        baseline = latest_baseline(db, repo.id)
        implicit = baseline is not None
    link_obj = _validate_link(db, repo.id, link) if link else {}
    snap_id = None
    from app.services import repositories as repo_svc
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    snap_id = snap.id if snap else None
    public = cs.public()
    public["spec"] = {"source": source, "base": base, "head": head, "paths": paths, "patch": patch}
    run = CvRun(repository_id=repo.id, kind="verify", status="queued", mode=pl.mode, source=source, change_set=public, head_sha=git_head,
                base_sha=cs.base_sha, tree_fingerprint=fp, index_snapshot_id=snap_id, plan=pl.public(), policy_version=version,
                policy_snapshot=rules, baseline_id=baseline.id if baseline else None, link=link_obj, requested_by=ctx.user.id)
    _persist(db, ctx, settings, repo, run, pl, coverage or any(c.check_id == "coverage" for c in pl.checks), fp_warn)
    run.plan = {**run.plan, "baseline": {"id": baseline.id if baseline else None, "selection": ("latest valid baseline (implicit)" if implicit else "explicit" if baseline else "none")}}
    db.commit()
    return run


def start_baseline(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, *, label: str, coverage: bool, confirm: bool) -> CvRun:
    ctx.require_write()
    if not confirm:
        raise CvInvalid("confirmation_required", "Recording a baseline runs the repository's whole test suite. Send confirm=true to start it.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    version, rules, _ = policy.current(db, repo.id)
    fp, fp_warn, git_head = changes.tree_fingerprint(root, settings)
    cs = changes.ChangeSet("baseline", git_head, None, git_head, [])
    pl = planner.plan(db, settings, repo, root, changes.ChangeSet("manifest", git_head, None, None, [changes.ChangedFile("baseline.py", "modified", None, None, "")]),
                      "full", rules, coverage)
    # a baseline is the whole suite; syntax / static do not apply
    pl.checks = [c for c in pl.checks if c.check_id in ("tests", "coverage")]
    for i, c in enumerate(pl.checks):
        c.ordinal, c.required = i, c.check_id == "tests"
    t = pl.check("tests")
    if t is not None and t.status == "planned":
        t.reason = "Run every discovered test to record the failures that exist before the change."
    pl.binding = []
    run = CvRun(repository_id=repo.id, kind="baseline", status="queued", mode="full", source="baseline",
                change_set={"label": (label or "")[:120], "files": [], "counts": {"total": 0}, "fingerprint": cs.fingerprint(), "source": "baseline"},
                head_sha=git_head, tree_fingerprint=fp, plan=pl.public(), policy_version=version, policy_snapshot=rules, requested_by=ctx.user.id)
    _persist(db, ctx, settings, repo, run, pl, coverage, fp_warn)
    db.commit()
    return run


def _persist(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, run: CvRun, pl: planner.Plan, coverage: bool, warn: list[str]) -> None:
    from app.models import CvCheck
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(CvRun).where(CvRun.repository_id == repo.id, CvRun.status.in_(("queued", "running"))))
        raise CvConflict("run_in_progress", "A verification run is already in progress for this repository.", {"run_id": active.id if active else None}) from None
    for c in pl.checks:
        db.add(CvCheck(run_id=run.id, repository_id=repo.id, check_id=c.check_id, ordinal=c.ordinal, tier=c.tier, required=c.required, reason=c.reason,
                       confidence=c.confidence, prerequisites=c.prerequisites, status=c.status, result=c.result))
    t = pl.check("tests")
    if t is not None and t.status == "planned":
        try:
            ex = execution.begin_existing(db, ctx, settings, repo, coverage, pl.test_targets)
            run.execution_run_id = ex.id
        except ConflictError as exc:
            if "already in progress" in exc.message:
                db.rollback()
                raise
            t_row = db.scalar(select(CvCheck).where(CvCheck.run_id == run.id, CvCheck.check_id == "tests"))
            if t_row is not None:
                t_row.status, t_row.result = "blocked", {"detail": exc.message}
    event_kind = "run_queued"
    db.add(CvEvent(run_id=run.id, repository_id=repo.id, kind=event_kind, detail={"mode": run.mode, "source": run.source, "warnings": warn}))
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action=f"verification.{run.kind}_requested", entity_type="cv_run",
                 entity_id=run.id, details={"repository_id": repo.id, "mode": run.mode, "source": run.source, "files": len(run.change_set.get("files", []))})


def cancel(db: Session, ctx: RequestContext, repo: Repository, run: CvRun, settings: Settings) -> CvRun:
    ctx.require_write()
    if run.status not in ("queued", "running"):
        raise CvConflict("run_finished", f"The run is already '{run.status}' and cannot be cancelled.")
    run.cancel_requested = True
    engine.event(db, run, "cancel_requested", by=ctx.user.id)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="verification.run_cancel_requested", entity_type="cv_run",
                 entity_id=run.id, details={"repository_id": repo.id})
    if run.status == "queued":
        root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
        for c in engine.checks_of(db, run.id):
            if c.status == "planned":
                c.status, c.finished_at = "cancelled", utcnow()
        engine._reject_pending_exec(db, run, "Cancelled before the tests started.")
        engine.finalize(db, settings, run, root, None, {"cancelled": True})
    db.commit()
    return run


def link_run(db: Session, ctx: RequestContext, repo: Repository, run: CvRun, link: dict[str, str]) -> CvRun:
    ctx.require_write()
    run.link = _validate_link(db, repo.id, link)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="verification.run_linked", entity_type="cv_run", entity_id=run.id,
                 details={"repository_id": repo.id, **run.link})
    db.commit()
    return run


def _validate_link(db: Session, repo_id: str, link: dict[str, str]) -> dict[str, Any]:
    model = LINK_TYPES.get(link.get("type", ""))
    if model is None:
        raise CvInvalid("invalid_link", f"type must be one of {', '.join(sorted(LINK_TYPES))}.")
    obj = db.get(model, link.get("id", ""))
    if obj is None or getattr(obj, "repository_id", None) != repo_id:
        raise CvNotFound("link_not_found", "The linked record does not exist in this repository.")
    return {"type": link["type"], "id": obj.id}


def compare(db: Session, settings: Settings, repo: Repository, run: CvRun, baseline_id: str) -> dict[str, Any]:
    from app.models import TestExecutionRun
    baseline = get_baseline(db, repo.id, baseline_id)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    ex = db.get(TestExecutionRun, run.execution_run_id) if run.execution_run_id else None
    scope = engine._scope(run.plan or {})
    compat = regression.compatibility(baseline, regression.config_hash(root, ex), scope)
    cov_requested = bool((run.coverage or {}).get("status") not in (None, "not_requested"))
    paths = [f["path"] for f in (run.change_set or {}).get("files", [])]
    return {"run_id": run.id, "baseline_id": baseline.id, "persisted": False, "compatibility": compat,
            "regression": regression.classify(db, settings, run, ex, baseline, compat, scope),
            "coverage": regression.coverage_summary(db, run, ex, baseline, compat, paths, cov_requested),
            "note": "This comparison is recomputed on request and does not change the run's recorded gate decision."}


def run_staleness(settings: Settings, repo: Repository, run: CvRun) -> dict[str, Any]:
    if run.status not in ("completed", "cancelled") or run.kind != "verify":
        return {"stale": None, "reason": None}
    try:
        root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
        fp, _, head = changes.tree_fingerprint(root, settings)
    except Exception:  # noqa: BLE001
        return {"stale": None, "reason": "The current source state could not be read."}
    if fp != run.tree_fingerprint:
        return {"stale": True, "reason": "The working tree or HEAD changed after this run; its result describes an earlier state.", "current_head": head}
    return {"stale": False, "reason": None, "current_head": head}


def baseline_out(b: CvBaseline) -> dict[str, Any]:
    return {"id": b.id, "label": b.label, "status": b.status, "run_id": b.run_id, "execution_run_id": b.execution_run_id, "head_sha": b.head_sha,
            "tree_fingerprint": b.tree_fingerprint, "scope": b.scope if b.scope.get("kind") == "all" else {"kind": "files", "files": (b.scope.get("files") or [])[:100]},
            "failing": (b.failing or [])[:200], "totals": b.totals, "coverage": b.coverage, "integrity_ok": regression.baseline_digest(b) == b.integrity_sha256,
            "created_by": b.created_by, "created_at": b.created_at}


def run_summary(run: CvRun) -> dict[str, Any]:
    return {"id": run.id, "kind": run.kind, "status": run.status, "mode": run.mode, "source": run.source, "head_sha": run.head_sha, "base_sha": run.base_sha,
            "tree_fingerprint": run.tree_fingerprint, "policy_version": run.policy_version, "baseline_id": run.baseline_id,
            "execution_run_id": run.execution_run_id, "gate_outcome": run.gate_outcome, "reason": run.reason, "cancel_requested": run.cancel_requested,
            "files_changed": (run.change_set or {}).get("counts", {}).get("total", 0), "link": run.link, "requested_by": run.requested_by,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at}


def overview(db: Session, repo: Repository) -> dict[str, Any]:
    version, rules, row = policy.current(db, repo.id)
    latest = db.scalar(select(CvRun).where(CvRun.repository_id == repo.id, CvRun.kind == "verify").order_by(CvRun.created_at.desc()).limit(1))
    counts = dict(db.execute(select(CvRun.gate_outcome, func.count()).where(CvRun.repository_id == repo.id, CvRun.kind == "verify", CvRun.gate_outcome.is_not(None))
                             .group_by(CvRun.gate_outcome)).all())
    active = db.scalar(select(CvRun).where(CvRun.repository_id == repo.id, CvRun.status.in_(("queued", "running"))))
    b = latest_baseline(db, repo.id)
    return {"policy_version": version, "policy_is_default": row is None, "policy": rules, "latest_run": run_summary(latest) if latest else None,
            "latest_baseline": baseline_out(b) if b else None, "active_run": run_summary(active) if active else None,
            "gate_counts": {k: int(counts.get(k, 0)) for k in ("PASS", "FAIL", "BLOCKED", "INCOMPLETE")}}
