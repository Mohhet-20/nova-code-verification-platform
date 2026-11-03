from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionBody(_Body):
    provider: Literal["local", "github"]
    external_repo: str = Field("", max_length=200)
    default_branch: str = Field("main", max_length=200)
    enabled_events: list[str] = Field(default_factory=lambda: ["pull_request"], max_length=5)
    token_env: str = Field("", max_length=64)
    webhook_secret_env: str = Field("", max_length=64)
    status_context: str = Field("nova/verification", max_length=60)
    enabled: bool = True


class StartBody(_Body):
    mode: Literal["focused", "standard", "full"] | None = None
    confirm: bool = False
    force: bool = False


class ReportBody(_Body):
    kind: Literal["review", "release"] = "release"
    workflow_run_id: str | None = Field(None, max_length=64)


class PolicyBody(_Body):
    rules: dict[str, Any]
    note: str = Field("", max_length=300)


class SendStatusBody(_Body):
    confirm: bool = False
