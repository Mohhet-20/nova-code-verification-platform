"""Evidence assembly and root-cause ranking for repair investigations.

Everything here is derived from stored NOVA data or the files on disk. Nothing is inferred from names alone and nothing is invented:
a statement is either a *fact* (``basis`` observed, or ``stated_by_requester`` for the requester's own words), a *hypothesis*
(a ranked candidate location), an *unknown*, or a suggested *next step*.

Ranking method (version 1.0). Each candidate location (file, optional symbol) collects evidence items; its score is their sum.
=====================================  ======  ==========================================================================
Evidence type                          Weight  Source
=====================================  ======  ==========================================================================
source_finding                         +5.0    the investigation was opened from this static/review finding at this location
localization                           +score  Phase 06 localization score for a linked failure (already net of its own contradictions)
static_finding_in_symbol               +3.0    an open stored static finding inside the candidate symbol (at most 2 per candidate)
static_finding_in_file                 +1.0    an open stored finding elsewhere in the candidate file (once)
uncommitted_change                     +1.5    Git reports the file as changed in the working tree
user_hint                              +1.0    the requester named the file (an indexed, existing file)
description_mentions_file              +1.0    the description names an indexed path
=====================================  ======  ==========================================================================
Confidence for rank 1: ``high`` when score >= 6, lead over rank 2 >= 2 and at least two different evidence types; ``medium`` when
score >= 3.5; otherwise ``low``. Rank > 1 is never ``high``. If there is no candidate or the best score is below
``repair_min_root_cause_score`` the result is ``insufficient_evidence``.
Limits: scores are priorities for where to look, not probabilities; correlation is not causation; unindexed code is not ranked;
runtime values are not visible; the ranking cannot see code that is not on disk.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import (
    CoverageFile,
    FailureFrame,
    FailureReport,
    Finding,
    IndexedFile,
    Investigation,
    LocalizationCandidate,
    RepairInvestigation,
    Repository,
    ReviewFinding,
    Symbol,
)
from app.security.paths import UnsafePathError, resolve_repository_root, safe_join
from app.services import repositories as repo_svc
from app.services.analysis.model import OPEN_STATUSES
from app.services.analysis.redaction import scrub
from app.services.debugging import localize
from app.services.review import gitops
from app.services.testing import execution

VERSION = "1.0"
WEIGHTS = {"source_finding": 5.0, "localization": "score", "static_finding_in_symbol": 3.0, "static_finding_in_file": 1.0,
           "uncommitted_change": 1.5, "user_hint": 1.0, "description_mentions_file": 1.0}
LIMITATIONS = [
    "Scores rank where to look first; they are not probabilities and not proof of a defect.",
    "Only indexed, on-disk code can be ranked; runtime values are not visible in a traceback or a finding.",
    "Correlation between evidence and a location is not causation.",
]
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,5}")


def _ev(eid: str, etype: str, weight: float, detail: str, ref: str) -> dict[str, Any]:
    return {"id": eid, "type": etype, "weight": round(weight, 2), "detail": detail[:300], "ref": ref}


class _Cand:
    def __init__(self, rel_path: str, symbol: dict[str, Any] | None) -> None:
        self.rel_path = rel_path
        self.symbol = symbol
        self.evidence: dict[str, dict[str, Any]] = {}
        self.contradicting: list[dict[str, Any]] = []
        self.line: int | None = None

    def add(self, ev: dict[str, Any]) -> None:
        self.evidence.setdefault(ev["id"], ev)

    @property
    def score(self) -> float:
        return round(sum(e["weight"] for e in self.evidence.values()), 2)

    @property
    def types(self) -> set[str]:
        return {e["type"] for e in self.evidence.values()}


def symbol_at(db: Session, snapshot_id: str | None, rel_path: str, line: int | None) -> dict[str, Any] | None:
    """The smallest indexed symbol containing ``line``, or None (never guessed)."""
    if snapshot_id is None or line is None:
        return None
    rows = db.execute(
        select(Symbol).join(IndexedFile, Symbol.file_id == IndexedFile.id)
        .where(Symbol.snapshot_id == snapshot_id, IndexedFile.rel_path == rel_path, Symbol.start_line <= line, Symbol.end_line >= line)
    ).scalars().all()
    if not rows:
        return None
    s = min(rows, key=lambda r: (r.end_line - r.start_line, r.start_line))
    return {"name": s.qualified_name, "stable_id": s.stable_id, "start_line": s.start_line, "end_line": s.end_line, "kind": s.kind}


def load_sources(db: Session, repo: Repository, inv: RepairInvestigation) -> dict[str, Any]:
    failure = db.get(FailureReport, inv.failure_id) if inv.failure_id else None
    dbg = db.get(Investigation, inv.debug_investigation_id) if inv.debug_investigation_id else None
    if failure is None and dbg is not None:
        failure = db.get(FailureReport, dbg.failure_id)
    finding = db.get(Finding, inv.finding_id) if inv.finding_id else None
    rfinding = db.get(ReviewFinding, inv.review_finding_id) if inv.review_finding_id else None
    for obj in (failure, finding, rfinding):
        if obj is not None and obj.repository_id != repo.id:
            raise ValueError("source belongs to another repository")
    return {"failure": failure, "finding": finding, "review_finding": rfinding}


def valid_hint(root: Path, indexed: set[str], rel: str) -> str | None:
    try:
        target = safe_join(root, rel)
    except UnsafePathError:
        return None
    return rel if (rel in indexed or target.is_file()) and rel and not rel.startswith(".git/") else None


def assemble(db: Session, settings: Settings, repo: Repository, inv: RepairInvestigation) -> dict[str, Any]:
    """(Re)build the evidence for ``inv`` and store it. Deterministic for identical stored data and files."""
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    sid = snap.id if snap else None
    indexed: dict[str, IndexedFile] = {}
    if snap:
        indexed = {f.rel_path: f for f in db.scalars(select(IndexedFile).where(IndexedFile.snapshot_id == snap.id))}
    src = load_sources(db, repo, inv)
    failure: FailureReport | None = src["failure"]
    finding: Finding | None = src["finding"]
    rfinding: ReviewFinding | None = src["review_finding"]
    facts: list[dict[str, Any]] = []
    unknowns: list[str] = []
    next_steps: list[str] = []
    cands: dict[tuple[str, str], _Cand] = {}

    def cand(path: str, line: int | None) -> _Cand:
        sym = symbol_at(db, sid, path, line)
        c = cands.setdefault((path, sym["name"] if sym else ""), _Cand(path, sym))
        if c.line is None:
            c.line = line
        return c

    def fact(text: str, source: str, ref: str, basis: str = "observed") -> None:
        facts.append({"id": f"F{len(facts) + 1}", "text": scrub(text, 400), "source": source, "ref": ref, "basis": basis})

    if inv.description.strip():
        fact("Requester's description: " + inv.description.strip().replace("\n", " ")[:300], "user_description", "description", "stated_by_requester")
    if not snap:
        unknowns.append("The repository has no completed index, so locations cannot be mapped to symbols or checked against the code graph.")
        next_steps.append("Index the repository so evidence can be tied to symbols.")

    # --- failure evidence
    if failure is not None:
        fact(f"Failure {failure.category}: {failure.summary} (test {failure.test_id or 'unknown'}, seen {failure.occurrence_count}x).",
             "failure_report", failure.id)
        frames = list(db.scalars(select(FailureFrame).where(FailureFrame.failure_id == failure.id).order_by(FailureFrame.idx).limit(40)))
        shown = 0
        for fr in frames:
            if fr.scope == "repository" and not fr.is_test and fr.rel_path and shown < 4:
                fact(f"Stack frame in {fr.rel_path}:{fr.line} ({fr.function or 'unknown function'}).", "failure_frame", f"{failure.id}#{fr.idx}")
                shown += 1
        rows = list(db.scalars(select(LocalizationCandidate).where(LocalizationCandidate.failure_id == failure.id).order_by(LocalizationCandidate.rank)))
        if not rows and snap:
            try:
                res = localize.run_localization(db, settings, repo, failure)
                rows = list(db.scalars(select(LocalizationCandidate).where(LocalizationCandidate.failure_id == failure.id).order_by(LocalizationCandidate.rank)))
                for m in res.get("missing_evidence", [])[:5]:
                    unknowns.append(f"Localization: {m}")
            except Exception:  # noqa: BLE001 - localization failing must not block assembling the rest
                unknowns.append("Failure localization could not be computed for this failure.")
        if not rows:
            unknowns.append("No localization candidates exist for the linked failure.")
        for r in rows[:5]:
            c = cands.setdefault((r.rel_path, r.symbol_name or ""), _Cand(r.rel_path, (
                {"name": r.symbol_name, "stable_id": r.symbol_stable_id, "start_line": r.start_line, "end_line": r.end_line, "kind": "symbol"}
                if r.symbol_name else None)))
            c.line = c.line or r.start_line
            c.add(_ev(f"loc:{r.rank}", "localization", r.score, f"Failure localization rank {r.rank} ({r.role.replace('_', ' ')}).", failure.id))
            c.contradicting.extend(r.contradicting or [])
        sim = list(db.scalars(select(FailureReport).where(
            FailureReport.repository_id == repo.id, FailureReport.id != failure.id, FailureReport.category == failure.category,
            FailureReport.exception_type == failure.exception_type).order_by(FailureReport.last_seen_at.desc()).limit(5))) if failure.exception_type else []
        similar = [{"id": s.id, "summary": s.summary[:160], "test_id": s.test_id, "status": s.status} for s in sim]
        if failure.last_execution_run_id and not inv.baseline_execution_run_id:
            inv.baseline_execution_run_id = failure.last_execution_run_id
    else:
        similar = []
        next_steps.append("Attach a failing test result (ingest the pytest output) so the failure can be localized from the stack.")
        unknowns.append("No failure report is linked, so there is no runtime evidence (stack, exception) for this defect.")

    # --- finding evidence
    for kind, f_ in (("static finding", finding), ("review finding", rfinding)):
        if f_ is None:
            continue
        line = getattr(f_, "start_line", None)
        fact(f"{kind.capitalize()} {f_.rule_id} ({f_.severity}) at {f_.rel_path}:{line or '?'}: {f_.title}.",
             "static_finding" if f_ is finding else "review_finding", f_.id)
        c = cand(f_.rel_path, line)
        c.add(_ev(f"src:{f_.id}", "source_finding", WEIGHTS["source_finding"], f"Investigation opened from {f_.rule_id} at this location.", f_.id))

    # --- requester hints and description mentions
    for h in inv.hint_files:
        ok = valid_hint(root, set(indexed), h)
        if ok is None:
            unknowns.append(f"The hinted file {h[:120]!r} is not a file in this repository and was ignored.")
            continue
        c = cands.setdefault((ok, ""), _Cand(ok, None))
        c.add(_ev(f"hint:{ok}", "user_hint", 1.0, "The requester named this file.", "hint"))
    for tok in dict.fromkeys(PATH_TOKEN.findall(inv.description or "")):
        if tok in indexed:
            c = cands.setdefault((tok, ""), _Cand(tok, None))
            c.add(_ev(f"mention:{tok}", "description_mentions_file", 1.0, "The description names this indexed file.", "description"))

    # --- static findings, uncommitted changes, coverage, staleness for each candidate file
    changed: set[str] = set()
    try:
        prep = gitops.prepare(root, settings, "working_tree", None, None)
        acq = gitops.acquire(root, settings, prep, [], time.monotonic() + 20.0, with_hunks=False)
        changed = {c.path for c in acq.changes}
        fact(f"Git reports {len(changed)} changed file(s) in the working tree.", "git", "working_tree")
    except Exception:  # noqa: BLE001 - git is optional evidence
        unknowns.append("Uncommitted changes could not be read (not a Git repository, or Git is unavailable).")
    for (path, _sym), c in sorted(cands.items()):
        stored = list(db.scalars(select(Finding).where(Finding.repository_id == repo.id, Finding.rel_path == path,
                                                       Finding.status.in_(OPEN_STATUSES)).limit(50)))
        inside = 0
        outside_added = False
        for f_ in stored:
            if finding is not None and f_.id == finding.id:
                continue
            in_sym = c.symbol is not None and c.symbol.get("start_line") is not None and \
                c.symbol["start_line"] <= f_.start_line <= (c.symbol["end_line"] or f_.start_line)
            if in_sym and inside < 2:
                inside += 1
                c.add(_ev(f"fis:{f_.id}", "static_finding_in_symbol", 3.0, f"Open finding {f_.rule_id} at line {f_.start_line} inside this symbol.", f_.id))
            elif not in_sym and not outside_added:
                outside_added = True
                c.add(_ev(f"fif:{f_.id}", "static_finding_in_file", 1.0, f"Open finding {f_.rule_id} elsewhere in this file.", f_.id))
        if path in changed:
            c.add(_ev(f"chg:{path}", "uncommitted_change", 1.5, "The file has uncommitted changes.", "git"))
        run = execution.latest_measured(db, repo.id)
        if run is not None:
            cov = db.scalar(select(CoverageFile).where(CoverageFile.execution_run_id == run.id, CoverageFile.rel_path == path))
            if cov is not None:
                fact(f"Measured line coverage of {path} in run {run.id[:8]} was {cov.line_percent:.0f}%.", "coverage", run.id)
        idx = indexed.get(path)
        if idx is not None:
            try:
                if hashlib.sha256(safe_join(root, path).read_bytes()).hexdigest() != idx.sha256:
                    unknowns.append(f"{path} changed after it was indexed; symbol ranges and findings for it may be out of date.")
            except OSError:
                unknowns.append(f"{path} could not be read from disk.")
        elif snap:
            unknowns.append(f"{path} is not part of the index, so its symbols and dependencies are unknown.")

    # --- ranking
    ordered = sorted((c for c in cands.values() if c.evidence), key=lambda c: (-c.score, c.rel_path, c.symbol["name"] if c.symbol else ""))
    min_score = settings.repair_min_root_cause_score
    state = "ranked" if ordered and ordered[0].score >= min_score else "insufficient_evidence"
    hyps: list[dict[str, Any]] = []
    for rank, c in enumerate(ordered[:8], 1):
        lead = c.score - ordered[1].score if rank == 1 and len(ordered) > 1 else (c.score if rank == 1 else 0)
        if rank == 1 and c.score >= 6 and lead >= 2 and len(c.types) >= 2:
            conf = "high"
        elif c.score >= 3.5:
            conf = "medium"
        else:
            conf = "low"
        if rank > 1 and conf == "high":
            conf = "medium"
        ev = sorted(c.evidence.values(), key=lambda e: (-e["weight"], e["id"]))
        label = f"{c.rel_path}" + (f" · {c.symbol['name']}" if c.symbol else "")
        hyps.append({"rank": rank, "rel_path": c.rel_path, "symbol": c.symbol["name"] if c.symbol else None,
                     "symbol_stable_id": c.symbol["stable_id"] if c.symbol else None,
                     "start_line": c.symbol["start_line"] if c.symbol else c.line, "end_line": c.symbol["end_line"] if c.symbol else c.line,
                     "score": c.score, "confidence": conf, "kind": "hypothesis", "status": "unverified",
                     "rationale": f"{label}: {len(ev)} evidence item(s) ({', '.join(sorted(c.types))}); score {c.score}.",
                     "evidence": ev, "contradicting": c.contradicting[:5]})
    top = hyps[0] if hyps else None
    if state == "insufficient_evidence":
        unknowns.append("The available evidence does not single out a location (best score "
                        f"{top['score'] if top else 0} < {min_score}).")
        next_steps.append("Provide a failing test result or open the investigation from a finding, then refresh the evidence.")
    elif top is not None:
        next_steps.append(f"Read {top['rel_path']}" + (f" {top['symbol']} (lines {top['start_line']}-{top['end_line']})" if top["symbol"] else "")
                          + " and reproduce the behavior with a focused test before changing it.")
    if snap and not db.scalar(select(CoverageFile.id).where(CoverageFile.repository_id == repo.id).limit(1)):
        unknowns.append("No measured coverage exists, so it is unknown whether the candidate code is exercised by tests.")
        next_steps.append("Run the tests with coverage to learn which candidate lines are exercised.")
    ev_doc = {"version": VERSION, "assembled_at": utcnow().isoformat(), "state": state, "facts": facts, "hypotheses": hyps,
              "unknowns": list(dict.fromkeys(unknowns))[:15], "next_steps": list(dict.fromkeys(next_steps))[:8], "similar_failures": similar,
              "weights": {k: v for k, v in WEIGHTS.items()}, "limitations": LIMITATIONS, "min_score": min_score,
              "index": {"snapshot_id": sid, "present": snap is not None}}
    inv.evidence = ev_doc
    inv.evidence_assembled_at = utcnow()
    inv.index_snapshot_id = sid
    relevant: list[str] = []
    for h in hyps[:3]:
        if h["score"] >= min_score and h["rel_path"] not in relevant:
            relevant.append(h["rel_path"])
    for h in inv.hint_files:
        ok = valid_hint(root, set(indexed), h)
        if ok and ok not in relevant:
            relevant.append(ok)
    if finding is not None and finding.rel_path not in relevant:
        relevant.append(finding.rel_path)
    if rfinding is not None and rfinding.rel_path not in relevant:
        relevant.append(rfinding.rel_path)
    inv.relevant_files = relevant[:10]
    if top is not None and state == "ranked":
        miss = ev_doc["unknowns"][:3]
        inv.root_cause = {"state": state, "kind": "hypothesis", "rel_path": top["rel_path"], "symbol": top["symbol"], "confidence": top["confidence"],
                          "score": top["score"], "summary": f"Most likely location: {top['rel_path']}" + (f" · {top['symbol']}" if top["symbol"] else ""),
                          "confidence_rationale": top["rationale"] + (" Missing: " + "; ".join(miss) if miss else ""),
                          "note": "A hypothesis ranked from evidence, not a confirmed cause."}
    else:
        inv.root_cause = {"state": "insufficient_evidence", "kind": "unknown", "confidence": "low", "score": top["score"] if top else 0,
                          "summary": "The evidence does not identify a likely location.",
                          "confidence_rationale": "; ".join(ev_doc["unknowns"][:3]) or "No evidence was available.",
                          "note": "Add a failing test result, a finding or a file hint, then refresh the evidence."}
    return ev_doc
