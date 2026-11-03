from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.intelligence import Freshness, IndexRef, SymbolOut
from app.schemas.repositories import SnapshotOut

DISCLAIMER = ("Static analysis estimate. It follows imports, inheritance and name-resolved calls found in source "
              "text. Dynamic imports, reflection, dynamic dispatch, generated code and runtime wiring are not "
              "modelled, so the real blast radius may be larger or smaller.")


class FileRef(BaseModel):
    file_id: str
    path: str


class GraphItem(BaseModel):
    kind: str  # file | symbol | module
    id: str  # file id, or symbol stable id
    name: str
    path: str
    depth: int
    relationship: str  # IMPORTS_FILE | CALLS | EXTENDS_SYMBOL
    status: str  # resolved | ambiguous
    evidence: str  # observed | inferred
    line: int | None = None
    chain: list[str] = Field(default_factory=list)  # human-readable path from the root to this node
    explanation: str = ""


class UnresolvedImport(BaseModel):
    specifier: str
    kind: str
    status: str  # unresolved | unsupported | external
    note: str | None = None
    line: int
    package: str | None = None


class DependenciesOut(BaseModel):
    root: FileRef
    direction: str
    max_depth: int
    items: list[GraphItem]
    total: int
    page: int
    page_size: int
    pages: int
    truncated: bool = False  # traversal stopped at the node cap
    more_beyond_depth: bool = False
    unresolved: list[UnresolvedImport] = Field(default_factory=list)
    unresolved_total: int = 0
    index: IndexRef
    note: str = ""


class ImpactSummary(BaseModel):
    direct: int
    transitive: int
    total: int
    files_affected: int
    max_depth_reached: int


class CycleRef(BaseModel):
    id: str
    size: int
    severity: str
    files: list[str]


class ImpactOut(BaseModel):
    target_kind: str  # file | symbol
    target_id: str
    target_name: str
    target_path: str
    max_depth: int
    summary: ImpactSummary
    items: list[GraphItem]
    total: int
    page: int
    page_size: int
    pages: int
    truncated: bool = False
    more_beyond_depth: bool = False
    cycles: list[CycleRef] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER
    index: IndexRef


class CycleOut(BaseModel):
    id: str
    kind: str = "structural_dependency_cycle"
    size: int
    severity: str
    severity_reason: str
    files: list[FileRef]
    files_truncated: bool = False
    edges: list[dict[str, object]] = Field(default_factory=list)
    explanation: str = ""


class CyclesPage(BaseModel):
    items: list[CycleOut]
    total: int
    page: int
    page_size: int
    pages: int
    severity_rules: dict[str, str]
    note: str
    index: IndexRef | None = None


class SymbolLink(BaseModel):
    stable_id: str | None = None
    name: str
    qualified_name: str | None = None
    kind: str | None = None
    path: str | None = None
    line: int | None = None
    relationship: str
    status: str
    evidence: str


class SymbolDetail(SymbolOut):
    children: list[SymbolOut]
    callers: list[SymbolLink]
    callees: list[SymbolLink]
    base_links: list[SymbolLink]
    subclasses: list[SymbolLink]
    caller_total: int
    callee_total: int
    unresolved_call_total: int
    index: IndexRef
    note: str = ""


class IndexStatus(BaseModel):
    state: str  # never_indexed | running | completed | completed_with_warnings | failed
    serving: IndexRef | None  # generation queries are answered from
    latest: SnapshotOut | None  # newest attempt, whatever its outcome
    last_failure: str | None = None
    message: str
    pending: Freshness | None = None
    files_awaiting_indexing: int | None = None
    analysis_version: int
    recoverable_with_rebuild: bool = True


class MetricDefinitions(BaseModel):
    definitions: dict[str, str]


class Metrics(BaseModel):
    indexed: bool
    index: IndexRef | None = None
    generated_at: datetime
    files: int = 0
    files_by_language: dict[str, int] = Field(default_factory=dict)
    files_by_analysis_status: dict[str, int] = Field(default_factory=dict)
    parse_failures: int = 0
    symbols: int = 0
    symbols_by_kind: dict[str, int] = Field(default_factory=dict)
    edges_by_type: dict[str, int] = Field(default_factory=dict)
    imports_by_status: dict[str, int] = Field(default_factory=dict)
    calls_by_status: dict[str, int] = Field(default_factory=dict)
    cycles: int = 0
    max_dependency_depth: int = 0
    files_awaiting_indexing: int | None = None
    definitions: dict[str, str] = Field(default_factory=dict)
