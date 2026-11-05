"""Dependency-aware impact of a planned transformation, using the existing import graph and test association.

All answers carry an explicit completeness statement: the graph reflects the last published index, so dependents added or removed
since then are invisible. Import *module* edges are compared before/after from the file contents themselves, so "no new cycles" is
proved from the edit rather than assumed.
"""
from __future__ import annotations

import ast
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import IndexedFile, Repository, Symbol
from app.services.intelligence import graph, graph_queries
from app.services.refactor.common import parser_for
from app.services.review import impact as review_impact

ENTRY_NAMES = {"__init__.py", "__main__.py", "main.py", "app.py", "cli.py", "manage.py", "index.js", "index.ts", "index.tsx", "index.jsx"}
MAX_DEPTH = 4
MAX_NODES = 300


def module_basenames(rel_paths: Any) -> set[str]:
    """Names that could be submodules: file stems and package directory names of the indexed files."""
    out: set[str] = set()
    for rel in rel_paths:
        parts = rel.split("/")
        stem = parts[-1].rsplit(".", 1)[0]
        out.add(parts[-2] if stem == "__init__" and len(parts) > 1 else stem)
    return out


def module_specs(text: str, language: str, known_modules: set[str] | None = None) -> set[str]:
    """The set of modules a file imports: what determines the file-level dependency graph.

    ``from pkg import name`` also counts ``pkg.name`` when ``name`` could be a submodule (it is a known module name), because then the
    statement imports a file; for other names only the module matters, so renaming a function cannot look like a changed edge."""
    out: set[str] = set()
    if language == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return {"<unparsed>"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                out.add("." * n.level + (n.module or ""))
                out.update("." * n.level + (n.module + "." if n.module else "") + a.name for a in n.names if a.name != "*" and a.name in (known_modules or set()))
        return out
    parser = parser_for(language)
    if parser is None:
        return {"<unparsed>"}
    tree2 = parser.parse(text.encode("utf-8"))  # type: ignore[attr-defined]
    stack = [tree2.root_node]
    while stack:
        n = stack.pop()
        if n.type in ("import_statement", "export_statement"):
            src = n.child_by_field_name("source")
            if src is not None:
                out.add(src.text.decode("utf-8", errors="replace").strip("'\"`"))
        stack.extend(n.children)
    out.update(m.group(1) for m in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", text))
    return out


def build(db: Session, settings: Settings, repo: Repository, snapshot_id: str | None, changed: dict[str, tuple[str, str, str]],
          index_complete: bool, index_note: str) -> dict[str, Any]:
    """``changed``: rel_path -> (language, old_text, new_text)."""
    result: dict[str, Any] = {"changed_files": sorted(changed), "index_complete": index_complete, "snapshot_id": snapshot_id,
                              "caveats": [], "direct_dependents": [], "transitive_dependents": [], "cycles": {}, "public_entry_points": [], "affected_tests": {}}
    if not index_complete:
        result["caveats"].append(index_note or "The index is stale; dependents added or removed since it was built are not shown.")
    if snapshot_id is None:
        result["caveats"].append("No published index exists; dependents and tests are unknown.")
        result["cycles"] = {"new_cycles": None, "proof": "unavailable"}
        return result
    ids = dict(db.execute(select(IndexedFile.rel_path, IndexedFile.id).where(IndexedFile.snapshot_id == snapshot_id, IndexedFile.rel_path.in_(sorted(changed)))).all())
    id_to_path = {v: k for k, v in ids.items()}
    seeds = sorted(ids.values())
    if seeds:
        res = graph.bfs(seeds, graph_queries.file_expander(db, snapshot_id, "in"), MAX_DEPTH, MAX_NODES)
        names = graph_queries.file_paths(db, list(res.visited))
        first = graph_queries.file_expander(db, snapshot_id, "in")(seeds)  # includes dependents that are themselves changed
        direct_ids = {dst for src, dst, _ in first if src != dst}
        all_names = {**names, **graph_queries.file_paths(db, list(direct_ids))}
        direct = sorted(all_names[k] for k in direct_ids if k in all_names)
        trans = sorted(names[k] for k, v in res.visited.items() if v.depth > 1 and k in names and k not in direct_ids)
        result["direct_dependents"] = direct
        result["transitive_dependents"] = trans[:200]
        result["dependent_summary"] = {"direct": len(direct), "transitive": len(trans), "max_depth": MAX_DEPTH, "truncated": res.truncated,
                                       "more_beyond_depth": res.more_beyond_depth}
        if res.truncated or res.more_beyond_depth:
            result["caveats"].append("The transitive dependent list is bounded; more dependents may exist.")
        nodes, edges, capped = graph_queries.file_edges(db, snapshot_id)
        cycles = graph.find_cycles(nodes, edges)
        involving = []
        paths = graph_queries.file_paths(db, [i for c in cycles for i in c]) if cycles else {}
        for c in cycles:
            if set(c) & set(seeds):
                involving.append([paths.get(i, i) for i in c][:12])
        known = module_basenames(db.scalars(select(IndexedFile.rel_path).where(IndexedFile.snapshot_id == snapshot_id)))
        edge_changes = sorted(rel for rel, (lang, old, new) in changed.items() if module_specs(old, lang, known) != module_specs(new, lang, known))
        result["cycles"] = {"existing_cycles_involving_changed_files": involving[:20], "edge_set_changed_in": edge_changes,
                            "new_cycles": [] if not edge_changes and not capped else None,
                            "proof": ("The set of imported modules is identical before and after in every changed file, so the file dependency graph is unchanged and no cycle can be introduced."
                                      if not edge_changes and not capped else
                                      "Import statements changed in the files listed; cycle introduction was not evaluated." if edge_changes else "The edge list was capped; cycles are not guaranteed complete.")}
    syms = db.execute(select(Symbol.file_id, Symbol.name, Symbol.kind, Symbol.start_line).where(Symbol.snapshot_id == snapshot_id, Symbol.file_id.in_(seeds), Symbol.exported.is_(True))
                      .order_by(Symbol.file_id, Symbol.start_line).limit(200)).all() if seeds else []
    entries: list[dict[str, Any]] = []
    for fid, name, kind, line in syms:
        entries.append({"rel_path": id_to_path.get(fid, ""), "symbol": name, "kind": kind, "line": line, "reason": "exported symbol"})
    for rel in sorted(changed):
        if rel.rsplit("/", 1)[-1] in ENTRY_NAMES:
            entries.append({"rel_path": rel, "symbol": None, "kind": "file", "line": 1, "reason": "conventional entry-point or package file"})
    result["public_entry_points"] = entries[:60]
    all_paths = sorted(set(changed) | set(result["direct_dependents"]))
    result["affected_tests"] = review_impact.tests_state_for(db, repo, all_paths, set())
    result["affected_tests"]["basis"] = "static import association from the latest test discovery; not proof that a test exercises the change"
    return result
