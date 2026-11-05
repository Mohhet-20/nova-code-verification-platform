"""Refactoring transformation lifecycle: plan -> generate -> validate -> submit -> approve -> apply -> (re-index) -> verify.

Planning and validation never write to the repository. The only writer is ``apply``: it needs an approved transformation, an explicit
``confirm``, an atomic status claim and a per-repository lock, and it goes patch engine (hash-checked, all-or-nothing,
originals retained). Analysis-only requests end in ``analyzed`` and can never be applied.
"""
from __future__ import annotations

import ast
import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import IndexedFile, RefactorTransformation, Repository, TestExecutionRun
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services import repositories as repo_svc
from app.services.analysis.redaction import scrub
from app.services.intelligence.freshness import compute_freshness
from app.services.refactor import cycles as cycles_mod
from app.services.refactor import (
    duplicates,
    extract,
    impact,
    pyscope,
    registry,
    rename_js,
    rename_py,
    simplify,
)
from app.services.refactor.common import (
    PLAN_VERSION,
    EditError,
    PlanResult,
    Workspace,
    apply_edits,
    dedupe,
    edit_dict,
    find_conflicts,
    parser_for,
)
from app.services.refactor.errors import RefactorConflict, RefactorInvalid, RefactorNotFound, from_patch_error
from app.services.repair import patch as patchlib
from app.services.repair import selection
from app.services.repair import service as repair_service

log = logging.getLogger("nova.refactor")

TRANSITIONS: dict[str, set[str]] = {
    "planned": {"generated", "blocked", "analyzed"},
    "blocked": {"closed"},
    "generated": {"validated", "invalid", "rejected", "closed"},
    "invalid": {"validated", "invalid", "rejected", "closed"},
    "validated": {"validated", "invalid", "awaiting_approval", "rejected"},
    "awaiting_approval": {"awaiting_approval", "validated", "invalid", "approved", "rejected"},
    "approved": {"approved", "invalid", "applying", "rejected"},
    "applying": {"applied", "approved", "invalid"},
    "applied": {"applied", "verified", "verification_failed"},
    "verification_failed": {"verification_failed", "verified"},
    "verified": {"verified", "verification_failed"},
    "analyzed": {"closed"},
    "rejected": set(),
    "closed": set(),
}
ACK_CODES = {"public_api", "serialization_name", "dynamic_reference", "external_reference", "wildcard_import", "unresolved_attribute_references",
             "reexport_changed", "possibly_unbound", "side_effects"}
APPLIED = ("applied", "verification_failed", "verified")


def move(t: RefactorTransformation, new: str) -> None:
    if new != t.status and new not in TRANSITIONS.get(t.status, set()):
        raise RefactorConflict("invalid_state_transition", f"A transformation that is '{t.status}' cannot move to '{new}'.")
    t.status = new


def _audit(db: Session, ctx: RequestContext | None, repo: Repository, action: str, t: RefactorTransformation, **details: Any) -> None:
    audit.record(db, workspace_id=repo.workspace_id, actor_user_id=ctx.user.id if ctx else t.created_by, action=action, entity_type="refactor_transformation",
                 entity_id=t.id, details={"repository_id": repo.id, "type_id": t.type_id, **details})


def get_transformation(db: Session, repo: Repository, tid: str) -> RefactorTransformation:
    t = db.get(RefactorTransformation, tid)
    if t is None or t.repository_id != repo.id:
        raise RefactorNotFound("transformation_not_found", "No such refactoring in this repository.")
    return t


def _integrity(t: RefactorTransformation) -> None:
    if not t.diff_text or hashlib.sha256(t.diff_text.encode("utf-8")).hexdigest() != t.patch_sha256:
        raise RefactorConflict("patch_tampered", "The stored diff no longer matches its recorded hash. It was changed after creation and will not be used.")


# ------------------------------------------------------------------------------------------------ workspace
def build_workspace(db: Session, settings: Settings, repo: Repository) -> tuple[Workspace, Any, Any]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    fresh = compute_freshness(db, repo, settings)
    indexed: dict[str, tuple[str, str]] = {}
    if snap is not None:
        for rel, lang, sha in db.execute(select(IndexedFile.rel_path, IndexedFile.language, IndexedFile.sha256).where(IndexedFile.snapshot_id == snap.id)):
            indexed[rel] = (lang.lower(), sha)
    ws = Workspace(root, settings, indexed, fresh.state, [fresh.note] if fresh.note else [])
    return ws, snap, fresh


