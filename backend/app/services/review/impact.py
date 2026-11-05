"""Dependency-aware impact of a set of changed files, reusing the bounded breadth-first traversal (cycles cannot loop)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    FailureFrame,
    FailureReport,
    IndexedFile,
    Relationship,
    Repository,
    TestDiscovery,
    TestFile,
)
from app.services import repositories as repo_svc
from app.services.debugging import investigate

MAX_FILES_DETAILED = 40
MAX_LIST = 20


def _importers_by_path(db: Session, snapshot_id: str, paths: list[str]) -> dict[str, list[str]]:
    """Files that import each given path (direct, resolved IMPORTS_FILE edges in the index)."""
    if not paths:
        return {}
    target = {f.id: f.rel_path for f in db.scalars(select(IndexedFile).where(IndexedFile.snapshot_id == snapshot_id, IndexedFile.rel_path.in_(paths)))}
    if not target:
        return {}
    rows = db.execute(select(Relationship.target_file_id, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Relationship.source_file_id)
                      .where(Relationship.snapshot_id == snapshot_id, Relationship.type == "IMPORTS_FILE",
                             Relationship.target_file_id.in_(list(target)), Relationship.status == "resolved")).all()
    out: dict[str, list[str]] = {}
    for tid, src in rows:
        out.setdefault(target[tid], []).append(src)
    return {k: sorted(set(v)) for k, v in out.items()}


def importers(db: Session, snapshot_id: str | None, paths: list[str]) -> dict[str, list[str]]:
    return _importers_by_path(db, snapshot_id, paths) if snapshot_id else {}


def build(db: Session, settings: Settings, repo: Repository, snapshot_id: str | None, changed: list[dict[str, Any]],
          index_stale: bool) -> dict[str, Any]:
    """``changed``: dicts with path, old_path, change_type, is_test, symbol_ids (stable ids of modified symbols)."""
    depth = settings.review_impact_depth
    code = [c for c in changed if not c["is_test"]]
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    out: dict[str, Any] = {"index_snapshot_id": snapshot_id, "index_stale": index_stale, "max_depth": depth, "graph_available": False,
                           "files": [], "dependents_total": 0, "dependents_incomplete": False, "affected_tests": [],
                           "failures": [], "limitations": [], "complete": True}
    lim = out["limitations"]
    if snap is None:
        lim.append("The repository has no completed index; dependents, callers and tests are unknown.")
        out["complete"] = False
        return out
    if index_stale:
        lim.append("The index does not describe some of the reviewed files; dependents come from the indexed version and may be incomplete.")
        out["complete"] = False
    all_dependents: dict[str, dict[str, Any]] = {}
    tests: dict[str, dict[str, Any]] = {}
    graph_any = False
    for c in code[:MAX_FILES_DETAILED]:
        probe = c.get("old_path") or c["path"]
        res = investigate.impact(db, settings, repo, probe, None, depth)
        graph_any = graph_any or res["graph_available"]
        by_caller: dict[tuple[str, str], dict[str, Any]] = {}
        for sid in (c.get("symbol_ids") or [])[:5]:
            sres = investigate.impact(db, settings, repo, probe, sid, depth)
            for cl in sres["callers"]:
                key = (cl["symbol"], cl["rel_path"])
                if key not in by_caller or cl["depth"] < by_caller[key]["depth"]:
                    by_caller[key] = cl
        callers = sorted(by_caller.values(), key=lambda x: (x["depth"], x["rel_path"], x["symbol"]))
        entry = {"rel_path": c["path"], "change_type": c["change_type"], "indexed": res["graph_available"] or not any("not in the latest index" in x for x in res["limitations"]),
                 "direct_dependents": sum(1 for d in res["dependents"] if d["direct"]),
                 "transitive_dependents": sum(1 for d in res["dependents"] if not d["direct"]),
                 "dependents": res["dependents"][:MAX_LIST], "callers": callers[:MAX_LIST], "truncated": res["truncated"]}
        out["files"].append(entry)
        for d in res["dependents"]:
            cur = all_dependents.get(d["rel_path"])
            if cur is None or d["depth"] < cur["depth"]:
                all_dependents[d["rel_path"]] = d
        for t in res["related_tests"]:
            tests.setdefault(t["rel_path"], {"rel_path": t["rel_path"], "relationship": "imports_changed_file", "covers": [], "note":
                                             "This test file imports a changed file. That does not show it exercises the changed behavior."})["covers"].append(c["path"])
        for x in res["limitations"]:
            if x not in lim:
                lim.append(x)
        out["dependents_incomplete"] = out["dependents_incomplete"] or res["truncated"] or any("beyond depth" in x for x in res["limitations"])
    if len(code) > MAX_FILES_DETAILED:
        lim.append(f"Impact was computed for the first {MAX_FILES_DETAILED} of {len(code)} changed code files.")
        out["complete"] = False
    changed_paths = {c["path"] for c in changed} | {c["old_path"] for c in changed if c.get("old_path")}
    out["graph_available"] = graph_any
    own = {c["path"] for c in code}
    deps = [d for p, d in sorted(all_dependents.items()) if p not in own]
    out["dependents_total"] = len(deps)
    out["dependents"] = deps[:MAX_LIST * 2]
    out["affected_tests"] = [dict(t, covers=sorted(set(t["covers"]))) for _, t in sorted(tests.items())][:MAX_LIST * 2]
    disc = db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo.id).order_by(TestDiscovery.started_at.desc()).limit(1))
    out["tests_discovered"] = disc is not None
    if disc is None:
        lim.append("Tests have not been discovered, so affected tests are unknown (not absent).")
    if not graph_any:
        out["complete"] = False
    rows = db.execute(select(FailureReport, FailureFrame.rel_path).join(FailureFrame, FailureFrame.failure_id == FailureReport.id)
                      .where(FailureReport.repository_id == repo.id, FailureReport.status.in_(("open", "investigating")),
                             FailureFrame.scope == "repository", FailureFrame.rel_path.in_(sorted(changed_paths) or [""]))).all()
    seen: dict[str, dict[str, Any]] = {}
    for rep, path in rows:
        e = seen.setdefault(rep.id, {"id": rep.id, "summary": rep.summary[:200], "test_id": rep.test_id, "status": rep.status, "files": set()})
        e["files"].add(path)
    out["failures"] = [dict(e, files=sorted(e["files"])) for e in sorted(seen.values(), key=lambda e: e["id"])][:MAX_LIST]
    out["has_failure_history"] = bool(db.scalar(select(FailureReport.id).where(FailureReport.repository_id == repo.id).limit(1)))
    return out


def tests_state_for(db: Session, repo: Repository, paths: list[str], changed_test_paths: set[str]) -> dict[str, Any]:
    """Static test association for the given source paths from the latest discovery (import-based only)."""
    disc = db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo.id).order_by(TestDiscovery.started_at.desc()).limit(1))
    if disc is None:
        return {"discovered": False, "associated": {}, "any_test_changed": bool(changed_test_paths)}
    assoc: dict[str, list[str]] = {}
    for tf in db.scalars(select(TestFile).where(TestFile.discovery_id == disc.id)):
        for a in tf.associated_sources:
            if a.get("via") == "import" and a.get("path") in paths:
                assoc.setdefault(a["path"], []).append(tf.rel_path)
    return {"discovered": True, "associated": {k: sorted(v) for k, v in assoc.items()}, "any_test_changed": bool(changed_test_paths),
            "discovery_id": disc.id}
