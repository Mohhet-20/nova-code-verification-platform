"""Map changed lines to symbols. Python is parsed directly (exact new content); the symbol index supplies stable ids only when it
describes exactly the same content. Files that cannot be parsed get no symbols - nothing is guessed."""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IndexedFile, Symbol
from app.services.analysis.complexity import cyclomatic_complexity

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
Ranges = list[list[int]]


@dataclass
class PySym:
    name: str            # "Class.method" (no module prefix)
    kind: str            # function | method | class
    start: int
    end: int
    public: bool
    signature: str
    complexity: int | None
    handlers: int        # try/except/raise nodes in the body: a crude error-handling footprint
    parent: str | None


@dataclass
class ChangedSym:
    name: str
    kind: str
    change_kind: str     # added | modified | removed
    start: int | None
    end: int | None
    public: bool
    signature_old: str | None = None
    signature_new: str | None = None
    complexity: int | None = None
    handlers_old: int | None = None
    handlers_new: int | None = None
    stable_id: str | None = None
    qualified: str | None = None
    mapping: str = "fresh_parse"
    notes: list[str] = field(default_factory=list)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Names and shape only: default values and annotations may contain secrets or noise and are not needed."""
    a = node.args
    parts: list[str] = []
    pos = [*a.posonlyargs, *a.args]
    n_def = len(a.defaults)
    for i, arg in enumerate(pos):
        parts.append(arg.arg + ("=…" if i >= len(pos) - n_def else ""))
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        parts.append(arg.arg + ("=…" if d is not None else ""))
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return f"{node.name}({', '.join(parts)})"[:300]


def _footprint(node: ast.AST) -> int:
    return sum(isinstance(n, (ast.Try, ast.ExceptHandler, ast.Raise)) for n in ast.walk(node))


def parse_python(source: str) -> list[PySym] | None:
    """None when the source does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename="<review>")
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError):
        return None
    out: list[PySym] = []

    def visit(body: list[ast.stmt], prefix: str, public: bool, in_class: bool) -> None:
        for node in body:
            if isinstance(node, (*FUNCS, ast.ClassDef)):
                name = f"{prefix}{node.name}"
                start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                dunder = node.name.startswith("__") and node.name.endswith("__")
                pub = public and (not node.name.startswith("_") or dunder)
                if isinstance(node, ast.ClassDef):
                    out.append(PySym(name, "class", start, node.end_lineno or node.lineno, pub, f"class {node.name}", None, 0, prefix[:-1] or None))
                    visit(node.body, name + ".", pub, True)
                else:
                    out.append(PySym(name, "method" if in_class else "function", start, node.end_lineno or node.lineno, pub,
                                     _signature(node), cyclomatic_complexity(node), _footprint(node), prefix[:-1] or None))
                    visit(node.body, name + ".", False, False)  # nested functions are never public API

    visit(tree.body, "", True, False)
    return out


def _subtract(start: int, end: int, children: list[tuple[int, int]]) -> list[tuple[int, int]]:
    segs = [(start, end)]
    for cs, ce in sorted(children):
        nxt: list[tuple[int, int]] = []
        for s, e in segs:
            if ce < s or cs > e:
                nxt.append((s, e))
                continue
            if cs > s:
                nxt.append((s, cs - 1))
            if ce < e:
                nxt.append((ce + 1, e))
        segs = nxt
    return segs


def _hits(segs: list[tuple[int, int]], ranges: Ranges, dels: list[int]) -> bool:
    for s, e in segs:
        if any(r[0] <= e and r[1] >= s for r in ranges):
            return True
        if any(s <= p <= e - 1 for p in dels):
            return True
    return False


def _module_level_hit(segs: list[tuple[int, int]], ranges: Ranges, dels: list[int], lines: list[str]) -> bool:
    """A change between definitions only counts when a changed line holds code (not blank / comment-only)."""
    def code(n: int) -> bool:
        return 1 <= n <= len(lines) and bool(lines[n - 1].strip()) and not lines[n - 1].lstrip().startswith("#")

    for s, e in segs:
        for a, b in ranges:
            lo, hi = max(a, s), min(b, e)
            if lo <= hi and any(code(n) for n in range(lo, min(hi, lo + 5000) + 1)):
                return True
        if any(s <= p <= e - 1 and (code(p) or code(p + 1)) for p in dels):
            return True
    return False