def _index_findings(res: PlanResult, fresh: Any, snap: Any, transform: bool) -> None:
    sev = "blocker" if transform else "warning"
    if snap is None or fresh.state == "never_indexed":
        res.add(sev, "not_indexed", "The repository has no published index. Index it first so files and dependents are known.")
        res.complete = False
    elif fresh.state == "running":
        res.add(sev, "index_running", "An indexing run is in progress; wait for it to finish so the plan uses a consistent index.")
        res.complete = False
    elif fresh.state == "stale":
        if (fresh.new or 0) > 0 or (fresh.removed or 0) > 0:
            res.add(sev, "stale_index", f"Files were added or removed since the index was built ({fresh.new or 0} new, {fresh.removed or 0} removed), so the set of "
                    "files that may reference the symbol is incomplete. Re-index and retry.", [{"path": p, "line": 0} for p in (fresh.sample_paths or [])[:5]])
        else:
            res.add("warning", "stale_index_content", f"{fresh.changed or 0} indexed file(s) changed since the index was built. Planning read the current files from disk; "
                    "dependents and tests derived from the index may be incomplete.", [{"path": p, "line": 0} for p in (fresh.sample_paths or [])[:5]])
        res.complete = False
    elif fresh.state == "unknown":
        res.add("warning", "index_freshness_unknown", "Index freshness could not be determined; treat dependents and tests as possibly incomplete.")
        res.complete = False


# ------------------------------------------------------------------------------------------------ plan creation
def _new_texts(ws: Workspace, res: PlanResult) -> dict[str, tuple[str, str]]:
    """rel -> (old_text, new_text) for every file the plan changes. Adds blockers to ``res`` for anything that cannot be generated safely."""
    out: dict[str, tuple[str, str]] = {}
    by_file: dict[str, list[Any]] = {}
    for e in dedupe(res.edits):
        by_file.setdefault(e.rel_path, []).append(e)
    for rel, edits in by_file.items():
        got = ws.read(rel)
        if got is None:
            res.add("blocker", "unreadable_file", f"{rel} cannot be read safely ({ws.unreadable.get(rel, 'unknown')}).")
            continue
        try:
            out[rel] = (got[0], apply_edits(got[0], edits))
        except EditError as exc:
            res.add("blocker", "stale_source", exc.message, exc.details)
    for rel, text in res.rewrites.items():
        got = ws.read(rel)
        if got is not None and rel not in out:
            out[rel] = (got[0], text)
    for rel, (old, new) in list(out.items()):
        if "\r" in old:
            res.add("blocker", "unsupported_line_endings", f"{rel} uses CRLF line endings; the patch engine supports LF files only.")
        elif not old.endswith("\n"):
            res.add("blocker", "no_final_newline", f"{rel} has no final newline; the patch engine supports files that end with a newline only.")
        elif old == new:
            del out[rel]
    return out


def _plan_doc(t_id: str, tdef: registry.RefactorType, res: PlanResult, ws: Workspace, snap: Any, texts: dict[str, tuple[str, str]], files: list[dict[str, Any]],
              impact_doc: dict[str, Any]) -> dict[str, Any]:
    edits: list[dict[str, Any]] = []
    for e in res.edits:
        got = ws.read(e.rel_path)
        d = edit_dict(e, got[0].split("\n") if got else [])
        d["precondition"] = {"file_sha256": got[1] if got else None, "text_at_position": e.old}
        d["postcondition"] = {"text_at_position": e.new}
        edits.append(d)
    edits += res.block_edits
    conflicts = [f.as_dict() for f in find_conflicts(res.edits)]
    ack = sorted({f.code for f in res.findings if f.code in ACK_CODES and f.severity in ("warning", "info")} & ACK_CODES)
    return {"version": PLAN_VERSION, "id": t_id, "type": tdef.id, "support": tdef.support,
            "snapshot": {"id": snap.id if snap else None, "index_state": ws.index_state, "complete": res.complete, "notes": ws.index_notes},
            "target": res.resolution, "files": files, "edits": edits[: ws.settings.refactor_max_edits], "edit_count": len(edits),
            "findings": [f.as_dict() for f in res.findings], "conflicts": conflicts, "side_effects": res.side_effects, "limitations": res.limitations,
            "acknowledgements_required": ack,
            "candidate_edits": [edit_dict(e, (ws.read(e.rel_path) or ("", ""))[0].split("\n")) for e in res.candidate_edits[:200]],
            "preconditions": [f"{f['path']} has sha256 {f['base_sha256'][:12]}…" for f in files if f.get("base_sha256")],
            "postconditions": ["Every changed file parses", "No new undefined names", "Imported module set unchanged unless stated"],
            "verification_strategy": {"tests": "Impact-selected discovered tests run through the controlled runner before (baseline) and after the change",
                                      "proves": "The selected tests passed after the change and no failure is new relative to the baseline",
                                      "does_not_prove": "Behavioral equivalence, absence of dynamic/reflective users, or correctness of untested paths"},
            "extra": res.extra, "impact_ref": bool(impact_doc)}


