from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkBody(_Body):
    type: Literal["repair", "refactor", "review", "debugging"]
    id: str = Field(min_length=1, max_length=64)


class ChangeBody(_Body):
    source: Literal["working_tree", "staged", "revisions", "patch", "manifest"] = "working_tree"
    mode: Literal["focused", "standard", "full"] | None = None
    base: str | None = Field(None, max_length=200)
    head: str | None = Field(None, max_length=200)
    paths: list[str] | None = Field(None, max_length=500)
    patch: str | None = Field(None, max_length=400_000)
    coverage: bool = False


class RunBody(ChangeBody):
    baseline_id: str | None = Field(None, max_length=64)
    confirm: bool = False
    link: LinkBody | None = None


class BaselineBody(_Body):
    label: str = Field("", max_length=120)
    coverage: bool = False
    confirm: bool = False


class PolicyBody(_Body):
    rules: dict[str, Any]
    note: str = Field("", max_length=300)
