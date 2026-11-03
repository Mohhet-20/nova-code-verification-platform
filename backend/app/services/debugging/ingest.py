"""Turn parsed failure evidence into deduplicated, sanitised ``FailureReport`` rows."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, PayloadTooLargeError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    FailureFrame,
    FailureOccurrence,
    FailureReport,
    IndexedFile,
    Repository,
    Symbol,
    TestDiscovery,
    TestExecutionRun,
    TestFile,
)
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services import repositories as repo_svc
from app.services.debugging import parsing

FINGERPRINT_VERSION = "1"
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{8,}\b")
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_WS = re.compile(r"\s+")


def message_structure(message: str) -> str:
    """The shape of a message: literals, numbers and ids removed so ``x=3`` and ``x=4`` match, ``KeyError`` stays distinct."""
    first = message.strip().split("\n", 1)[0][:300]
    first = _HEX.sub("H", first)
    first = _QUOTED.sub("S", first)
    first = _NUM.sub("N", first)
    return _WS.sub(" ", first).strip()[:200]


def compute_fingerprint(category: str, etype: str | None, message: str, test_key: str | None,
                        repo_frames: list[tuple[str, str | None]]) -> str:
    """SHA-1 over category, exception type, message shape, test id (without parameters) and up to six repository frames.

    Line numbers, timestamps, run ids and snapshot ids are deliberately excluded so edits that only move code do not
    split a failure, and a rerun does not create a new one.
    """
    payload = {"v": FINGERPRINT_VERSION, "c": category, "t": (etype or "").split(".")[-1], "m": message_structure(message),
               "k": test_key or "", "f": [[p, f or ""] for p, f in repo_frames[-6:]]}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()  # noqa: S324 - identity, not security


@dataclass
class Env:
    db: Session
    repo: Repository
    settings: Settings
    snapshot_id: str | None
    revision: str | None
    root: str
    indexed: dict[str, str]  # rel_path -> file id
    test_files: set[str] = field(default_factory=set)
    _symbols: dict[str, list[Symbol]] = field(default_factory=dict)

    def symbols(self, rel: str) -> list[Symbol]:
        if rel not in self._symbols:
            fid = self.indexed.get(rel)
            rows = list(self.db.scalars(select(Symbol).where(Symbol.file_id == fid, Symbol.kind.in_(("function", "method", "class"))))) if fid else []
            self._symbols[rel] = rows
        return self._symbols[rel]

    def symbol_at(self, rel: str, line: int | None) -> Symbol | None:
        if line is None:
            return None
        best: Symbol | None = None
        for s in self.symbols(rel):
            if s.start_line <= line <= s.end_line:
                if best is None or (s.kind != "class" and best.kind == "class") or \
                        (s.kind == best.kind and (s.end_line - s.start_line) < (best.end_line - best.start_line)):
                    best = s
        return best


def build_env(db: Session, settings: Settings, repo: Repository) -> Env:
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    root = ""
    try:
        root = str(resolve_repository_root(repo.root_path, settings.registration_base_dirs))
    except Exception:  # noqa: BLE001 - root only used for path stripping
        root = ""
    indexed: dict[str, str] = {}
    if snap is not None:
        indexed = dict(db.execute(select(IndexedFile.rel_path, IndexedFile.id).where(
            IndexedFile.snapshot_id == snap.id, IndexedFile.language == "Python")).all())
    tests: set[str] = set()
    d = db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo.id)
                  .order_by(TestDiscovery.started_at.desc()).limit(1))
    if d is not None:
        tests = set(db.scalars(select(TestFile.rel_path).where(TestFile.discovery_id == d.id)))
    return Env(db, repo, settings, snap.id if snap else None, snap.git_head if snap else None, root, indexed, tests)


def canonical_test_id(classname: str | None, name: str | None, env: Env) -> str | None:
    """pytest reports ``tests.test_a.TestK`` + ``test_m``; map it back to ``tests/test_a.py::TestK::test_m`` when indexed."""
    name = (name or "").strip()
    dotted = (classname or "").strip() or (name if "." in name else "")
    if not dotted and not name:
        return None
    parts = dotted.split(".") if dotted else []
    for k in range(len(parts), 0, -1):
        cand = "/".join(parts[:k]) + ".py"
        if cand in env.indexed:
            rest = parts[k:]
            if not classname:  # collection error: the whole file is the unit
                return cand
            return "::".join([cand, *rest, name])
    return f"{classname}::{name}".strip(":")[:500] if (classname or name) else None


def _summary(pf: parsing.ParsedFailure, test_id: str | None) -> str:
    first = pf.message.strip().split("\n", 1)[0]
    if pf.category == "collection_error":
        where = (test_id or "a test module").split("::")[0]
        return f"Collection error in {where}: {pf.exception_type or 'error'}"[:300]
    if pf.exception_type and not first.startswith(pf.exception_type):
        first = f"{pf.exception_type}: {first}" if first else pf.exception_type
    return (first or "Failure without a message")[:300]


def ingest_parsed(env: Env, ctx: RequestContext | None, pf: parsing.ParsedFailure, *, source: str,
                  execution_run_id: str | None, allow_suffix: bool, test_id: str | None = None) -> tuple[FailureReport, bool]:
    db, settings = env.db, env.settings
    indexed = set(env.indexed)
    warnings = list(pf.warnings)
    frames = pf.frames
    truncated = False
    if len(frames) > settings.debug_max_frames:
        frames, truncated = frames[-settings.debug_max_frames:], True
        warnings.append(f"Only the last {settings.debug_max_frames} frames were kept.")
    resolved = [(f, parsing.resolve_frame_path(f.path, indexed, env.root, env.test_files, allow_suffix)) for f in frames]
    escaped = sum(1 for _, r in resolved if r.escaped)
    if escaped:
        warnings.append(f"{escaped} frame path(s) outside the repository were not mapped to repository files.")
    tid = test_id or canonical_test_id(pf.classname, pf.test_name, env)
    if pf.category == "collection_error" and tid and not any(r.rel_path for _, r in resolved):
        tid_file = tid.split("::")[0]
        if tid_file in indexed:
            resolved.append((parsing.RawFrame(tid_file, None, None), parsing.ResolvedFrame(tid_file, tid_file, "repository", True)))
    message = parsing.clean_text(pf.message, 1500, env.root)
    repo_frames_for_fp: list[tuple[str, str | None]] = []
    rows: list[dict[str, Any]] = []
    for idx, (f, r) in enumerate(resolved):
        sym = env.symbol_at(r.rel_path, f.line) if r.rel_path else None
        func, fsrc = (f.function, "trace") if f.function else ((sym.qualified_name, "index") if sym else (None, None))
        rows.append(dict(idx=idx, rel_path=r.rel_path, display_path=r.display_path, function=(func or "")[:300] or None,
                         function_source=fsrc, line=f.line, col=f.col, scope=r.scope, is_test=r.is_test,
                         symbol_stable_id=sym.stable_id if sym else None))
        if r.rel_path:
            repo_frames_for_fp.append((r.rel_path, (sym.qualified_name if sym else func)))
    if pf.category != "collection_error" and not repo_frames_for_fp:
        warnings.append("No stack frame maps to an indexed repository file; localization will have little to work with.")
    if env.snapshot_id is None:
        warnings.append("The repository has no completed index; frames could not be mapped to symbols.")
    test_key = (tid or "").split("[")[0] or None
    fp = compute_fingerprint(pf.category, pf.exception_type, message, test_key, repo_frames_for_fp)
    now = utcnow()
    existing = db.scalar(select(FailureReport).where(FailureReport.repository_id == env.repo.id, FailureReport.fingerprint == fp))
    if existing is not None:
        _recur(db, existing, execution_run_id, source, now)
        db.commit()
        return existing, False
    report = FailureReport(
        repository_id=env.repo.id, fingerprint=fp, source=source, category=pf.category,
        exception_type=(pf.exception_type or None) and pf.exception_type[:200], test_id=tid,
        summary=parsing.clean_text(_summary(pf, tid), 300, env.root), message=message, frames_truncated=truncated,
        warnings=warnings[:10], index_snapshot_id=env.snapshot_id, revision=env.revision, status="open",
        occurrence_count=1, first_seen_at=now, last_seen_at=now, last_execution_run_id=execution_run_id,
        created_by=ctx.user.id if ctx else None)
    db.add(report)
    try:
        db.flush()
        for row in rows:
            db.add(FailureFrame(failure_id=report.id, **row))
        db.add(FailureOccurrence(failure_id=report.id, repository_id=env.repo.id, execution_run_id=execution_run_id,
                                 seen_at=now, source=source))
        if ctx is not None:
            audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="failures.ingested",
                         entity_type="failure_report", entity_id=report.id,
                         details={"repository_id": env.repo.id, "source": source, "category": pf.category})
        db.commit()
    except IntegrityError:  # a concurrent identical ingest won the race
        db.rollback()
        existing = db.scalar(select(FailureReport).where(FailureReport.repository_id == env.repo.id, FailureReport.fingerprint == fp))
        if existing is None:
            raise
        _recur(db, existing, execution_run_id, source, now)
        db.commit()
        return existing, False
    return report, True


def _recur(db: Session, rep: FailureReport, run_id: str | None, source: str, now: Any) -> None:
    if run_id is not None and db.scalar(select(FailureOccurrence.id).where(
            FailureOccurrence.failure_id == rep.id, FailureOccurrence.execution_run_id == run_id)):
        return  # the same execution is never counted twice
    db.add(FailureOccurrence(failure_id=rep.id, repository_id=rep.repository_id, execution_run_id=run_id, seen_at=now, source=source))
    rep.occurrence_count += 1
    rep.last_seen_at = now  # first_seen_at is preserved
    if run_id:
        rep.last_execution_run_id = run_id
    if rep.status in ("resolved", "ignored"):
        rep.status = "open"  # it came back


def ingest_text(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, source: str, text: str,
                test_id: str | None) -> list[tuple[FailureReport, bool]]:
    if not text.strip():
        raise ValidationFailedError("The failure report is empty.")
    if len(text.encode("utf-8", errors="replace")) > settings.debug_max_report_bytes:
        raise PayloadTooLargeError(f"A failure report may be at most {settings.debug_max_report_bytes} bytes.")
    if repo_svc.latest_snapshot(db, repo.id, completed_only=True) is None:
        raise ConflictError("Index the repository first so failures can be mapped to symbols.")
    env = build_env(db, settings, repo)
    if source == "junit_xml":
        try:
            parsed = parsing.parse_junit(text)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from None
        if not parsed:
            raise ValidationFailedError("The XML contains no failing or erroring test cases.")
    elif source == "python_traceback":
        parsed = [parsing.parse_python_traceback(text)]
    else:
        parsed = [parsing.parse_pytest_text(text, "", "failure")]
    return [ingest_parsed(env, ctx, pf, source=source if source != "python_traceback" else "manual", execution_run_id=None,
                          allow_suffix=False, test_id=test_id if len(parsed) == 1 else None) for pf in parsed]


def ingest_run(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, run_id: str) -> list[tuple[FailureReport, bool]]:
    run = db.scalar(select(TestExecutionRun).where(TestExecutionRun.id == run_id, TestExecutionRun.repository_id == repo.id))
    if run is None:
        raise NotFoundError("Test run not found.")
    if run.status in ("pending", "running"):
        raise ConflictError("That run has not finished yet.")
    if not run.failures:
        raise ConflictError("That run recorded no failures to ingest.")
    env = build_env(db, settings, repo)
    out = []
    for f in run.failures:
        pf = parsing.parse_pytest_text(str(f.get("traceback") or ""), str(f.get("message") or ""), str(f.get("kind") or "failure"))
        pf.test_name, pf.classname = str(f.get("name") or ""), str(f.get("classname") or "")
        out.append(ingest_parsed(env, ctx, pf, source="execution_run", execution_run_id=run.id, allow_suffix=run.kind == "generated"))
    return out
