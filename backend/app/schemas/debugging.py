from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v: Any) -> Any:
        """SQLite returns naive datetimes; expose every timestamp consistently as UTC."""
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class IngestRequest(BaseModel):
    source: Literal["execution_run", "junit_xml", "python_traceback", "pytest_text"]
    execution_run_id: str | None = Field(None, max_length=32)
    text: str | None = Field(None, max_length=400_000)  # real limit is enforced from settings with a 413
    test_id: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> IngestRequest:
        if self.source == "execution_run" and not self.execution_run_id:
            raise ValueError("execution_run_id is required for source 'execution_run'.")
        if self.source != "execution_run" and not (self.text or "").strip():
            raise ValueError("text is required for this source.")
        return self


class FrameOut(Out):
    idx: int
    display_path: str
    rel_path: str | None
    function: str | None
    function_source: str | None
    line: int | None
    col: int | None
    scope: str
    is_test: bool
    symbol_stable_id: str | None = None


class FailureOut(Out):
    id: str
    category: str
    exception_type: str | None
    test_id: str | None
    summary: str
    status: str
    source: str
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    top_location: str | None = None
    investigation_status: str | None = None
    investigation_id: str | None = None


class FailureDetail(FailureOut):
    message: str
    fingerprint: str
    warnings: list[str]
    frames_truncated: bool
    revision: str | None
    revision_known: bool
    index_snapshot_id: str | None
    frames: list[FrameOut]
    occurrences: list[dict[str, Any]]
    related_findings: list[dict[str, Any]]
    coverage: dict[str, Any]
    freshness: dict[str, Any]
    localization_meta: dict[str, Any]


class IngestOut(BaseModel):
    items: list[FailureOut]
    created: int
    recurred: int


class FailurePatch(BaseModel):
    status: Literal["open", "ignored", "resolved"]


class InvestigationOut(Out):
    id: str
    failure_id: str
    status: str
    resolution: str | None
    generator_version: str
    index_snapshot_id: str | None
    created_at: datetime
    updated_at: datetime
    failure_summary: str | None = None


class InvestigationDetail(InvestigationOut):
    content: dict[str, Any]
    notes: str
    verifications: list[dict[str, Any]] = []


class InvestigationPatch(BaseModel):
    status: Literal["open", "in_review", "closed"] | None = None
    notes: str | None = Field(None, max_length=4000)
    resolution: Literal["fixed_by_user", "not_a_bug", "cannot_reproduce", "wont_fix", "duplicate"] | None = None

    @model_validator(mode="after")
    def _check(self) -> InvestigationPatch:
        if self.status is None and self.notes is None and self.resolution is None:
            raise ValueError("Nothing to update.")
        return self


class VerificationRequest(BaseModel):
    test_ids: list[str] | None = Field(None, max_length=20)
    confirm: bool = False
    coverage: bool = False
