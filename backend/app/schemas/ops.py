from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentActionBody(_Body):
    note: str | None = Field(None, max_length=1000)


class RuleUpdateBody(_Body):
    description: str | None = Field(None, max_length=300)
    window_minutes: int | None = None
    comparator: str | None = None
    threshold: float | None = None
    min_samples: int | None = None
    severity: str | None = None
    cooldown_minutes: int | None = None
    enabled: bool | None = None


class SloUpdateBody(_Body):
    description: str | None = Field(None, max_length=300)
    target: float | None = None
    window_minutes: int | None = None
    min_samples: int | None = None
    threshold_seconds: float | None = None
    exclusions: list[str] | None = None


class DeploymentBody(_Body):
    summary: str = Field(..., min_length=1, max_length=300)
    version: str | None = Field(None, max_length=60)
    details: dict[str, Any] | None = None
