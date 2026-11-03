"""Request schemas for Phase 15 governance APIs (strict: unknown fields are rejected)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberCreate(Req):
    handle: str = Field(min_length=3, max_length=40)
    display_name: str = Field(default="", max_length=120)
    role: str = Field(max_length=20)


class RoleChange(Req):
    role: str = Field(max_length=20)


class TeamCreate(Req):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)


class TeamUpdate(Req):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1000)


class TeamMemberAdd(Req):
    user_id: str = Field(min_length=1, max_length=32)
    role: str = Field(default="member", max_length=10)


class OwnershipAssign(Req):
    repository_id: str = Field(min_length=1, max_length=32)
    accountable_user_id: str | None = Field(default=None, max_length=32)


class PolicyCreate(Req):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    policy_type: str = Field(max_length=40)
    resource_types: list[str] = Field(default_factory=list, max_length=8)
    team_id: str | None = Field(default=None, max_length=32)
    repository_id: str | None = Field(default=None, max_length=32)
    severity: str = Field(default="blocking", max_length=14)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    effective_from: datetime | None = None


class PolicyUpdate(Req):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    resource_types: list[str] | None = Field(default=None, max_length=8)
    team_id: str | None = None
    repository_id: str | None = None
    severity: str | None = Field(default=None, max_length=14)
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    effective_from: datetime | None = None
    note: str = Field(default="", max_length=500)


class EvaluationRequest(Req):
    """Only identifiers: results are always computed server-side from stored evidence."""
    resource_type: str = Field(max_length=30)
    resource_id: str = Field(min_length=1, max_length=32)
    instance_id: str | None = Field(default=None, max_length=32)


class DefinitionCreate(Req):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    resource_type: str = Field(max_length=30)
    spec: dict[str, Any]


class InstanceCreate(Req):
    resource_type: str = Field(max_length=30)
    resource_id: str = Field(min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=200)
    priority: str = Field(default="normal", max_length=10)
    team_id: str | None = Field(default=None, max_length=32)
    definition_key: str | None = Field(default=None, max_length=60)


class TransitionRequest(Req):
    action: str = Field(max_length=20)
    note: str = Field(default="", max_length=500)


class DecisionRequest(Req):
    decision: str = Field(max_length=20)
    justification: str = Field(default="", max_length=2000)
    stage: int | None = Field(default=None, ge=0, le=20)


class EvidenceRequest(Req):
    label: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class EnqueueRequest(Req):
    resource_type: str = Field(max_length=30)
    resource_id: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=500)
    priority: str = Field(default="normal", max_length=10)


class AssignRequest(Req):
    user_id: str | None = Field(default=None, max_length=32)
    note: str = Field(default="", max_length=500)