def _create(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, tdef: registry.RefactorType, title: str, params: dict[str, Any],
            res: PlanResult, ws: Workspace, snap: Any, fresh: Any) -> RefactorTransformation:
    analysis = tdef.mode == "analysis"
    _index_findings(res, fresh, snap, transform=not analysis)
    if not analysis:
        res.findings.extend(find_conflicts(res.edits))
        if res.blockers:
            res.candidate_edits = res.candidate_edits or list(res.edits)
    t = RefactorTransformation(repository_id=repo.id, type_id=tdef.id, title=scrub(title, 300), support_level=tdef.support, index_snapshot_id=snap.id if snap else None,
                               params=params, created_by=ctx.user.id)
    db.add(t)
    db.flush()
    texts: dict[str, tuple[str, str]] = {}
    files: list[dict[str, Any]] = []
    diff = ""
    if not analysis and tdef.support == "applicable" and not res.blockers:
        texts = _new_texts(ws, res)
        if not res.blockers and not texts:
            res.add("blocker", "no_changes", "The plan produces no change.")
        if not res.blockers:
            diff = "".join(patchlib.make_diff(rel, old, new) for rel, (old, new) in sorted(texts.items()))
            try:
                fps = patchlib.parse(diff, settings.repair_max_patch_bytes, settings.refactor_max_files)
                for fp in fps:
                    rel = fp.path
                    files.append({"path": rel, "change_type": "modify", "base_sha256": (ws.read(rel) or ("", None))[1],
                                  "result_sha256": patchlib.sha256_text(texts[rel][1]), "added": fp.added, "removed": fp.removed})
            except patchlib.PatchError as exc:
                res.add("blocker", exc.code, exc.message)
                diff, files = "", []
    changed = {rel: (ws.indexed.get(rel, ("", ""))[0], old, new) for rel, (old, new) in texts.items()}
    impact_doc: dict[str, Any] = {}
    if changed and not res.blockers:
        try:
            impact_doc = impact.build(db, settings, repo, snap.id if snap else None, changed, res.complete, " ".join(ws.index_notes))
        except Exception:  # noqa: BLE001 - impact is advisory; its absence is recorded, not fatal
            log.warning("refactor_impact_failed", extra={"repo": repo.id})
            impact_doc = {"error": "Impact analysis could not be computed.", "index_complete": res.complete}
    t.target = res.resolution
    t.plan = _plan_doc(t.id, tdef, res, ws, snap, texts, files, impact_doc)
    t.impact = impact_doc
    if res.blockers:
        move(t, "blocked")
    elif analysis or tdef.support != "applicable":
        move(t, "analyzed")
    else:
        t.diff_text, t.patch_sha256 = diff, hashlib.sha256(diff.encode("utf-8")).hexdigest()
        t.files = files
        t.originals = {rel: old for rel, (old, _) in texts.items()}
        move(t, "generated")
    _audit(db, ctx, repo, "refactor.created", t, status=t.status, files=len(files), blockers=len(res.blockers))
    db.commit()
    return t


def _need_type(type_id: str, allowed: tuple[str, ...]) -> registry.RefactorType:
    tdef = registry.get(type_id)
    if tdef is None:
        raise RefactorInvalid("unknown_type", f"Unknown refactoring type {type_id!r}.")
    if tdef.support == "unsupported":
        raise RefactorInvalid("unsupported_type", f"{tdef.title} is not supported: {tdef.reason}")
    if type_id not in allowed:
        raise RefactorInvalid("wrong_endpoint", f"{type_id} cannot be requested here.")
    return tdef


