from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.review import Out

Severity = Literal["critical", "high", "medium", "low", "info"]


class InvestigationCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field("", max_length=4000)
    severity: Severity = "medium"
    failure_id: str | None = Field(None, max_length=32)
    finding_id: str | None = Field(None, max_length=32)
    review_finding_id: str | None = Field(None, max_length=32)
    debug_investigation_id: str | None = Field(None, max_length=32)
    hint_files: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("hint_files")
    @classmethod
    def _hints(cls, v: list[str]) -> list[str]:
        if any(len(x) > 500 for x in v):
            raise ValueError("A file hint is too long.")
        return v


class InvestigationStatusPatch(BaseModel):
    status: Literal["open", "closed"]


class PatchCreate(BaseModel):
    source: Literal["generated", "manual", "ai_assisted"]
    title: str | None = Field(None, max_length=200)
    diff: str | None = Field(None, max_length=400_000)
    extra_files: list[str] = Field(default_factory=list, max_length=10)
    rule_id: str | None = Field(None, max_length=40)
    rel_path: str | None = Field(None, max_length=1024)
    line: int | None = Field(None, ge=1)


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ConfirmBody(BaseModel):
    confirm: bool = False


class VerifyBody(BaseModel):
    test_ids: list[str] = Field(default_factory=list, max_length=50)
    coverage: bool = False
    scope: Literal["selected", "all"] = "selected"
    confirm: bool = False

    @field_validator("test_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        if any(len(x) > 300 or "\x00" in x for x in v):
            raise ValueError("A test id is invalid.")
        return v


class InvestigationOut(Out):
    id: str
    title: str
    description: str
    severity: str
    status: str
    source_kind: str
    failure_id: str | None
    finding_id: str | None
    review_finding_id: str | None
    debug_investigation_id: str | None
    hint_files: list[str]
    relevant_files: list[str]
    root_cause: dict[str, Any]
    verification_outcome: str | None
    evidence_assembled_at: datetime | None
    proposal_generated_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    patch_count: int = 0
    latest_patch_status: str | None = None


class InvestigationDetail(InvestigationOut):
    evidence: dict[str, Any]
    proposal: dict[str, Any]
    baseline_execution_run_id: str | None
    index_snapshot_id: str | None


class PatchOut(Out):
    model_config = ConfigDict(from_attributes=True)
    id: str
    investigation_id: str
    source: str
    rule_id: str | None
    title: str
    summary: str
    status: str
    patch_sha256: str
    files: list[dict[str, Any]]
    base_head: str | None
    extra_files: list[str]
    validation: dict[str, Any]
    validated_at: datetime | None
    submitted_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    reject_reason: str | None
    applied_by: str | None
    applied_at: datetime | None
    apply_result: dict[str, Any]
    verification_summary: dict[str, Any]
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class PatchDetail(PatchOut):
    diff_text: str
    preview: dict[str, Any] = Field(default_factory=dict)


class VerificationOut(Out):
    id: str
    patch_id: str
    investigation_id: str
    execution_run_id: str | None
    baseline_execution_run_id: str | None
    selected_tests: list[dict[str, Any]]
    outcome: str
    outcome_reason: str
    comparison: dict[str, Any]
    static_check: dict[str, Any]
    criteria: list[dict[str, Any]]
    requested_by: str | None
    created_at: datetime
    evaluated_at: datetime | None


class VerificationDetail(VerificationOut):
    run: dict[str, Any] | None = None
    meaning: str = ""
