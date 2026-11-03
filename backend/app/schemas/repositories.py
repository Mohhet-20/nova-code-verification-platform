from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=2048)
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    files_indexed: int
    directories_visited: int
    total_bytes: int
    total_lines: int
    skipped_counts: dict[str, int]
    language_counts: dict[str, int]
    truncated: bool
    truncation_reason: str | None
    error_message: str | None
    limits: dict[str, int]
    git_head: str | None
    git_branch: str | None
    generation: int = 1
    mode: str = "incremental"
    files_new: int = 0
    files_changed: int = 0
    files_unchanged: int = 0
    files_removed: int | None = None
    files_failed: int = 0
    files_skipped: int = 0
    symbols_count: int = 0
    imports_total: int = 0
    imports_unresolved: int = 0
    relationships_count: int = 0
    analysis_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int | None = None


class IndexRequest(BaseModel):
    mode: Literal["incremental", "full"] = "incremental"


class RepositoryListItem(BaseModel):
    """List view: only a folder label, never the full filesystem path."""
    id: str
    name: str
    description: str
    root_label: str
    is_git: bool
    git_branch: str | None
    created_at: datetime
    latest_snapshot_status: str | None
    latest_snapshot_at: datetime | None
    files_indexed: int | None


class RepositoryDetail(BaseModel):
    id: str
    name: str
    description: str
    root_path: str
    root_label: str
    is_git: bool
    git_branch: str | None
    git_head: str | None
    git_dirty_count: int | None
    created_at: datetime
    updated_at: datetime
    latest_snapshot: SnapshotOut | None


class RepositorySummary(BaseModel):
    repository_id: str
    indexed: bool
    snapshot: SnapshotOut | None
    top_languages: list[tuple[str, int]]
    largest_files: list[tuple[str, int]]
    symbol_kinds: list[tuple[str, int]] = Field(default_factory=list)
    index_state: str = "none"  # none | ready | partial


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    change_status: str = "new"
    analysis_status: str = "unsupported"
    language_confidence: str = "high"
    parser: str | None = None
    symbol_count: int = 0
    import_count: int = 0
    rel_path: str
    extension: str
    language: str
    size_bytes: int
    line_count: int
    depth: int