def request_rename(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, *, type_id: str, rel_path: str, name: str, new_name: str,
                   function: str | None = None, class_name: str | None = None, kind: str | None = None) -> RefactorTransformation:
    ctx.require_write()
    tdef = _need_type(type_id, ("python.rename_symbol", "python.rename_local", "python.rename_method", "js.rename_symbol"))
    ws, snap, fresh = build_workspace(db, settings, repo)
    lang = ws.indexed.get(rel_path, ("", ""))[0]
    params = {"rel_path": rel_path, "name": name, "new_name": new_name, "function": function, "class_name": class_name, "kind": kind}
    if lang and lang not in tdef.languages:
        res = PlanResult()
        res.add("blocker", "language_mismatch", f"{rel_path} is a {lang} file but {type_id} handles {', '.join(tdef.languages)}.")
    elif type_id == "python.rename_symbol":
        res = rename_py.plan_symbol_rename(ws, rel_path, name, new_name, kind)
    elif type_id == "python.rename_local":
        if not function:
            raise RefactorInvalid("function_required", "Renaming a local needs the enclosing function name.")
        res = rename_py.plan_local_rename(ws, rel_path, function, name, new_name)
    elif type_id == "python.rename_method":
        if not class_name:
            raise RefactorInvalid("class_required", "Renaming a method needs the class name.")
        res = rename_py.plan_method_rename(ws, rel_path, class_name, name, new_name)
    else:
        res = rename_js.plan_symbol_rename(ws, rel_path, name, new_name, lang or "javascript", _importer_rows(db, snap, ws))
    scope = f" in {function}" if function else (f" of {class_name}" if class_name else "")
    return _create(db, ctx, settings, repo, tdef, f"Rename {name}{scope} to {new_name} ({rel_path})", params, res, ws, snap, fresh)


def _importer_rows(db: Session, snap: Any, ws: Workspace):  # noqa: ANN202
    from app.models import FileImport

    def rows(target_rel: str) -> list[dict[str, Any]]:
        if snap is None:
            return []
        target = db.scalar(select(IndexedFile.id).where(IndexedFile.snapshot_id == snap.id, IndexedFile.rel_path == target_rel))
        if target is None:
            return []
        q = db.execute(select(IndexedFile.rel_path, FileImport.kind, FileImport.line, FileImport.names)
                       .join(IndexedFile, IndexedFile.id == FileImport.file_id)
                       .where(FileImport.snapshot_id == snap.id, FileImport.resolved_file_id == target).order_by(IndexedFile.rel_path, FileImport.line))
        return [{"rel_path": r, "kind": k, "line": ln, "names": n or []} for r, k, ln, n in q]

    return rows


def request_extract(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, *, rel_path: str, start_line: int, end_line: int,
                    new_name: str) -> RefactorTransformation:
    ctx.require_write()
    tdef = _need_type("python.extract_function", ("python.extract_function",))
    ws, snap, fresh = build_workspace(db, settings, repo)
    res = extract.plan_extract(ws, rel_path, start_line, end_line, new_name)
    params = {"rel_path": rel_path, "start_line": start_line, "end_line": end_line, "new_name": new_name}
    t = _create(db, ctx, settings, repo, tdef, f"Extract {rel_path}:{start_line}-{end_line} into {new_name}()", params, res, ws, snap, fresh)
    if t.status == "blocked":
        t.support_level = "proposal_only"  # the region is analysed but cannot be applied automatically
        db.commit()
    return t


def create_duplicates(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, options: duplicates.DupOptions) -> RefactorTransformation:
    ctx.require_write()
    tdef = _need_type("analysis.duplicates", ("analysis.duplicates",))
    ws, snap, fresh = build_workspace(db, settings, repo)
    res = PlanResult()
    res.resolution = {"scope": options.path_prefix or "repository"}
    res.extra = duplicates.analyze(ws, options)
    res.limitations = res.extra["limitations"]
    t = _create(db, ctx, settings, repo, tdef, f"Duplicate analysis ({res.extra['total_groups']} group(s))", {"filters": res.extra["filters"]}, res, ws, snap, fresh)
    return t


def create_simplify(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, rel_path: str) -> RefactorTransformation:
    ctx.require_write()
    tdef = _need_type("python.simplify_conditionals", ("python.simplify_conditionals",))
    ws, snap, fresh = build_workspace(db, settings, repo)
    res = simplify.plan_simplify(ws, rel_path)
    return _create(db, ctx, settings, repo, tdef, f"Conditional simplification suggestions for {rel_path}", {"rel_path": rel_path}, res, ws, snap, fresh)


