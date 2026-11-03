"""Deterministic root-cause investigations and dependency-aware impact. Hypotheses are labelled and never confirmed here."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError
from app.models import (
    FailureFrame,
    FailureReport,
    FileImport,
    Finding,
    IndexedFile,
    Investigation,
    Relationship,
    Repository,
    Symbol,
    TestDiscovery,
    TestFile,
)
from app.security.auth import RequestContext
from app.services import audit
from app.services import repositories as repo_svc
from app.services.analysis.model import OPEN_STATUSES
from app.services.debugging import localize
from app.services.intelligence import graph, graph_queries

GENERATOR_VERSION = "1.0"
MAX_NODES = 200


def impact(db: Session, settings: Settings, repo: Repository, rel_path: str, symbol_stable_id: str | None,
           depth: int | None = None) -> dict[str, Any]:
    """Dependents of a file (and callers of a symbol) up to ``depth``. Cycles cannot loop: each node is visited once."""
    depth = max(1, min(depth or settings.debug_impact_depth, 4))
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    out: dict[str, Any] = {"rel_path": rel_path, "max_depth": depth, "dependents": [], "callers": [], "related_tests": [],
                           "limitations": [], "graph_available": False, "truncated": False}
    if snap is None:
        out["limitations"].append("The repository has no completed index; no dependency data exists.")
        return out
    f = db.scalar(select(IndexedFile).where(IndexedFile.snapshot_id == snap.id, IndexedFile.rel_path == rel_path))
    if f is None:
        out["limitations"].append("This file is not in the latest index (it may have been removed or renamed).")
        return out
    out["graph_available"] = bool(db.scalar(select(Relationship.id).where(Relationship.snapshot_id == snap.id).limit(1)))
    if not out["graph_available"]:
        out["limitations"].append("The dependency graph is empty; dependents and callers are unknown, not absent.")
        return out
    res = graph.bfs([f.id], graph_queries.file_expander(db, snap.id, "in"), depth, MAX_NODES)
    paths = graph_queries.file_paths(db, list(res.visited))
    for node, v in sorted(res.visited.items(), key=lambda kv: (kv[1].depth, paths.get(kv[0], ""))):
        confirmed = v.edge.get("status") == "resolved" and v.edge.get("evidence") == "observed"
        out["dependents"].append({"rel_path": paths.get(node, "?"), "depth": v.depth, "direct": v.depth == 1,
                                  "relationship": "confirmed_import" if confirmed else "likely_association"})
    out["truncated"] = res.truncated
    if res.truncated:
        out["limitations"].append(f"Traversal stopped at {MAX_NODES} nodes.")
    if res.more_beyond_depth:
        out["limitations"].append(f"More dependents exist beyond depth {depth}; they were not listed.")
    if symbol_stable_id:
        cres = graph.bfs([symbol_stable_id], graph_queries.symbol_reverse_expander(db, snap.id), depth, MAX_NODES)
        syms = {s.stable_id: s for s in db.scalars(select(Symbol).where(
            Symbol.snapshot_id == snap.id, Symbol.stable_id.in_([n for n in cres.visited if not n.startswith("file:")] or [""])))}
        fpaths = graph_queries.file_paths(db, [n[5:] for n in cres.visited if n.startswith("file:")] +
                                          [s.file_id for s in syms.values()])
        for node, v in sorted(cres.visited.items(), key=lambda kv: (kv[1].depth, kv[0])):
            if node.startswith("file:"):
                label, path = "(module level)", fpaths.get(node[5:], "?")
            else:
                s = syms.get(node)
                label, path = (s.qualified_name, fpaths.get(s.file_id, "?")) if s else ("?", "?")
            out["callers"].append({"symbol": label, "rel_path": path, "depth": v.depth,
                                   "relationship": "confirmed_call" if v.edge.get("status") == "resolved" else "likely_association"})
        if cres.truncated or cres.more_beyond_depth:
            out["limitations"].append("The caller list is bounded; more callers may exist.")
    unresolved = db.scalar(select(func.count()).select_from(FileImport).where(
        FileImport.snapshot_id == snap.id, FileImport.resolution_status == "unresolved")) or 0
    if unresolved:
        out["limitations"].append(f"{unresolved} import(s) in the repository could not be resolved; dependents reached only through them are missing.")
    disc = db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo.id).order_by(TestDiscovery.started_at.desc()).limit(1))
    if disc is None:
        out["limitations"].append("Tests have not been discovered, so related tests are unknown.")
    else:
        for tf in db.scalars(select(TestFile).where(TestFile.discovery_id == disc.id)):
            if any(a.get("path") == rel_path and a.get("via") == "import" for a in tf.associated_sources):
                out["related_tests"].append({"rel_path": tf.rel_path, "relationship": "confirmed_import"})
    return out


def _fact(text: str, ref: str) -> dict[str, str]:
    return {"text": text, "ref": ref, "kind": "fact"}


def build_content(db: Session, settings: Settings, repo: Repository, failure: FailureReport, loc: dict[str, Any]) -> dict[str, Any]:
    frames = list(db.scalars(select(FailureFrame).where(FailureFrame.failure_id == failure.id).order_by(FailureFrame.idx)))
    cands = loc["candidates"]
    repo_frames = [f for f in frames if f.scope == "repository"]
    obs = [_fact(f"Failure category: {failure.category}" + (f", exception type {failure.exception_type}." if failure.exception_type else "."), "failure"),
           _fact(f"Seen {failure.occurrence_count} time(s); first {failure.first_seen_at.date().isoformat()}, last {failure.last_seen_at.date().isoformat()}.", "failure")]
    if failure.test_id:
        obs.append(_fact(f"Failing test: {failure.test_id}.", "failure"))
    obs.append(_fact(f"The stack has {len(frames)} frame(s); {len(repo_frames)} map to repository files.", "frames"))
    obs.append(_fact("The source revision is " + (f"recorded as {failure.revision[:12]}." if failure.revision else "unknown (the repository was not a Git checkout or the head could not be read)."), "failure"))
    if cands:
        t = cands[0]
        obs.append(_fact(f"The highest-ranked location is {t['symbol'] or t['rel_path']} ({t['rel_path']}"
                         f"{':' + str(t['failing_line']) if t.get('failing_line') else ''}), role {t['role'].replace('_', ' ')}.", "localization"))
    hyps: list[dict[str, Any]] = []
    top = cands[0] if cands else None

    def sup(c: dict[str, Any]) -> list[dict[str, str]]:
        return [{"text": e["detail"], "ref": f"evidence:{e['id']}"} for e in c["evidence"][:5]]

    def con(c: dict[str, Any]) -> list[dict[str, str]]:
        return [{"text": e["detail"], "ref": f"evidence:{e['id']}"} for e in c["contradicting"]]

    if failure.category == "collection_error":
        hyps.append({"id": "H-collect", "title": "The test module cannot be imported",
                     "statement": f"Collecting {failure.test_id or 'the test module'} failed with {failure.exception_type or 'an error'}; the cause is in the module or something it imports.",
                     "supporting": [{"text": failure.summary, "ref": "failure"}], "contradicting": [],
                     "verification": ["Fix or inspect the reported file and rerun collection through an approved run."]})
    if top is not None and top["role"] in ("exception_origin", "immediate_failure"):
        callers = [c for c in cands if c["role"] == "caller_context"]
        hyps.append({"id": "H-origin", "title": f"Fault in {top['symbol'] or top['rel_path']}",
                     "statement": ("The code at the failing location mishandles its input or state." if top["role"] == "exception_origin" else
                                   "Repository code calls into a dependency or the runtime in a way that fails."),
                     "supporting": sup(top),
                     "contradicting": con(top) + ([{"text": "A caller may have passed invalid input, which would make the callee's rejection correct.", "ref": "frames"}] if callers else []),
                     "verification": [f"Reproduce with only {failure.test_id or 'the failing test'} and inspect the inputs reaching {top['symbol'] or top['rel_path']}.",
                                      "Decide whether the raised error is intended validation or a defect."]})
        if callers:
            c = callers[0]
            hyps.append({"id": "H-caller", "title": f"Invalid input from {c['symbol'] or c['rel_path']}",
                         "statement": "An upstream caller supplies a value the failing code does not accept.", "supporting": sup(c),
                         "contradicting": [{"text": "No evidence shows what value was passed; arguments are not captured in the stack.", "ref": "missing"}],
                         "verification": ["Log or assert the argument at the call site in a local run, then rerun the failing test."]})
    if top is not None and top["role"] == "called_by_test":
        hyps.append({"id": "H-under-test", "title": f"Behaviour of {top['symbol'] or top['rel_path']} differs from the test's expectation",
                     "statement": "The code called on the failing assertion line returns something the test does not expect.",
                     "supporting": sup(top), "contradicting": con(top),
                     "verification": ["Compare the actual and expected values in the assertion message with the documented behaviour."]})
    if failure.category == "assertion":
        hyps.append({"id": "H-expectation", "title": "The test's expectation is wrong or outdated",
                     "statement": "The production code may be correct and the asserted value stale.",
                     "supporting": [{"text": "An assertion failure alone does not say which side is wrong.", "ref": "failure"}],
                     "contradicting": [], "verification": ["Check the requirement or docstring that defines the expected value."]})
    changed = [c for c in cands for e in c["evidence"] if e["type"] == "changed_since_failure"]
    if changed:
        hyps.append({"id": "H-change", "title": f"A change to {changed[0]['rel_path']} is involved",
                     "statement": "The file differs from the version indexed at ingestion; the difference may have introduced or fixed the problem.",
                     "supporting": [{"text": e["detail"], "ref": f"evidence:{e['id']}"} for e in changed[0]["evidence"] if e["type"] == "changed_since_failure"],
                     "contradicting": [{"text": "Only a content hash difference is known; the nature of the change is not.", "ref": "missing"}],
                     "verification": ["Review the actual diff in version control; NOVA has not seen it."]})
    for h in hyps:
        h.update(kind="hypothesis", status="unverified")
    findings = []
    if cands:
        paths = sorted({c["rel_path"] for c in cands[:3]})
        for fd in db.scalars(select(Finding).where(Finding.repository_id == repo.id, Finding.status.in_(OPEN_STATUSES), Finding.rel_path.in_(paths))
                             .order_by(Finding.severity, Finding.rel_path, Finding.start_line).limit(10)):
            findings.append({"id": fd.id, "rule_id": fd.rule_id, "title": fd.title, "severity": fd.severity, "rel_path": fd.rel_path,
                             "start_line": fd.start_line})
    related = []
    if cands:
        paths = sorted({c["rel_path"] for c in cands[:3]})
        rows = db.execute(select(FailureReport.id, FailureReport.summary, FailureReport.occurrence_count, FailureReport.status)
                          .join(FailureFrame, FailureFrame.failure_id == FailureReport.id)
                          .where(FailureReport.repository_id == repo.id, FailureReport.id != failure.id, FailureFrame.rel_path.in_(paths),
                                 FailureFrame.is_test.is_(False)).distinct().order_by(FailureReport.occurrence_count.desc(), FailureReport.id).limit(10)).all()
        related = [{"id": r[0], "summary": r[1], "occurrence_count": r[2], "status": r[3]} for r in rows]
    imp = impact(db, settings, repo, top["rel_path"], top.get("symbol_stable_id"), None) if top else None
    next_tests = []
    if failure.test_id:
        next_tests.append(f"Rerun {failure.test_id} on its own through the approved verification workflow.")
    if top:
        next_tests.append(f"Plan additional tests for {top['symbol'] or top['rel_path']} in the Tests workspace (static suggestions; review before running).")
    for t in (imp or {}).get("related_tests", [])[:3]:
        next_tests.append(f"Include {t['rel_path']} (imports the candidate file) in a verification run.")
    missing = list(loc["missing_evidence"])
    if failure.occurrence_count == 1:
        missing.append("The failure has been seen once; flakiness cannot be ruled out.")
    return {
        "summary": failure.summary, "observations": obs, "candidates": cands[:5], "localization_state": loc["state"],
        "hypotheses": hyps, "missing_information": missing,
        "verification_steps": ["Review the ranked locations and decide which hypothesis to test first.",
                               "Select the relevant tests and review the execution request.",
                               "Approve a verification run; NOVA compares its result with this failure.",
                               "Treat a passing run as 'not reproduced in that run', not as proof of a fix."],
        "next_tests": next_tests, "related_findings": findings, "related_failures": related, "impact": imp,
        "limitations": localize.LIMITATIONS + ["Hypotheses are generated from templates and evidence; none has been verified.",
                                               "No AI provider was used."] + loc.get("notes", []),
    }


def create(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, failure: FailureReport) -> Investigation:
    open_inv = db.scalar(select(Investigation).where(Investigation.failure_id == failure.id, Investigation.status != "closed"))
    if open_inv is not None:
        raise ConflictError("An investigation is already open for this failure.", {"investigation_id": open_inv.id})
    loc = localize.run_localization(db, settings, repo, failure)
    content = build_content(db, settings, repo, failure, loc)
    inv = Investigation(repository_id=repo.id, failure_id=failure.id, status="open", content=content, notes="",
                        generator_version=GENERATOR_VERSION, index_snapshot_id=loc["snapshot_id"], created_by=ctx.user.id)
    db.add(inv)
    db.flush()
    from sqlalchemy.exc import IntegrityError

    if failure.status == "open":
        failure.status = "investigating"
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="investigations.created",
                 entity_type="investigation", entity_id=inv.id, details={"repository_id": repo.id, "failure_id": failure.id})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "UNIQUE" not in str(exc.orig).upper():
            raise
        raise ConflictError("An investigation is already open for this failure.") from None
    return inv

