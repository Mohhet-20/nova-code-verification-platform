from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RenameType = Literal["python.rename_symbol", "python.rename_local", "python.rename_method", "js.rename_symbol"]


def _clean(v: str) -> str:
    return v.strip()


class RenameBody(BaseModel):
    type_id: RenameType
    rel_path: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    new_name: str = Field(min_length=1, max_length=200)
    function: str | None = Field(None, max_length=300)
    class_name: str | None = Field(None, max_length=200)
    kind: str | None = Field(None, max_length=20)

    _strip = field_validator("rel_path", "name", "new_name")(_clean)


class ExtractBody(BaseModel):
    rel_path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(ge=1, le=1_000_000)
    end_line: int = Field(ge=1, le=1_000_000)
    new_name: str = Field(min_length=1, max_length=200)

    _strip = field_validator("rel_path", "new_name")(_clean)


class DuplicatesBody(BaseModel):
    languages: list[Literal["python", "javascript", "jsx", "typescript", "tsx"]] = Field(default_factory=list, max_length=5)
    min_lines: int | None = Field(None, ge=3, le=500)
    min_tokens: int | None = Field(None, ge=10, le=5000)
    threshold: float | None = Field(None, ge=0.5, le=1.0)
    path_prefix: str = Field("", max_length=500)
    exclude_paths: list[str] = Field(default_factory=list, max_length=50)
    exclude_tests: bool = False
    kinds: list[Literal["exact", "renamed", "structural"]] = Field(default_factory=list, max_length=3)
    include_statement_sequences: bool = True
    max_groups: int = Field(100, ge=1, le=500)


class SimplifyBody(BaseModel):
    rel_path: str = Field(min_length=1, max_length=500)


class CyclesBody(BaseModel):
    path_prefix: str = Field("", max_length=500)


class ApproveBody(BaseModel):
    acknowledged_warnings: list[str] = Field(default_factory=list, max_length=30)


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ApplyBody(BaseModel):
    confirm: bool = False


class RunBody(BaseModel):
    test_ids: list[str] = Field(default_factory=list, max_length=50)
    coverage: bool = False
    scope: Literal["selected", "all"] = "selected"
    confirm: bool = False


class TransformationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: str
    type_id: str
    title: str
    status: str
    support_level: str
    index_snapshot_id: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    patch_sha256: str | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    verification_summary: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None = None
    approved_at: datetime | None = None
    applied_at: datetime | None = None
    blockers: int = 0
    warnings: int = 0


class TransformationDetail(TransformationOut):
    params: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    impact: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    apply_result: dict[str, Any] = Field(default_factory=dict)
    reject_reason: str | None = None
    submitted_at: datetime | None = None
    approved_by: str | None = None
    rejected_at: datetime | None = None
    applied_by: str | None = None
    baseline_execution_run_id: str | None = None
    baseline_note: str | None = None
    has_diff: bool = False


class VerificationDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    transformation_id: str
    execution_run_id: str | None = None
    baseline_execution_run_id: str | None = None
    selected_tests: list[dict[str, Any]] = Field(default_factory=list)
    outcome: str
    outcome_reason: str = ""
    meaning: str = ""
    comparison: dict[str, Any] = Field(default_factory=dict)
    static_check: dict[str, Any] = Field(default_factory=dict)
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    evaluated_at: datetime | None = None
    run: dict[str, Any] | None = None