def create_cycle_suggestions(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, path_prefix: str) -> RefactorTransformation:
    ctx.require_write()
    tdef = _need_type("architecture.cycle_suggestions", ("architecture.cycle_suggestions",))
    ws, snap, fresh = build_workspace(db, settings, repo)
    res = PlanResult()
    if snap is not None:
        res.extra = cycles_mod.suggest(db, snap.id, path_prefix)
        res.limitations = res.extra["limitations"]
    return _create(db, ctx, settings, repo, tdef, "Import-cycle suggestions", {"path_prefix": path_prefix}, res, ws, snap, fresh)


# ------------------------------------------------------------------------------------------------ validation
def _syntax_errors(results: list[patchlib.FileResult], ws: Workspace) -> list[dict[str, str]]:
    errs: list[dict[str, str]] = []
    for r in results:
        lang = ws.indexed.get(r.path, ("", ""))[0]
        if lang == "python":
            try:
                ast.parse(r.result)
            except SyntaxError as exc:
                errs.append({"code": "syntax_error", "message": f"{r.path} would not parse (line {exc.lineno})."})
        elif lang in ("javascript", "jsx", "typescript", "tsx"):
            parser = parser_for(lang)
            if parser is None:
                continue
            after = parser.parse(r.result.encode("utf-8")).root_node.has_error  # type: ignore[attr-defined]
            before = parser.parse((r.original or "").encode("utf-8")).root_node.has_error  # type: ignore[attr-defined]
            if after and not before:
                errs.append({"code": "syntax_error", "message": f"{r.path} would contain syntax errors after the change."})
    return errs


def validate(db: Session, ctx: RequestContext | None, settings: Settings, repo: Repository, t: RefactorTransformation) -> RefactorTransformation:
    """Non-destructive validation against the files as they are now. Moves the transformation to ``validated`` or ``invalid``."""
    if t.status in ("applying", *APPLIED, "rejected", "closed", "blocked", "analyzed"):
        raise RefactorConflict("invalid_state_transition", f"A transformation that is '{t.status}' cannot be validated.")
    _integrity(t)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    tmp_base = Path(settings.artifact_dir) / "refactor-tmp"
    bases = {f["path"]: f["base_sha256"] for f in t.files}
    now = utcnow()
    checks: list[dict[str, str]] = []

    def chk(cid: str, status: str, detail: str) -> None:
        checks.append({"id": cid, "status": status, "detail": detail})

    try:
        results = patchlib.validate_isolated(root, settings, tmp_base, t.diff_text, set(bases), bases, settings.refactor_max_files)
    except patchlib.PatchError as exc:
        chk("source_unchanged", "fail" if exc.code == "stale" else "pass", exc.message if exc.code == "stale" else "Sources match the plan.")
        chk("patch_applies", "fail", exc.message)
        t.validation = {"ok": False, "checked_at": now.isoformat(), "errors": [{"code": exc.code, "message": exc.message, "details": exc.details[:5]}], "warnings": [],
                        "stale": exc.code == "stale", "working_tree_modified": False, "checks": checks}
        t.validated_at = now
        if t.status != "invalid":
            move(t, "invalid")
        if ctx:
            _audit(db, ctx, repo, "refactor.validated", t, ok=False, code=exc.code)
        db.commit()
        return t
    except SyntaxError as exc:
        t.validation = {"ok": False, "checked_at": now.isoformat(), "errors": [{"code": "syntax_error", "message": f"The result would not compile (line {exc.lineno})."}],
                        "warnings": [], "stale": False, "working_tree_modified": False, "checks": [{"id": "syntax", "status": "fail", "detail": "Compilation failed."}]}
        t.validated_at = now
        if t.status != "invalid":
            move(t, "invalid")
        db.commit()
        return t
    ws = Workspace(root, settings, {r.path: _lang_of(r.path) for r in results})
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    chk("source_unchanged", "pass", "Every file still has the content the plan was built from.")
    chk("paths_inside_repository", "pass", "Paths are repository-relative, allow-listed and contain no symlinks.")
    chk("patch_applies", "pass", "The diff applies cleanly in an isolated copy; the working tree was not modified.")
    syn = _syntax_errors(results, ws)
    errors += syn
    chk("syntax", "fail" if syn else "pass", syn[0]["message"] if syn else "Every changed file parses.")
    new_undefined: list[str] = []
    for r in results:
        if r.path.endswith(".py"):
            a, b = pyscope.analyze(r.path, r.original or ""), pyscope.analyze(r.path, r.result)
            if a is not None and b is not None:
                new_undefined += sorted(f"{r.path}:{n}" for n in pyscope.undefined_names(b) - pyscope.undefined_names(a))
    if new_undefined:
        errors.append({"code": "undefined_names_introduced", "message": f"The change would leave undefined name(s): {', '.join(new_undefined[:5])}."})
    chk("no_new_undefined_names", "fail" if new_undefined else "pass", "No Python name becomes undefined." if not new_undefined else ", ".join(new_undefined[:5]))
    known = impact.module_basenames(db.scalars(select(IndexedFile.rel_path).where(IndexedFile.snapshot_id == t.index_snapshot_id)) if t.index_snapshot_id else ws.indexed)
    edge_changes = sorted(r.path for r in results if impact.module_specs(r.original or "", _lang_of(r.path)[0], known) != impact.module_specs(r.result, _lang_of(r.path)[0], known))
    chk("dependency_edges", "pass" if not edge_changes else "info",
        "The set of imported modules is unchanged in every file." if not edge_changes else f"Import statements changed in {', '.join(edge_changes[:5])}.")
    if edge_changes:
        warnings.append("Import statements changed; the dependency graph may differ from the plan's impact analysis.")
    static = repair_service.static_check(root, results)
    if static["new_findings"]:
        warnings.append(f"The change would introduce {len(static['new_findings'])} new static-analysis finding(s).")
    chk("static_analysis", "pass" if static["available"] and not static["new_findings"] else ("unavailable" if not static["available"] else "warn"),
        f"{len(static['new_findings'])} new, {static['resolved_findings']} resolved finding(s)." if static["available"] else static.get("note", "Unavailable."))
    for cid, label in (("formatting", "Formatter"), ("lint", "Linter"), ("type_check", "Type checker")):
        chk(cid, "unavailable", f"{label} is not configured for this repository; not run.")
    for w in [w for r in results for w in r.warnings]:
        warnings.append(w)
    for f in t.files:
        r = next(x for x in results if x.path == f["path"])
        f["result_sha256"] = r.result_sha256
    t.files = [dict(f) for f in t.files]
    ok = not errors
    t.validation = {"ok": ok, "checked_at": now.isoformat(), "errors": errors, "warnings": warnings, "stale": False, "working_tree_modified": False, "checks": checks,
                    "static_check": static, "files": [{"path": r.path, "change_type": r.change_type, "added": r.added, "removed": r.removed} for r in results],
                    "not_checked": ["Behavior of untested code paths", "Dynamic or reflective references", "Formatting, lint and type rules (not configured)"]}
    t.validated_at = now
    if ok:
        if t.status in ("generated", "invalid", "validated", "awaiting_approval"):
            move(t, "validated")
    elif t.status != "invalid":
        move(t, "invalid")
    if ctx:
        _audit(db, ctx, repo, "refactor.validated", t, ok=ok, files=len(results))
    db.commit()
    return t


