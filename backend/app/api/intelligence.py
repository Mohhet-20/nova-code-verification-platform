"""Read-only code-navigation endpoints over the newest published index generation."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models import (
    FileImport,
    IndexDiagnostic,
    IndexedFile,
    IndexSnapshot,
    Relationship,
    Repository,
    Symbol,
)
from app.schemas.common import Page, make_page
from app.schemas.intelligence import (
    CapabilitiesOut,
    DependencyOut,
    DiagnosticOut,
    FileDetail,
    Freshness,
    ImportOut,
    IndexRef,
    LanguageCapabilityOut,
    SymbolHit,
    SymbolOut,
    SymbolSearchPage,
)
from app.schemas.repositories import SnapshotOut
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.intelligence import parser_registry
from app.services.intelligence.freshness import compute_freshness
from app.services.intelligence.types import ANALYSIS_VERSION

router = APIRouter(prefix="/repositories/{repository_id}", tags=["intelligence"])
caps_router = APIRouter(tags=["intelligence"])


def _esc(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _current(db: Session, ctx: RequestContext, repository_id: str) -> tuple[Repository, IndexSnapshot | None]:
    repo = svc.get_repository(db, ctx, repository_id)
    return repo, svc.latest_snapshot(db, repo.id, completed_only=True)


def _ref(s: IndexSnapshot) -> IndexRef:
    return IndexRef(snapshot_id=s.id, generation=s.generation, status=s.status, finished_at=s.finished_at)


def _symbol_out(sym: Symbol, path: str) -> dict[str, Any]:
    return dict(
        id=sym.id, stable_id=sym.stable_id, name=sym.name, qualified_name=sym.qualified_name,
        kind=sym.kind, language=sym.language, file_id=sym.file_id, file_path=path,
        start_line=sym.start_line, start_col=sym.start_col, end_line=sym.end_line, end_col=sym.end_col,
        parent_stable_id=sym.parent_stable_id, visibility=sym.visibility, exported=sym.exported,
        signature=sym.signature, doc_summary=sym.doc_summary, bases=list(sym.bases or []),
        parser=sym.parser, extraction_method=sym.extraction_method, confidence=sym.confidence)


def _file(db: Session, repo: Repository, snap: IndexSnapshot | None, file_id: str) -> IndexedFile:
    f = None
    if snap is not None:
        f = db.scalar(select(IndexedFile).where(
            IndexedFile.id == file_id, IndexedFile.repository_id == repo.id,
            IndexedFile.snapshot_id == snap.id))
    if f is None:
        raise NotFoundError("File not found in the current index.")
    return f


@router.get("/snapshots", response_model=Page[SnapshotOut])
def list_snapshots(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[SnapshotOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    where = IndexSnapshot.repository_id == repo.id
    total = db.scalar(select(func.count()).select_from(IndexSnapshot).where(where)) or 0
    rows = db.scalars(select(IndexSnapshot).where(where)
                      .order_by(IndexSnapshot.started_at.desc(), IndexSnapshot.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([SnapshotOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/snapshots/{snapshot_id}/diagnostics", response_model=Page[DiagnosticOut])
def snapshot_diagnostics(repository_id: str, snapshot_id: str, page: int = Query(1, ge=1),
                         page_size: int = Query(50, ge=1, le=200), code: str | None = Query(None, max_length=40),
                         severity: str | None = Query(None, pattern="^(info|warning|error)$"),
                         ctx: RequestContext = Depends(get_context),
                         db: Session = Depends(get_db)) -> Page[DiagnosticOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    snap = db.scalar(select(IndexSnapshot).where(IndexSnapshot.id == snapshot_id,
                                                 IndexSnapshot.repository_id == repo.id))
    if snap is None:
        raise NotFoundError("Snapshot not found.")
    cond = [IndexDiagnostic.snapshot_id == snap.id]
    if code:
        cond.append(IndexDiagnostic.code == code)
    if severity:
        cond.append(IndexDiagnostic.severity == severity)
    total = db.scalar(select(func.count()).select_from(IndexDiagnostic).where(*cond)) or 0
    rows = db.scalars(select(IndexDiagnostic).where(*cond)
                      .order_by(IndexDiagnostic.code, IndexDiagnostic.rel_path, IndexDiagnostic.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([DiagnosticOut(rel_path=r.rel_path, code=r.code, severity=r.severity, detail=r.detail)
                      for r in rows], total, page, page_size)


@router.get("/freshness", response_model=Freshness)
def freshness(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)) -> Freshness:
    """Cheap staleness check: compares file size and mtime with the index. Nothing is read or hashed."""
    return compute_freshness(db, svc.get_repository(db, ctx, repository_id), settings)


@router.get("/files/{file_id}", response_model=FileDetail)
def file_detail(repository_id: str, file_id: str, ctx: RequestContext = Depends(get_context),
                db: Session = Depends(get_db)) -> FileDetail:
    repo, snap = _current(db, ctx, repository_id)
    f = _file(db, repo, snap, file_id)
    assert snap is not None
    deps = db.scalar(select(func.count()).select_from(Relationship)
                     .where(Relationship.source_file_id == f.id)) or 0
    dependents = db.scalar(select(func.count(func.distinct(Relationship.source_file_id)))
                           .where(Relationship.target_file_id == f.id,
                                  Relationship.source_file_id != f.id)) or 0
    return FileDetail(
        id=f.id, rel_path=f.rel_path, extension=f.extension, language=f.language,
        language_confidence=f.language_confidence, size_bytes=f.size_bytes, line_count=f.line_count,
        sha256=f.sha256, change_status=f.change_status, analysis_status=f.analysis_status, parser=f.parser,
        analysis_version=f.analysis_version, parse_diagnostics=list(f.parse_diagnostics or []),
        symbol_count=f.symbol_count, import_count=f.import_count, dependency_count=deps,
        dependent_count=dependents, index=_ref(snap))


@router.get("/files/{file_id}/imports", response_model=Page[ImportOut])
def file_imports(repository_id: str, file_id: str, page: int = Query(1, ge=1),
                 page_size: int = Query(100, ge=1, le=500), status: str | None = Query(
                     None, pattern="^(resolved|external|unresolved|unsupported)$"),
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[ImportOut]:
    repo, snap = _current(db, ctx, repository_id)
    f = _file(db, repo, snap, file_id)
    cond = [FileImport.file_id == f.id]
    if status:
        cond.append(FileImport.resolution_status == status)
    total = db.scalar(select(func.count()).select_from(FileImport).where(*cond)) or 0
    target = IndexedFile.__table__.alias("target")
    rows = db.execute(select(FileImport, target.c.rel_path)
                      .outerjoin(target, target.c.id == FileImport.resolved_file_id).where(*cond)
                      .order_by(FileImport.line, FileImport.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    items = [ImportOut(id=i.id, kind=i.kind, specifier=i.specifier, names=list(i.names or []), level=i.level,
                       line=i.line, type_only=i.type_only, resolution_status=i.resolution_status,
                       resolution_note=i.resolution_note, package=i.package, resolved_path=p)
             for i, p in rows]
    return make_page(items, total, page, page_size)


@router.get("/files/{file_id}/dependencies", response_model=Page[DependencyOut])
def file_dependencies(repository_id: str, file_id: str, page: int = Query(1, ge=1),
                      page_size: int = Query(100, ge=1, le=500),
                      ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[DependencyOut]:
    """What this file depends on: repository files, declared/external packages."""
    repo, snap = _current(db, ctx, repository_id)
    f = _file(db, repo, snap, file_id)
    cond = [Relationship.source_file_id == f.id]
    total = db.scalar(select(func.count()).select_from(Relationship).where(*cond)) or 0
    target = IndexedFile.__table__.alias("target")
    rows = db.execute(select(Relationship, target.c.rel_path)
                      .outerjoin(target, target.c.id == Relationship.target_file_id).where(*cond)
                      .order_by(Relationship.type, Relationship.target_text, Relationship.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    items = [DependencyOut(type=r.type, status=r.status, evidence=r.evidence, path=p, target_text=r.target_text,
                           symbol_stable_id=r.target_symbol_stable_id) for r, p in rows]
    return make_page(items, total, page, page_size)


@router.get("/files/{file_id}/dependents", response_model=Page[DependencyOut])
def file_dependents(repository_id: str, file_id: str, page: int = Query(1, ge=1),
                    page_size: int = Query(100, ge=1, le=500),
                    ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[DependencyOut]:
    """Which repository files depend on this one (reverse edges)."""
    repo, snap = _current(db, ctx, repository_id)
    f = _file(db, repo, snap, file_id)
    cond = [Relationship.target_file_id == f.id, Relationship.source_file_id != f.id]
    total = db.scalar(select(func.count()).select_from(Relationship).where(*cond)) or 0
    source = IndexedFile.__table__.alias("source")
    rows = db.execute(select(Relationship, source.c.rel_path)
                      .join(source, source.c.id == Relationship.source_file_id).where(*cond)
                      .order_by(source.c.rel_path, Relationship.type, Relationship.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    items = [DependencyOut(type=r.type, status=r.status, evidence=r.evidence, path=p, target_text=r.target_text,
                           symbol_stable_id=r.source_symbol_stable_id) for r, p in rows]
    return make_page(items, total, page, page_size)


@router.get("/symbols", response_model=Page[SymbolOut])
def list_symbols(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                 kind: str | None = Query(None, max_length=20), language: str | None = Query(None, max_length=40),
                 file_id: str | None = Query(None, max_length=40),
                 exported: bool | None = Query(None), q: str | None = Query(None, max_length=200),
                 ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[SymbolOut]:
    repo, snap = _current(db, ctx, repository_id)
    if snap is None:
        return make_page([], 0, page, page_size)
    cond = [Symbol.snapshot_id == snap.id, Symbol.repository_id == repo.id]
    if kind:
        cond.append(Symbol.kind == kind)
    if language:
        cond.append(Symbol.language == language)
    if file_id:
        cond.append(Symbol.file_id == file_id)
    if exported is not None:
        cond.append(Symbol.exported == exported)
    if q and q.strip():
        esc = _esc(q.strip().lower())
        cond.append(or_(Symbol.name_lower.like(f"%{esc}%", escape="\\"),
                        Symbol.qualified_lower.like(f"%{esc}%", escape="\\")))
    total = db.scalar(select(func.count()).select_from(Symbol).where(*cond)) or 0
    rows = db.execute(select(Symbol, IndexedFile.rel_path).join(IndexedFile, IndexedFile.id == Symbol.file_id)
                      .where(*cond).order_by(IndexedFile.rel_path, Symbol.start_line, Symbol.start_col, Symbol.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([SymbolOut(**_symbol_out(s, p)) for s, p in rows], total, page, page_size)


@router.get("/symbols/search", response_model=SymbolSearchPage)
def search_symbols(repository_id: str, q: str = Query(min_length=1, max_length=200),
                   page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   kind: str | None = Query(None, max_length=20), language: str | None = Query(None, max_length=40),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> SymbolSearchPage:
    """Ranked, case-insensitive search. Every hit says *why* it matched; ambiguity is reported, not hidden."""
    repo, snap = _current(db, ctx, repository_id)
    q = q.strip()
    if snap is None or not q:
        return SymbolSearchPage(items=[], total=0, page=page, page_size=page_size, pages=1, index=None)
    ql, esc = q.lower(), _esc(q.lower())
    rank = case(
        (Symbol.name_lower == ql, 0), (Symbol.qualified_lower == ql, 1),
        (Symbol.name_lower.like(f"{esc}%", escape="\\"), 2),
        (Symbol.qualified_lower.like(f"{esc}%", escape="\\"), 3),
        (Symbol.name_lower.like(f"%{esc}%", escape="\\"), 4), else_=5).label("rank")
    match = or_(Symbol.name_lower.like(f"%{esc}%", escape="\\"),
                Symbol.qualified_lower.like(f"%{esc}%", escape="\\"))
    cond = [Symbol.snapshot_id == snap.id, Symbol.repository_id == repo.id, match]
    if kind:
        cond.append(Symbol.kind == kind)
    if language:
        cond.append(Symbol.language == language)
    total = db.scalar(select(func.count()).select_from(Symbol).where(*cond)) or 0
    rows = db.execute(select(Symbol, IndexedFile.rel_path, rank)
                      .join(IndexedFile, IndexedFile.id == Symbol.file_id).where(*cond)
                      .order_by(rank, func.length(Symbol.name), Symbol.name_lower, IndexedFile.rel_path,
                                Symbol.start_line)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    labels = ["exact_name", "exact_qualified_name", "name_prefix", "qualified_prefix", "name_substring",
              "qualified_substring"]
    items = [SymbolHit(**_symbol_out(s, p), matched_on=labels[r]) for s, p, r in rows]
    exact = db.scalar(select(func.count(func.distinct(Symbol.stable_id))).where(
        Symbol.snapshot_id == snap.id, Symbol.name_lower == ql)) or 0
    base = make_page(items, total, page, page_size)
    return SymbolSearchPage(items=base.items, total=base.total, page=base.page, page_size=base.page_size,
                            pages=base.pages, index=_ref(snap), ambiguous=exact > 1)


@caps_router.get("/capabilities", response_model=CapabilitiesOut)
def capabilities(_: RequestContext = Depends(get_context),
                 settings: Settings = Depends(get_settings)) -> CapabilitiesOut:
    langs = [LanguageCapabilityOut(
        language=c.language, status=c.status, parser_available=c.parser_available,
        structural_supported=c.structural_supported, semantic_supported=c.semantic_supported,
        backend=c.backend, backend_version=c.backend_version, symbol_kinds=list(c.symbol_kinds),
        relationships=list(c.relationships), notes=c.notes, unavailable_reason=c.unavailable_reason)
        for c in parser_registry.all_capabilities()]
    return CapabilitiesOut(
        analysis_version=ANALYSIS_VERSION, languages=langs,
        unsupported_relationships=["REFERENCES", "IMPLEMENTS", "type resolution", "dynamic imports", "dynamic dispatch"],
        limits={"max_files": settings.index_max_files, "max_depth": settings.index_max_depth,
                "max_file_bytes": settings.index_max_file_bytes, "max_total_bytes": settings.index_max_total_bytes,
                "time_budget_seconds": settings.index_time_budget_seconds,
                "max_symbols_per_file": settings.index_max_symbols_per_file})

