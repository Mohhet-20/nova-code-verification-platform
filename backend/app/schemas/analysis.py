from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Page

Severity = Literal["critical", "high", "medium", "low", "info"]
Category = Literal["correctness", "maintainability", "reliability", "security", "complexity"]
Status = Literal["new", "existing", "resolved", "reopened", "suppressed"]


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["incremental", "full"] = "incremental"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: str
    index_snapshot_id: str | None
    status: str
    mode: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    files_eligible: int
    files_analyzed: int
    files_unchanged: int
    files_excluded: int
    files_skipped: int
    files_parse_failed: int
    files_failed: int
    files_not_reached: int
    findings_new: int
    findings_reopened: int
    findings_resolved: int
    findings_open: int
    rules_enabled: int
    rules_disabled: int
    config_source: str
    analyzer_versions: dict[str, str]
    warnings: list[str]
    analyzer_errors: list[dict[str, Any]]
    error_message: str | None


class ConfigState(BaseModel):
    valid: bool
    source: str
    errors: list[str] = Field(default_factory=list)


class AnalysisStatus(BaseModel):
    state: str  # never_analyzed | running | completed | completed_with_warnings | failed
    latest: RunOut | None
    findings_open: int
    index_generation: int | None
    analyzed_index_generation: int | None
    index_changed_since_run: bool | None
    config: ConfigState
    message: str


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: str
    rule_id: str
    analyzer_id: str
    rel_path: str
    file_id: str | None = None  # id in the current index generation, if the file is still indexed
    language: str
    start_line: int
    end_line: int
    start_col: int | None
    end_col: int | None
    symbol_stable_id: str | None
    scope: str
    severity: Severity
    category: Category
    title: str
    explanation: str
    evidence: str
    remediation: str
    limitations: str
    confidence: Literal["high", "medium", "low"]
    metric_value: float | None
    fingerprint: str
    status: Status
    resolution_reason: str | None
    suppression_reason: str | None
    reopened_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    analyzer_version: str
    rule_version: str
    first_run_id: str
    last_run_id: str


class FindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["suppressed", "existing"]
    reason: str | None = Field(None, max_length=300)

    @field_validator("reason")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class CountItem(BaseModel):
    key: str
    count: int


class Summary(BaseModel):
    analyzed: bool
    latest_run: RunOut | None
    total_findings: int
    open: int
    by_severity: dict[str, int]
    by_category: dict[str, int]
    by_status: dict[str, int]
    resolved_by_reason: dict[str, int]
    top_rules: list[CountItem]
    files_analyzed: int
    analyzer_errors: int
    warnings: int
    notes: list[str]


class OptionOut(BaseModel):
    name: str
    default: int | float | bool
    effective: int | float | bool
    minimum: float | None
    maximum: float | None
    description: str


class RuleOut(BaseModel):
    id: str
    title: str
    description: str
    category: str
    severity: str
    effective_severity: str
    enabled: bool
    languages: list[str]
    version: str
    analyzer_id: str
    remediation: str
    limitations: str
    confidence: str
    support: str
    options: list[OptionOut]
    references: list[str]


class AnalyzerOut(BaseModel):
    id: str
    name: str
    version: str
    languages: list[str]
    rules: int


class LanguageSupport(BaseModel):
    language: str
    status: str  # implemented | unsupported
    note: str


class RulesOut(BaseModel):
    rules: list[RuleOut]
    analyzers: list[AnalyzerOut]
    languages: list[LanguageSupport]
    config: ConfigState


class ConfigOut(BaseModel):
    path: str
    state: ConfigState
    exclude: list[str]
    exclude_generated: bool
    disabled_rules: list[str]
    severity_overrides: dict[str, str]
    option_overrides: dict[str, dict[str, Any]]
    server_limits: dict[str, float]
    editable_via_api: bool = False
    note: str


FindingPage = Page[FindingOut]
RunPage = Page[RunOut]
