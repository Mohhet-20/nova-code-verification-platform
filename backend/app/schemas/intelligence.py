from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import Page


class IndexRef(BaseModel):
    snapshot_id: str
    generation: int
    status: str
    finished_at: datetime | None


class SymbolOut(BaseModel):
    id: str
    stable_id: str
    name: str
    qualified_name: str
    kind: str
    language: str
    file_id: str
    file_path: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    parent_stable_id: str | None
    visibility: str | None
    exported: bool
    signature: str | None
    doc_summary: str | None
    bases: list[str]
    parser: str
    extraction_method: str
    confidence: str


class SymbolHit(SymbolOut):
    matched_on: str  # exact_name | exact_qualified_name | name_prefix | qualified_prefix | name_substring | qualified_substring


class SymbolSearchPage(Page[SymbolHit]):
    index: IndexRef | None
    ambiguous: bool = False  # several distinct symbols share the exact queried name


class ImportOut(BaseModel):
    id: str
    kind: str
    specifier: str
    names: list[dict[str, str | None]]
    level: int
    line: int
    type_only: bool
    resolution_status: str
    resolution_note: str | None
    package: str | None
    resolved_path: str | None


class DependencyOut(BaseModel):
    type: str
    status: str
    evidence: str
    path: str | None  # other end of the edge when it is a repository file
    target_text: str
    symbol_stable_id: str | None = None


class FileDetail(BaseModel):
    id: str
    rel_path: str
    extension: str
    language: str
    language_confidence: str
    size_bytes: int
    line_count: int
    sha256: str
    change_status: str
    analysis_status: str
    parser: str | None
    analysis_version: int
    parse_diagnostics: list[str]
    symbol_count: int
    import_count: int
    dependency_count: int
    dependent_count: int
    index: IndexRef


class DiagnosticOut(BaseModel):
    rel_path: str | None
    code: str
    severity: str
    detail: str | None


class Freshness(BaseModel):
    state: str  # never_indexed | running | fresh | stale | unknown
    snapshot_id: str | None = None
    generation: int | None = None
    indexed_at: datetime | None = None
    last_attempt_status: str | None = None
    changed: int | None = None
    new: int | None = None
    removed: int | None = None
    sample_paths: list[str] = Field(default_factory=list)
    git_head_changed: bool | None = None
    note: str = ""


class LanguageCapabilityOut(BaseModel):
    language: str
    status: str
    parser_available: bool
    structural_supported: bool
    semantic_supported: bool
    backend: str | None
    backend_version: str | None
    symbol_kinds: list[str]
    relationships: list[str]
    notes: str
    unavailable_reason: str | None


class CapabilitiesOut(BaseModel):
    analysis_version: int
    languages: list[LanguageCapabilityOut]
    unsupported_relationships: list[str]
    limits: dict[str, int | float]
