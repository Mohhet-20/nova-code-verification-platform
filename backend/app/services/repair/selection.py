"""Which discovered tests are relevant to a set of changed files, and why. Selection only reads stored discovery and index data."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FailureReport, RepairInvestigation, Repository, TestFile
from app.services import repositories as repo_svc
from app.services.repair.errors import RepairConflict, RepairInvalid
from app.services.review import impact
from app.services.testing import service as testing_service


def known_ids(db: Session, repo: Repository) -> tuple[set[str], dict[str, TestFile]]:
    d = testing_service.latest_discovery(db, repo.id)
    if d is None:
        raise RepairConflict("discovery_required", "Run test discovery first so verification can only target tests NOVA knows about.")
    files = {tf.rel_path: tf for tf in db.scalars(select(TestFile).where(TestFile.discovery_id == d.id, TestFile.parse_status == "ok"))}
    ids = set(files)
    for tf in files.values():
        ids.update(t["id"] for t in tf.tests)
    return ids, files


def is_known(ids: set[str], target: str) -> bool:
    base = target.split("[")[0]
    return target in ids or base in ids or (target.split("::")[0] in ids and "::" not in target)


def select_tests(db: Session, repo: Repository, inv: RepairInvestigation | None, paths: list[str], requested: list[str] | None = None,
                 failure: FailureReport | None = None) -> dict[str, Any]:
    """Return ``{"targets": [{target, reason, kind}], "notes": [...]}``. Requested targets must be discovered tests."""
    ids, files = known_ids(db, repo)
    targets: dict[str, dict[str, str]] = {}
    notes: list[str] = []
    if requested:
        bad = [t for t in requested if not is_known(ids, t)]
        if bad:
            raise RepairInvalid("unknown_test", "Only discovered test files or test ids can be run.",
                                [{"field": "test_ids", "message": f"not discovered: {t[:120]}"} for t in bad[:5]])
        for t in requested:
            targets[t] = {"target": t, "reason": "Selected by the requester.", "kind": "requested"}
        return {"targets": list(targets.values()), "notes": notes}
    if failure is not None and failure.test_id and is_known(ids, failure.test_id):
        targets[failure.test_id] = {"target": failure.test_id, "reason": "The original failing test.", "kind": "failing_test"}
    elif failure is not None:
        notes.append("The original failing test could not be matched to a discovered test.")
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    imp = impact.importers(db, snap.id if snap else None, paths)
    for path in paths:
        for importer in imp.get(path, []):
            if importer in files and importer not in targets:
                targets[importer] = {"target": importer, "reason": f"The test file imports {path} (static import; not proof it exercises the change).",
                                     "kind": "imports_changed_file"}
        for tf in files.values():
            if any(a.get("rel_path") == path for a in (tf.associated_sources or [])) and tf.rel_path not in targets:
                targets[tf.rel_path] = {"target": tf.rel_path, "reason": f"Discovery associated this test file with {path}.", "kind": "associated_source"}
    if not snap:
        notes.append("The repository is not indexed, so tests that import the changed files cannot be found.")
    if not targets:
        notes.append("No discovered test is linked to the changed files. Select tests explicitly or run the whole discovered suite.")
    return {"targets": list(targets.values()), "notes": notes}
