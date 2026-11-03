"""Review orchestration: acquire -> normalize -> map symbols -> analyze -> correlate -> impact -> risk -> recommend -> comments.

Each stage records its own status. A failed optional stage turns the review into ``completed_with_warnings`` and is listed;
only a failure to read the changes at all makes the review ``failed``. Nothing here writes to the repository or to git.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NovaError, TooManyRequestsError
from app.db.base import utcnow
from app.models import (
    AnalysisRun,
    CodeReview,
    CoverageFile,
    Finding,
    Repository,
    ReviewChange,
    ReviewChangedSymbol,
    ReviewFinding,
    TestExecutionRun,
)
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services import repositories as repo_svc
from app.services.analysis.orchestrator import read_source
from app.services.analysis.redaction import scrub
from app.services.debugging.parsing import looks_like_test
from app.services.intelligence.language_detection import detect_language
from app.services.review import findings as fnd
from app.services.review import gitops, impact, recommend, risk, symbols
from app.services.review.symbols import ChangedSym

logger = logging.getLogger("nova.review")
GENERATOR_VERSION = "1.0"
CODE_LANGUAGES = {"python", "javascript", "typescript", "jsx", "tsx", "java", "go", "rust", "c", "c++", "c#", "ruby", "php", "kotlin",
                  "swift", "shell", "bash", "sql", "scala", "lua", "perl", "r", "dart", "objective-c"}
CHECKS_RUN = ["Phase 04 Python static-analysis rules on changed lines", "Public interface change check (REV-API-001)",
              "Removed/renamed module still imported check (REV-DEL-001)", "Changed code without associated tests (REV-TEST-001)",
              "Sensitive-area observations (REV-INFO-001/002)"]


@dataclass
class FileCtx:
    change: gitops.RawChange
    row: ReviewChange
    language: str
    is_test: bool
    new_bytes: bytes | None = None
    old_bytes: bytes | None = None
    syms: list[ChangedSym] = field(default_factory=list)
    index_current: bool | None = None  # None = not applicable
    unmapped: bool = False


class Stages:
    def __init__(self, review: CodeReview) -> None:
        self.review = review
        self.items: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def run(self, name: str, fn: Callable[[], Any], default: Any = None) -> Any:
        t0 = time.monotonic()
        item: dict[str, Any] = {"name": name, "status": "ok", "detail": ""}
        try:
            result = fn()
        except Exception as exc:  # a stage must never take the review down silently or leak details
            item["status"] = "failed"
            item["detail"] = f"{type(exc).__name__}" + (f": {scrub(str(exc), 200)}" if isinstance(exc, NovaError) else "")
            self.warnings.append(f"Stage '{name}' failed ({item['detail']}); its results are missing from this review.")
            logger.warning("review_stage_failed", extra={"stage": name, "error": type(exc).__name__})
            result = default
        item["duration_ms"] = int((time.monotonic() - t0) * 1000)
        self.items.append(item)
        return result

    def note(self, name: str, detail: str) -> None:
        for it in self.items:
            if it["name"] == name:
                it["detail"] = (it["detail"] + " " + detail).strip()[:400]
                if it["status"] == "ok":
                    it["status"] = "warning"
                return


def begin(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, mode: str, base_ref: str | None,
          head_ref: str | None, paths: list[str]) -> CodeReview:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    active = db.scalar(select(func.count()).select_from(CodeReview).where(
        CodeReview.repository_id == repo.id, CodeReview.status.in_(("pending", "running")))) or 0
    if active >= 3:
        raise TooManyRequestsError("Three reviews are already running for this repository; wait for one to finish.")
    prep = gitops.prepare(root, settings, mode, base_ref, head_ref)
    review = CodeReview(repository_id=repo.id, mode=mode, status="pending", base_ref=prep.base_ref, head_ref=prep.head_ref,
                        base_sha=prep.base_sha, head_sha=prep.head_sha, git_head=prep.git_head, selected_paths=paths,
                        warnings=list(prep.warnings), generator_version=GENERATOR_VERSION, created_by=ctx.user.id)
    db.add(review)
    db.flush()
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="review.created", entity_type="code_review",
                 entity_id=review.id, details={"repository_id": repo.id, "mode": mode, "paths": len(paths)})
    db.commit()
    return review


def _lang(path: str) -> str:
    return detect_language(PurePosixPath(path).name).language


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _read_new(root: Path, settings: Settings, prep_mode: str, ch: gitops.RawChange) -> tuple[bytes | None, str]:
    if ch.change_type == "deleted" or ch.binary or ch.change_type == "unsupported":
        return None, "skip"
    if prep_mode in ("revisions", "staged"):
        return gitops.read_blob(root, settings, ch.new_blob)
    res = read_source(root, ch.path, settings.review_max_file_bytes)
    return (res.data, "ok") if res.status == "ok" else (None, {"missing": "missing", "too_large": "too_large"}.get(res.status, "error"))


def run_review(db: Session, settings: Settings, review_id: str) -> None:
    review = db.get(CodeReview, review_id)
    if review is None or review.status not in ("pending",):
        return
    repo = db.get(Repository, review.repository_id)
    assert repo is not None
    review.status, review.started_at = "running", utcnow()
    db.commit()
    st = Stages(review)
    deadline = time.monotonic() + settings.review_time_budget_seconds
    try:
        _run(db, settings, review, repo, st, deadline)
    except Exception as exc:
        db.rollback()
        review = db.get(CodeReview, review_id)
        assert review is not None
        review.status = "failed"
        review.error_message = (f"{type(exc).__name__}: {scrub(str(exc), 300)}" if isinstance(exc, NovaError) else f"The review could not be completed ({type(exc).__name__}).")
        review.stages = st.items
        review.warnings = [*review.warnings, *st.warnings][:50]
        review.finished_at = utcnow()
        logger.warning("review_failed", extra={"error": type(exc).__name__})
        db.commit()


def _run(db: Session, settings: Settings, review: CodeReview, repo: Repository, st: Stages, deadline: float) -> None:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    prep = gitops.Prepared(review.mode, review.base_ref, review.head_ref, review.base_sha, review.head_sha, review.git_head, [])
    t0 = time.monotonic()
    acq = gitops.acquire(root, settings, prep, list(review.selected_paths), deadline)
    st.items.append({"name": "acquire_changes", "status": "warning" if acq.warnings else "ok", "detail": f"{acq.total_files} changed file(s) read from git.",
                     "duration_ms": int((time.monotonic() - t0) * 1000)})
    warnings = [*review.warnings, *acq.warnings]
    review.files_total, review.files_omitted = acq.total_files, acq.omitted
    review.files_analyzed = len(acq.changes)

    # ---- normalize ------------------------------------------------------------------------------------------------
    files: list[FileCtx] = []

    def normalize() -> None:
        for ch in acq.changes:
            lang = _lang(ch.path)
            new, rd = _read_new(root, settings, review.mode, ch)
            old = None
            if lang == "Python" and ch.change_type in ("modified", "renamed", "deleted"):
                old, _ = gitops.read_blob(root, settings, ch.old_blob)
            notes = list(ch.notes)
            if ch.binary:
                status = "binary"
            elif ch.change_type == "unsupported":
                status = "unsupported"
            elif rd == "too_large":
                status = "too_large"
                notes.append(f"Larger than the {settings.review_max_file_bytes}-byte analysis limit; not analysed.")
            elif rd in ("missing", "error"):
                status = "not_read"
                notes.append("The new content could not be read.")
            elif not ch.hunks_read and ch.change_type != "deleted":
                status = "not_read"
            elif ch.hunks_read and not ch.new_ranges and not ch.old_ranges and ch.status == "M" and not (ch.added or ch.removed):
                status = "mode_only"
                notes.append("Only the file mode changed.")
            else:
                status = "ok"
            if status in ("not_read", "too_large") and rd not in ("missing", "error", "too_large"):
                warnings.append(f"{ch.path}: line ranges are unknown (diff limit reached).")
            row = ReviewChange(review_id=review.id, repository_id=repo.id, rel_path=ch.path, change_type=ch.change_type, old_path=ch.old_path,
                               new_path=None if ch.change_type == "deleted" else ch.path, old_blob=ch.old_blob, new_blob=ch.new_blob,
                               new_sha256=_sha(new), language=lang, is_test=looks_like_test(ch.path), is_binary=ch.binary,
                               lines_added=ch.added, lines_removed=ch.removed, new_ranges=ch.new_ranges, old_ranges=ch.old_ranges,
                               extraction_status=status, notes=notes[:5])
            db.add(row)
            files.append(FileCtx(ch, row, lang, row.is_test, new, old))
        db.flush()

    st.run("normalize_changes", normalize)
    review.lines_added = sum(f.change.added or 0 for f in files)
    review.lines_removed = sum(f.change.removed or 0 for f in files)

    # ---- symbols & index --------------------------------------------------------------------------------------------
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    review.index_snapshot_id = snap.id if snap else None
    index_notes: list[str] = []

    def map_symbols() -> None:
        for f in files:
            ch = f.change
            if f.row.extraction_status not in ("ok",) and ch.change_type != "deleted":
                continue
            if ch.change_type == "unsupported":
                continue
            if f.language == "Python":
                new_src = f.new_bytes.decode("utf-8-sig", "replace") if f.new_bytes is not None else None
                old_src = f.old_bytes.decode("utf-8-sig", "replace") if f.old_bytes is not None else None
                if new_src is None and ch.change_type != "deleted":
                    f.unmapped = True
                    continue
                syms, notes = symbols.map_python(new_src, old_src, ch.new_ranges, ch.deletion_points, ch.change_type == "added",
                                                 ch.change_type == "deleted")
                if any("syntax error" in n for n in notes):
                    f.unmapped = True
                f.syms = syms
                f.row.notes = [*f.row.notes, *notes][:6]
                if ch.change_type == "deleted":
                    cur, why = symbols.attach_index(db, snap.id if snap else None, ch.path, _sha(f.old_bytes), [])
                    f.index_current = cur
                else:
                    f.index_current, why = symbols.attach_index(db, snap.id if snap else None, ch.path, f.row.new_sha256, syms)
                if f.index_current is False and not f.row.is_test:
                    index_notes.append(f"{ch.path}: {why}.")
            elif f.language.lower() in CODE_LANGUAGES and f.row.new_sha256:
                syms, why = symbols.index_only_symbols(db, snap.id if snap else None, ch.path, f.row.new_sha256, ch.new_ranges, ch.deletion_points)
                f.syms = syms
                f.index_current = why is None
                if why:
                    f.unmapped = True
                    f.row.notes = [*f.row.notes, f"No symbols mapped: {why}."][:6]
                    index_notes.append(f"{ch.path}: {why}.")
        for f in files:
            for s in f.syms:
                db.add(ReviewChangedSymbol(review_id=review.id, change_id=f.row.id, rel_path=f.row.rel_path, symbol_stable_id=s.stable_id,
                                           qualified_name=(s.qualified or s.name)[:1200], kind=s.kind, start_line=s.start, end_line=s.end,
                                           change_kind=s.change_kind, is_public=s.public, signature_old=s.signature_old,
                                           signature_new=s.signature_new, mapping=s.mapping, complexity=s.complexity))
        db.flush()

    st.run("map_symbols", map_symbols)
    if snap is None:
        index_notes.append("The repository has no completed index; symbols were parsed from the changed files only and have no stable ids.")
    elif snap.git_head and (review.head_sha or review.git_head) and snap.git_head != (review.head_sha or review.git_head):
        index_notes.append("The index was built at a different commit than the reviewed one.")
    review.index_stale = bool(index_notes)
    review.index_notes = sorted(set(index_notes))[:15]
    review.symbols_changed = sum(1 for f in files for s in f.syms if s.kind != "module")
    if review.index_stale:
        warnings.append("The symbol index does not fully describe the reviewed content; re-index for exact symbol ids and dependents.")

    # ---- static analysis & correlation ---------------------------------------------------------------------------
    py_new = [(f.change.path, f.new_bytes) for f in files if f.language == "Python" and f.new_bytes is not None and f.row.extraction_status == "ok"]
    outcome: fnd.AnalyzerOutcome | None = None
    if time.monotonic() > deadline:
        warnings.append("The time budget was reached before static analysis; no analyzer findings are included.")
    else:
        outcome = st.run("static_analysis", lambda: fnd.analyze(root, py_new))
    drafts: list[fnd.Draft] = []
    resolved_by_change = 0
    if outcome is not None:
        warnings.extend(outcome.errors)
        warnings.extend(f"Static analysis skipped {s}" for s in outcome.skipped[:10])
        paths = sorted(f.change.path for f in files)
        stored = {x.fingerprint: x for x in db.scalars(select(Finding).where(Finding.repository_id == repo.id, Finding.rel_path.in_(paths or [""]),
                                                                              Finding.status != "resolved"))}
        ranges = {f.change.path: f.change.new_ranges for f in files}
        added = {f.change.path for f in files if f.change.change_type == "added"}

        def correlate() -> tuple[list[fnd.Draft], int]:
            d, resolved, _pre = fnd.drafts_from_analyzers(outcome, ranges, added, stored)
            return d, resolved

        res = st.run("correlate_findings", correlate, default=([], 0))
        drafts, resolved_by_change = res

    # ---- impact ---------------------------------------------------------------------------------------------------
    changed_info = [{"path": f.change.path, "old_path": f.change.old_path if f.change.change_type in ("renamed", "deleted") else None,
                     "change_type": f.change.change_type, "is_test": f.is_test,
                     "symbol_ids": [s.stable_id for s in f.syms if s.stable_id and s.change_kind == "modified"]} for f in files]
    for c in changed_info:
        if c["change_type"] == "deleted":
            c["old_path"] = c["path"]
    imp = st.run("impact_analysis", lambda: impact.build(db, settings, repo, review.index_snapshot_id, changed_info, review.index_stale),
                 default={"graph_available": False, "files": [], "dependents_total": 0, "dependents": [], "affected_tests": [], "failures": [],
                          "limitations": ["Impact analysis failed; dependents and tests are unknown."], "complete": False, "max_depth": settings.review_impact_depth,
                          "has_failure_history": False, "dependents_incomplete": False, "index_stale": review.index_stale,
                          "index_snapshot_id": review.index_snapshot_id})
    review.impact = imp

    # ---- heuristic checks -----------------------------------------------------------------------------------------------
    source_files = [f for f in files if not f.is_test and f.row.extraction_status in ("ok", "mode_only") or (f.change.change_type == "deleted" and not f.is_test)]
    changed_tests = {f.change.path for f in files if f.is_test}
    code_paths = [f.change.path for f in source_files if f.language == "Python"]
    tstate = impact.tests_state_for(db, repo, code_paths, changed_tests)
    probe_paths = sorted({(f.change.old_path or f.change.path) for f in source_files if f.change.change_type in ("deleted", "renamed") or
                          any(s.public and s.change_kind in ("removed", "modified") for s in f.syms)})
    imps = impact.importers(db, review.index_snapshot_id, probe_paths)
    per_file = [{"path": f.change.path, "old_path": f.change.old_path if f.change.change_type != "deleted" else f.change.path,
                 "change_type": f.change.change_type, "is_test": f.is_test, "language": f.language, "symbols": f.syms,
                 "changed_paths": {x.change.path for x in files}} for f in files]
    drafts += st.run("change_checks", lambda: fnd.heuristic_drafts(per_file, imps, tstate), default=[])

    # ---- risk -----------------------------------------------------------------------------------------------------------
    cov_pct, cov_note = _coverage(db, repo, [f.change.path for f in source_files if f.change.change_type != "added"])
    prioritized, noise = fnd.prioritize(drafts, settings)

    def compute_risk() -> dict[str, Any]:
        code = [f for f in source_files if f.language.lower() in CODE_LANGUAGES and f.row.extraction_status in ("ok",)]
        inp = risk.Inputs()
        inp.code_files = len(code)
        inp.analyzable_files = len(code) + sum(1 for f in source_files if f.change.change_type == "deleted" and f.language.lower() in CODE_LANGUAGES)
        inp.lines_changed = sum((f.change.added or 0) + (f.change.removed or 0) for f in code)
        inp.symbols_available = not any(f.unmapped for f in code)
        if not inp.symbols_available:
            inp.notes.append("Some changed code files could not be mapped to symbols; symbol-based factors are unavailable.")
        syms = [(f, s) for f in code for s in f.syms if s.kind != "module"]
        inp.symbols_changed = len(syms)
        for f, s in syms:
            if s.public and (s.change_kind == "removed" or (s.signature_old and s.signature_new and s.signature_old != s.signature_new
                                                            and fnd.breaking_change(s.signature_old, s.signature_new))):
                inp.public_changes.append(f"{f.change.path}::{s.name}")
            if s.handlers_old is not None and s.handlers_new is not None and s.handlers_old != s.handlers_new:
                inp.error_changes.append(f"{f.change.path}::{s.name}")
            if s.complexity and s.change_kind in ("added", "modified") and (inp.max_complexity is None or s.complexity > inp.max_complexity):
                inp.max_complexity, inp.max_complexity_name = s.complexity, f"{f.change.path}::{s.name}"
            if fnd.AUTH_NAME_RE.search(s.name):
                inp.auth_hits.append(f"{f.change.path}::{s.name}")
        for f in source_files:
            if fnd.AUTH_RE.search(f.change.path) and f.change.path not in inp.auth_hits:
                inp.auth_hits.append(f.change.path)
            if fnd.DATA_RE.search(f.change.path):
                inp.data_hits.append(f.change.path)
        analysis_ran = db.scalar(select(AnalysisRun.id).where(AnalysisRun.repository_id == repo.id).limit(1)) is not None
        an = [d for d in drafts if d.source == "analyzer"]
        inp.existing_findings = [(d.severity, d.title) for d in an if d.related.get("preexisting")] if analysis_ran else None
        inp.new_findings = [(d.severity, d.title) for d in an if not d.related.get("preexisting")] if outcome is not None else None
        inp.dependents = imp.get("dependents_total") if imp.get("graph_available") else None
        inp.dependents_incomplete = bool(imp.get("dependents_incomplete") or not imp.get("complete", True))
        if cov_pct:
            inp.coverage_uncovered = sum(1 - v / 100 for v in cov_pct.values()) / len(cov_pct)
            inp.coverage_note = cov_note
        else:
            inp.coverage_note = cov_note or "No measured coverage exists for the changed files."
        inp.failures = len(imp.get("failures", [])) if imp.get("has_failure_history") else None
        return risk.compute(inp)

    rk = st.run("risk_analysis", compute_risk, default={"state": "unknown", "score": None, "category": "unknown", "reason": "The risk stage failed.",
                                                        "factors": [], "weights": risk.WEIGHTS, "formula": "", "disclaimer": risk.DISCLAIMER,
                                                        "thresholds": [], "notes": [], "version": risk.VERSION})
    review.risk, review.risk_state, review.risk_score, review.risk_category = rk, rk["state"], rk.get("score"), rk["category"]

    # ---- recommendations -----------------------------------------------------------------------------------------------
    def recs() -> list[dict[str, Any]]:
        items, omitted = recommend.build(per_file, prioritized, imp, tstate, cov_pct, imps, changed_tests, settings.review_max_recommendations)
        if omitted:
            warnings.append(f"{omitted} further test recommendation(s) were omitted by the limit of {settings.review_max_recommendations}.")
        return items

    review.recommendations = st.run("test_recommendations", recs, default=[])

    # ---- persist findings ---------------------------------------------------------------------------------------------
    prior = _prior_decisions(db, repo.id, review.id, [d.fingerprint for d in prioritized])

    def persist() -> None:
        for d in prioritized:
            p = prior.get(d.fingerprint)
            db.add(ReviewFinding(review_id=review.id, repository_id=repo.id, fingerprint=d.fingerprint, stable_id=d.fingerprint[:16],
                                 rule_id=d.rule_id, analyzer_id=d.analyzer_id, rel_path=d.rel_path, start_line=d.start_line, end_line=d.end_line,
                                 scope=d.scope, severity=d.severity, category=d.category, title=d.title[:200], explanation=scrub(d.explanation, 2000),
                                 evidence=scrub(d.evidence, 1500), remediation=scrub(d.remediation, 1000), limitations=scrub(d.limitations, 600),
                                 confidence=d.confidence, actionable=d.actionable, source=d.source, ai_assisted=False,
                                 status=p.status if p else "open", rationale=p.rationale if p else None,
                                 inherited_from_review_id=p.review_id if p else None, related=d.related))
        db.flush()

    st.run("persist_findings", persist)
    review.findings_total = len(prioritized)

    # ---- summary -----------------------------------------------------------------------------------------------------------
    sev = Counter(d.severity for d in prioritized)
    cat = Counter(d.category for d in prioritized)
    if rk["state"] == "ranked":
        top = sorted([x for x in rk["factors"] if x["status"] == "measured" and (x["contribution"] or 0) > 0], key=lambda x: -x["contribution"])[:3]
        risk_line = f"Estimated change risk: {rk['category']} (heuristic score {rk['score']} of 100, not a probability)."
    else:
        top = []
        risk_line = f"Change risk is unknown: {rk.get('reason')}"
    headline = (f"{len(files)} changed file(s) analysed of {acq.total_files}; +{review.lines_added}/-{review.lines_removed} lines; "
                f"{review.symbols_changed} changed symbol(s); {len(prioritized)} review finding(s). {risk_line}")
    summary: dict[str, Any] = {
        "headline": headline, "by_severity": dict(sev), "by_category": dict(cat), "top_risk_factors": [{"id": x["id"], "label": x["label"], "contribution": x["contribution"]} for x in top],
        "noise_control": noise, "findings_resolved_by_change": resolved_by_change,
        "clean_review_note": ("No findings were produced by the enabled checks. This does not show that the change is free of defects."
                              if not prioritized else None),
        "checks_run": CHECKS_RUN, "ai": {"used": False, "note": "No AI provider was used. Every statement comes from the diff, the index, or documented rules."},
        "untested_assumptions": ["Hypotheses and recommendations are not verified facts.", "Nothing was executed."],
        "files_by_type": dict(Counter(f.change.change_type for f in files)), "files_not_analysed": [
            {"rel_path": f.change.path, "reason": f.row.extraction_status} for f in files if f.row.extraction_status not in ("ok", "mode_only")][:30]}
    review.summary = summary
    review.warnings = [*dict.fromkeys([*warnings, *st.warnings])][:50]
    review.stages = st.items
    bad = any(i["status"] in ("failed",) for i in st.items)
    review.status = "completed_with_warnings" if (bad or review.warnings) else "completed"
    review.finished_at = utcnow()
    db.commit()


def _coverage(db: Session, repo: Repository, paths: list[str]) -> tuple[dict[str, float], str]:
    run = db.scalar(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id, TestExecutionRun.coverage_status == "measured")
                    .order_by(TestExecutionRun.created_at.desc()).limit(1))
    if run is None or not paths:
        return {}, "No measured coverage run exists." if run is None else ""
    rows = db.scalars(select(CoverageFile).where(CoverageFile.execution_run_id == run.id, CoverageFile.rel_path.in_(paths)))
    out = {r.rel_path: float(r.line_percent) for r in rows}
    return out, (f"File-level percentages from execution run {run.id[:8]} ({run.created_at:%Y-%m-%d}); they describe the code as it was then, "
                 "not necessarily the reviewed lines.")


def _prior_decisions(db: Session, repo_id: str, review_id: str, fps: list[str]) -> dict[str, ReviewFinding]:
    if not fps:
        return {}
    rows = db.scalars(select(ReviewFinding).where(ReviewFinding.repository_id == repo_id, ReviewFinding.review_id != review_id,
                                                  ReviewFinding.fingerprint.in_(fps), ReviewFinding.status != "open")
                      .order_by(ReviewFinding.created_at.desc()))
    out: dict[str, ReviewFinding] = {}
    for r in rows:
        out.setdefault(r.fingerprint, r)
    return out


def preview(db: Session, settings: Settings, repo: Repository, mode: str, base_ref: str | None, head_ref: str | None, paths: list[str]) -> dict[str, Any]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    prep = gitops.prepare(root, settings, mode, base_ref, head_ref)
    acq = gitops.acquire(root, settings, prep, paths, time.monotonic() + 20, with_hunks=False)
    files = [{"rel_path": c.path, "old_path": c.old_path, "change_type": c.change_type, "added": c.added, "removed": c.removed, "binary": c.binary,
              "language": _lang(c.path), "is_test": looks_like_test(c.path)} for c in acq.changes]
    return {"executes": False, "mode": mode, "base_sha": prep.base_sha, "head_sha": prep.head_sha, "git_head": prep.git_head,
            "total_files": acq.total_files, "omitted": acq.omitted, "files": files[:200], "warnings": acq.warnings,
            "limits": {"max_files": settings.review_max_files, "max_diff_bytes": settings.review_max_diff_bytes},
            "explanation": "These files will be read from git and analysed statically. Nothing is executed, and neither the working tree nor Git history is modified."}