def _lang_of(rel: str) -> tuple[str, str]:
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    return ({"py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx"}.get(ext, ext), "")


# ------------------------------------------------------------------------------------------------ approval
def submit(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation) -> RefactorTransformation:
    ctx.require_write()
    if t.status == "awaiting_approval":
        raise RefactorConflict("already_submitted", "The transformation is already awaiting approval.")
    if t.status != "validated":
        raise RefactorConflict("invalid_state_transition", f"Only a validated transformation can be submitted for approval (this one is '{t.status}').")
    validate(db, ctx, settings, repo, t)
    if t.status != "validated":
        raise RefactorConflict("stale_source" if t.validation.get("stale") else "validation_failed", "It no longer validates; it was not submitted.", t.validation.get("errors"))
    move(t, "awaiting_approval")
    t.submitted_at = utcnow()
    _audit(db, ctx, repo, "refactor.submitted", t)
    db.commit()
    return t


def approve(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation, acknowledged: list[str]) -> RefactorTransformation:
    ctx.require_write()
    if t.status == "approved":
        raise RefactorConflict("already_approved", "The transformation is already approved.")
    if t.status != "awaiting_approval":
        raise RefactorConflict("invalid_state_transition", f"Only a transformation awaiting approval can be approved (this one is '{t.status}').")
    required = set(t.plan.get("acknowledgements_required", []))
    missing = sorted(required - set(acknowledged))
    if missing:
        raise RefactorInvalid("acknowledgement_required", "Review and acknowledge these warnings before approving: " + ", ".join(missing),
                              [{"field": "acknowledged_warnings", "message": m} for m in missing])
    validate(db, ctx, settings, repo, t)
    if t.status != "validated":
        raise RefactorConflict("stale_source" if t.validation.get("stale") else "validation_failed",
                               "It no longer validates against the current files, so it was not approved. Revalidate and resubmit.", t.validation.get("errors"))
    t.status = "awaiting_approval"
    move(t, "approved")
    t.approved_by, t.approved_at = ctx.user.id, utcnow()
    _audit(db, ctx, repo, "refactor.approved", t, sha256=(t.patch_sha256 or "")[:16], acknowledged=sorted(set(acknowledged) & required))
    db.commit()
    return t


