"""Explainable bug localization.

Scores are **heuristic priorities**, not probabilities. Every point of a candidate's score comes from one evidence item
with a documented weight (``DEFAULT_WEIGHTS``); evidence is normalised to a key, so one observation counts once even when
several tables describe it (a finding that overlaps the frame line is not also counted as "same symbol"; a symbol that is
on the stack is not also counted as "called by the test").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import (
    CoverageFile,
    FailureFrame,
    FailureOccurrence,
    FailureReport,
    Finding,
    IndexedFile,
    IndexSnapshot,
    LocalizationCandidate,
    Relationship,
    Repository,
    Symbol,
    TestExecutionRun,
)
from app.services import repositories as repo_svc
from app.services.analysis.model import OPEN_STATUSES

ANALYSIS_VERSION = "1.0"

DEFAULT_WEIGHTS: dict[str, float] = {
    "exception_origin": 5.0,         # the exception surfaced on this repository line (final stack frame)
    "immediate_failure": 3.5,        # deepest repository frame; the exception itself came from outside the repository
    "caller_context": 2.0,           # repository frame above the origin; reduced by `caller_decay` per hop
    "caller_decay": 0.25,
    "caller_floor": 0.75,
    "called_on_failing_line": 3.0,   # no repository frames: resolved call on the failing line of the test
    "called_by_failing_test": 1.0,   # resolved call elsewhere in the failing test body
    "ambiguous_call_factor": 0.5,    # multiplier when the call resolution had several candidates
    "test_imports_file": 0.75,       # the failing test's file imports this file (resolved import)
    "finding_on_frame_line": 2.0,    # open static-analysis finding whose range contains the frame line
    "finding_in_symbol": 1.0,        # open finding elsewhere in the same function/class
    "finding_in_file": 0.4,          # open finding elsewhere in the file
    "finding_cap": 3.0,              # ceiling on the total contribution of findings
    "executed_in_measured_run": 0.4, # the frame line was executed in the latest measured coverage run
    "not_executed_in_measured_run": -0.5,  # contradiction: that line was not executed in the latest measured run
    "related_failure": 0.6,          # another failure report has a repository frame in this file
    "related_failure_cap": 1.5,
    "changed_since_failure": 0.8,    # the file's content hash differs between the failure's snapshot and the latest one
    "min_score": 1.5,                # below this the result is "insufficient evidence"
    "high_margin": 1.5,              # lead over the next candidate required for high confidence
}

LIMITATIONS = [
    "Scores are heuristic priorities, not probabilities that a file is defective.",
    "The first or deepest stack frame is not assumed to be the root cause; callers and test-called code are ranked separately.",
    "Findings, coverage and dependency data describe the repository as indexed, which may differ from the code that failed.",
]


def load_weights(settings: Settings) -> tuple[dict[str, float], str | None]:
    w = dict(DEFAULT_WEIGHTS)
    if not settings.debug_weights_json.strip():
        return w, None
    try:
        raw = json.loads(settings.debug_weights_json)
        if not isinstance(raw, dict) or any(k not in w or isinstance(v, bool) or not isinstance(v, (int, float)) for k, v in raw.items()):
            raise ValueError("unknown key or non-numeric value")
        w.update({k: float(v) for k, v in raw.items()})
        return w, None
    except (ValueError, TypeError) as exc:
        return dict(DEFAULT_WEIGHTS), f"NOVA_DEBUG_WEIGHTS_JSON is invalid ({exc}); default weights were used."


@dataclass
class Cand:
    rel_path: str
    symbol: Symbol | None
    line: int | None
    role: str
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    contradicting: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, etype: str, weight: float, detail: str, ref: str) -> None:
        key = hashlib.sha1(f"{etype}|{ref}".encode(), usedforsecurity=False).hexdigest()[:12]
        if key not in self.evidence and key not in self.contradicting:
            (self.contradicting if weight < 0 else self.evidence)[key] = {"id": key, "type": etype, "weight": weight, "detail": detail}

    @property
    def score(self) -> float:
        return round(max(0.0, sum(e["weight"] for e in self.evidence.values()) + sum(e["weight"] for e in self.contradicting.values())), 2)

    @property
    def key(self) -> tuple[str, str]:
        return (self.rel_path, self.symbol.stable_id if self.symbol else "")


@dataclass
class Result:
    state: str  # ranked | insufficient_evidence
    candidates: list[Cand]
    missing: list[str]
    notes: list[str]
    snapshot_id: str | None
    stale: bool
    roles_by_frame: list[dict[str, Any]]


ROLE_RANK = {"exception_origin": 0, "immediate_failure": 1, "called_by_test": 2, "caller_context": 3, "related": 4}


def _merge(cands: dict[tuple[str, str], Cand], new: Cand) -> Cand:
    cur = cands.get(new.key)
    if cur is None:
        cands[new.key] = new
        return new
    if ROLE_RANK[new.role] < ROLE_RANK[cur.role]:
        cur.role, cur.line = new.role, new.line
    return cur


def localize(db: Session, settings: Settings, repo: Repository, failure: FailureReport) -> Result:
    weights, weight_note = load_weights(settings)
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    missing: list[str] = []
    notes: list[str] = [weight_note] if weight_note else []
    frames = list(db.scalars(select(FailureFrame).where(FailureFrame.failure_id == failure.id).order_by(FailureFrame.idx)))
    if snap is None:
        return Result("insufficient_evidence", [], ["The repository has no completed index."], notes, None, False, [])
    stale = failure.index_snapshot_id is not None and failure.index_snapshot_id != snap.id
    if stale:
        notes.append("The repository was re-indexed after this failure was ingested; line numbers and symbols may have moved.")
    files = {f.rel_path: f for f in db.scalars(select(IndexedFile).where(IndexedFile.snapshot_id == snap.id, IndexedFile.language == "Python"))}
    sym_by_stable = {s.stable_id: s for s in db.scalars(select(Symbol).where(
        Symbol.snapshot_id == snap.id, Symbol.stable_id.in_([f.symbol_stable_id for f in frames if f.symbol_stable_id] or [""])))}

    def symbol_for(fr: FailureFrame) -> Symbol | None:
        if fr.symbol_stable_id and fr.symbol_stable_id in sym_by_stable:
            return sym_by_stable[fr.symbol_stable_id]
        f = files.get(fr.rel_path or "")
        if not f or fr.line is None:
            return None
        rows = [s for s in db.scalars(select(Symbol).where(Symbol.file_id == f.id, Symbol.kind.in_(("function", "method", "class"))))
                if s.start_line <= fr.line <= s.end_line]
        rows.sort(key=lambda s: (s.kind == "class", s.end_line - s.start_line, s.stable_id))
        return rows[0] if rows else None

    repo_frames = [f for f in frames if f.scope == "repository" and f.rel_path in files]
    prod = [f for f in repo_frames if not f.is_test]
    tests = [f for f in repo_frames if f.is_test]
    cands: dict[tuple[str, str], Cand] = {}
    roles: list[dict[str, Any]] = []
    last = frames[-1] if frames else None
    for rank, fr in enumerate(reversed(prod)):  # rank 0 = deepest
        sym = symbol_for(fr)
        if rank == 0:
            role = "exception_origin" if last is not None and fr.idx == last.idx else "immediate_failure"
        else:
            role = "caller_context"
        c = _merge(cands, Cand(fr.rel_path or "", sym, fr.line, role))
        where = f"line {fr.line}" if fr.line else "an unknown line"
        label = sym.qualified_name if sym else fr.rel_path
        if role == "exception_origin":
            c.add("exception_origin", weights["exception_origin"], f"The exception surfaced at {where} of {label} (final stack frame).", f"frame:{fr.idx}")
        elif role == "immediate_failure":
            c.add("immediate_failure", weights["immediate_failure"],
                  f"Deepest repository frame ({label}, {where}); the exception itself was raised in code outside the repository.", f"frame:{fr.idx}")
        else:
            w = max(weights["caller_floor"], weights["caller_context"] - weights["caller_decay"] * (rank - 1))
            c.add("caller_context", round(w, 2), f"{label} is on the stack {rank} call(s) above the failure and may have passed the faulty input.", f"frame:{fr.idx}")
        roles.append({"frame_index": fr.idx, "role": role, "rel_path": fr.rel_path, "line": fr.line})
    test_frame = tests[-1] if tests else None
    if test_frame is not None:
        roles.append({"frame_index": test_frame.idx, "role": "failing_test", "rel_path": test_frame.rel_path, "line": test_frame.line})
    # call graph from the failing test (only for symbols that are not already on the stack: no double counting)
    has_calls = False
    if test_frame is not None:
        tsym = symbol_for(test_frame)
        if tsym is not None:
            rels = list(db.scalars(select(Relationship).where(
                Relationship.snapshot_id == snap.id, Relationship.type == "CALLS", Relationship.source_symbol_stable_id == tsym.stable_id,
                Relationship.status.in_(("resolved", "ambiguous")), Relationship.target_symbol_stable_id.is_not(None))))
            has_calls = bool(rels)
            on_stack = {k for k in cands}
            targets = {s.stable_id: s for s in db.scalars(select(Symbol).where(
                Symbol.snapshot_id == snap.id, Symbol.stable_id.in_([r.target_symbol_stable_id for r in rels] or [""])))}
            tfile = {i: p for p, i in ((p, f.id) for p, f in files.items())}
            for r in sorted(rels, key=lambda r: (r.line or 0, r.target_symbol_stable_id or "")):
                tgt = targets.get(r.target_symbol_stable_id or "")
                path = tfile.get(tgt.file_id) if tgt else None
                if tgt is None or path is None or path == test_frame.rel_path:
                    continue
                if (path, tgt.stable_id) in on_stack:
                    continue  # already observed on the stack; the call graph would describe the same fact again
                on_line = r.line is not None and test_frame.line is not None and r.line == test_frame.line
                base = weights["called_on_failing_line"] if on_line else weights["called_by_failing_test"]
                if r.status == "ambiguous":
                    base *= weights["ambiguous_call_factor"]
                c = _merge(cands, Cand(path, tgt, tgt.start_line, "called_by_test"))
                c.add("called_on_failing_line" if on_line else "called_by_failing_test", round(base, 2),
                      f"{tgt.qualified_name} is called {'on the failing line' if on_line else 'in the body'} of the failing test"
                      f"{' (call resolution was ambiguous)' if r.status == 'ambiguous' else ''}.", f"call:{r.id}")
            # the test's own file imports
            imported = {r.target_file_id for r in db.scalars(select(Relationship).where(
                Relationship.snapshot_id == snap.id, Relationship.type == "IMPORTS_FILE", Relationship.status.in_(("resolved", "ambiguous")),
                Relationship.source_file_id == files[test_frame.rel_path or ""].id)) if r.target_file_id}
            for c in cands.values():
                f = files.get(c.rel_path)
                if f is not None and f.id in imported:
                    c.add("test_imports_file", weights["test_imports_file"], "The failing test's module imports this file.", f"import:{f.id}")
    if test_frame is not None and not has_calls and not prod:
        missing.append("No resolved call from the failing test to repository code was found, so no code under test could be identified.")
    if not repo_frames:
        missing.append("No stack frame maps to an indexed repository file.")
    # findings
    open_findings = list(db.scalars(select(Finding).where(
        Finding.repository_id == repo.id, Finding.status.in_(OPEN_STATUSES), Finding.rel_path.in_(sorted({c.rel_path for c in cands.values()}) or [""]))))
    for c in cands.values():
        spent = 0.0
        for fd in sorted((x for x in open_findings if x.rel_path == c.rel_path), key=lambda x: (x.start_line, x.rule_id, x.id)):
            if c.line is not None and fd.start_line <= c.line <= fd.end_line:
                w, kind, why = weights["finding_on_frame_line"], "finding_on_frame_line", "overlaps the failing line"
            elif c.symbol is not None and c.symbol.start_line <= fd.start_line <= c.symbol.end_line:
                w, kind, why = weights["finding_in_symbol"], "finding_in_symbol", "is in the same function"
            else:
                w, kind, why = weights["finding_in_file"], "finding_in_file", "is elsewhere in the same file"
            w = min(w, weights["finding_cap"] - spent)
            if w <= 0:
                continue
            spent += w
            c.add(kind, round(w, 2), f"Open finding {fd.rule_id} ({fd.title}) at line {fd.start_line} {why}.", f"finding:{fd.id}")
    # coverage (measured data only)
    cov_run = db.scalar(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id, TestExecutionRun.coverage_status == "measured")
                        .order_by(TestExecutionRun.finished_at.desc(), TestExecutionRun.id.desc()).limit(1))
    own_runs = set(db.scalars(select(FailureOccurrence.execution_run_id).where(FailureOccurrence.failure_id == failure.id)))
    if cov_run is None:
        missing.append("No measured coverage exists for this repository.")
    elif cov_run.id in own_runs:
        missing.append("The latest measured coverage comes from the run that produced this failure, so it was not used as support.")
    else:
        cov = {c.rel_path: c for c in db.scalars(select(CoverageFile).where(CoverageFile.execution_run_id == cov_run.id))}
        when = cov_run.finished_at.date().isoformat() if cov_run.finished_at else "an earlier run"
        for c in cands.values():
            cf = cov.get(c.rel_path)
            if cf is None or c.line is None:
                continue
            miss = any(a <= c.line <= b for a, b in cf.missing_lines)
            if miss:
                c.add("not_executed_in_measured_run", weights["not_executed_in_measured_run"],
                      f"Line {c.line} was not executed in the latest measured run ({when}); that run may predate this failure.", f"cov:{cov_run.id}")
            else:
                c.add("executed_in_measured_run", weights["executed_in_measured_run"],
                      f"Line {c.line} was executed in the latest measured run ({when}).", f"cov:{cov_run.id}")
    # other failures touching the same files
    paths = sorted({c.rel_path for c in cands.values()})
    if paths:
        rows = db.execute(select(FailureFrame.rel_path, FailureFrame.failure_id).join(FailureReport, FailureReport.id == FailureFrame.failure_id).where(
            FailureReport.repository_id == repo.id, FailureReport.id != failure.id, FailureFrame.rel_path.in_(paths),
            FailureFrame.scope == "repository", FailureFrame.is_test.is_(False))).all()
        by_path: dict[str, set[str]] = {}
        for p, fid in rows:
            by_path.setdefault(p, set()).add(fid)
        for c in cands.values():
            n = len(by_path.get(c.rel_path, ()))
            if n:
                w = min(weights["related_failure"] * n, weights["related_failure_cap"])
                c.add("related_failure", round(w, 2), f"{n} other failure report(s) have a repository frame in this file.", f"related:{c.rel_path}")
    # content change between the failure's snapshot and the latest one (hash comparison only)
    if stale and failure.index_snapshot_id:
        old = dict(db.execute(select(IndexedFile.rel_path, IndexedFile.sha256).where(
            IndexedFile.snapshot_id == failure.index_snapshot_id, IndexedFile.rel_path.in_(paths or [""]))).all())
        if not old:
            missing.append("The snapshot the failure was ingested against is no longer available, so file changes cannot be compared.")
        for c in cands.values():
            now = files.get(c.rel_path)
            if now is not None and c.rel_path in old and old[c.rel_path] != now.sha256:
                c.add("changed_since_failure", weights["changed_since_failure"],
                      "The file's content differs from the version indexed when this failure was ingested.", f"changed:{c.rel_path}")
    else:
        missing.append("Source change history between runs is not available (no reliable diff for this failure).")
    if not db.scalar(select(Relationship.id).where(Relationship.snapshot_id == snap.id).limit(1)):
        missing.append("The dependency graph is empty, so call and import relationships could not be used.")
    ordered = sorted(cands.values(), key=lambda c: (-c.score, ROLE_RANK[c.role], c.rel_path, c.line or 0, c.symbol.stable_id if c.symbol else ""))
    ordered = ordered[:settings.debug_max_candidates]
    if not ordered or ordered[0].score < weights["min_score"]:
        notes.append("The available evidence is too thin to rank candidates without being misleading.")
        return Result("insufficient_evidence", [], missing, notes, snap.id, stale, roles)
    return Result("ranked", ordered, missing, notes, snap.id, stale, roles)


def confidence(c: Cand, rank: int, ordered: list[Cand], weights: dict[str, float]) -> str:
    types = {e["type"] for e in c.evidence.values()}
    strong = c.role in ("exception_origin", "immediate_failure") or "called_on_failing_line" in types
    if strong and len(types) >= 2 and rank == 1:
        margin = c.score - (ordered[1].score if len(ordered) > 1 else 0.0)
        if len(ordered) == 1 or margin >= weights["high_margin"]:
            return "high"
    if strong or c.score >= 3.0:
        return "medium"
    return "low"


def explain(c: Cand, rank: int) -> str:
    parts = [f"{e['detail']} (+{e['weight']:g})" for e in sorted(c.evidence.values(), key=lambda e: (-e["weight"], e["id"]))]
    neg = [f"{e['detail']} ({e['weight']:g})" for e in c.contradicting.values()]
    text = f"Rank {rank}, score {c.score:g}. " + " ".join(parts[:4])
    if neg:
        text += " Against: " + " ".join(neg)
    return text[:1500]


def run_localization(db: Session, settings: Settings, repo: Repository, failure: FailureReport) -> dict[str, Any]:
    """Compute, persist and describe the ranking. Deterministic for identical stored evidence."""
    weights, _ = load_weights(settings)
    res = localize(db, settings, repo, failure)
    db.execute(delete(LocalizationCandidate).where(LocalizationCandidate.failure_id == failure.id))
    out: list[dict[str, Any]] = []
    for i, c in enumerate(res.candidates, 1):
        conf = confidence(c, i, res.candidates, weights)
        ev = sorted(c.evidence.values(), key=lambda e: (-e["weight"], e["id"]))
        ct = sorted(c.contradicting.values(), key=lambda e: e["id"])
        row = LocalizationCandidate(
            failure_id=failure.id, rank=i, rel_path=c.rel_path, symbol_stable_id=c.symbol.stable_id if c.symbol else None,
            symbol_name=c.symbol.qualified_name[:300] if c.symbol else None, start_line=c.symbol.start_line if c.symbol else c.line,
            end_line=c.symbol.end_line if c.symbol else c.line, score=c.score, confidence=conf, role=c.role, evidence=ev,
            contradicting=ct, explanation=explain(c, i), analysis_version=ANALYSIS_VERSION, snapshot_id=res.snapshot_id)
        db.add(row)
        out.append({"rank": i, "rel_path": row.rel_path, "symbol": row.symbol_name, "symbol_stable_id": row.symbol_stable_id,
                    "start_line": row.start_line, "end_line": row.end_line, "failing_line": c.line, "score": row.score,
                    "confidence": conf, "role": c.role, "evidence": ev, "contradicting": ct, "explanation": row.explanation})
    failure.localization_meta = {"state": res.state, "snapshot_id": res.snapshot_id, "analysis_version": ANALYSIS_VERSION,
                                 "computed_at": utcnow().isoformat(), "missing": res.missing[:10]}
    db.commit()
    return {"state": res.state, "analysis_version": ANALYSIS_VERSION, "candidates": out, "missing_evidence": res.missing,
            "notes": res.notes, "stale": res.stale, "frame_roles": res.roles_by_frame, "limitations": LIMITATIONS,
            "weights": {k: v for k, v in weights.items()}, "snapshot_id": res.snapshot_id,
            "interpretation": "Scores rank where to look first. They are heuristic priorities, not probabilities and not proof of a defect."}


def snapshot_info(db: Session, snapshot_id: str | None) -> IndexSnapshot | None:
    return db.get(IndexSnapshot, snapshot_id) if snapshot_id else None

