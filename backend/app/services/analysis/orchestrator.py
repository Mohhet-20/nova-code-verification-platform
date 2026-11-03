"""Runs analyzers over the files of the published index and keeps findings consistent across runs.

Execution model: the run is created synchronously (so conflicts and bad configuration are reported to the caller), then the
work executes in-process in a FastAPI background task. There is no durable queue and no cancellation. A run left ``running``
by a killed process is marked failed at startup (see ``recover_interrupted_analysis``).

Source files are *read as bytes and parsed with the standard-library ``ast`` module*. Nothing from a repository is imported,
compiled to bytecode, executed or passed to a shell.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import stat
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NovaError, TooManyRequestsError
from app.db.base import utcnow
from app.models import AnalysisFileState, AnalysisRun, Finding, IndexedFile, IndexSnapshot, Repository, Symbol
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root, safe_join
from app.services import audit
from app.services import repositories as repo_svc
from app.services.analysis import catalog, fingerprint
from app.services.analysis import config as cfg_mod
from app.services.analysis.analyzers.py_facts import collect_facts
from app.services.analysis.model import (
    OPEN_STATUSES,
    SEVERITY_RANK,
    FileInput,
    RawFinding,
    RuleContext,
)
from app.services.analysis.redaction import scrub

logger = logging.getLogger("nova.analysis")
SUPPORTED_LANGUAGES = ("Python",)
BATCH = 25


# ----------------------------------------------------------------------------------------------- start

def begin_run(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, mode: str) -> AnalysisRun:
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        raise ConflictError("This repository has no published index yet. Index it before running analysis.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    effective = cfg_mod.load_for_repository(root)  # raises InvalidAnalysisConfig (422) with every problem listed
    running = db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.status == "running")) or 0
    if running >= settings.analysis_max_concurrent_runs:
        raise TooManyRequestsError("Too many analysis runs are in progress. Try again shortly.")
    active = db.scalar(select(AnalysisRun).where(AnalysisRun.repository_id == repo.id, AnalysisRun.status == "running"))
    if active is not None:
        raise ConflictError("Analysis is already running for this repository.", {"run_id": active.id})
    enabled = sum(1 for r in effective.rules.values() if r.enabled)
    run = AnalysisRun(
        repository_id=repo.id, index_snapshot_id=snap.id, status="running", mode=mode, requested_by=ctx.user.id,
        rules_enabled=enabled, rules_disabled=len(effective.rules) - enabled, config_source=effective.source,
        config_hash=effective.digest, analyzer_versions=catalog.analyzer_versions())
    db.add(run)
    try:
        db.flush()
        audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="analysis.started",
                     entity_type="analysis_run", entity_id=run.id, details={"repository_id": repo.id, "mode": mode})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("Analysis is already running for this repository.") from None
    return run


def recover_interrupted_analysis(db: Session) -> int:
    result = db.execute(update(AnalysisRun).where(AnalysisRun.status == "running").values(
        status="failed", finished_at=utcnow(), error_message="Interrupted: the backend stopped before analysis finished."))
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------------------------- file access

@dataclass
class _Read:
    status: str  # ok | missing | too_large | unreadable
    data: bytes = b""


def read_source(root: Path, rel_path: str, max_bytes: int) -> _Read:
    """Read one repository file as bytes. Never follows symlinks; never reads more than ``max_bytes``."""
    try:
        path = safe_join(root, rel_path)
        st = os.lstat(path)
    except FileNotFoundError:
        return _Read("missing")
    except (OSError, NovaError):
        return _Read("unreadable")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return _Read("unreadable")
    if st.st_size > max_bytes:
        return _Read("too_large")
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return _Read("unreadable")
    return _Read("too_large") if len(data) > max_bytes else _Read("ok", data)


def parse_python(data: bytes, rel_path: str) -> tuple[FileInput | None, str | None]:
    """Returns (file, None) or (None, short reason). Reasons never contain source text."""
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "not valid UTF-8"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename="<analysis>", type_comments=False)
    except SyntaxError as exc:
        return None, f"syntax error at line {exc.lineno or '?'}"
    except (ValueError, RecursionError, MemoryError, OverflowError) as exc:
        return None, f"cannot be parsed ({type(exc).__name__})"
    return FileInput(rel_path, source, tree, source.count("\n") + (0 if source.endswith("\n") or not source else 1)), None


# ----------------------------------------------------------------------------------------------- run

@dataclass
class _Totals:
    new: int = 0
    reopened: int = 0
    resolved: int = 0


def run_analysis(db: Session, settings: Settings, run_id: str) -> None:
    """Execute a run created by ``begin_run``. Always leaves the run in a final state."""
    run = db.get(AnalysisRun, run_id)
    if run is None:
        return
    started = time.monotonic()
    try:
        _execute(db, settings, run, started)
    except Exception as exc:
        db.rollback()
        logger.error("analysis_run_failed", extra={"run_id": run_id, "error_type": type(exc).__name__})
        run = db.get(AnalysisRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_message = (exc.message if isinstance(exc, NovaError) else f"Unexpected {type(exc).__name__}")[:500]
            run.finished_at = utcnow()
            run.duration_ms = int((time.monotonic() - started) * 1000)
            db.commit()


def _execute(db: Session, settings: Settings, run: AnalysisRun, started: float) -> None:
    repo = db.get(Repository, run.repository_id)
    snap = db.get(IndexSnapshot, run.index_snapshot_id or "")
    if repo is None or snap is None:
        raise ConflictError("The repository or its index is no longer available.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    config = cfg_mod.load_for_repository(root)
    index = catalog.rule_index()
    setup = hashlib.sha256((config.digest + json.dumps(catalog.analyzer_versions(), sort_keys=True)).encode()).hexdigest()
    warn: list[str] = []

    rows = db.execute(select(IndexedFile.id, IndexedFile.rel_path, IndexedFile.language)
                      .where(IndexedFile.snapshot_id == snap.id).order_by(IndexedFile.rel_path)).all()
    eligible_all = [r.rel_path for r in rows if r.language in SUPPORTED_LANGUAGES]
    eligible = eligible_all[: settings.analysis_max_files]
    run.files_eligible = len(eligible_all)
    if len(eligible_all) > len(eligible):
        run.files_not_reached += len(eligible_all) - len(eligible)
        warn.append(f"Only the first {len(eligible)} of {len(eligible_all)} Python files were analysed (file limit).")
    states = {s.rel_path: s for s in db.scalars(select(AnalysisFileState).where(AnalysisFileState.repository_id == repo.id))}

    handled: set[str] = set()  # paths whose open findings must NOT be touched by the end-of-run reconciliation
    excluded: set[str] = set()
    unchanged: list[str] = []
    any_evaluated = False
    analyzer_errors: dict[tuple[str, str, str], list] = {}  # (analyzer, rule, exc) -> [count, first path]
    parse_failures: list[str] = []
    totals = _Totals()
    force = run.mode == "full"
    disabled_rules = {rid for rid, r in config.rules.items() if not r.enabled}
    since_commit = 0
    stopped_at: int | None = None

    for i, rel in enumerate(eligible):
        if time.monotonic() - started > settings.analysis_time_budget_seconds:
            stopped_at = i
            break
        if config.is_excluded(rel):
            excluded.add(rel)
            run.files_excluded += 1
            continue
        read = read_source(root, rel, settings.analysis_max_file_bytes)
        if read.status == "missing":
            continue  # reconciled below as file_removed
        handled.add(rel)
        if read.status == "too_large":
            run.files_skipped += 1
            warn.append(f"{rel}: skipped (larger than the {settings.analysis_max_file_bytes}-byte analysis limit).")
            continue
        if read.status == "unreadable":
            run.files_failed += 1
            _state(db, states, repo.id, rel, "", setup, "read_failed", run.id)
            warn.append(f"{rel}: could not be read.")
            continue
        sha = hashlib.sha256(read.data).hexdigest()
        if config.exclude_generated and cfg_mod.looks_generated(rel, read.data[:600].decode("utf-8", "ignore")):
            excluded.add(rel)
            handled.discard(rel)
            run.files_excluded += 1
            continue
        st = states.get(rel)
        if not force and st is not None and st.status == "analyzed" and st.content_sha == sha and st.fingerprint_of_setup == setup:
            unchanged.append(rel)
            run.files_unchanged += 1
            continue
        parsed, reason = parse_python(read.data, rel)
        if parsed is None:
            run.files_parse_failed += 1
            parse_failures.append(f"{rel}: {reason}")
            _state(db, states, repo.id, rel, sha, setup, "parse_failed", run.id)
            continue
        facts = collect_facts(parsed.tree)
        raw: list[RawFinding] = []
        evaluated: set[str] = set()
        failed_here = False
        for analyzer in catalog.ANALYZERS:
            if "Python" not in analyzer.languages:
                continue
            enabled = config.enabled_options([r.spec.id for r in analyzer.rules])
            if not enabled:
                continue
            res = analyzer.run(lambda options, p=parsed, f=facts: RuleContext(p, options, f), enabled)
            raw.extend(res.findings)
            failed_rules = {rid for rid, _ in res.errors}
            evaluated.update(rid for rid in enabled if rid not in failed_rules)
            for rid, exc_name in res.errors:
                failed_here = True
                entry = analyzer_errors.setdefault((analyzer.id, rid, exc_name), [0, rel])
                entry[0] += 1
        any_evaluated = any_evaluated or bool(evaluated)
        if failed_here:
            run.files_failed += 1
        else:
            run.files_analyzed += 1
        _state(db, states, repo.id, rel, sha, setup, "analyzer_failed" if failed_here else "analyzed", run.id)
        _persist_file(db, settings, run, repo, snap, config, index, rel, raw, evaluated, totals, warn)
        since_commit += 1
        if since_commit >= BATCH:
            db.commit()
            since_commit = 0

    if stopped_at is not None:
        remaining = eligible[stopped_at:]
        handled.update(remaining)
        run.files_not_reached += len(remaining)
        warn.append(f"The time budget ({settings.analysis_time_budget_seconds:g}s) was reached; "
                    f"{len(remaining)} file(s) were not analysed and their findings were left unchanged.")
    handled.update(eligible_all[settings.analysis_max_files:])
    handled.update(unchanged)

    if unchanged:
        for chunk in _chunks(unchanged, 400):
            where = (Finding.repository_id == run.repository_id, Finding.rel_path.in_(chunk), Finding.status != "resolved")
            # Seen again, unchanged: a finding that was new/reopened in an earlier run is now simply existing.
            db.execute(update(Finding).where(*where, Finding.status.in_(("new", "reopened")),
                                             Finding.first_run_id != run.id).values(status="existing"))
            db.execute(update(Finding).where(*where).values(last_seen_at=utcnow(), last_run_id=run.id))
    totals.resolved += _reconcile(db, run, root, excluded, handled, disabled_rules)

    db.flush()
    open_count = db.scalar(select(func.count()).select_from(Finding).where(
        Finding.repository_id == repo.id, Finding.status.in_(OPEN_STATUSES))) or 0
    run.findings_new, run.findings_reopened, run.findings_resolved = totals.new, totals.reopened, totals.resolved
    run.findings_open = int(open_count)

    if parse_failures:
        warn.append(f"{len(parse_failures)} file(s) could not be parsed and were not analysed "
                    f"(first: {parse_failures[0]}). Their existing findings were left unchanged.")
    errors_out = [{"analyzer_id": a, "rule_id": r, "error_type": e, "files": c, "first_file": f}
                  for (a, r, e), (c, f) in sorted(analyzer_errors.items())]
    if errors_out:
        warn.append(f"{len(errors_out)} analyzer rule(s) raised errors; affected files were only partially analysed.")
    if run.rules_disabled and run.config_source == "repository":
        warn.append(f"{run.rules_disabled} rule(s) are disabled by configuration.")
    if run.files_excluded:
        warn.append(f"{run.files_excluded} file(s) were excluded by configuration or as generated.")
    run.warnings = [scrub(w, 300) for w in warn][:50]
    run.analyzer_errors = errors_out[:50]
    usable = run.files_analyzed + run.files_unchanged
    material = bool(parse_failures or errors_out or run.files_failed or run.files_skipped or run.files_not_reached)
    if run.files_eligible > 0 and usable == 0 and not any_evaluated and (run.files_parse_failed + run.files_failed) > 0:
        run.status, run.error_message = "failed", "No file could be analysed; see warnings."
    else:
        run.status = "completed_with_warnings" if material else "completed"
    run.finished_at = utcnow()
    run.duration_ms = int((time.monotonic() - started) * 1000)
    audit.record(db, workspace_id=repo.workspace_id, actor_user_id=run.requested_by, action="analysis.finished",
                 entity_type="analysis_run", entity_id=run.id,
                 details={"status": run.status, "open": run.findings_open, "files_analyzed": run.files_analyzed})
    db.commit()


def _chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _state(db: Session, states: dict[str, AnalysisFileState], repo_id: str, rel: str, sha: str, setup: str,
           status: str, run_id: str) -> None:
    st = states.get(rel)
    if st is None:
        st = AnalysisFileState(repository_id=repo_id, rel_path=rel)
        db.add(st)
        states[rel] = st
    st.content_sha, st.fingerprint_of_setup, st.status, st.run_id, st.analyzed_at = sha, setup, status, run_id, utcnow()


# ----------------------------------------------------------------------------------------------- lifecycle

def _persist_file(db: Session, settings: Settings, run: AnalysisRun, repo: Repository, snap: IndexSnapshot,
                  config: cfg_mod.EffectiveConfig, index: dict, rel: str, raw: list[RawFinding], evaluated: set[str],
                  totals: _Totals, warn: list[str]) -> None:
    now = utcnow()
    raw.sort(key=lambda r: (r.line, r.col or 0, r.rule_id, r.discriminator))
    ordinals: Counter[tuple[str, str, str]] = Counter()
    keyed: list[tuple[str, RawFinding]] = []
    for r in raw:
        key = (r.rule_id, r.scope, r.discriminator)
        fp = fingerprint.make(r.rule_id, rel, r.scope, r.discriminator, ordinals[key])
        ordinals[key] += 1
        keyed.append((fp, r))
    truncated = len(keyed) > settings.analysis_max_findings_per_file
    if truncated:
        keyed.sort(key=lambda kr: (SEVERITY_RANK[_severity(config, index, kr[1])], kr[1].line))
        keyed = keyed[: settings.analysis_max_findings_per_file]
        warn.append(f"{rel}: more than {settings.analysis_max_findings_per_file} findings; only the most severe were kept "
                    "and no findings in this file were marked resolved.")
    existing = {f.fingerprint: f for f in db.scalars(select(Finding).where(
        Finding.repository_id == repo.id, Finding.rel_path == rel))}
    symbols = _symbols_for(db, snap.id, rel) if any(r.scope for _, r in keyed) else {}
    current: set[str] = set()
    for fp, r in keyed:
        analyzer, spec = index[r.rule_id]
        current.add(fp)
        sym = _resolve_symbol(symbols, r)
        fields = dict(
            analyzer_id=analyzer.id, language="Python", start_line=r.line, end_line=max(r.end_line, r.line),
            start_col=r.col, end_col=r.end_col, symbol_stable_id=sym, scope=r.scope[:300],
            severity=_severity(config, index, r), category=spec.category, title=spec.title[:200],
            explanation=scrub(r.explanation, 2000), evidence=scrub(r.evidence, 1000), remediation=scrub(spec.remediation, 1000),
            limitations=scrub(spec.limitations, 600), confidence=r.confidence or spec.confidence, metric_value=r.metric,
            analyzer_version=analyzer.version, rule_version=spec.version, last_seen_at=now, last_run_id=run.id)
        f = existing.get(fp)
        if f is None:
            db.add(Finding(repository_id=repo.id, rule_id=r.rule_id, rel_path=rel, fingerprint=fp, status="new",
                           first_seen_at=now, first_run_id=run.id, **fields))
            totals.new += 1
            continue
        for k, v in fields.items():
            setattr(f, k, v)
        if f.status == "resolved":
            f.status, f.resolution_reason, f.resolved_at = "reopened", None, None
            f.reopened_count += 1
            totals.reopened += 1
        elif f.status in ("new", "reopened") and f.first_run_id != run.id:
            f.status = "existing"
    if truncated:
        return
    for fp, f in existing.items():
        if fp in current or f.status == "resolved":
            continue
        if f.rule_id in evaluated:
            f.status, f.resolution_reason, f.resolved_at = "resolved", "fixed", now
            totals.resolved += 1
        elif not config.rules.get(f.rule_id, cfg_mod.EffectiveRule(True, None, {})).enabled:
            f.status, f.resolution_reason, f.resolved_at = "resolved", "rule_disabled", now
            totals.resolved += 1


def _severity(config: cfg_mod.EffectiveConfig, index: dict, r: RawFinding) -> str:
    override = config.rules[r.rule_id].severity
    return override or r.severity or index[r.rule_id][1].severity


def _symbols_for(db: Session, snapshot_id: str, rel: str) -> dict[str, list[tuple[int, str]]]:
    rows = db.execute(select(Symbol.name, Symbol.start_line, Symbol.stable_id)
                      .join(IndexedFile, IndexedFile.id == Symbol.file_id)
                      .where(Symbol.snapshot_id == snapshot_id, IndexedFile.rel_path == rel,
                             IndexedFile.snapshot_id == snapshot_id,
                             Symbol.kind.in_(("function", "method", "constructor", "class")))).all()
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for name, line, sid in rows:
        out[name].append((line, sid))
    return out


def _resolve_symbol(symbols: dict[str, list[tuple[int, str]]], r: RawFinding) -> str | None:
    """Match the enclosing definition by name and proximity to its line. None when the index is stale or ambiguous."""
    if not r.scope or r.def_line is None:
        return None
    cands = [(abs(line - r.def_line), sid) for line, sid in symbols.get(r.scope.rsplit(".", 1)[-1], [])
             if abs(line - r.def_line) <= 8]
    if not cands:
        return None
    cands.sort()
    if len(cands) > 1 and cands[0][0] == cands[1][0]:
        return None
    return cands[0][1]


def _reconcile(db: Session, run: AnalysisRun, root: Path, excluded: set[str], handled: set[str],
               disabled_rules: set[str]) -> int:
    """Close findings whose file or rule is no longer evaluated, with an explicit reason. Findings in files that were skipped,
    failed, unparsed or not reached are never touched: not analysing a file is not evidence the problem is gone."""
    now = utcnow()
    resolved = 0
    paths = db.execute(select(Finding.rel_path).where(Finding.repository_id == run.repository_id,
                                                      Finding.status != "resolved").distinct()).scalars().all()
    for rel in paths:
        if rel in handled:
            continue
        if rel in excluded:
            reason = "file_excluded"
        else:
            try:
                present = os.path.lexists(safe_join(root, rel))
            except NovaError:
                present = False
            # Gone from disk: removed. Still there but no longer an eligible, analysed file: excluded.
            reason = "file_excluded" if present else "file_removed"
        result = db.execute(update(Finding).where(
            Finding.repository_id == run.repository_id, Finding.rel_path == rel, Finding.status != "resolved")
            .values(status="resolved", resolution_reason=reason, resolved_at=now))
        resolved += int(result.rowcount or 0)  # type: ignore[attr-defined]
        if reason == "file_removed":
            db.execute(AnalysisFileState.__table__.delete().where(
                AnalysisFileState.repository_id == run.repository_id, AnalysisFileState.rel_path == rel))
    if disabled_rules:
        result = db.execute(update(Finding).where(
            Finding.repository_id == run.repository_id, Finding.status != "resolved", Finding.rule_id.in_(disabled_rules))
            .values(status="resolved", resolution_reason="rule_disabled", resolved_at=now))
        resolved += int(result.rowcount or 0)  # type: ignore[attr-defined]
    return resolved
