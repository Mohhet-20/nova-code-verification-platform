"""ORM records. Source file contents are never stored here, only metadata."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, new_id, utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    handle: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Org roles: owner (organization administrator) | maintainer (engineering lead) | engineer | reviewer | auditor | viewer
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")  # active | removed
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("workspace_id", "root_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(1000), default="")
    root_path: Mapped[str] = mapped_column(String(2048))  # canonical absolute path
    is_git: Mapped[bool] = mapped_column(Boolean, default=False)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_dirty_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshots: Mapped[list[IndexSnapshot]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )


class IndexSnapshot(Base):
    """One indexing run == one index *generation*. Queries read only the newest published generation."""

    __tablename__ = "index_snapshots"
    __table_args__ = (
        Index("uq_index_snapshots_one_running", "repository_id", unique=True,
              sqlite_where=text("status = 'running'"), postgresql_where=text("status = 'running'")),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    files_indexed: Mapped[int] = mapped_column(Integer, default=0)
    directories_visited: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_lines: Mapped[int] = mapped_column(BigInteger, default=0)
    skipped_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    language_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    truncation_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    limits: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    git_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # --- Phase 02: generation, change accounting, counts ---
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    mode: Mapped[str] = mapped_column(String(20), default="incremental", server_default="incremental")
    files_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_changed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_unchanged: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_removed: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unknown (partial scan)
    files_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    symbols_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    imports_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    imports_unresolved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    relationships_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    analysis_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repository: Mapped[Repository] = relationship(back_populates="snapshots")


class IndexedFile(Base):
    __tablename__ = "indexed_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "rel_path"),
        Index("ix_indexed_files_snapshot_language", "snapshot_id", "language"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("index_snapshots.id", ondelete="CASCADE"), index=True
    )
    rel_path: Mapped[str] = mapped_column(String(1024))  # POSIX style, relative to the repo root
    extension: Mapped[str] = mapped_column(String(32), default="")
    language: Mapped[str] = mapped_column(String(40), default="Other")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    # --- Phase 02 ---
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True)  # denormalized for isolation checks
    mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    change_status: Mapped[str] = mapped_column(String(20), default="new", server_default="new")
    language_confidence: Mapped[str] = mapped_column(String(10), default="high", server_default="high")
    analysis_status: Mapped[str] = mapped_column(String(20), default="unsupported", server_default="unsupported")
    parser: Mapped[str | None] = mapped_column(String(60), nullable=True)
    analysis_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    symbol_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    import_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parse_diagnostics: Mapped[list[str]] = mapped_column(JSON, default=list)


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "stable_id"),
        Index("ix_symbols_snapshot_name", "snapshot_id", "name_lower"),
        Index("ix_symbols_snapshot_qualified", "snapshot_id", "qualified_lower"),
        Index("ix_symbols_snapshot_kind", "snapshot_id", "kind"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("index_snapshots.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True)
    stable_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    name_lower: Mapped[str] = mapped_column(String(200))
    qualified_name: Mapped[str] = mapped_column(String(1200))
    qualified_lower: Mapped[str] = mapped_column(String(1200))
    kind: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(40))
    start_line: Mapped[int] = mapped_column(Integer)
    start_col: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    end_col: Mapped[int] = mapped_column(Integer)
    parent_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visibility: Mapped[str | None] = mapped_column(String(12), nullable=True)
    exported: Mapped[bool] = mapped_column(Boolean, default=False)
    signature: Mapped[str | None] = mapped_column(String(300), nullable=True)
    doc_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    bases: Mapped[list[str]] = mapped_column(JSON, default=list)
    parser: Mapped[str] = mapped_column(String(60))
    extraction_method: Mapped[str] = mapped_column(String(30), default="syntax-tree")
    confidence: Mapped[str] = mapped_column(String(12), default="observed")
    file_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileImport(Base):
    """Raw import/export statements as observed, plus how (and whether) they resolved."""

    __tablename__ = "file_imports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("index_snapshots.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # import | export | reexport | require
    specifier: Mapped[str] = mapped_column(String(500))
    names: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, default=list)
    level: Mapped[int] = mapped_column(Integer, default=0)
    line: Mapped[int] = mapped_column(Integer, default=0)
    type_only: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_status: Mapped[str] = mapped_column(String(12), default="unresolved")
    resolution_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    package: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("indexed_files.id", ondelete="SET NULL"), nullable=True, index=True)


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (
        Index("ix_relationships_snapshot_target", "snapshot_id", "target_file_id"),
        Index("ix_relationships_snapshot_source", "snapshot_id", "source_file_id"),
        Index("ix_relationships_snapshot_tsym", "snapshot_id", "target_symbol_stable_id"),
        Index("ix_relationships_snapshot_ssym", "snapshot_id", "source_symbol_stable_id"),
        Index("ix_relationships_snapshot_type", "snapshot_id", "type"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("index_snapshots.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(30))  # IMPORTS_FILE | DEPENDS_ON_PACKAGE | EXTENDS_SYMBOL
    # Single-column FK indexes: without them every DELETE of an indexed_files row scans these tables (measured: 5.3 s for 1.7k files).
    source_file_id: Mapped[str] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True)
    source_symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_file_id: Mapped[str | None] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"), nullable=True, index=True)
    target_symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_text: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(12))  # resolved | ambiguous | external | unresolved | unsupported
    evidence: Mapped[str] = mapped_column(String(12))  # observed | inferred
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)  # source line of the edge, when it has one


class FileCall(Base):
    """A syntactically observed call site plus the outcome of conservative, name-based resolution."""

    __tablename__ = "file_calls"
    __table_args__ = (Index("ix_file_calls_snapshot_status", "snapshot_id", "resolution_status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("index_snapshots.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("indexed_files.id", ondelete="CASCADE"), index=True)
    caller_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # None = module level
    callee: Mapped[str | None] = mapped_column(String(200), nullable=True)  # None = not a simple dotted name
    line: Mapped[int] = mapped_column(Integer, default=0)
    resolution_status: Mapped[str] = mapped_column(String(12), default="unresolved")
    resolution_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)


class IndexDiagnostic(Base):
    """Bounded per-run report of why files were skipped or only partially analyzed."""

    __tablename__ = "index_diagnostics"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("index_snapshots.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    code: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="info")
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200))
    problem_statement: Mapped[str] = mapped_column(Text)
    objective: Mapped[str] = mapped_column(Text)
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | planned
    plan: Mapped[TaskPlan | None] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )


class TaskPlan(TimestampMixin, Base):
    __tablename__ = "task_plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    generator: Mapped[str] = mapped_column(String(60))
    generator_version: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    edited_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    task: Mapped[Task] = relationship(back_populates="plan")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ----------------------------------------------------------------------------- Phase 04: static analysis

class AnalysisRun(Base):
    """One static-analysis run over the published index of a repository."""

    __tablename__ = "analysis_runs"
    __table_args__ = (
        Index("uq_analysis_runs_one_running", "repository_id", unique=True,
              sqlite_where=text("status = 'running'"), postgresql_where=text("status = 'running'")),
        Index("ix_analysis_runs_repo_started", "repository_id", "started_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # generation analysed
    status: Mapped[str] = mapped_column(String(24))  # pending | running | completed | completed_with_warnings | failed
    mode: Mapped[str] = mapped_column(String(12), default="incremental")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files_eligible: Mapped[int] = mapped_column(Integer, default=0)
    files_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    files_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    files_excluded: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    files_parse_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_not_reached: Mapped[int] = mapped_column(Integer, default=0)
    findings_new: Mapped[int] = mapped_column(Integer, default=0)
    findings_reopened: Mapped[int] = mapped_column(Integer, default=0)
    findings_resolved: Mapped[int] = mapped_column(Integer, default=0)
    findings_open: Mapped[int] = mapped_column(Integer, default=0)
    rules_enabled: Mapped[int] = mapped_column(Integer, default=0)
    rules_disabled: Mapped[int] = mapped_column(Integer, default=0)
    config_source: Mapped[str] = mapped_column(String(20), default="defaults")
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    analyzer_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    analyzer_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AnalysisFileState(Base):
    """What the last run learned about one file; drives incremental analysis and safe resolution."""

    __tablename__ = "analysis_file_states"
    __table_args__ = (UniqueConstraint("repository_id", "rel_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024))
    content_sha: Mapped[str] = mapped_column(String(64), default="")
    fingerprint_of_setup: Mapped[str] = mapped_column(String(64), default="")  # config + analyzer versions
    status: Mapped[str] = mapped_column(String(20))  # analyzed | parse_failed | analyzer_failed | read_failed
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Finding(Base):
    """A diagnostic that persists across runs. Never stores source text or secret values."""

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("repository_id", "fingerprint"),
        CheckConstraint("severity IN ('critical','high','medium','low','info')", name="severity"),
        CheckConstraint("status IN ('new','existing','resolved','reopened','suppressed')", name="status"),
        CheckConstraint("confidence IN ('high','medium','low')", name="confidence"),
        Index("ix_findings_repo_status", "repository_id", "status"),
        Index("ix_findings_repo_path", "repository_id", "rel_path"),
        Index("ix_findings_repo_rule", "repository_id", "rule_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    rule_id: Mapped[str] = mapped_column(String(40))
    analyzer_id: Mapped[str] = mapped_column(String(40))
    rel_path: Mapped[str] = mapped_column(String(1024))
    language: Mapped[str] = mapped_column(String(40), default="Python")
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_col: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_col: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope: Mapped[str] = mapped_column(String(300), default="")  # enclosing class/function, e.g. "Svc.run"
    severity: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    explanation: Mapped[str] = mapped_column(String(2000))
    evidence: Mapped[str] = mapped_column(String(1000))
    remediation: Mapped[str] = mapped_column(String(1000))
    limitations: Mapped[str] = mapped_column(String(600), default="")
    confidence: Mapped[str] = mapped_column(String(8))
    metric_value: Mapped[float | None] = mapped_column(nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="new")
    resolution_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)  # fixed | file_removed | rule_disabled | file_excluded
    suppression_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status_changed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analyzer_version: Mapped[str] = mapped_column(String(20))
    rule_version: Mapped[str] = mapped_column(String(20))
    first_run_id: Mapped[str] = mapped_column(String(32))
    last_run_id: Mapped[str] = mapped_column(String(32))


# ---------------------------------------------------------------------------------------------- Phase 05: test engineering

class TestDiscovery(Base):
    """One static scan for tests. Nothing here means a test ran or passed."""

    __tablename__ = "test_discoveries"
    __test__ = False  # not a pytest class
    __table_args__ = (Index("ix_test_discoveries_repo_started", "repository_id", "started_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24))  # completed | completed_with_warnings | failed
    framework: Mapped[str] = mapped_column(String(20), default="pytest")
    framework_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # which config file, python_files …
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_unparsable: Mapped[int] = mapped_column(Integer, default=0)
    tests_found: Mapped[int] = mapped_column(Integer, default=0)
    source_files_with_tests: Mapped[int] = mapped_column(Integer, default=0)
    source_files_total: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    excluded: Mapped[list[str]] = mapped_column(JSON, default=list)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TestFile(Base):
    __tablename__ = "test_files"
    __test__ = False
    __table_args__ = (UniqueConstraint("discovery_id", "rel_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    discovery_id: Mapped[str] = mapped_column(ForeignKey("test_discoveries.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), default="")
    framework: Mapped[str] = mapped_column(String(20), default="pytest")
    parse_status: Mapped[str] = mapped_column(String(16))  # ok | syntax_error | unreadable | too_large
    parse_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    test_count: Mapped[int] = mapped_column(Integer, default=0)
    # [{"id": "tests/test_a.py::TestX::test_y", "name": "test_y", "class": "TestX"|null, "line": 12, "parametrized": bool}]
    tests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{"path": "src/a.py", "via": "import"|"name", "confidence": "high"|"low"}]
    associated_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)


class TestGenerationRun(Base):
    __tablename__ = "test_generation_runs"
    __test__ = False
    __table_args__ = (Index("ix_test_generation_runs_repo_created", "repository_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # generated | validated | rejected | approved
    request_hash: Mapped[str] = mapped_column(String(64), default="")  # identical requests return the existing run
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{"path", "symbol"|null, "sha256"}]
    generator_version: Mapped[str] = mapped_column(String(20))
    tool_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    artifact_dir: Mapped[str] = mapped_column(String(300), default="")  # relative to the artifact root
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TestCandidate(Base):
    """One staged test file for one target. The file is what gets reviewed, approved (by hash) and run."""

    __tablename__ = "test_candidates"
    __test__ = False
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("test_generation_runs.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    target_path: Mapped[str] = mapped_column(String(1024))
    target_symbol: Mapped[str | None] = mapped_column(String(300), nullable=True)
    module: Mapped[str | None] = mapped_column(String(300), nullable=True)
    import_root: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # repo-relative; "" = repository root
    # Planned cases: [{"id","test_name","target_symbol","category","title","purpose","preconditions","input_strategy",
    #                  "expected","rationale","confidence","assumptions":[],"review_required","runnable"}]
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(8), default="low")
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    runnable_count: Mapped[int] = mapped_column(Integer, default=0)
    staged_path: Mapped[str] = mapped_column(String(300), default="")  # relative to the artifact root; "" = nothing staged
    content_sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12))  # generated | validated | rejected | approved
    validation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{"check","passed","detail"}]
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exported_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class TestExecutionRun(Base):
    """One approved execution of generated candidates or of the repository's existing suite."""

    __tablename__ = "test_execution_runs"
    __test__ = False
    __table_args__ = (
        Index("uq_test_execution_runs_one_active", "repository_id", unique=True,
              sqlite_where=text("status IN ('pending','running')"),
              postgresql_where=text("status IN ('pending','running')")),
        Index("ix_test_execution_runs_repo_created", "repository_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))  # generated | existing
    generation_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(12))  # pending running passed failed timed_out rejected error
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)  # why rejected / error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command: Mapped[list[str]] = mapped_column(JSON, default=list)  # argv with absolute paths replaced by placeholders
    environment_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tests_collected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    stdout_tail: Mapped[str] = mapped_column(Text, default="")
    stderr_tail: Mapped[str] = mapped_column(Text, default="")
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Coverage: not_requested | measured | no_data | tool_unavailable | failed
    coverage_status: Mapped[str] = mapped_column(String(16), default="not_requested")
    coverage_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    coverage_tool: Mapped[str | None] = mapped_column(String(40), nullable=True)
    coverage_branch: Mapped[bool] = mapped_column(Boolean, default=False)
    coverage_source: Mapped[list[str]] = mapped_column(JSON, default=list)  # explicit measured scope (repo-relative)
    lines_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_covered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    branches_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branches_covered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_hashes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    tool_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_dir: Mapped[str] = mapped_column(String(300), default="")
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CoverageFile(Base):
    __tablename__ = "coverage_files"
    __table_args__ = (UniqueConstraint("execution_run_id", "rel_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    execution_run_id: Mapped[str] = mapped_column(ForeignKey("test_execution_runs.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024))
    statements: Mapped[int] = mapped_column(Integer)
    covered: Mapped[int] = mapped_column(Integer)
    line_percent: Mapped[float] = mapped_column(Float)
    missing_lines: Mapped[list[list[int]]] = mapped_column(JSON, default=list)  # [[start, end], …] ranges
    branches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branches_covered: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ------------------------------------------------------------------------------------------------ Phase 06: debugging

