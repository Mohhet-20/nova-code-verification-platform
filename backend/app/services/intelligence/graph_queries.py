"""Database-backed graph queries. Every traversal expands one whole frontier per query (no N+1) and is bounded."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import IndexedFile, Relationship
from app.services.intelligence import graph

LINK_STATUSES = ("resolved", "ambiguous")
SYMBOL_EDGE_TYPES = ("CALLS", "EXTENDS_SYMBOL")
MAX_EDGE_ROWS = 500_000  # hard ceiling when loading the whole file graph for cycle analysis
MAX_TRAVERSAL_NODES = 5_000
CHUNK = 400


def chunks(items: list[str], n: int = CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def file_expander(db: Session, snapshot_id: str, direction: str) -> Callable[[list[str]], list[graph.Edge]]:
    """Edges between files through resolved IMPORTS_FILE relationships. direction: out (dependencies) | in (dependents)."""
    out = direction == "out"

    def expand(frontier: list[str]) -> list[graph.Edge]:
        edges: list[graph.Edge] = []
        for part in chunks(frontier):
            col = Relationship.source_file_id if out else Relationship.target_file_id
            rows = db.execute(select(Relationship.source_file_id, Relationship.target_file_id, Relationship.status,
                                     Relationship.evidence, Relationship.line, Relationship.type)
                              .where(Relationship.snapshot_id == snapshot_id, Relationship.type == "IMPORTS_FILE",
                                     Relationship.status.in_(LINK_STATUSES), Relationship.target_file_id.is_not(None),
                                     Relationship.source_file_id != Relationship.target_file_id, col.in_(part)))
            for s, t, st, ev, ln, ty in rows:
                if s is None or t is None:
                    continue
                meta: dict[str, Any] = dict(type=ty, status=st, evidence=ev, line=ln)
                edges.append((s, t, meta) if out else (t, s, meta))
        return edges

    return expand


def symbol_reverse_expander(db: Session, snapshot_id: str) -> Callable[[list[str]], list[graph.Edge]]:
    """Who calls / extends the frontier symbols. Module-level callers become leaf nodes ``file:<id>``."""

    def expand(frontier: list[str]) -> list[graph.Edge]:
        edges: list[graph.Edge] = []
        syms = [n for n in frontier if not n.startswith("file:")]
        for part in chunks(syms):
            rows = db.execute(select(Relationship.source_symbol_stable_id, Relationship.target_symbol_stable_id,
                                     Relationship.source_file_id, Relationship.status, Relationship.evidence,
                                     Relationship.line, Relationship.type)
                              .where(Relationship.snapshot_id == snapshot_id,
                                     Relationship.type.in_(SYMBOL_EDGE_TYPES),
                                     Relationship.status.in_(LINK_STATUSES),
                                     Relationship.target_symbol_stable_id.in_(part)))
            for s_sym, t_sym, s_file, st, ev, ln, ty in rows:
                src = s_sym if s_sym else f"file:{s_file}"
                if t_sym is None or src == t_sym:
                    continue
                edges.append((t_sym, src, dict(type=ty, status=st, evidence=ev, line=ln, file_id=s_file)))
        return edges

    return expand


# Python's sqlite3 module releases and re-acquires the GIL for every result row. Under concurrent requests each hand-off queues behind the
# other request threads, so a query returning thousands of rows collapsed from ~1,400 to ~20 queries/s at 8 threads (measured). For the
# whole-graph loads below, SQLite therefore returns ONE row holding all ids, joined with control characters that cannot occur in hex ids.
_FIELD, _RECORD = "", ""


def _single_row(db: Session) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def file_edges(db: Session, snapshot_id: str) -> tuple[list[str], list[tuple[str, str]], bool]:
    """All file nodes and resolved import edges of a snapshot (self-edges kept: they are cycles of size 1)."""
    edge_filter = (Relationship.snapshot_id == snapshot_id, Relationship.type == "IMPORTS_FILE",
                   Relationship.status.in_(LINK_STATUSES), Relationship.target_file_id.is_not(None))
    if _single_row(db):
        ids = db.scalar(select(func.group_concat(IndexedFile.id, _RECORD)).where(IndexedFile.snapshot_id == snapshot_id))
        nodes = ids.split(_RECORD) if ids else []
        count, joined = db.execute(select(func.count(), func.group_concat(Relationship.source_file_id + _FIELD + Relationship.target_file_id, _RECORD))
                                   .where(*edge_filter)).one()
        if count <= MAX_EDGE_ROWS:
            pairs = [rec.split(_FIELD) for rec in joined.split(_RECORD)] if joined else []
            return nodes, [(a, b) for a, b in pairs], False
    else:
        nodes = list(db.scalars(select(IndexedFile.id).where(IndexedFile.snapshot_id == snapshot_id)))
    rows = db.execute(select(Relationship.source_file_id, Relationship.target_file_id).where(*edge_filter).limit(MAX_EDGE_ROWS + 1)).all()
    capped = len(rows) > MAX_EDGE_ROWS
    return nodes, [(a, b) for a, b in rows[:MAX_EDGE_ROWS] if b is not None], capped


def file_paths(db: Session, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in chunks(sorted(set(ids))):
        out.update(db.execute(select(IndexedFile.id, IndexedFile.rel_path).where(IndexedFile.id.in_(part))).all())
    return out
