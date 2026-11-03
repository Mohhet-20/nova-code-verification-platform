"""Proposal-only suggestions for breaking import cycles, derived from the published import graph (nothing is edited)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FileImport
from app.services.intelligence import graph, graph_queries

MAX_CYCLES = 20
MAX_SIZE = 12


def suggest(db: Session, snapshot_id: str, path_prefix: str = "") -> dict[str, Any]:
    nodes, edges, capped = graph_queries.file_edges(db, snapshot_id)
    cycles = graph.find_cycles(nodes, edges)
    ids = [i for c in cycles for i in c]
    paths = graph_queries.file_paths(db, ids) if ids else {}
    out: list[dict[str, Any]] = []
    for comp in cycles:
        files = sorted(paths.get(i, i) for i in comp)
        if path_prefix and not any(f.startswith(path_prefix) for f in files):
            continue
        members = set(comp)
        names_by_edge: dict[tuple[str, str], list[str]] = defaultdict(list)
        rows = db.execute(select(FileImport.file_id, FileImport.resolved_file_id, FileImport.names, FileImport.line, FileImport.type_only)
                          .where(FileImport.snapshot_id == snapshot_id, FileImport.file_id.in_(members), FileImport.resolved_file_id.in_(members))).all()
        lines: dict[tuple[str, str], int] = {}
        type_only: dict[tuple[str, str], bool] = {}
        for a, b, names, line, to in rows:
            if a == b:
                continue
            key = (a, b)
            names_by_edge[key] += [n.get("name") or "*" for n in (names or [])] or ["(module)"]
            lines.setdefault(key, line)
            type_only[key] = type_only.get(key, True) and bool(to)
        if not names_by_edge:
            continue
        weakest = min(names_by_edge, key=lambda k: (len(set(names_by_edge[k])), paths.get(k[0], ""), paths.get(k[1], "")))
        a, b = weakest
        used = sorted(set(names_by_edge[weakest]))
        strategies = [{"id": "move_shared_symbols", "summary": f"Move {', '.join(used[:6])} from {paths.get(b, b)} into a new module that both files import.",
                       "edge": {"from": paths.get(a, a), "to": paths.get(b, b), "line": lines.get(weakest)}},
                      {"id": "defer_import", "summary": f"Import inside the function that needs it in {paths.get(a, a)} (breaks the load-time cycle but keeps the dependency)."},
                      {"id": "dependency_inversion", "summary": "Depend on an interface/protocol defined in a lower-level module instead of the concrete module."}]
        if type_only.get(weakest):
            strategies.insert(0, {"id": "type_only_import", "summary": "The weakest edge is type-only; guard it with TYPE_CHECKING / `import type`."})
        out.append({"id": graph.cycle_id(files), "files": files[:MAX_SIZE], "size": len(comp), "truncated_files": len(files) > MAX_SIZE,
                    "weakest_edge": {"from": paths.get(a, a), "to": paths.get(b, b), "imported_names": used[:20], "count": len(used)},
                    "strategies": strategies})
    out.sort(key=lambda c: (-c["size"], c["id"]))
    return {"cycles": out[:MAX_CYCLES], "total": len(out), "truncated": len(out) > MAX_CYCLES, "edges_capped": capped,
            "limitations": ["Cycles come from resolved file-level imports in the published index; a cycle may be harmless at runtime.",
                            "Suggestions are heuristics (weakest edge = fewest imported names). No edit is generated."]}