class FailureReport(Base):
    """One distinct failure (identified by a stable fingerprint). Recurrences update this row; they do not add rows."""

    __tablename__ = "failure_reports"
    __table_args__ = (
        UniqueConstraint("repository_id", "fingerprint"),
        Index("ix_failure_reports_repo_last_seen", "repository_id", "last_seen_at"),
        Index("ix_failure_reports_repo_status", "repository_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))  # execution_run | junit_xml | manual
    category: Mapped[str] = mapped_column(String(20))  # assertion | exception | collection_error | test_error | unknown
    exception_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    test_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(String(1500), default="")  # sanitized
    frames_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = unknown; never invented
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | investigating | resolved | ignored
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_execution_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_execution_runs.id", ondelete="SET NULL"), nullable=True)
    localization_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class FailureFrame(Base):
    __tablename__ = "failure_frames"
    __table_args__ = (UniqueConstraint("failure_id", "idx"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    failure_id: Mapped[str] = mapped_column(ForeignKey("failure_reports.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)  # 0 = outermost call, last = where the exception surfaced
    rel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # only for repository files
    display_path: Mapped[str] = mapped_column(String(300), default="")  # always safe to show
    function: Mapped[str | None] = mapped_column(String(300), nullable=True)
    function_source: Mapped[str | None] = mapped_column(String(10), nullable=True)  # trace | index
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    col: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(12))  # repository | dependency | runtime | unknown
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class FailureOccurrence(Base):
    """Links a failure to an execution that produced it (or to a manual report)."""

    __tablename__ = "failure_occurrences"
    __table_args__ = (UniqueConstraint("failure_id", "execution_run_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    failure_id: Mapped[str] = mapped_column(ForeignKey("failure_reports.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    execution_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_execution_runs.id", ondelete="SET NULL"), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(20))


class LocalizationCandidate(Base):
    __tablename__ = "localization_candidates"
    __table_args__ = (UniqueConstraint("failure_id", "rank"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    failure_id: Mapped[str] = mapped_column(ForeignKey("failure_reports.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    rel_path: Mapped[str] = mapped_column(String(1024))
    symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    symbol_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(8))  # high | medium | low
    role: Mapped[str] = mapped_column(String(24))  # exception_origin | immediate_failure | caller_context | called_by_test | related
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    contradicting: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(String(1500), default="")
    analysis_version: Mapped[str] = mapped_column(String(20))
    snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        Index("ix_investigations_repo_updated", "repository_id", "updated_at"),
        Index("uq_investigations_one_open", "failure_id", unique=True,
              sqlite_where=text("status != 'closed'"), postgresql_where=text("status != 'closed'")),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    failure_id: Mapped[str] = mapped_column(ForeignKey("failure_reports.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | in_review | closed
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str | None] = mapped_column(String(24), nullable=True)  # user supplied
    generator_version: Mapped[str] = mapped_column(String(20))
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VerificationRun(Base):
    """A user-approved rerun linked to an investigation, plus the evaluated comparison with the original failure."""

    __tablename__ = "verification_runs"
    __table_args__ = (Index("ix_verification_runs_investigation", "investigation_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    failure_id: Mapped[str] = mapped_column(ForeignKey("failure_reports.id", ondelete="CASCADE"), index=True)
    execution_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_execution_runs.id", ondelete="SET NULL"), nullable=True)
    baseline_execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    test_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(String(40), default="PENDING")
    outcome_reason: Mapped[str] = mapped_column(String(500), default="")
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------------------------------------ Phase 07: code review

class CodeReview(Base):
    """One review of a concrete set of changes. Stores analysis output, never source text or diffs."""

    __tablename__ = "code_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','completed','completed_with_warnings','failed')", name="status"),
        Index("ix_code_reviews_repo_created", "repository_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16))  # revisions | staged | unstaged | working_tree
    status: Mapped[str] = mapped_column(String(24), default="pending")
    base_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    head_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = not applicable / not resolvable
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_head: Mapped[str | None] = mapped_column(String(64), nullable=True)  # HEAD when the changes were read
    selected_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    index_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    index_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    files_total: Mapped[int] = mapped_column(Integer, default=0)  # changed files git reported
    files_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    files_omitted: Mapped[int] = mapped_column(Integer, default=0)
    lines_added: Mapped[int] = mapped_column(Integer, default=0)
    lines_removed: Mapped[int] = mapped_column(Integer, default=0)
    symbols_changed: Mapped[int] = mapped_column(Integer, default=0)
    findings_total: Mapped[int] = mapped_column(Integer, default=0)
    risk_state: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ranked | unknown
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_category: Mapped[str | None] = mapped_column(String(12), nullable=True)
    risk: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommendations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generator_version: Mapped[str] = mapped_column(String(10), default="1.0")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewChange(Base):
    __tablename__ = "review_changes"
    __table_args__ = (UniqueConstraint("review_id", "rel_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    review_id: Mapped[str] = mapped_column(ForeignKey("code_reviews.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024))  # the new path (the old path for deletions)
    change_type: Mapped[str] = mapped_column(String(12))  # added | modified | deleted | renamed | unsupported
    old_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    new_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    old_blob: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_blob: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)  # of the content that was analysed
    language: Mapped[str] = mapped_column(String(40), default="Other")
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    is_binary: Mapped[bool] = mapped_column(Boolean, default=False)
    lines_added: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_removed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_ranges: Mapped[list[list[int]]] = mapped_column(JSON, default=list)  # changed line ranges in the new revision
    old_ranges: Mapped[list[list[int]]] = mapped_column(JSON, default=list)
    extraction_status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | binary | unsupported | too_large | not_read | mode_only
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)


class ReviewChangedSymbol(Base):
    __tablename__ = "review_changed_symbols"
    __table_args__ = (Index("ix_review_changed_symbols_review", "review_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    review_id: Mapped[str] = mapped_column(ForeignKey("code_reviews.id", ondelete="CASCADE"))
    change_id: Mapped[str] = mapped_column(ForeignKey("review_changes.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024))
    symbol_stable_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    qualified_name: Mapped[str] = mapped_column(String(1200))
    kind: Mapped[str] = mapped_column(String(20))
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    change_kind: Mapped[str] = mapped_column(String(10))  # added | modified | removed
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    signature_old: Mapped[str | None] = mapped_column(String(300), nullable=True)
    signature_new: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mapping: Mapped[str] = mapped_column(String(16))  # index | fresh_parse
    complexity: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ReviewFinding(Base):
    __tablename__ = "review_findings"
    __table_args__ = (
        UniqueConstraint("review_id", "fingerprint"),
        CheckConstraint("severity IN ('critical','high','medium','low','info')", name="severity"),
        CheckConstraint("status IN ('open','accepted','dismissed','follow_up')", name="status"),
        CheckConstraint("confidence IN ('high','medium','low')", name="confidence"),
        Index("ix_review_findings_review_path", "review_id", "rel_path"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    review_id: Mapped[str] = mapped_column(ForeignKey("code_reviews.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(40))  # identical for the same issue across reviews
    stable_id: Mapped[str] = mapped_column(String(16))
    rule_id: Mapped[str] = mapped_column(String(40))
    analyzer_id: Mapped[str] = mapped_column(String(40))
    rel_path: Mapped[str] = mapped_column(String(1024))
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)  # lines in the NEW revision; None = whole file / deleted file
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(300), default="")
    severity: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    explanation: Mapped[str] = mapped_column(String(2000))
    evidence: Mapped[str] = mapped_column(String(1500))
    remediation: Mapped[str] = mapped_column(String(1000))
    limitations: Mapped[str] = mapped_column(String(600), default="")
    confidence: Mapped[str] = mapped_column(String(8))
    actionable: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(12))  # analyzer | heuristic
    ai_assisted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="open")
    rationale: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_changed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inherited_from_review_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # finding_ids, failure_ids, tests
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------------------------------------ Phase 08: repair

REPAIR_STATUSES = ("open", "analyzed", "proposed", "patched", "applied", "verified", "verification_failed", "closed")
PATCH_STATUSES = ("draft", "validated", "invalid", "awaiting_approval", "approved", "rejected", "applying", "applied",
                  "verification_failed", "verified")


class RepairInvestigation(Base):
    """A defect to repair: its evidence, a proposal and the patches/verifications that follow. Holds no source text."""

    __tablename__ = "repair_investigations"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(REPAIR_STATUSES) + "')", name="status"),
        CheckConstraint("severity IN ('critical','high','medium','low','info')", name="severity"),
        Index("ix_repair_inv_repo_created", "repository_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(24), default="open")
    source_kind: Mapped[str] = mapped_column(String(24), default="manual")  # manual | failure | finding | review_finding | debug_investigation
    failure_id: Mapped[str | None] = mapped_column(ForeignKey("failure_reports.id", ondelete="SET NULL"), nullable=True)
    finding_id: Mapped[str | None] = mapped_column(ForeignKey("findings.id", ondelete="SET NULL"), nullable=True)
    review_finding_id: Mapped[str | None] = mapped_column(ForeignKey("review_findings.id", ondelete="SET NULL"), nullable=True)
    debug_investigation_id: Mapped[str | None] = mapped_column(ForeignKey("investigations.id", ondelete="SET NULL"), nullable=True)
    hint_files: Mapped[list[str]] = mapped_column(JSON, default=list)  # stated by the requester; treated as a hint, not a fact
    relevant_files: Mapped[list[str]] = mapped_column(JSON, default=list)  # files a patch may touch without being flagged
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_assembled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    root_cause: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposal_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline_execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verification_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)  # latest: PASSED | FAILED | INCOMPLETE | BLOCKED | PENDING
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepairPatch(Base):
    """A proposed change as a unified diff, bound to the exact content it was made against.

    Phase 08 deliberately stores the diff text and the original content of the patched files (needed for rollback and audit).
    """

    __tablename__ = "repair_patches"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(PATCH_STATUSES) + "')", name="status"),
        CheckConstraint("source IN ('generated','manual','ai_assisted')", name="source"),
        Index("ix_repair_patch_inv", "investigation_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("repair_investigations.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(10))
    rule_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    diff_text: Mapped[str] = mapped_column(Text)
    patch_sha256: Mapped[str] = mapped_column(String(64))
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # path, change_type, base_sha256, result_sha256, added, removed
    originals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # path -> original text (None for created files)
    base_head: Mapped[str | None] = mapped_column(String(64), nullable=True)  # git HEAD when the patch was bound, if available
    extra_files: Mapped[list[str]] = mapped_column(JSON, default=list)  # files the requester explicitly allowed beyond the investigation's
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    applied_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    apply_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RepairVerification(Base):
    """One verification attempt for an applied patch. Test execution itself is a Phase 05 TestExecutionRun."""

    __tablename__ = "repair_verifications"
    __table_args__ = (
        CheckConstraint("outcome IN ('PENDING','PASSED','FAILED','INCOMPLETE','BLOCKED')", name="outcome"),
        Index("ix_repair_verif_patch", "patch_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    patch_id: Mapped[str] = mapped_column(ForeignKey("repair_patches.id", ondelete="CASCADE"))
    investigation_id: Mapped[str] = mapped_column(ForeignKey("repair_investigations.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    execution_run_id: Mapped[str | None] = mapped_column(ForeignKey("test_execution_runs.id", ondelete="SET NULL"), nullable=True)
    baseline_execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_tests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{target, reason}]
    outcome: Mapped[str] = mapped_column(String(12), default="PENDING")
    outcome_reason: Mapped[str] = mapped_column(String(600), default="")
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    static_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------------------------- Phase 09: refactoring
REFACTOR_STATUSES = ("planned", "blocked", "generated", "invalid", "validated", "awaiting_approval", "approved", "rejected", "applying", "applied",
                     "verification_failed", "verified", "analyzed", "closed")


class RefactorTransformation(Base):
    """One refactoring request: its plan, impact, generated multi-file diff and lifecycle.

    The diff is applied with the Phase 08 patch engine; verification is a Phase 05 test run referenced from ``RefactorVerification``.
    Analysis-only requests (duplicate detection, conditional simplification) never get a diff and end in ``analyzed``.
    """

    __tablename__ = "refactor_transformations"
    __table_args__ = (
        Index("ix_refactor_repo_created", "repository_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    type_id: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(24), default="planned")
    support_level: Mapped[str] = mapped_column(String(20), default="applicable")  # applicable | proposal_only
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diff_text: Mapped[str] = mapped_column(Text, default="")
    patch_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{path, change_type, base_sha256, result_sha256, added, removed}]
    originals: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    applied_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    apply_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline_execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    baseline_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    verification_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RefactorVerification(Base):
    """Verification attempt for an applied transformation. Test execution itself is a Phase 05 TestExecutionRun."""

    __tablename__ = "refactor_verifications"
    __table_args__ = (
        CheckConstraint("outcome IN ('PENDING','PASSED','FAILED','INCOMPLETE','BLOCKED')", name="refactor_outcome"),
        Index("ix_refactor_verif_tx", "transformation_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    transformation_id: Mapped[str] = mapped_column(ForeignKey("refactor_transformations.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    execution_run_id: Mapped[str | None] = mapped_column(ForeignKey("test_execution_runs.id", ondelete="SET NULL"), nullable=True)
    baseline_execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_tests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(String(12), default="PENDING")
    outcome_reason: Mapped[str] = mapped_column(String(600), default="")
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    static_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------------------------------------ Phase 10: continuous verification

class CvPolicy(Base):
    """One immutable version of a repository's quality-gate policy. Runs snapshot the version they used."""

    __tablename__ = "cv_policies"
    __table_args__ = (UniqueConstraint("repository_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CvBaseline(Base):
    """A recorded test result for a known source state, used to classify later failures."""

    __tablename__ = "cv_baselines"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the cv_run that produced it
    execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tree_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"kind": "all"|"files", "files": [...]}
    failing: Mapped[list[str]] = mapped_column(JSON, default=list)
    totals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    integrity_sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(12), default="valid")  # valid | superseded
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CvRun(Base):
    """A verification run (or a baseline-capture run) bound to one source state."""

    __tablename__ = "cv_runs"
    __table_args__ = (
        Index("uq_cv_runs_one_active", "repository_id", unique=True,
              sqlite_where=text("status IN ('queued','running')"), postgresql_where=text("status IN ('queued','running')")),
        Index("ix_cv_runs_repo_created", "repository_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12), default="verify")  # verify | baseline
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued running completed cancelled error
    mode: Mapped[str] = mapped_column(String(10), default="focused")
    source: Mapped[str] = mapped_column(String(14), default="working_tree")
    change_set: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tree_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    index_snapshot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version: Mapped[int] = mapped_column(Integer, default=0)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    gate_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)  # PASS FAIL BLOCKED INCOMPLETE
    gate_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    regression: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    static_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    link: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"type": "repair"|"refactor"|"review", "id": ...}
    reason: Mapped[str] = mapped_column(String(500), default="")
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CvCheck(Base):
    """One planned check of a run, with its reason, requirement level and (later) its result."""

    __tablename__ = "cv_checks"
    __table_args__ = (UniqueConstraint("run_id", "check_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("cv_runs.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    check_id: Mapped[str] = mapped_column(String(40))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[int] = mapped_column(Integer, default=0)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(String(600), default="")
    confidence: Mapped[str] = mapped_column(String(8), default="medium")
    prerequisites: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(14), default="planned")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CvEvent(Base):
    __tablename__ = "cv_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("cv_runs.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- CI/CD integration (Phase 11) ----


class CiConnection(Base):
    """A provider connection for one repository. Secrets are never stored: only the *names* of environment variables that hold them."""

    __tablename__ = "ci_connections"
    __table_args__ = (UniqueConstraint("repository_id", "provider", "external_repo"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))  # local | github
    external_repo: Mapped[str] = mapped_column(String(200), default="")  # "owner/name" for github, "" for local
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    enabled_events: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_env: Mapped[str] = mapped_column(String(64), default="")
    webhook_secret_env: Mapped[str] = mapped_column(String(64), default="")
    status_context: Mapped[str] = mapped_column(String(60), default="nova/verification")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(14), default="unconfigured")  # unconfigured | validated | error
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiReadinessPolicy(Base):
    __tablename__ = "ci_readiness_policies"
    __table_args__ = (UniqueConstraint("repository_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeRequest(Base):
    """A normalized pull request / merge request / local branch. Never contains diffs or source text."""

    __tablename__ = "change_requests"
    __table_args__ = (UniqueConstraint("repository_id", "provider", "external_id"), Index("ix_cr_repo_updated", "repository_id", "updated_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("ci_connections.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(200))
    source_branch: Mapped[str] = mapped_column(String(255), default="")
    target_branch: Mapped[str] = mapped_column(String(255), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(String(4000), default="")
    author: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(10), default="open")  # open | closed | merged
    changed_files: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    files_source: Mapped[str] = mapped_column(String(10), default="unknown")  # git | provider | unknown
    files_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    head_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiWorkflowRun(Base):
    """One verification workflow bound to the exact head revision of a change request."""

    __tablename__ = "ci_workflow_runs"
    __table_args__ = (UniqueConstraint("repository_id", "idempotency_key"), Index("ix_ciwf_cr", "change_request_id", "created_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    change_request_id: Mapped[str] = mapped_column(ForeignKey("change_requests.id", ondelete="CASCADE"))
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), default="focused")
    trigger: Mapped[str] = mapped_column(String(12), default="manual")  # manual | event
    idempotency_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(12), default="queued")  # awaiting | queued | running | completed | cancelled | error
    cv_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)
    readiness: Mapped[str | None] = mapped_column(String(12), nullable=True)  # READY NOT_READY BLOCKED INCOMPLETE
    readiness_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    readiness_policy_version: Mapped[int] = mapped_column(Integer, default=0)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(String(500), default="")
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CiStatusReport(Base):
    """The intent to report (and the result of reporting) a verification outcome to an external provider."""

    __tablename__ = "ci_status_reports"
    __table_args__ = (UniqueConstraint("workflow_run_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    workflow_run_id: Mapped[str] = mapped_column(ForeignKey("ci_workflow_runs.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(16))
    head_sha: Mapped[str] = mapped_column(String(64))
    context: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(10))  # provider state: success failure error pending
    description: Mapped[str] = mapped_column(String(140), default="")
    nova_gate: Mapped[str | None] = mapped_column(String(12), nullable=True)
    nova_readiness: Mapped[str | None] = mapped_column(String(12), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending sent unconfigured retry_scheduled failed_permanent skipped_stale
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_class: Mapped[str] = mapped_column(String(40), default="")
    response_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(500), default="")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiWebhookDelivery(Base):
    __tablename__ = "ci_webhook_deliveries"
    __table_args__ = (UniqueConstraint("connection_id", "delivery_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("ci_connections.id", ondelete="CASCADE"), index=True)
    delivery_id: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(40), default="")
    outcome: Mapped[str] = mapped_column(String(24))  # accepted | ignored | stale_event
    change_request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiReport(Base):
    """A generated report, stored with its hash so it can be shown to be reproducible from the records it cites."""

    __tablename__ = "ci_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    change_request_id: Mapped[str] = mapped_column(ForeignKey("change_requests.id", ondelete="CASCADE"), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(10))  # review | release
    head_sha: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    markdown: Mapped[str] = mapped_column(String, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- Security engineering and supply-chain intelligence (Phase 12) ----


class SecScan(Base):
    __tablename__ = "sec_scans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | completed | failed
    scan_types: Mapped[list[str]] = mapped_column(JSON, default=list)  # dependencies | code | secrets
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    source_digest: Mapped[str] = mapped_column(String(64), default="")
    inventory_digest: Mapped[str] = mapped_column(String(64), default="")
    tool_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(500), default="")
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecComponent(Base):
    __tablename__ = "sec_components"
    __table_args__ = (Index("ix_sec_components_scan_pkg", "scan_id", "ecosystem", "name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scan_id: Mapped[str] = mapped_column(ForeignKey("sec_scans.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    ecosystem: Mapped[str] = mapped_column(String(16))  # PyPI | npm
    name: Mapped[str] = mapped_column(String(214))
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_state: Mapped[str] = mapped_column(String(12), default="pinned")  # pinned | range | unpinned | unparseable
    relationship_kind: Mapped[str] = mapped_column(String(10), default="direct")  # direct | transitive
    scope: Mapped[str] = mapped_column(String(10), default="runtime")  # runtime | dev | optional | unknown
    manifests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    declared: Mapped[list[str]] = mapped_column(JSON, default=list)
    paths: Mapped[list[list[str]]] = mapped_column(JSON, default=list)
    license: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    assessment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # advisory coverage for this component


class SecAdvisory(Base):
    """Source-attributed advisory data, scoped to a workspace so one tenant's imported data never influences another's scans."""

    __tablename__ = "sec_advisories"
    __table_args__ = (UniqueConstraint("workspace_id", "provider", "advisory_id", "ecosystem", "package"),
                      Index("ix_sec_advisories_pkg", "workspace_id", "ecosystem", "package"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    advisory_id: Mapped[str] = mapped_column(String(100))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    ecosystem: Mapped[str] = mapped_column(String(16))
    package: Mapped[str] = mapped_column(String(214))
    summary: Mapped[str] = mapped_column(String(500), default="")
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    versions: Mapped[list[str]] = mapped_column(JSON, default=list)
    fixed: Mapped[list[str]] = mapped_column(JSON, default=list)
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)
    score: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scoring_system: Mapped[str | None] = mapped_column(String(20), nullable=True)
    references: Mapped[list[str]] = mapped_column(JSON, default=list)
    published: Mapped[str | None] = mapped_column(String(40), nullable=True)
    modified: Mapped[str | None] = mapped_column(String(40), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    uncertainty: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_sha256: Mapped[str] = mapped_column(String(64), default="")


class SecFinding(Base):
    __tablename__ = "sec_findings"
    __table_args__ = (UniqueConstraint("repository_id", "fingerprint"), Index("ix_sec_findings_state", "repository_id", "status", "severity"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))  # code | secret | dependency
    rule_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1500), default="")
    severity: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[str] = mapped_column(String(8))
    evidence_quality: Mapped[str] = mapped_column(String(14), default="unknown")
    rel_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(200), default="")
    component_key: Mapped[str | None] = mapped_column(String(240), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    remediation: Mapped[str] = mapped_column(String(1500), default="")
    references: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(14), default="open")  # open | resolved | false_positive | accepted_risk | suppressed
    resolution_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    priority: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    suppression_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_scan_id: Mapped[str] = mapped_column(String(32))
    last_seen_scan_id: Mapped[str] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecEvent(Base):
    """Append-only history for findings, suppressions, exceptions, policies and remediations."""

    __tablename__ = "sec_events"
    __table_args__ = (Index("ix_sec_events_entity", "repository_id", "entity_type", "entity_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecSuppression(Base):
    __tablename__ = "sec_suppressions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(14))  # finding | rule_in_path
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    path_glob: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(300), default="")


class SecException(Base):
    """A policy exception. It never hides a finding: the policy evaluation lists every exception it applied."""

    __tablename__ = "sec_exceptions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    selector: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type: finding|advisory|package|rule, value}
    reason: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SecPolicy(Base):
    __tablename__ = "sec_policies"
    __table_args__ = (UniqueConstraint("repository_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecEvaluation(Base):
    __tablename__ = "sec_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("sec_scans.id", ondelete="CASCADE"), index=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(14))  # pass | fail | indeterminate
    decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecRemediation(Base):
    __tablename__ = "sec_remediations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("sec_findings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # dependency | code
    status: Mapped[str] = mapped_column(String(18), default="proposed")  # proposed | cannot_fix | validated | validation_failed | submitted | rejected
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diff_text: Mapped[str] = mapped_column(String, default="")
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    investigation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    patch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecReport(Base):
    __tablename__ = "sec_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("sec_scans.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(10))  # json | markdown | html | csv
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- Operational intelligence (Phase 13) ----------------
class OpsConfig(Base):
    """Versioned alert-rule / SLO definitions. Old versions are preserved; the highest version per (kind, key) is current."""
    __tablename__ = "ops_configs"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "key", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # alert_rule | slo
    key: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OpsIncident(Base):
    """One row per deterministic fingerprint; recurrence reopens it. Lifecycle history lives in ops_events."""
    __tablename__ = "ops_incidents"
    __table_args__ = (UniqueConstraint("workspace_id", "fingerprint"), Index("ix_ops_incidents_status", "workspace_id", "status"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(12))  # alert | event
    rule_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    component: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(14), default="open")  # open | acknowledged | investigating | resolved | reopened
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpsEvent(Base):
    """Append-only reliability events (lifecycle changes, recorded deployments / config changes, notifications)."""
    __tablename__ = "ops_events"
    __table_args__ = (Index("ix_ops_events_type_time", "workspace_id", "event_type", "occurred_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("ops_incidents.id", ondelete="CASCADE"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="info")
    component: Mapped[str] = mapped_column(String(40), default="platform")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class OpsEvaluation(Base):
    __tablename__ = "ops_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[str] = mapped_column(String(60), index=True)
    rule_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # firing | ok | no_data | disabled
    observed: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    incident_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OpsDelivery(Base):
    __tablename__ = "ops_deliveries"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", "attempt"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    incident_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(12))  # webhook | slack
    destination: Mapped[str] = mapped_column(String(120))  # host only; never the full URL or secret
    idempotency_key: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12))  # delivered | failed | blocked | skipped
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
