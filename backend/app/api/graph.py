"""Dependency graph, change-impact, cycle, symbol-detail, index-status and metrics endpoints (read-only except rebuild)."""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.repositories import _index_job
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, UnsafePathError
from app.db.base import utcnow
from app.db.session import get_db
from app.models import FileCall, FileImport, IndexedFile, IndexSnapshot, Relationship, Repository, Symbol
from app.schemas.graph import (
    DISCLAIMER,
    CycleOut,
    CycleRef,
    CyclesPage,
    DependenciesOut,
    FileRef,
    GraphItem,
    ImpactOut,
    ImpactSummary,
    IndexStatus,
    Metrics,
    SymbolDetail,
    SymbolLink,
    UnresolvedImport,
)
from app.schemas.intelligence import IndexRef, SymbolOut
from app.schemas.repositories import SnapshotOut
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.intelligence import graph
from app.services.intelligence import graph_queries as gq
from app.services.intelligence.freshness import compute_freshness
from app.services.intelligence.types import ANALYSIS_VERSION

router = APIRouter(prefix="/repositories/{repository_id}", tags=["graph"])

SEVERITY_RULES = {
    "info": "A file imports itself.",
    "medium": "2-3 files import each other, directly or transitively.",
    "high": "4 or more files are mutually reachable through imports.",
}
CYCLE_NOTE = ("A structural dependency cycle means these files can reach each other through resolved imports. "
              "It is a coupling smell, not proof of a runtime failure; many languages tolerate import cycles.")
METRIC_DEFINITIONS = {
    "files": "Files in the published index generation (including skipped and unparsed files).",
    "symbols": "Definitions extracted by the parsers (functions, classes, methods, constants, ...).",
    "edges_by_type": "Stored relationships: IMPORTS_FILE (file imports file), DEPENDS_ON_PACKAGE (external "
                     "package), EXTENDS_SYMBOL (inheritance, shown as INHERITS), CALLS (name-resolved call).",
    "imports_by_status": "Import statements by resolution: resolved, external, unresolved, unsupported (dynamic).",
    "calls_by_status": "Observed call sites by resolution: resolved, ambiguous, unresolved, unsupported, external.",
    "cycles": "Structural dependency cycles: strongly connected file groups (plus self-imports) in the resolved "
              "import graph.",
    "max_dependency_depth": "Longest chain of file imports after collapsing each cycle into one node.",
    "parse_failures": "Files whose parser failed or was unavailable.",
    "files_awaiting_indexing": "Files whose size or modification time differs from the index, plus new and removed "
                               "files. Metadata-only estimate; null unless requested.",
}


def _ref(s: IndexSnapshot) -> IndexRef:
    return IndexRef(snapshot_id=s.id, generation=s.generation, status=s.status, finished_at=s.finished_at)


def _published(db: Session, ctx: RequestContext, repository_id: str) -> tuple[Repository, IndexSnapshot]:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        raise NotFoundError("This repository has no published index yet. Run indexing first.")
    return repo, snap


def clean_rel_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    if (not p or "\x00" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p)
            or any(seg == ".." for seg in p.split("/"))):
        raise UnsafePathError("The path must be a repository-relative path without '..' segments.")
    return p.lstrip("./") if p.startswith("./") else p


def _resolve_file(db: Session, snap: IndexSnapshot, file_id: str | None, path: str | None) -> IndexedFile:
    if bool(file_id) == bool(path):
        raise UnsafePathError("Provide exactly one of file_id or path.")
    cond = [IndexedFile.snapshot_id == snap.id]
    cond.append(IndexedFile.id == file_id if file_id else IndexedFile.rel_path == clean_rel_path(path or ""))
    f = db.scalar(select(IndexedFile).where(*cond))
    if f is None:
        raise NotFoundError("File not found in the current index.")
    return f


def _page(items: list, page: int, size: int) -> list:
    return items[(page - 1) * size: page * size]


