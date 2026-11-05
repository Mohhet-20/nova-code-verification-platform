"""Enterprise governance records (teams, ownership, policies, workflows, review queue).

The tenant (organization) is the existing ``Workspace``; org roles live on ``Membership.role``. Records described as
append-only (policy versions, evaluations, workflow events/decisions, queue history, audit events) are protected by
database triggers on SQLite (migration 0015); other databases need equivalent triggers or privileges.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, new_id, utcnow


def _ts(index: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, index=index)


class UserToken(Base):
    """Per-user API credential. Only a SHA-256 digest is stored; the plaintext is shown once at creation."""

    __tablename__ = "user_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("workspace_id", "name"), CheckConstraint("status IN ('active','archived')", name="status_valid"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(10), default="active")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),
                      CheckConstraint("role IN ('admin','member','reviewer')", name="role_valid"),
                      CheckConstraint("status IN ('active','removed')", name="status_valid"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10), default="member")
    status: Mapped[str] = mapped_column(String(10), default="active")
    added_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepositoryOwnership(Base):
    """Explicit owner of a repository. A repository with an owning team is visible only to that team and org-wide roles."""

    __tablename__ = "repository_ownership"
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    accountable_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_at: Mapped[datetime] = _ts()


class GovernancePolicy(Base):
    __tablename__ = "governance_policies"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),
                      CheckConstraint("severity IN ('informational','warning','blocking')", name="severity_valid"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(1000), default="")
    policy_type: Mapped[str] = mapped_column(String(40))
    resource_types: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = every supported type
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True)
    severity: Mapped[str] = mapped_column(String(14), default="blocking")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GovernancePolicyVersion(Base):
    """Immutable snapshot of every policy version (append-only)."""

    __tablename__ = "governance_policy_versions"
    __table_args__ = (UniqueConstraint("policy_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    policy_id: Mapped[str] = mapped_column(ForeignKey("governance_policies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    changed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _ts()


class PolicyEvaluation(Base):
    """Recorded evaluation result (append-only); keeps the policy version it was produced under."""

    __tablename__ = "policy_evaluations"
    __table_args__ = (Index("ix_policy_evaluations_instance", "instance_id", "evaluated_at"),
                      Index("ix_policy_evaluations_resource", "workspace_id", "resource_type", "resource_id"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("governance_policies.id", ondelete="CASCADE"), index=True)
    policy_key: Mapped[str] = mapped_column(String(60))
    policy_version: Mapped[int] = mapped_column(Integer)
    policy_type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(14))
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_instances.id", ondelete="CASCADE"), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(14))  # pass | fail | warning | unavailable | not_applicable
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    trigger: Mapped[str] = mapped_column(String(30))
    evaluated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    evaluated_at: Mapped[datetime] = _ts()


class WorkflowDefinition(Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (UniqueConstraint("workspace_id", "key", "version"),
                      Index("uq_workflow_definitions_one_active", "workspace_id", "key", unique=True,
                            sqlite_where=text("active = 1"), postgresql_where=text("active = true")))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(1000), default="")
    resource_type: Mapped[str] = mapped_column(String(30), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)  # validated WorkflowSpec (stages, mode, policies, ...)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()


STATES = ("DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED", "COMPLETED", "CANCELLED")


class WorkflowInstance(Base):
    __tablename__ = "workflow_instances"
    __table_args__ = (
        CheckConstraint("state IN ('DRAFT','SUBMITTED','IN_REVIEW','CHANGES_REQUESTED','APPROVED','REJECTED','COMPLETED','CANCELLED')", name="state_valid"),
        # at most one live (non-terminal) instance per governed resource
        Index("uq_workflow_instances_one_live", "workspace_id", "resource_type", "resource_id", unique=True,
              sqlite_where=text("state IN ('DRAFT','SUBMITTED','IN_REVIEW','CHANGES_REQUESTED','APPROVED')"),
              postgresql_where=text("state IN ('DRAFT','SUBMITTED','IN_REVIEW','CHANGES_REQUESTED','APPROVED')")),
        Index("ix_workflow_instances_state", "workspace_id", "state", "created_at"),
        Index("ix_workflow_instances_team", "team_id", "state"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    definition_id: Mapped[str] = mapped_column(ForeignKey("workflow_definitions.id", ondelete="RESTRICT"))
    definition_version: Mapped[int] = mapped_column(Integer)
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)  # frozen copy: later definition edits never change this instance
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(32))
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="DRAFT")
    round: Mapped[int] = mapped_column(Integer, default=1)  # incremented on resubmission after CHANGES_REQUESTED
    stage_index: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[str | None] = mapped_column(String(12), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resource_author: Mapped[str | None] = mapped_column(String(32), nullable=True)  # user id of the resource creator, when known
    row_version: Mapped[int] = mapped_column(Integer, default=1)  # compare-and-swap guard for concurrent decisions
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # ORM optimistic locking: every UPDATE is "... WHERE row_version = <value read>"; a concurrent writer gets StaleDataError.
    __mapper_args__ = {"version_id_col": row_version}


class WorkflowDecision(Base):
    """One decision per (instance, round, stage, user) - enforced by the database (append-only)."""

    __tablename__ = "workflow_decisions"
    __table_args__ = (UniqueConstraint("instance_id", "round", "stage_index", "user_id"),
                      CheckConstraint("decision IN ('approve','reject','request_changes','abstain')", name="decision_valid"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(ForeignKey("workflow_instances.id", ondelete="CASCADE"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    stage_index: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))
    justification: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts()


class WorkflowEvent(Base):
    """Append-only transition/audit trail for an instance."""

    __tablename__ = "workflow_events"
    __table_args__ = (Index("ix_workflow_events_instance", "instance_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(ForeignKey("workflow_instances.id", ondelete="CASCADE"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(40))
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class ReviewQueueItem(Base):
    __tablename__ = "review_queue_items"
    __table_args__ = (
        CheckConstraint("status IN ('open','done','cancelled')", name="status_valid"),
        CheckConstraint("priority IN ('low','normal','high','urgent')", name="priority_valid"),
        UniqueConstraint("instance_id"),
        Index("ix_review_queue_status", "workspace_id", "status", "priority", "created_at"),
        Index("ix_review_queue_assignee", "assignee_user_id", "status"),
        Index("ix_review_queue_team", "team_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_instances.id", ondelete="CASCADE"), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(32))
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(500))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    risk_level: Mapped[str | None] = mapped_column(String(12), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="open")
    assignee_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewQueueHistory(Base):
    """Append-only assignment/status history of a queue item."""

    __tablename__ = "review_queue_history"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("review_queue_items.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20))  # created | assigned | claimed | released | reassigned | closed | cancelled
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    from_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _ts()
