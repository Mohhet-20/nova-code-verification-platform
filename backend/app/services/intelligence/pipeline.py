"""Index generation pipeline: discover -> detect -> parse -> persist -> resolve -> publish.

A run builds a complete *new generation* of the index under a snapshot that is still ``running``.
Queries only read the newest published generation, so partially written rows are invisible.
Publishing (status flip + deletion of the previous generation) happens in one final commit; if the
run fails for any reason, the staged rows are removed and the previous generation stays untouched.

Nothing from the repository is executed. File contents are read as bytes, parsed as data, and are
never logged or persisted (only metadata, symbol names and short signatures are stored).
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import new_id, utcnow
from app.models import (
    FileCall,
    FileImport,
    IndexDiagnostic,
    IndexedFile,
    IndexSnapshot,
    Relationship,
    Repository,
    Symbol,
)
from app.security.paths import resolve_repository_root
from app.services import audit, git
from app.services.indexer import IndexLimits, ScannedFile, scan_repository
from app.services.intelligence import import_analyzer as ia
from app.services.intelligence import manifests
from app.services.intelligence.call_resolver import (
    AMBIGUOUS,
    Binding,
    CallContext,
    SymRef,
    resolve_call,
)
from app.services.intelligence.call_resolver import (
    RESOLVED as CALL_RESOLVED,
)
from app.services.intelligence.language_detection import detect_language
from app.services.intelligence.symbol_extractor import extract_symbols
from app.services.intelligence.types import (
    ANALYSIS_VERSION,
    EXTERNAL,
    FAILED,
    PARTIAL,
    RESOLVED,
    UNAVAILABLE,
    UNRESOLVED,
    ParsedCall,
    ParsedImport,
    ParsedSymbol,
)

logger = logging.getLogger("nova.pipeline")

PUBLISHED = ("completed", "completed_with_warnings")
KEEP_SNAPSHOTS = 10
FILE_BATCH = 300
COPY_CHUNK = 200
MAX_FILE_DIAGNOSTICS = 8


def stable_symbol_id(repository_id: str, rel_path: str, local_key: str, kind: str) -> str:
    raw = f"{repository_id}\0{rel_path}\0{local_key}\0{kind}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _declared(manifest_blobs: list[tuple[str, bytes]]) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    py: set[str] = set()
    js: dict[str, frozenset[str]] = {}
    for rel, data in manifest_blobs:
        base = rel.rsplit("/", 1)[-1]
        if base == "package.json":
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            js[d] = frozenset(manifests.javascript_dependencies(data))
        else:
            py |= manifests.python_dependencies(rel, data)
    return frozenset(py), js


def _chunks(items: list[str], n: int):  # type: ignore[no-untyped-def]
    for i in range(0, len(items), n):
        yield items[i:i + n]


class _Run:
    def __init__(self, db: Session, settings: Settings, snapshot: IndexSnapshot, repo: Repository,
                 limits: IndexLimits) -> None:
        self.db, self.settings, self.snapshot, self.repo, self.limits = db, settings, snapshot, repo, limits
        self.prev: IndexSnapshot | None = db.scalar(
            select(IndexSnapshot).where(
                IndexSnapshot.repository_id == repo.id, IndexSnapshot.status.in_(PUBLISHED),
                IndexSnapshot.id != snapshot.id,
            ).order_by(IndexSnapshot.generation.desc(), IndexSnapshot.started_at.desc()).limit(1))
        self.prev_files: dict[str, dict] = {}
        if self.prev is not None:
            rows = db.execute(select(
                IndexedFile.rel_path, IndexedFile.id, IndexedFile.sha256, IndexedFile.analysis_version,
                IndexedFile.analysis_status, IndexedFile.parser, IndexedFile.symbol_count,
                IndexedFile.import_count, IndexedFile.parse_diagnostics, IndexedFile.language,
            ).where(IndexedFile.snapshot_id == self.prev.id))
            self.prev_files = {r[0]: {"id": r[1], "sha": r[2], "ver": r[3], "status": r[4],
                                      "parser": r[5], "sym": r[6], "imp": r[7], "diag": r[8],
                                      "lang": r[9]} for r in rows}
        self.prev_paths: set[str] = set(self.prev_files)
        self.buf_files: list[IndexedFile] = []
        self.buf_syms: list[dict] = []
        self.buf_imps: list[dict] = []
        self.buf_calls: list[dict] = []
        self.copy_pairs: list[tuple[str, str]] = []  # (new_file_id, old_file_id)
        self.manifest_blobs: list[tuple[str, bytes]] = []
        self.languages: Counter[str] = Counter()
        self.analysis: Counter[str] = Counter()
        self.changes: Counter[str] = Counter()
        self.parse_diags: list[tuple[str, str, str, str]] = []  # (rel, code, severity, detail)
        self.symbols_total = 0
        self.imports_total = 0
        self.file_paths: dict[str, str] = {}  # file_id -> rel_path
        self.file_langs: dict[str, str] = {}

    # ---- per file -------------------------------------------------------------------------
    def on_file(self, f: ScannedFile) -> None:
        guess = detect_language(f.rel_path.rsplit("/", 1)[-1], f.data[:256])
        language = guess.language
        file_id = new_id()
        prev = self.prev_files.get(f.rel_path)
        row = IndexedFile(
            id=file_id, snapshot_id=self.snapshot.id, repository_id=self.repo.id, rel_path=f.rel_path,
            extension=f.extension, language=language, size_bytes=f.size_bytes, line_count=f.line_count,
            sha256=f.sha256, depth=f.depth, mtime_ns=f.mtime_ns, language_confidence=guess.confidence)
        reusable = (self.snapshot.mode != "full" and prev is not None and prev["sha"] == f.sha256 and prev["ver"] == ANALYSIS_VERSION
                    and prev["lang"] == language and prev["status"] not in (FAILED, UNAVAILABLE))
        if prev is None:
            row.change_status = "new"
        else:
            row.change_status = "unchanged" if prev["sha"] == f.sha256 else "changed"
        if manifests.is_manifest(f.rel_path):
            self.manifest_blobs.append((f.rel_path, f.data))
        if reusable:
            assert prev is not None
            row.analysis_status, row.parser = prev["status"], prev["parser"]
            row.analysis_version = prev["ver"]
            row.symbol_count, row.import_count = prev["sym"], prev["imp"]
            row.parse_diagnostics = list(prev["diag"] or [])
            self.copy_pairs.append((file_id, prev["id"]))
            self.symbols_total += prev["sym"]
            self.imports_total += prev["imp"]
        else:
            result = extract_symbols(language, f.data, f.rel_path, self.settings.index_max_symbols_per_file)
            row.analysis_status, row.parser = result.status, result.parser
            row.analysis_version = ANALYSIS_VERSION
            row.parse_diagnostics = [_short(d, 200) for d in result.diagnostics[:MAX_FILE_DIAGNOSTICS]]
            ids = self._add_symbols(file_id, f.rel_path, language, f.sha256, result.parser, result.symbols)
            self._add_imports(file_id, result.imports)
            self._add_calls(file_id, ids, result.calls)
            row.symbol_count, row.import_count = len(result.symbols), len(result.imports)
            self.symbols_total += row.symbol_count
            self.imports_total += row.import_count
        self.changes[row.change_status] += 1
        self.analysis[row.analysis_status] += 1
        self.languages[language] += 1
        if row.analysis_status in (FAILED, PARTIAL, UNAVAILABLE):
            sev = "error" if row.analysis_status == FAILED else "warning"
            detail = "; ".join(row.parse_diagnostics[:2]) or row.analysis_status
            self.parse_diags.append((f.rel_path, f"ANALYSIS_{row.analysis_status.upper()}", sev,
                                     _short(detail, 300)))
        self.file_paths[file_id] = f.rel_path
        self.file_langs[file_id] = language
        self.buf_files.append(row)
        if len(self.buf_files) >= FILE_BATCH:
            self.flush()

    def _add_calls(self, file_id: str, ids: dict[str, str], calls: list[ParsedCall]) -> None:
        for c in calls:
            self.buf_calls.append(dict(
                snapshot_id=self.snapshot.id, repository_id=self.repo.id, file_id=file_id,
                caller_stable_id=ids.get(c.caller_key) if c.caller_key else None,
                callee=c.callee, line=c.line, resolution_status="unresolved", resolution_note=None,
                candidate_count=0))

    def _add_symbols(self, file_id: str, rel: str, language: str, sha: str, parser: str,
                     symbols: list[ParsedSymbol]) -> dict[str, str]:
        ids = {s.local_key: stable_symbol_id(self.repo.id, rel, s.local_key, s.kind) for s in symbols}
        for s in symbols:
            self.buf_syms.append(dict(
                snapshot_id=self.snapshot.id, repository_id=self.repo.id, file_id=file_id,
                stable_id=ids[s.local_key], name=_short(s.name, 200), name_lower=_short(s.name, 200).lower(),
                qualified_name=_short(s.qualified_name, 1200),
                qualified_lower=_short(s.qualified_name, 1200).lower(), kind=s.kind, language=language,
                start_line=s.start_line, start_col=s.start_col, end_line=s.end_line, end_col=s.end_col,
                parent_stable_id=ids.get(s.parent_key) if s.parent_key else None,
                visibility=s.visibility, exported=s.exported,
                signature=_short(s.signature, 300) if s.signature else None,
                doc_summary=_short(s.doc_summary, 300) if s.doc_summary else None,
                bases=[_short(b, 200) for b in s.bases[:10]], parser=parser, confidence=s.confidence,
                file_sha256=sha))
        return ids

    def _add_imports(self, file_id: str, imports: list[ParsedImport]) -> None:
        for i in imports:
            self.buf_imps.append(dict(
                snapshot_id=self.snapshot.id, repository_id=self.repo.id, file_id=file_id, kind=i.kind,
                specifier=_short(i.specifier, 500), names=i.names[:50], level=i.level, line=i.line,
                type_only=i.type_only))

    def flush(self) -> None:
        self.db.add_all(self.buf_files)
        self.db.flush()  # files first (FK targets)
        self._bulk(Symbol, self.buf_syms)
        self._bulk(FileImport, self.buf_imps)
        self._bulk(FileCall, self.buf_calls)
        self.buf_files.clear()
        self.buf_syms.clear()
        self.buf_imps.clear()
        self.buf_calls.clear()

    def _bulk(self, model: type, rows: list[dict]) -> None:
        """Core multi-row INSERT: avoids per-row ORM object construction (the dominant cost at scale)."""
        table = model.__table__  # type: ignore[attr-defined]
        for i in range(0, len(rows), 1000):
            chunk = rows[i:i + 1000]
            keys = {k for r in chunk for k in r}  # executemany needs one key set; absent == NULL
            for r in chunk:
                r.setdefault("id", new_id())
                for k in keys:
                    r.setdefault(k, None)
            self.db.execute(insert(table), chunk)

    # ---- unchanged-file reuse ---------------------------------------------------------------
    def copy_unchanged(self) -> None:
        old_to_new = {old: new for new, old in self.copy_pairs}
        sym_cols = [c for c in Symbol.__table__.columns if c.name not in ("id", "snapshot_id", "file_id")]
        imp_cols = [c for c in FileImport.__table__.columns
                    if c.name in ("kind", "specifier", "names", "level", "line", "type_only")]
        for chunk in _chunks(list(old_to_new), COPY_CHUNK):
            rows = [dict(r._mapping, snapshot_id=self.snapshot.id, file_id=old_to_new[r._mapping["file_id"]])
                    for r in self.db.execute(select(Symbol.file_id, *sym_cols).where(Symbol.file_id.in_(chunk)))]
            for r in rows:
                r["id"] = new_id()
            self._bulk(Symbol, rows)
            irows = [dict(r._mapping, snapshot_id=self.snapshot.id, repository_id=self.repo.id,
                          file_id=old_to_new[r._mapping["file_id"]], id=new_id())
                     for r in self.db.execute(select(FileImport.file_id, *imp_cols)
                                              .where(FileImport.file_id.in_(chunk)))]
            self._bulk(FileImport, irows)
            crows = [dict(r._mapping, snapshot_id=self.snapshot.id, repository_id=self.repo.id,
                          file_id=old_to_new[r._mapping["file_id"]], id=new_id(),
                          resolution_status="unresolved", resolution_note=None, candidate_count=0)
                     for r in self.db.execute(select(FileCall.file_id, FileCall.caller_stable_id, FileCall.callee,
                                                     FileCall.line).where(FileCall.file_id.in_(chunk)))]
            self._bulk(FileCall, crows)

    # ---- resolution -------------------------------------------------------------------------
    def resolve_all(self) -> tuple[int, int, int]:
        """Resolve every import against the *new* path set. Returns (imports, unresolved, relationships)."""
        sid = self.snapshot.id
        paths = frozenset(self.file_paths.values())
        path_to_id = {p: i for i, p in self.file_paths.items()}
        declared_py, declared_js = _declared(self.manifest_blobs)
        ctx = ia.ResolverContext(paths=paths, declared_py=declared_py, declared_js_by_dir=declared_js)
        updates: list[dict] = []
        rels: list[dict] = []
        seen_rel: set[tuple[str, str, str]] = set()
        unresolved = 0
        bindings: dict[tuple[str, str], Binding] = {}
        rows = self.db.execute(select(FileImport.id, FileImport.file_id, FileImport.specifier,
                                      FileImport.level, FileImport.names, FileImport.kind, FileImport.line)
                               .where(FileImport.snapshot_id == sid))
        for imp_id, file_id, spec, level, names, kind, imp_line in rows:
            rel = self.file_paths[file_id]
            lang = self.file_langs[file_id]
            if kind == "dynamic":
                updates.append({"id": imp_id, "resolution_status": "unsupported",
                                "resolution_note": "dynamic import; target is not statically determinable",
                                "package": None, "resolved_file_id": None})
                continue
            if kind == "export" and not spec:  # local export list: there is no module to resolve
                updates.append({"id": imp_id, "resolution_status": "unsupported",
                                "resolution_note": "local export list; no module source",
                                "package": None, "resolved_file_id": None})
                continue
            if lang in ia.PY_LANGS:
                res = ia.resolve_python(ctx, rel, spec, level, [n.get("name") or "" for n in (names or [])])
            elif lang in ia.JS_LANGS:
                res = ia.resolve_javascript(ctx, rel, spec)
            else:
                continue
            first = path_to_id.get(res.targets[0]) if res.targets else None
            _bind(bindings, file_id, lang in ia.PY_LANGS, spec, level, names or [], res, path_to_id)
            updates.append({"id": imp_id, "resolution_status": res.status,
                            "resolution_note": _short(res.note, 200) if res.note else None,
                            "package": res.package, "resolved_file_id": first})
            if res.status == UNRESOLVED:
                unresolved += 1
            if res.status == RESOLVED:
                for t in res.targets:
                    tid = path_to_id.get(t)
                    if tid is None or (file_id, tid, "F") in seen_rel:
                        continue
                    seen_rel.add((file_id, tid, "F"))
                    rels.append(dict(snapshot_id=sid, repository_id=self.repo.id, type="IMPORTS_FILE",
                                             source_file_id=file_id, target_file_id=tid, target_text=t,
                                             status=RESOLVED, evidence="observed", line=imp_line))
            elif res.status == EXTERNAL:
                key = (file_id, res.package or spec, "P")
                if key not in seen_rel:
                    seen_rel.add(key)
                    rels.append(dict(snapshot_id=sid, repository_id=self.repo.id,
                                             type="DEPENDS_ON_PACKAGE", source_file_id=file_id,
                                             target_text=_short(res.package or spec, 500),
                                             status=EXTERNAL, evidence="observed", line=imp_line))
        for i in range(0, len(updates), 500):
            self.db.execute(update(FileImport), updates[i:i + 500])
        rels.extend(self._extends(path_to_id))
        rels.extend(self._resolve_calls(bindings))
        self._bulk(Relationship, rels)
        return self.imports_total, unresolved, len(rels)

    def _resolve_calls(self, bindings: dict[tuple[str, str], Binding]) -> list[dict]:
        """Resolve observed call sites by name (see call_resolver) and emit CALLS edges for resolved/ambiguous ones."""
        sid = self.snapshot.id
        ctx = CallContext(bindings=bindings)
        for stable, fid, name, kind, parent, bases in self.db.execute(
                select(Symbol.stable_id, Symbol.file_id, Symbol.name, Symbol.kind, Symbol.parent_stable_id,
                       Symbol.bases).where(Symbol.snapshot_id == sid)
                .order_by(Symbol.file_id, Symbol.start_line, Symbol.start_col, Symbol.id)):
            ctx.add(SymRef(stable, fid, name, kind, parent), list(bases or []))
        updates: list[dict] = []
        edges: list[dict] = []
        seen: set[tuple[str | None, str, str]] = set()
        for cid, fid, caller_id, callee, line in self.db.execute(
                select(FileCall.id, FileCall.file_id, FileCall.caller_stable_id, FileCall.callee, FileCall.line)
                .where(FileCall.snapshot_id == sid)):
            res = resolve_call(ctx, fid, ctx.syms.get(caller_id) if caller_id else None, callee)
            updates.append({"id": cid, "resolution_status": res.status,
                            "resolution_note": _short(res.note, 200) if res.note else None,
                            "candidate_count": len(res.targets)})
            if res.status in (CALL_RESOLVED, AMBIGUOUS):
                for t in res.targets:
                    key = (caller_id, t.stable_id, fid)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(dict(snapshot_id=sid, repository_id=self.repo.id, type="CALLS",
                                      source_file_id=fid, source_symbol_stable_id=caller_id,
                                      target_file_id=t.file_id, target_symbol_stable_id=t.stable_id,
                                      target_text=_short(callee or "", 500), status=res.status,
                                      evidence="inferred", line=line))
        for i in range(0, len(updates), 500):
            self.db.execute(update(FileCall), updates[i:i + 500])
        return edges

    def _extends(self, path_to_id: dict[str, str]) -> list[dict]:
        """EXTENDS_SYMBOL: base names bound in the same file or through a directly resolved import. Inferred."""
        sid = self.snapshot.id
        out: list[dict] = []
        child_rows = self.db.execute(select(Symbol.file_id, Symbol.stable_id, Symbol.bases)
                                     .where(Symbol.snapshot_id == sid, Symbol.kind.in_(("class", "interface")))
                                     ).all()
        children = [(f, s, b) for f, s, b in child_rows if b]
        if not children:
            return out
        decl: dict[tuple[str, str], str] = {}  # (file_id, name) -> stable_id
        for fid, stable, name in self.db.execute(select(Symbol.file_id, Symbol.stable_id, Symbol.name).where(
                Symbol.snapshot_id == sid, Symbol.kind.in_(("class", "interface")))):
            decl.setdefault((fid, name), stable)
        bindings: dict[tuple[str, str], tuple[str, str]] = {}  # (file_id, local) -> (target_file_id, imported name)
        for fid, names, target in self.db.execute(select(FileImport.file_id, FileImport.names, FileImport.resolved_file_id)
                                                  .where(FileImport.snapshot_id == sid,
                                                         FileImport.resolution_status == RESOLVED)):
            if target is None:
                continue
            for n in names or []:
                nm = n.get("name")
                if nm and nm != "*":
                    bindings[(fid, n.get("alias") or nm)] = (target, nm)
        for fid, stable, bases in children:
            for base in bases:
                head = base.split("(")[0].split("<")[0].strip()
                simple = head.split(".")[-1]
                target_stable, target_file = None, None
                if (fid, head) in decl:
                    target_stable, target_file = decl[(fid, head)], fid
                elif (fid, head.split(".")[0]) in bindings and "." not in head:
                    tf, nm = bindings[(fid, head)]
                    target_stable, target_file = decl.get((tf, nm)), tf
                elif (fid, simple) in decl and "." not in head:
                    target_stable, target_file = decl[(fid, simple)], fid
                if target_stable:
                    out.append(dict(snapshot_id=sid, repository_id=self.repo.id, type="EXTENDS_SYMBOL",
                                            source_file_id=fid, source_symbol_stable_id=stable,
                                            target_file_id=target_file, target_symbol_stable_id=target_stable,
                                            target_text=_short(base, 500), status=RESOLVED,
                                            evidence="inferred"))
        return out


def _stem(path: str) -> str:
    parts = path.rsplit(".", 1)[0].split("/")
    return parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]


def _bind(bindings: dict[tuple[str, str], Binding], file_id: str, python: bool, spec: str, level: int,
          names: list[dict], res, path_to_id: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Record how local names introduced by one import can be traced to a file or package (for call resolution)."""
    plain = python and level == 0 and len(names) == 1 and (names[0].get("name") or "") == spec
    for n in names:
        nm, alias = n.get("name") or "", n.get("alias")
        if python:
            local = alias or ("" if "." in nm or nm == "*" else nm)
        else:
            local = alias or ("" if nm in ("*", "default") else nm)
        if not local:
            continue
        if res.status == EXTERNAL:
            bindings[(file_id, local)] = Binding("ext", package=res.package)
        elif res.status == RESOLVED and res.targets:
            first = path_to_id.get(res.targets[0])
            if python:
                if plain:
                    bindings[(file_id, local)] = Binding("mod", file_id=first)
                    continue
                sub = next((t for t in res.targets if _stem(t) == nm), None)
                if sub is not None and path_to_id.get(sub):
                    bindings[(file_id, local)] = Binding("mod", file_id=path_to_id[sub])
                else:
                    bindings[(file_id, local)] = Binding("sym", file_id=first, name=nm)
            elif nm == "*":
                bindings[(file_id, local)] = Binding("mod", file_id=first)
            elif nm == "default":
                bindings[(file_id, local)] = Binding("default", file_id=first)
            else:
                bindings[(file_id, local)] = Binding("sym", file_id=first, name=nm)


