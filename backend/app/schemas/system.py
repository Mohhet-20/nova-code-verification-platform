from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class WorkspaceContext(BaseModel):
    workspace_id: str
    workspace_name: str
    user_handle: str
    user_display_name: str
    role: str
    auth_mode: str  # "local-trust" | "bearer-token"
    multi_user_isolation: bool
    ai_provider: str
    planner: str
    version: str
    environment: str
    limits: dict[str, int]
    registration_restricted: bool


class AuditEventOut(BaseModel):
    id: str
    action: str
    entity_type: str
    entity_id: str
    details: dict[str, Any]
    created_at: datetime


class Overview(BaseModel):
    repositories: int
    indexed_repositories: int
    indexed_files: int
    tasks_total: int
    tasks_by_status: dict[str, int]
    recent_events: list[AuditEventOut]