def map_python(new_src: str | None, old_src: str | None, new_ranges: Ranges, deletion_points: list[int], added_file: bool,
               deleted_file: bool) -> tuple[list[ChangedSym], list[str]]:
    """Changed symbols for one Python file plus short notes (never source text)."""
    notes: list[str] = []
    new = parse_python(new_src) if new_src is not None else None
    old = parse_python(old_src) if old_src is not None else None
    if new_src is not None and new is None:
        notes.append("The new version has a syntax error, so no symbols were mapped.")
    if old_src is not None and old is None:
        notes.append("The previous version could not be parsed, so removed symbols and signature changes are unknown.")
    out: list[ChangedSym] = []
    old_by = {s.name: s for s in old or []}
    new_by = {s.name: s for s in new or []}
    if new is not None:
        children: dict[str | None, list[tuple[int, int]]] = {}
        for s in new:
            children.setdefault(s.parent, []).append((s.start, s.end))
        for s in new:
            prev = old_by.get(s.name)
            if added_file or (old is not None and prev is None):
                out.append(ChangedSym(s.name, s.kind, "added", s.start, s.end, s.public, None, s.signature, s.complexity, None, s.handlers))
                continue
            own = _subtract(s.start, s.end, children.get(s.name, []))
            if _hits(own, new_ranges, deletion_points):
                out.append(ChangedSym(s.name, s.kind, "modified", s.start, s.end, s.public, prev.signature if prev else None, s.signature,
                                      s.complexity, prev.handlers if prev else None, s.handlers))
        top = _subtract(1, 10**9, children.get(None, []))
        if not added_file and _module_level_hit(top, new_ranges, deletion_points, (new_src or "").splitlines()):
            out.append(ChangedSym("<module level>", "module", "modified", None, None, False))
    if old is not None and (deleted_file or new is not None):
        for name, s in old_by.items():
            if name not in new_by:
                out.append(ChangedSym(name, s.kind, "removed", None, None, s.public, s.signature, None, None, s.handlers, None))
    return out, notes


def attach_index(db: Session, snapshot_id: str | None, rel_path: str, new_sha256: str | None, syms: list[ChangedSym]) -> tuple[bool, str]:
    """Attach stable ids when the index describes exactly this content. Returns (index_current, note)."""
    if snapshot_id is None:
        return False, "no completed index"
    f = db.scalar(select(IndexedFile).where(IndexedFile.snapshot_id == snapshot_id, IndexedFile.rel_path == rel_path))
    if f is None:
        return False, "file is not in the index"
    if new_sha256 is None or f.sha256 != new_sha256:
        return False, "indexed content differs from the reviewed content"
    rows = list(db.scalars(select(Symbol).where(Symbol.snapshot_id == snapshot_id, Symbol.file_id == f.id)))
    by_key = {(r.start_line, r.name): r for r in rows}
    for s in syms:
        if s.start is None:
            continue
        simple = s.name.rsplit(".", 1)[-1]
        r = by_key.get((s.start, simple))
        if r is None:  # decorated definitions: the index may start at the def line rather than the decorator
            cands = [x for x in rows if x.name == simple and s.start <= x.start_line <= s.end]
            r = cands[0] if len(cands) == 1 else None
        if r is not None:
            s.stable_id, s.qualified, s.mapping = r.stable_id, r.qualified_name, "index"
    return True, ""


def index_only_symbols(db: Session, snapshot_id: str | None, rel_path: str, new_sha256: str | None, new_ranges: Ranges,
                       deletion_points: list[int]) -> tuple[list[ChangedSym], str | None]:
    """Non-Python files: symbols only when the index describes exactly the reviewed content."""
    if snapshot_id is None or new_sha256 is None:
        return [], "no index data for this file"
    f = db.scalar(select(IndexedFile).where(IndexedFile.snapshot_id == snapshot_id, IndexedFile.rel_path == rel_path))
    if f is None or f.sha256 != new_sha256:
        return [], "the index does not describe the reviewed content of this file"
    out: list[ChangedSym] = []
    for r in db.scalars(select(Symbol).where(Symbol.snapshot_id == snapshot_id, Symbol.file_id == f.id)):
        if r.kind in ("function", "method", "class") and _hits([(r.start_line, r.end_line)], new_ranges, deletion_points):
            out.append(ChangedSym(r.name, r.kind, "modified", r.start_line, r.end_line, bool(r.exported), None, r.signature,
                                  None, None, None, r.stable_id, r.qualified_name, "index"))
    return out, None