def reject(db: Session, ctx: RequestContext, repo: Repository, t: RefactorTransformation, reason: str) -> RefactorTransformation:
    ctx.require_write()
    if len(reason.strip()) < 5:
        raise RefactorInvalid("reason_required", "A rejection needs a reason of at least 5 characters.")
    if t.status == "rejected":
        raise RefactorConflict("already_rejected", "The transformation is already rejected.")
    move(t, "rejected")
    t.rejected_by, t.rejected_at, t.reject_reason = ctx.user.id, utcnow(), scrub(reason.strip(), 500)
    _audit(db, ctx, repo, "refactor.rejected", t, reason=t.reject_reason)
    db.commit()
    return t


def close(db: Session, ctx: RequestContext, repo: Repository, t: RefactorTransformation) -> RefactorTransformation:
    ctx.require_write()
    move(t, "closed")
    _audit(db, ctx, repo, "refactor.closed", t)
    db.commit()
    return t


# ------------------------------------------------------------------------------------------------ apply
def recovery(t: RefactorTransformation, results: list[patchlib.FileResult] | None = None) -> dict[str, Any]:
    return {"originals_retained": True,
            "files": [{"path": f["path"], "before_sha256": f["base_sha256"], "after_sha256": f["result_sha256"]} for f in t.files],
            "reverse_diff": patchlib.reverse_diff(results)[:20000] if results else "",
            "instructions": ["NOVA did not commit anything. To undo this refactoring, restore the original content of the files listed above.",
                             "If the files are tracked by Git: review `git diff`, then `git checkout -- <path>` restores a file (this discards other uncommitted edits to it).",
                             "Without Git: apply the reverse diff shown here (`patch -R -p1`).",
                             "The original text of each changed file is retained by NOVA with this refactoring for audit."]}


def apply(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation, confirm: bool) -> RefactorTransformation:
    ctx.require_write()
    if not confirm:
        raise RefactorInvalid("confirmation_required", "Applying a refactoring changes files in the repository. Send confirm=true to proceed.")
    if t.status in APPLIED:
        raise RefactorConflict("already_applied", "This refactoring has already been applied.")
    if t.status == "applying":
        raise RefactorConflict("apply_in_progress", "This refactoring is being applied by another request.")
    if t.status != "approved":
        raise RefactorConflict("approval_required", f"A refactoring must be approved before it can be applied (this one is '{t.status}').")
    _integrity(t)
    if t.baseline_execution_run_id:
        base = db.get(TestExecutionRun, t.baseline_execution_run_id)
        if base is not None and base.status in ("pending", "running"):
            raise RefactorConflict("baseline_running", "The baseline test run is still in progress; applying now would contaminate it. Wait for it to finish.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    claimed = db.execute(update(RefactorTransformation).where(RefactorTransformation.id == t.id, RefactorTransformation.status == "approved")
                         .values(status="applying")).rowcount
    db.commit()
    db.refresh(t)
    if claimed != 1:
        raise RefactorConflict("apply_in_progress", "This refactoring is being applied by another request, or is no longer approved.")
    lock = repair_service._repo_lock(repo.id)
    if not lock.acquire(timeout=30):
        t.status = "approved"
        db.commit()
        raise RefactorConflict("apply_in_progress", "Another change is being applied in this repository. Try again shortly.")
    results: list[patchlib.FileResult] = []
    try:
        bases = {f["path"]: f["base_sha256"] for f in t.files}
        results = patchlib.build(root, settings, t.diff_text, set(bases), bases, settings.refactor_max_files)
        for f in t.files:
            expected = f.get("result_sha256")
            r = next(x for x in results if x.path == f["path"])
            if expected and r.result_sha256 != expected:
                raise patchlib.PatchError("patch_tampered", f"The result for {f['path']!r} differs from what was validated.")
        patchlib.write_results(root, settings, results)
    except patchlib.PatchError as exc:
        t.status = "invalid" if exc.code in ("stale", "conflict", "result_invalid", "patch_tampered") else "approved"
        t.validation = {**t.validation, "ok": False, "stale": exc.code == "stale", "errors": [{"code": exc.code, "message": exc.message, "details": exc.details[:5]}],
                        "checked_at": utcnow().isoformat()}
        t.apply_result = {"ok": False, "error": exc.code, "message": exc.message, "files_changed": False}
        _audit(db, ctx, repo, "refactor.apply_refused", t, code=exc.code)
        db.commit()
        if exc.code == "patch_tampered":
            raise RefactorConflict("patch_tampered", exc.message) from exc
        raise from_patch_error(exc) from exc
    except Exception as exc:
        log.warning("refactor_apply_failed", extra={"transformation_id": t.id, "error": type(exc).__name__})
        patchlib.restore(root, settings, results)
        t.status = "approved"
        t.apply_result = {"ok": False, "error": "apply_failed", "message": "Writing failed; any files already written were restored.", "files_changed": False}
        _audit(db, ctx, repo, "refactor.apply_failed", t, error=type(exc).__name__)
        db.commit()
        raise RefactorConflict("apply_failed", "Writing the files failed and the original content was restored. Nothing was changed.") from exc
    finally:
        lock.release()
    t.status = "applied"
    t.applied_by, t.applied_at = ctx.user.id, utcnow()
    t.apply_result = {"ok": True, "files_changed": True, **recovery(t, results)}
    _audit(db, ctx, repo, "refactor.applied", t, sha256=(t.patch_sha256 or "")[:16],
           files=[{"path": f["path"], "before": (f["base_sha256"] or "")[:12], "after": (f["result_sha256"] or "")[:12]} for f in t.files])
    db.commit()
    return t


