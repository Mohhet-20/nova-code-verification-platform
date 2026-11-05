from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ShortText = Field(min_length=1, max_length=1000)


def _clean_list(values: list[str]) -> list[str]:
    cleaned = [v.strip() for v in values if v and v.strip()]
    if any(len(v) > 1000 for v in cleaned):
        raise ValueError("each item must be at most 1000 characters")
    return cleaned


class TaskCreate(BaseModel):
    repository_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=3, max_length=200)
    problem_statement: str = Field(min_length=10, max_length=10_000)
    objective: str = Field(min_length=5, max_length=5_000)
    scope: list[str] = Field(default_factory=list, max_length=30)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)

    _scope = field_validator("scope", mode="after")(_clean_list)

    @field_validator("acceptance_criteria", mode="after")
    @classmethod
    def _criteria(cls, v: list[str]) -> list[str]:
        cleaned = _clean_list(v)
        if not cleaned:
            raise ValueError("at least one non-empty acceptance criterion is required")
        return cleaned

    @field_validator("title", "problem_statement", "objective")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class PlanStep(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    kind: Literal["investigate", "implement", "test", "verify", "review"]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000)
    target_paths: list[str] = Field(default_factory=list, max_length=20)
    requires_approval: bool = False


class ExpectedCheck(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    suggested_command: str | None = Field(default=None, max_length=300)
    rationale: str = Field(default="", max_length=1000)
    # Always False: nothing is executed. Reserved for the verification runner.
    executed: bool = False


class PlanContent(BaseModel):
    summary: str = Field(max_length=2000)
    confidence: Literal["low", "medium", "high"]
    relevant_files: list[str] = Field(default_factory=list, max_length=30)
    steps: list[PlanStep] = Field(min_length=1, max_length=40)
    expected_checks: list[ExpectedCheck] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class PlanOut(BaseModel):
    generator: str
    generator_version: str
    revision: int
    edited_by_user: bool
    snapshot_id: str | None
    updated_at: datetime
    content: PlanContent


class PlanUpdate(BaseModel):
    content: PlanContent


class TaskOut(BaseModel):
    id: str
    repository_id: str
    repository_name: str
    title: str
    problem_statement: str
    objective: str
    scope: list[str]
    acceptance_criteria: list[str]
    status: str
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskOut):
    plan: PlanOut | None
