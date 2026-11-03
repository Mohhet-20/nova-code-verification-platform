from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DiscoveryOut(Out):
    id: str
    status: str
    framework: str
    framework_config: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    files_found: int
    files_unparsable: int
    tests_found: int
    source_files_with_tests: int
    source_files_total: int
    errors: list[str]
    excluded: list[str]


class TestFileOut(Out):
    __test__ = False
    id: str
    rel_path: str
    framework: str
    parse_status: str
    parse_error: str | None
    test_count: int
    tests: list[dict[str, Any]]
    associated_sources: list[dict[str, Any]]
    notes: list[str]


class TestsOverview(BaseModel):
    discovery: DiscoveryOut | None
    note: str
    generation_runs: int
    candidates_by_status: dict[str, int]
    execution_runs_by_status: dict[str, int]
    latest_coverage_run_id: str | None


class TargetIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    symbol: str | None = Field(None, max_length=300)


class PlanRequest(BaseModel):
    targets: list[TargetIn] = Field(min_length=1, max_length=10)
    include_templates: bool = True


class GenerateRequest(PlanRequest):
    pass


class PlanOut(BaseModel):
    targets: list[dict[str, Any]]
    note: str


class CandidateOut(Out):
    id: str
    target_path: str
    target_symbol: str | None
    module: str | None
    cases: list[dict[str, Any]]
    confidence: str
    review_required: bool
    runnable_count: int
    size_bytes: int
    content_sha256: str
    status: str
    validation: list[dict[str, Any]]
    rejection_reasons: list[str]
    warnings: list[str]
    approved_at: datetime | None
    exported_path: str | None
    source: str | None = None


class GenerationOut(Out):
    id: str
    status: str
    targets: list[dict[str, Any]]
    generator_version: str
    tool_versions: dict[str, Any]
    warnings: list[str]
    created_at: datetime
    candidates: list[CandidateOut] = []
    created: bool = True


class ApproveRequest(BaseModel):
    candidate_ids: list[str] | None = Field(None, max_length=50)


class ExecuteRequest(BaseModel):
    candidate_ids: list[str] | None = Field(None, max_length=50)
    coverage: bool = True


class ExecuteExistingRequest(BaseModel):
    confirm: Literal[True]
    coverage: bool = True


class ExportRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=300)
    overwrite: bool = False


class ExecutionRunOut(Out):
    id: str
    kind: str
    generation_run_id: str | None
    candidate_ids: list[str]
    status: str
    reason: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    exit_code: int | None
    command: list[str]
    environment_profile: dict[str, Any]
    tests_collected: int | None
    tests_passed: int | None
    tests_failed: int | None
    tests_errors: int | None
    tests_skipped: int | None
    failures: list[dict[str, Any]]
    stdout_tail: str
    stderr_tail: str
    output_truncated: bool
    coverage_status: str
    coverage_error: str | None
    coverage_tool: str | None
    coverage_branch: bool
    lines_total: int | None
    lines_covered: int | None
    line_percent: float | None
    branches_total: int | None
    branches_covered: int | None
    branch_percent: float | None
    source_hashes: dict[str, str]
    tool_versions: dict[str, Any]


class CoverageFileOut(Out):
    rel_path: str
    statements: int
    covered: int
    line_percent: float
    missing_lines: list[list[int]]
    branches: int | None
    branches_covered: int | None


class CoverageOut(BaseModel):
    state: Literal["never_measured", "measured"]
    message: str
    run: ExecutionRunOut | None
    unmeasured_targets: list[str] = []
    basis: str | None = None


class GapOut(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    coverage_run_id: str | None
    discovery_id: str | None
    note: str