def schedule_reindex(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation) -> str | None:
    """Create the incremental indexing run that refreshes symbols and imports after an apply. The caller starts it in the background."""
    try:
        snap = repo_svc.begin_indexing(db, ctx, settings, repo, "incremental")
    except Exception as exc:  # noqa: BLE001 - e.g. an indexing run is already in progress
        t.apply_result = {**t.apply_result, "reindex": {"scheduled": False, "reason": scrub(getattr(exc, "message", str(exc)), 200)}}
        db.commit()
        return None
    t.apply_result = {**t.apply_result, "reindex": {"scheduled": True, "snapshot_id": snap.id}}
    db.commit()
    return snap.id


def rollback_guidance(db: Session, settings: Settings, repo: Repository, t: RefactorTransformation) -> dict[str, Any]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    results: list[patchlib.FileResult] = []
    for f in t.files:
        original = t.originals.get(f["path"])
        try:
            current = patchlib.read_text_file(patchlib.check_path(root, f["path"], settings), f["path"], settings)[0]
        except patchlib.PatchError:
            current = None
        if current is not None and original is not None:
            results.append(patchlib.FileResult(f["path"], "modify", f["base_sha256"], f["result_sha256"] or "", f["added"], f["removed"], original, current))
    out = recovery(t, results)
    out["applied"] = t.status in APPLIED
    out["current_matches_refactored_result"] = all(patchlib.sha256_text(r.result) == next(f for f in t.files if f["path"] == r.path)["result_sha256"] for r in results) if results else None
    return out


# ------------------------------------------------------------------------------------------------ baseline
def start_baseline(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, t: RefactorTransformation, test_ids: list[str] | None,
                   coverage: bool, confirm: bool) -> TestExecutionRun:
    from app.services.testing import execution

    ctx.require_write()
    if not confirm:
        raise RefactorInvalid("confirmation_required", "A baseline run executes the repository's tests. Send confirm=true to start it.")
    if t.status not in ("validated", "awaiting_approval", "approved"):
        raise RefactorConflict("baseline_not_allowed", f"A baseline must be recorded before the change is applied (this one is '{t.status}').")
    paths = [f["path"] for f in t.files]
    sel = selection.select_tests(db, repo, None, paths, test_ids, None)
    targets = [x["target"] for x in sel["targets"]]
    if not targets:
        raise RefactorConflict("no_tests_selected", "No discovered test is linked to the changed files. " + " ".join(sel["notes"]))
    run = execution.begin_existing(db, ctx, settings, repo, coverage, targets)
    t.baseline_execution_run_id = run.id
    t.baseline_note = "Explicit baseline recorded before the change was applied."
    _audit(db, ctx, repo, "refactor.baseline_started", t, execution_run_id=run.id, targets=len(targets))
    db.commit()
    return run