def _pages(total: int, size: int) -> int:
    return max(1, -(-total // size))


def _cycles(db: Session, snap: IndexSnapshot) -> tuple[list[list[str]], list[tuple[str, str]], bool, list[str]]:
    nodes, edges, capped = gq.file_edges(db, snap.id)
    return graph.find_cycles(nodes, edges), edges, capped, nodes


def _file_chain(res: graph.BfsResult, node: str, root: str, paths: dict[str, str], out: bool) -> list[str]:
    seq = [paths.get(n, n) for n in res.path_to(node, [root])]
    return seq if out else list(reversed(seq))


# ---------------------------------------------------------------- dependencies

@router.get("/dependencies", response_model=DependenciesOut)
def dependencies(repository_id: str, file_id: str | None = Query(None, max_length=40),
                 path: str | None = Query(None, max_length=1000),
                 direction: str = Query("out", pattern="^(out|in)$"), depth: int = Query(1, ge=1, le=10),
                 page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> DependenciesOut:
    """Files a file depends on (direction=out) or that depend on it (direction=in), up to ``depth`` hops."""
    _, snap = _published(db, ctx, repository_id)
    root = _resolve_file(db, snap, file_id, path)
    res = graph.bfs([root.id], gq.file_expander(db, snap.id, direction), depth, gq.MAX_TRAVERSAL_NODES)
    paths = gq.file_paths(db, [root.id, *res.visited])
    out = direction == "out"
    ordered = sorted(res.visited.items(), key=lambda kv: (kv[1].depth, paths.get(kv[0], kv[0])))
    items: list[GraphItem] = []
    for node, v in _page(ordered, page, page_size):
        p = paths.get(node, node)
        parent_path = paths.get(v.parent or "", "")
        verb = "imports" if out else "is imported by"
        direct = v.depth == 1
        chain = _file_chain(res, node, root.id, paths, out)
        items.append(GraphItem(
            kind="file", id=node, name=p.rsplit("/", 1)[-1], path=p, depth=v.depth, relationship=v.edge["type"],
            status=v.edge["status"], evidence=v.edge["evidence"], line=v.edge.get("line"), chain=chain,
            explanation=(f"{root.rel_path} {verb} {p}." if direct else
                         f"{root.rel_path} reaches {p} in {v.depth} steps, last through {parent_path} {verb} {p}.")))
    unresolved: list[UnresolvedImport] = []
    unresolved_total = 0
    if out:
        cond = [FileImport.file_id == root.id, FileImport.resolution_status.in_(("unresolved", "unsupported"))]
        unresolved_total = db.scalar(select(func.count()).select_from(FileImport).where(*cond)) or 0
        for i in db.scalars(select(FileImport).where(*cond).order_by(FileImport.line, FileImport.id).limit(100)):
            unresolved.append(UnresolvedImport(
                specifier=i.specifier or "(dynamic)", kind=i.kind, status=i.resolution_status,
                note=i.resolution_note, line=i.line, package=i.package))
    note = ("Edges are resolved file-to-file imports. Unresolved and unsupported imports are listed separately and "
            "never counted as dependencies.")
    return DependenciesOut(
        root=FileRef(file_id=root.id, path=root.rel_path), direction=direction, max_depth=depth, items=items,
        total=len(ordered), page=page, page_size=page_size, pages=_pages(len(ordered), page_size),
        truncated=res.truncated, more_beyond_depth=res.more_beyond_depth, unresolved=unresolved,
        unresolved_total=unresolved_total, index=_ref(snap), note=note)


# ---------------------------------------------------------------- impact

@router.get("/dependencies/impact", response_model=ImpactOut)
def impact(repository_id: str, file_id: str | None = Query(None, max_length=40),
           path: str | None = Query(None, max_length=1000), symbol_id: str | None = Query(None, max_length=40),
           max_depth: int = Query(5, ge=1, le=10), page: int = Query(1, ge=1),
           page_size: int = Query(50, ge=1, le=200), ctx: RequestContext = Depends(get_context),
           db: Session = Depends(get_db)) -> ImpactOut:
    """What may break if a file or symbol changes: reverse dependency traversal with evidence and paths."""
    _, snap = _published(db, ctx, repository_id)
    if symbol_id and (file_id or path):
        raise UnsafePathError("Provide either symbol_id or file_id/path, not both.")
    if symbol_id:
        return _symbol_impact(db, snap, symbol_id, max_depth, page, page_size)
    root = _resolve_file(db, snap, file_id, path)
    res = graph.bfs([root.id], gq.file_expander(db, snap.id, "in"), max_depth, gq.MAX_TRAVERSAL_NODES)
    paths = gq.file_paths(db, [root.id, *res.visited])
    ordered = sorted(res.visited.items(), key=lambda kv: (kv[1].depth, paths.get(kv[0], kv[0])))
    items = []
    for node, v in _page(ordered, page, page_size):
        p = paths.get(node, node)
        via = paths.get(v.parent or "", "")
        items.append(GraphItem(
            kind="file", id=node, name=p.rsplit("/", 1)[-1], path=p, depth=v.depth, relationship=v.edge["type"],
            status=v.edge["status"], evidence=v.edge["evidence"], line=v.edge.get("line"),
            chain=_file_chain(res, node, root.id, paths, False),
            explanation=(f"{p} imports {root.rel_path} directly." if v.depth == 1 else
                         f"{p} imports {via}, which depends on {root.rel_path} ({v.depth} steps).")))
    direct = sum(1 for v in res.visited.values() if v.depth == 1)
    summary = ImpactSummary(direct=direct, transitive=len(res.visited) - direct, total=len(res.visited),
                            files_affected=len(res.visited),
                            max_depth_reached=max((v.depth for v in res.visited.values()), default=0))
    comps, _, _, _ = _cycles(db, snap)
    affected = set(res.visited) | {root.id}
    cycle_refs = []
    for c in comps:
        if affected.intersection(c):
            sev, _why = graph.cycle_severity(len(c), len(c) == 1)
            cycle_refs.append(CycleRef(id=graph.cycle_id(c), size=len(c), severity=sev,
                                       files=[paths.get(n) or gq.file_paths(db, [n]).get(n, n) for n in c[:20]]))
        if len(cycle_refs) >= 20:
            break
    return ImpactOut(
        target_kind="file", target_id=root.id, target_name=root.rel_path.rsplit("/", 1)[-1],
        target_path=root.rel_path, max_depth=max_depth, summary=summary, items=items, total=len(ordered),
        page=page, page_size=page_size, pages=_pages(len(ordered), page_size), truncated=res.truncated,
        more_beyond_depth=res.more_beyond_depth, cycles=cycle_refs, disclaimer=DISCLAIMER, index=_ref(snap))


def _symbol_impact(db: Session, snap: IndexSnapshot, symbol_id: str, max_depth: int, page: int,
                   page_size: int) -> ImpactOut:
    target = db.scalar(select(Symbol).where(Symbol.snapshot_id == snap.id, Symbol.stable_id == symbol_id))
    if target is None:
        raise NotFoundError("Symbol not found in the current index.")
    res = graph.bfs([target.stable_id], gq.symbol_reverse_expander(db, snap.id), max_depth, gq.MAX_TRAVERSAL_NODES)
    sym_ids = [n for n in res.visited if not n.startswith("file:")]
    syms: dict[str, tuple[Symbol, str]] = {}
    for part in gq.chunks(sorted({target.stable_id, *sym_ids})):
        for s, p in db.execute(select(Symbol, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Symbol.file_id)
                               .where(Symbol.snapshot_id == snap.id, Symbol.stable_id.in_(part))):
            syms[s.stable_id] = (s, p)
    file_paths = gq.file_paths(db, [n[5:] for n in res.visited if n.startswith("file:")]
                               + [v.edge["file_id"] for v in res.visited.values() if v.edge.get("file_id")])

    def label(n: str) -> str:
        if n.startswith("file:"):
            return f"{file_paths.get(n[5:], n[5:])} (module level)"
        return syms[n][0].qualified_name if n in syms else n

    ordered = sorted(res.visited.items(), key=lambda kv: (kv[1].depth, label(kv[0])))
    items = []
    for node, v in _page(ordered, page, page_size):
        chain = list(reversed([label(n) for n in res.path_to(node, [target.stable_id])]))
        if node.startswith("file:"):
            kind, name, path, nid = "module", label(node), file_paths.get(node[5:], ""), node[5:]
        else:
            found = syms.get(node)
            path = found[1] if found else ""
            kind, name, nid = (found[0].kind if found else "symbol"), (found[0].qualified_name if found else node), node
        rel = v.edge["type"]
        verb = {"CALLS": "calls", "EXTENDS_SYMBOL": "inherits from"}[rel]
        parent = label(v.parent) if v.parent else target.qualified_name
        items.append(GraphItem(
            kind=kind, id=nid, name=name, path=path, depth=v.depth, relationship=rel, status=v.edge["status"],
            evidence=v.edge["evidence"], line=v.edge.get("line"), chain=chain,
            explanation=(f"{name} {verb} {target.qualified_name}." if v.depth == 1 else
                         f"{name} {verb} {parent}, which reaches {target.qualified_name} ({v.depth} steps).")))
    direct = sum(1 for v in res.visited.values() if v.depth == 1)
    files: set[str] = set()
    for node in res.visited:
        if node.startswith("file:"):
            files.add(file_paths.get(node[5:], ""))
        elif node in syms:
            files.add(syms[node][1])
    summary = ImpactSummary(direct=direct, transitive=len(res.visited) - direct, total=len(res.visited),
                            files_affected=len({f for f in files if f}),
                            max_depth_reached=max((v.depth for v in res.visited.values()), default=0))
    tpath = syms[target.stable_id][1]
    return ImpactOut(
        target_kind="symbol", target_id=target.stable_id, target_name=target.qualified_name, target_path=tpath,
        max_depth=max_depth, summary=summary, items=items, total=len(ordered), page=page, page_size=page_size,
        pages=_pages(len(ordered), page_size), truncated=res.truncated, more_beyond_depth=res.more_beyond_depth,
        cycles=[], disclaimer=DISCLAIMER, index=_ref(snap))


# ---------------------------------------------------------------- cycles

@router.get("/dependencies/cycles", response_model=CyclesPage)
def dependency_cycles(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                      min_size: int = Query(1, ge=1, le=1000),
                      severity: str | None = Query(None, pattern="^(info|medium|high)$"),
                      ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> CyclesPage:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        return CyclesPage(items=[], total=0, page=page, page_size=page_size, pages=1,
                          severity_rules=SEVERITY_RULES, note="No published index yet.", index=None)
    comps, edges, capped, _ = _cycles(db, snap)
    sel = []
    for c in comps:
        sev, why = graph.cycle_severity(len(c), len(c) == 1)
        if len(c) >= min_size and (severity is None or sev == severity):
            sel.append((c, sev, why))
    sel.sort(key=lambda t: ({"high": 0, "medium": 1, "info": 2}[t[1]], -len(t[0]), t[0]))
    part = _page(sel, page, page_size)
    all_ids = [n for c, _, _ in part for n in c[:50]]
    paths = gq.file_paths(db, all_ids)
    items = []
    for c, sev, why in part:
        members = set(c)
        shown = c[:50]
        cyc_edges = sorted({(a, b) for a, b in edges if a in members and b in members})[:100]
        items.append(CycleOut(
            id=graph.cycle_id(c), size=len(c), severity=sev, severity_reason=why,
            files=[FileRef(file_id=n, path=paths.get(n, n)) for n in shown], files_truncated=len(c) > len(shown),
            edges=[{"from": paths.get(a, a), "to": paths.get(b, b)} for a, b in cyc_edges if a in paths and b in paths],
            explanation=("Following imports from any of these files can lead back to the same file."
                         if len(c) > 1 else "This file imports itself.")))
    note = CYCLE_NOTE + (" The edge set was capped, so some cycles may be missing." if capped else "")
    return CyclesPage(items=items, total=len(sel), page=page, page_size=page_size, pages=_pages(len(sel), page_size),
                      severity_rules=SEVERITY_RULES, note=note, index=_ref(snap))


# ---------------------------------------------------------------- symbol detail

@router.get("/symbols/{symbol_id}", response_model=SymbolDetail)
def symbol_detail(repository_id: str, symbol_id: str, ctx: RequestContext = Depends(get_context),
                  db: Session = Depends(get_db)) -> SymbolDetail:
    _, snap = _published(db, ctx, repository_id)
    row = db.execute(select(Symbol, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Symbol.file_id)
                     .where(Symbol.snapshot_id == snap.id, Symbol.stable_id == symbol_id)).first()
    if row is None:
        raise NotFoundError("Symbol not found in the current index.")
    sym, path = row
    from app.api.intelligence import _symbol_out

    kids = db.execute(select(Symbol, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Symbol.file_id)
                      .where(Symbol.snapshot_id == snap.id, Symbol.parent_stable_id == sym.stable_id)
                      .order_by(Symbol.start_line, Symbol.start_col).limit(200)).all()

    def fetch(links_where, other_col, other_file_col):  # noqa: ANN001, ANN202
        total = db.scalar(select(func.count()).select_from(Relationship).where(*links_where)) or 0
        rels = db.execute(select(Relationship.type, Relationship.status, Relationship.evidence, Relationship.line,
                                 other_col, other_file_col, Relationship.target_text)
                          .where(*links_where).order_by(Relationship.type, other_col, Relationship.id)
                          .limit(50)).all()
        ids = [r[4] for r in rels if r[4]]
        look = {}
        for s, p in db.execute(select(Symbol, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Symbol.file_id)
                               .where(Symbol.snapshot_id == snap.id, Symbol.stable_id.in_(ids))):
            look[s.stable_id] = (s, p)
        out = []
        for ty, st, ev, ln, sid, _fid, ttext in rels:
            o = look.get(sid)
            out.append(SymbolLink(stable_id=sid, name=(o[0].name if o else ttext or "(module level)"),
                                  qualified_name=o[0].qualified_name if o else None, kind=o[0].kind if o else None,
                                  path=o[1] if o else None, line=ln, relationship=ty, status=st, evidence=ev))
        return total, out

    base = [Relationship.snapshot_id == snap.id, Relationship.status.in_(gq.LINK_STATUSES)]
    caller_total, callers = fetch(
        [*base, Relationship.type == "CALLS", Relationship.target_symbol_stable_id == sym.stable_id],
        Relationship.source_symbol_stable_id, Relationship.source_file_id)
    callee_total, callees = fetch(
        [*base, Relationship.type == "CALLS", Relationship.source_symbol_stable_id == sym.stable_id],
        Relationship.target_symbol_stable_id, Relationship.target_file_id)
    _, base_links = fetch(
        [*base, Relationship.type == "EXTENDS_SYMBOL", Relationship.source_symbol_stable_id == sym.stable_id],
        Relationship.target_symbol_stable_id, Relationship.target_file_id)
    _, subs = fetch(
        [*base, Relationship.type == "EXTENDS_SYMBOL", Relationship.target_symbol_stable_id == sym.stable_id],
        Relationship.source_symbol_stable_id, Relationship.source_file_id)
    unresolved = db.scalar(select(func.count()).select_from(FileCall).where(
        FileCall.snapshot_id == snap.id, FileCall.caller_stable_id == sym.stable_id,
        FileCall.resolution_status.in_(("unresolved", "unsupported", "ambiguous")))) or 0
    return SymbolDetail(
        **_symbol_out(sym, path), children=[SymbolOut(**_symbol_out(s, p)) for s, p in kids], callers=callers,
        callees=callees, base_links=base_links, subclasses=subs, caller_total=caller_total,
        callee_total=callee_total, unresolved_call_total=unresolved, index=_ref(snap),
        note="Callers and callees come from name-based static resolution and may be incomplete.")


# ---------------------------------------------------------------- index status / rebuild

@router.get("/index/status", response_model=IndexStatus)
def index_status(repository_id: str, include_pending: bool = Query(False),
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> IndexStatus:
    repo = svc.get_repository(db, ctx, repository_id)
    latest = svc.latest_snapshot(db, repo.id)
    serving = svc.latest_snapshot(db, repo.id, completed_only=True)
    if latest is None:
        state, msg = "never_indexed", "This repository has not been indexed."
    elif latest.status == "running":
        state, msg = "running", "An indexing run is in progress; queries use the previous generation."
    elif latest.status == "failed":
        state = "failed"
        msg = ("The latest run failed" + (" and queries are served from the previous generation."
               if serving else "; nothing is queryable yet.") + " Run indexing again or rebuild.")
    else:
        state = latest.status
        msg = ("The index is up to date with its last run." if state == "completed" else
               "The last run finished with warnings: some files could not be fully analysed.")
    pending = compute_freshness(db, repo, settings) if include_pending and latest is not None else None
    awaiting = None
    if pending is not None and pending.state in ("fresh", "stale"):
        awaiting = (pending.changed or 0) + (pending.new or 0) + (pending.removed or 0)
    failure = None
    if latest is not None and latest.status == "failed":
        failure = latest.error_message or "Indexing failed."
    elif serving is not None and latest is not None and latest.status == "running":
        prior_failed = db.scalar(select(IndexSnapshot).where(
            IndexSnapshot.repository_id == repo.id, IndexSnapshot.status == "failed",
            IndexSnapshot.started_at > serving.started_at).order_by(IndexSnapshot.started_at.desc()).limit(1))
        failure = prior_failed.error_message if prior_failed else None
    return IndexStatus(
        state=state, serving=_ref(serving) if serving else None,
        latest=SnapshotOut.model_validate(latest) if latest else None, last_failure=failure, message=msg,
        pending=pending, files_awaiting_indexing=awaiting, analysis_version=ANALYSIS_VERSION)


@router.post("/index/rebuild", response_model=SnapshotOut, status_code=202)
def rebuild_index(repository_id: str, background: BackgroundTasks, ctx: RequestContext = Depends(get_context),
                  db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> SnapshotOut:
    """Full re-extraction of every file. Use after a parser/analysis change or to recover from a bad index."""
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    snapshot = svc.begin_indexing(db, ctx, settings, repo, "full")
    background.add_task(_index_job, ctx.workspace.id, ctx.user.id, snapshot.id)
    return SnapshotOut.model_validate(snapshot)


# ---------------------------------------------------------------- metrics

def _group(db: Session, col, *where) -> dict[str, int]:  # noqa: ANN001
    return {str(k): int(n) for k, n in db.execute(select(col, func.count()).where(*where).group_by(col)).all()}


@router.get("/metrics", response_model=Metrics)
def metrics(repository_id: str, include_pending: bool = Query(False), ctx: RequestContext = Depends(get_context),
            db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Metrics:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    now: datetime = utcnow()
    if snap is None:
        return Metrics(indexed=False, generated_at=now, definitions=METRIC_DEFINITIONS)
    fq = IndexedFile.snapshot_id == snap.id
    by_analysis = _group(db, IndexedFile.analysis_status, fq)
    by_lang = _group(db, IndexedFile.language, fq)
    by_kind = _group(db, Symbol.kind, Symbol.snapshot_id == snap.id)
    comps, edges, _, nodes = _cycles(db, snap)
    awaiting = None
    if include_pending:
        fr = compute_freshness(db, repo, settings)
        if fr.state in ("fresh", "stale"):
            awaiting = (fr.changed or 0) + (fr.new or 0) + (fr.removed or 0)
    return Metrics(
        indexed=True, index=_ref(snap), generated_at=now, files=sum(by_analysis.values()),
        files_by_language=by_lang, files_by_analysis_status=by_analysis,
        parse_failures=by_analysis.get("failed", 0) + by_analysis.get("unavailable", 0),
        symbols=sum(by_kind.values()), symbols_by_kind=by_kind,
        edges_by_type=_group(db, Relationship.type, Relationship.snapshot_id == snap.id),
        imports_by_status=_group(db, FileImport.resolution_status, FileImport.snapshot_id == snap.id),
        calls_by_status=_group(db, FileCall.resolution_status, FileCall.snapshot_id == snap.id),
        cycles=len(comps), max_dependency_depth=graph.max_dependency_depth(nodes, edges),
        files_awaiting_indexing=awaiting, definitions=METRIC_DEFINITIONS)
