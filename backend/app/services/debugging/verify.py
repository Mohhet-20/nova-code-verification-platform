"""Approved reruns and the comparison with the original failure. Execution always goes workflow."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    FailureOccurrence,
    FailureReport,
    Investigation,
    Repository,
    TestDiscovery,
    TestExecutionRun,
    TestFile,
    VerificationRun,
)
from app.security.auth import RequestContext
from app.services import audit
from app.services.debugging import ingest
from app.services.testing import execution

OUTCOMES = ("PENDING", "PERSISTS", "NOT_REPRODUCED_IN_VERIFICATION_RUN", "CHANGED_FAILURE", "INCONCLUSIVE")
MEANING = {
    "PENDING": "The verification run has not finished.",
    "PERSISTS": "The original failure (same fingerprint) occurred again in the verification run.",
    "NOT_REPRODUCED_IN_VERIFICATION_RUN": ("The selected tests ran to completion and the original failure did not occur in this run. "
                                           "This is not proof of a fix: flaky or order-dependent failures can pass once."),
    "CHANGED_FAILURE": "The same test failed again, but with a different failure signature.",
    "INCONCLUSIVE": "The run could not show whether the failure still occurs.",
}


def valid_targets(db: Session, repo: Repository, requested: list[str] | None, failure: FailureReport) -> tuple[list[str], list[str]]:
    """Return (targets, default_candidates). Targets must be discovered test files or discovered test ids; nothing else runs."""
    d = db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo.id).order_by(TestDiscovery.started_at.desc()).limit(1))
    if d is None:
        raise ConflictError("Run test discovery first so verification can only target tests NOVA knows about.")
    ids: set[str] = set()
    for tf in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id, TestFile.parse_status == "ok")):
        ids.add(tf.rel_path)
        ids.update(t["id"] for t in tf.tests)

    def known(t: str) -> bool:
        base = t.split("[")[0]
        return t in ids or base in ids or (t.split("::")[0] in ids and "::" not in t)

    defaults = [failure.test_id] if failure.test_id and known(failure.test_id) else []
    chosen = requested if requested else defaults
    if not chosen:
        raise ValidationFailedError("Select at least one discovered test to rerun; the failing test could not be matched to discovery.")
    bad = [t for t in chosen if not known(t)]
    if bad:
        raise ValidationFailedError("Only discovered test files or test ids can be run.", [{"field": "test_ids", "message": f"not discovered: {t[:120]}"} for t in bad[:5]])
    return list(dict.fromkeys(t.split("[")[0] if "[" in t and t.split("[")[0] in ids else t for t in chosen)), defaults


def preview(db: Session, repo: Repository, inv: Investigation, failure: FailureReport, test_ids: list[str] | None, coverage: bool) -> dict[str, Any]:
    targets, defaults = valid_targets(db, repo, test_ids, failure)
    return {"executes": False, "requires": "Send the same request with confirm=true to approve and start this run.",
            "targets": targets, "default_targets": defaults, "coverage": coverage,
            "execution": "restricted subprocess on this machine (timeout, minimal environment, bounded output); not a sandbox: "
                         "network and file system are not isolated; the repository's own test code runs.",
            "baseline_execution_run_id": failure.last_execution_run_id,
            "comparison": "The result is compared with fingerprint " + failure.fingerprint[:12] + "."}


def start(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, inv: Investigation, failure: FailureReport,
          test_ids: list[str] | None, coverage: bool) -> VerificationRun:
    if inv.status == "closed":
        raise ConflictError("The investigation is closed.")
    targets, _ = valid_targets(db, repo, test_ids, failure)
    run = execution.begin_existing(db, ctx, settings, repo, coverage, targets)
    vr = VerificationRun(investigation_id=inv.id, repository_id=repo.id, failure_id=failure.id, execution_run_id=run.id,
                         baseline_execution_run_id=failure.last_execution_run_id, test_ids=targets, outcome="PENDING",
                         outcome_reason=MEANING["PENDING"], requested_by=ctx.user.id)
    db.add(vr)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="verification.approved",
                 entity_type="verification_run", entity_id=vr.id,
                 details={"repository_id": repo.id, "investigation_id": inv.id, "execution_run_id": run.id, "targets": len(targets)})
    db.commit()
    return vr


def evaluate(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, vr: VerificationRun) -> VerificationRun:
    """Decide the outcome once the linked run has finished. Idempotent: an evaluated verification is not re-decided."""
    if vr.outcome != "PENDING" or vr.execution_run_id is None:
        return vr
    run = db.get(TestExecutionRun, vr.execution_run_id)
    failure = db.get(FailureReport, vr.failure_id)
    if run is None or failure is None or run.status in ("pending", "running"):
        return vr
    outcome, reason = "INCONCLUSIVE", ""
    if run.status in ("timed_out", "error", "rejected"):
        reason = f"The run ended as '{run.status}'" + (f": {run.reason}" if run.reason else ".") + " No result was produced for the original failure."
    else:
        same = changed = False
        if run.failures:
            try:
                for rep, _ in ingest.ingest_run(db, ctx, settings, repo, run.id):
                    if rep.id == failure.id:
                        same = True
                    elif failure.test_id and rep.test_id == failure.test_id:
                        changed = True
            except ConflictError:
                pass
        if not same and run.failures:  # no ctx path: compare without creating records
            same = bool(db.scalar(select(FailureOccurrence.id).where(FailureOccurrence.failure_id == failure.id,
                                                                     FailureOccurrence.execution_run_id == run.id)))
        if same:
            outcome, reason = "PERSISTS", MEANING["PERSISTS"]
        elif changed:
            outcome, reason = "CHANGED_FAILURE", MEANING["CHANGED_FAILURE"]
        elif (run.tests_collected or 0) < 1 or (run.tests_passed or 0) < 1:
            reason = "No selected test is recorded as passed, so the original failure could not be evaluated."
        elif (run.tests_skipped or 0) > 0:
            reason = "Some selected tests were skipped, so it is unknown whether the original test actually ran."
        elif run.tests_errors:
            reason = "The run reported errors unrelated to the original failure; the original test's result is unclear."
        else:
            outcome, reason = "NOT_REPRODUCED_IN_VERIFICATION_RUN", MEANING["NOT_REPRODUCED_IN_VERIFICATION_RUN"]
    vr.outcome, vr.outcome_reason, vr.evaluated_at = outcome, reason[:500], utcnow()
    db.commit()
    return vr


def summarize_run(run: TestExecutionRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"id": run.id, "kind": run.kind, "status": run.status, "reason": run.reason, "exit_code": run.exit_code,
            "tests_collected": run.tests_collected, "tests_passed": run.tests_passed, "tests_failed": run.tests_failed,
            "tests_errors": run.tests_errors, "tests_skipped": run.tests_skipped, "finished_at": run.finished_at,
            "failures": [{"name": f.get("test"), "message": f.get("message")} for f in (run.failures or [])[:10]]}
