from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanBody(_Body):
    scan_types: list[Literal["dependencies", "code", "secrets"]] = Field(default_factory=lambda: ["dependencies", "code", "secrets"], min_length=1, max_length=3)
    exclude_globs: list[str] = Field(default_factory=list, max_length=50)
    include_dev: bool = True


class TriageBody(_Body):
    status: Literal["open", "false_positive", "accepted_risk"]
    reason: str = Field("", max_length=500)


class SuppressionBody(_Body):
    finding_id: str | None = Field(None, max_length=64)
    rule_id: str | None = Field(None, max_length=40)
    path_glob: str | None = Field(None, max_length=300)
    reason: str = Field(max_length=500)
    expires_at: datetime | None = None


class RevokeBody(_Body):
    reason: str = Field("", max_length=300)


class ExceptionBody(_Body):
    selector: dict[str, str]
    reason: str = Field(max_length=500)
    expires_at: datetime


class PolicyBody(_Body):
    rules: dict[str, Any]
    note: str = Field("", max_length=300)


class EvaluateBody(_Body):
    scan_id: str | None = Field(None, max_length=64)
    change_request_id: str | None = Field(None, max_length=64)


class ReportBody(_Body):
    scan_id: str | None = Field(None, max_length=64)
    format: Literal["json", "markdown", "html", "csv"] = "json"


class AdvisoryImportBody(_Body):
    documents: list[dict[str, Any]] = Field(min_length=1, max_length=200)