def _limits(settings: Settings) -> IndexLimits:
    return IndexLimits(settings.index_max_files, settings.index_max_depth, settings.index_max_file_bytes,
                       settings.index_max_total_bytes, float(settings.index_time_budget_seconds),
                       settings.index_max_diagnostics, tuple(settings.index_extra_excludes))


def run_index(db: Session, settings: Settings, workspace_id: str, actor_user_id: str | None,
              snapshot_id: str) -> None:
    """Build and publish one generation. Always leaves the snapshot completed*, or failed with the old index intact."""
    snapshot = db.get(IndexSnapshot, snapshot_id)
    if snapshot is None:
        return
    repo = db.get(Repository, snapshot.repository_id)
    assert repo is not None
    started = time.monotonic()
    try:
        root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
        run = _Run(db, settings, snapshot, repo, _limits(settings))
        prev_gen = run.prev.generation if run.prev is not None else 0
        snapshot.generation = prev_gen + 1

        snap: IndexSnapshot = snapshot

        def on_progress(r) -> None:  # type: ignore[no-untyped-def]
            run.flush()
            snap.files_indexed = r.files_indexed
            snap.total_bytes = r.total_bytes
            db.commit()

        result = scan_repository(root, run.limits, run.on_file, on_progress)
        run.flush()
        run.copy_unchanged()
        imports_total, unresolved, rel_count = run.resolve_all()
        info = git.inspect_repository(root, settings.git_timeout_seconds, settings.git_max_output_bytes)

        indexed_paths = set(run.file_paths.values())
        removed = 0 if run.prev is None else (
            None if result.truncated else len(run.prev_paths - indexed_paths))
        failed = run.analysis[FAILED]
        warnings: list[str] = []
        if result.truncated:
            warnings.append(f"Scan stopped early ({result.truncation_reason}); the index is incomplete.")
        if failed:
            warnings.append(f"{failed} file(s) could not be analyzed.")
        if run.analysis[PARTIAL]:
            warnings.append(f"{run.analysis[PARTIAL]} file(s) have syntax errors; symbols are partial.")
        if run.analysis[UNAVAILABLE]:
            warnings.append(f"{run.analysis[UNAVAILABLE]} file(s) need a parser that is not available.")

        diag_rows: list[IndexDiagnostic] = []
        cap = settings.index_max_diagnostics
        for d in result.diagnostics:
            diag_rows.append(IndexDiagnostic(snapshot_id=snapshot.id, rel_path=d.rel_path, code=d.code,
                                             severity="info", detail=None))
        for rel, code, sev, detail in run.parse_diags:
            diag_rows.append(IndexDiagnostic(snapshot_id=snapshot.id, rel_path=rel, code=code,
                                             severity=sev, detail=detail))
        if len(diag_rows) > cap:
            warnings.append(f"Diagnostics truncated to {cap} of {len(diag_rows)}.")
            diag_rows = sorted(diag_rows, key=lambda r: r.severity == "info")[:cap]
        db.add_all(diag_rows)

        snapshot.files_indexed = result.files_indexed
        snapshot.directories_visited = result.directories_visited
        snapshot.total_bytes, snapshot.total_lines = result.total_bytes, result.total_lines
        snapshot.skipped_counts = dict(result.skipped)
        snapshot.files_skipped = sum(result.skipped.values())
        snapshot.language_counts = dict(run.languages)
        snapshot.analysis_counts = dict(run.analysis)
        snapshot.truncated, snapshot.truncation_reason = result.truncated, result.truncation_reason
        snapshot.files_new = run.changes["new"]
        snapshot.files_changed = run.changes["changed"]
        snapshot.files_unchanged = run.changes["unchanged"]
        snapshot.files_removed, snapshot.files_failed = removed, failed
        snapshot.symbols_count = run.symbols_total
        snapshot.imports_total, snapshot.imports_unresolved = imports_total, unresolved
        snapshot.relationships_count = rel_count
        snapshot.warnings = warnings
        snapshot.git_head, snapshot.git_branch = info.head, info.branch
        snapshot.duration_ms = int((time.monotonic() - started) * 1000)
        snapshot.finished_at = utcnow()
        repo.is_git, repo.git_branch = info.is_git, info.branch
        repo.git_head, repo.git_dirty_count = info.head, info.dirty_count

        # ---- publish: status flip + removal of the previous generation, one commit ----
        snapshot.status = "completed_with_warnings" if warnings else "completed"
        _delete_other_generations(db, repo.id, snapshot.id)
        old = db.scalars(select(IndexSnapshot.id).where(IndexSnapshot.repository_id == repo.id)
                         .order_by(IndexSnapshot.started_at.desc(), IndexSnapshot.id.desc())
                         .offset(KEEP_SNAPSHOTS)).all()
        if old:
            db.execute(delete(IndexSnapshot).where(IndexSnapshot.id.in_(old),
                                                   IndexSnapshot.status != "running"))
        audit.record(db, workspace_id=workspace_id, actor_user_id=actor_user_id, action="index.completed",
                     entity_type="snapshot", entity_id=snapshot.id,
                     details={"files_indexed": result.files_indexed, "symbols": run.symbols_total,
                              "status": snapshot.status, "mode": snapshot.mode})
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("index_failed", extra={"snapshot_id": snapshot_id, "error_type": type(exc).__name__})
        snapshot = db.get(IndexSnapshot, snapshot_id)
        if snapshot is not None:
            # Remove only this run's staged rows; the previous generation is left exactly as it was.
            for model in (Relationship, FileCall, FileImport, Symbol, IndexedFile):
                db.execute(delete(model).where(model.snapshot_id == snapshot_id))
            db.execute(delete(IndexDiagnostic).where(IndexDiagnostic.snapshot_id == snapshot_id))
            snapshot.status = "failed"
            snapshot.finished_at = utcnow()
            snapshot.duration_ms = int((time.monotonic() - started) * 1000)
            snapshot.error_message = getattr(exc, "message", None) or f"Indexing failed ({type(exc).__name__})."
            audit.record(db, workspace_id=workspace_id, actor_user_id=actor_user_id, action="index.failed",
                         entity_type="snapshot", entity_id=snapshot_id,
                         details={"error_type": type(exc).__name__})
            db.commit()


def _delete_other_generations(db: Session, repository_id: str, keep_snapshot_id: str) -> None:
    """Remove every other generation's rows with a few set-based statements (children first).

    Deleting child tables explicitly is far cheaper than relying on per-row ON DELETE CASCADE.
    """
    others = select(IndexSnapshot.id).where(IndexSnapshot.repository_id == repository_id,
                                            IndexSnapshot.id != keep_snapshot_id)
    for model in (Relationship, FileCall, FileImport, Symbol):
        db.execute(delete(model).where(model.snapshot_id.in_(others)))
    db.execute(delete(IndexedFile).where(IndexedFile.repository_id == repository_id,
                                         IndexedFile.snapshot_id != keep_snapshot_id))


def count_published(db: Session, repository_id: str) -> int:
    return db.scalar(select(func.count()).select_from(IndexSnapshot).where(
        IndexSnapshot.repository_id == repository_id, IndexSnapshot.status.in_(PUBLISHED))) or 0

