from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v: Any) -> Any:
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


Mode = Literal["revisions", "staged", "unstaged", "working_tree"]


class ReviewCreate(BaseModel):
    mode: Mode
    base: str | None = Field(None, max_length=200, description="Base revision (required for mode 'revisions').")
    head: str | None = Field(None, max_length=200, description="Head revision; defaults to HEAD in mode 'revisions'.")
    paths: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _check(self) -> ReviewCreate:
        if self.mode == "revisions" and not self.base:
            raise ValueError("base is required for mode 'revisions'.")
        if self.mode != "revisions" and (self.base or self.head):
            raise ValueError(f"base and head are only used by mode 'revisions', not '{self.mode}'.")
        return self


class ReviewOut(Out):
    id: str
    mode: str
    status: str
    base_ref: str | None
    head_ref: str | None
    base_sha: str | None
    head_sha: str | None
    git_head: str | None
    selected_paths: list[str]
    index_snapshot_id: str | None
    index_stale: bool
    index_notes: list[str]
    files_total: int
    files_analyzed: int
    files_omitted: int
    lines_added: int
    lines_removed: int
    symbols_changed: int
    findings_total: int
    risk_state: str | None
    risk_score: float | None
    risk_category: str | None
    warnings: list[str]
    error_message: str | None
    generator_version: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ReviewDetail(ReviewOut):
    summary: dict[str, Any]
    stages: list[dict[str, Any]]


class ChangedSymbolOut(Out):
    qualified_name: str
    symbol_stable_id: str | None
    kind: str
    start_line: int | None
    end_line: int | None
    change_kind: str
    is_public: bool
    signature_old: str | None
    signature_new: str | None
    mapping: str
    complexity: int | None


class ChangeOut(Out):
    id: str
    rel_path: str
    change_type: str
    old_path: str | None
    new_path: str | None
    old_blob: str | None
    new_blob: str | None
    language: str
    is_test: bool
    is_binary: bool
    lines_added: int | None
    lines_removed: int | None
    new_ranges: list[list[int]]
    old_ranges: list[list[int]]
    extraction_status: str
    notes: list[str]
    symbols: list[ChangedSymbolOut] = []


class FindingOut(Out):
    id: str
    stable_id: str
    fingerprint: str
    review_id: str
    rule_id: str
    analyzer_id: str
    rel_path: str
    start_line: int | None
    end_line: int | None
    scope: str
    severity: str
    category: str
    title: str
    explanation: str
    evidence: str
    remediation: str
    limitations: str
    confidence: str
    actionable: bool
    source: str
    ai_assisted: bool
    status: str
    rationale: str | None
    inherited_from_review_id: str | None
    related: dict[str, Any]
    created_at: datetime


class FindingPatch(BaseModel):
    status: Literal["open", "accepted", "dismissed", "follow_up"]
    rationale: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> FindingPatch:
        if self.status == "dismissed" and len((self.rationale or "").strip()) < 5:
            raise ValueError("A rationale of at least 5 characters is required to dismiss a finding.")
        return self
