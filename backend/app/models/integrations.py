"""Phase 16: enterprise integrations (installations, credentials, inbound events, CI evidence, outbound webhooks, jobs, idempotency, links).

The tenant is the existing ``Workspace``. Secrets are stored only as authenticated ciphertext in ``integration_secrets``; every other table
holds identifiers and sanitized summaries, never credentials or full external payloads.
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


def _opt_ts() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=True)


def _ws() -> Mapped[str]:
    return mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)


def _user() -> Mapped[str | None]:
    return mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class IntegrationInstallation(Base):
    __tablename__ = "integration_installations"
    __table_args__ = (
        CheckConstraint("connection_status IN ('unknown','connected','failing')", name="connection_status_valid"),
        Index("uq_integration_installations_name", "workspace_id", "name", unique=True, sqlite_where=text("removed_at IS NULL"), postgresql_where=text("removed_at IS NULL")),
        Index("ix_integration_installations_type", "workspace_id", "integration_type"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    integration_type: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # validated, non-secret
    credential_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)  # "secret:<id>" or "env:<NAME>"; never the credential itself
    requested_permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    granted_permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    connection_status: Mapped[str] = mapped_column(String(10), default="unknown")
    last_checked_at: Mapped[datetime | None] = _opt_ts()
    last_success_at: Mapped[datetime | None] = _opt_ts()
    last_sync_at: Mapped[datetime | None] = _opt_ts()
    last_error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    webhook_secret_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = _user()
    updated_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    removed_at: Mapped[datetime | None] = _opt_ts()


class IntegrationSecret(Base):
    """Authenticated-encryption envelope. Rows are never updated except to set ``revoked_at``; a replacement is a new row."""

    __tablename__ = "integration_secrets"
    __table_args__ = (Index("ix_integration_secrets_owner", "owner_type", "owner_id"),
                      CheckConstraint("owner_type IN ('installation','installation_webhook','subscription')", name="owner_type_valid"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    owner_type: Mapped[str] = mapped_column(String(24))
    owner_id: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = _opt_ts()


class IntegrationEvent(Base):
    """Inbound provider delivery. Stores identifiers and a minimal normalized summary, never the raw payload."""

    __tablename__ = "integration_events"
    __table_args__ = (
        UniqueConstraint("installation_id", "delivery_id"),
        CheckConstraint("status IN ('accepted','processed','ignored','failed')", name="status_valid"),
        Index("ix_integration_events_installation", "installation_id", "received_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    installation_id: Mapped[str] = mapped_column(ForeignKey("integration_installations.id", ondelete="CASCADE"))
    delivery_id: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(10), default="accepted")
    repository_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_ref: Mapped[str] = mapped_column(String(200), default="")
    normalized: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = _ts()
    processed_at: Mapped[datetime | None] = _opt_ts()


class CiEvidenceRun(Base):
    """External CI run recorded as *evidence*. It never sets a NOVA approval or release state; policies decide what it satisfies."""

    __tablename__ = "ci_evidence_runs"
    __table_args__ = (
        UniqueConstraint("installation_id", "source", "external_run_id"),
        CheckConstraint("status IN ('pending','success','failed','cancelled','unknown')", name="status_valid"),
        Index("ix_ci_evidence_runs_commit", "workspace_id", "repository_id", "head_sha", "received_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    installation_id: Mapped[str] = mapped_column(ForeignKey("integration_installations.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(30))
    external_run_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120), default="")
    head_sha: Mapped[str] = mapped_column(String(40))
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="unknown")
    external_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    started_at: Mapped[datetime | None] = _opt_ts()
    completed_at: Mapped[datetime | None] = _opt_ts()
    received_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("uq_webhook_subscriptions_name", "workspace_id", "name", unique=True, sqlite_where=text("removed_at IS NULL"), postgresql_where=text("removed_at IS NULL")),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list[str]] = mapped_column(JSON, default=list)
    secret_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_delivery_at: Mapped[datetime | None] = _opt_ts()
    created_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    removed_at: Mapped[datetime | None] = _opt_ts()


class OutboxEvent(Base):
    """Logical event, persisted in the same transaction as the change that caused it. ``(workspace_id, dedupe_key)`` is unique."""

    __tablename__ = "outbox_events"
    __table_args__ = (UniqueConstraint("workspace_id", "dedupe_key"), Index("ix_outbox_events_created", "workspace_id", "created_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    event_type: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(120))
    fanned_out_at: Mapped[datetime | None] = _opt_ts()
    created_at: Mapped[datetime] = _ts()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id"),
        CheckConstraint("state IN ('pending','in_progress','retrying','succeeded','failed')", name="state_valid"),
        Index("ix_webhook_deliveries_due", "state", "next_attempt_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    subscription_id: Mapped[str] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(12), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = _ts()
    lease_until: Mapped[datetime | None] = _opt_ts()
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_attempt_at: Mapped[datetime | None] = _opt_ts()
    completed_at: Mapped[datetime | None] = _opt_ts()
    replayed_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()


class WebhookDeliveryAttempt(Base):
    __tablename__ = "webhook_delivery_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("webhook_deliveries.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = _ts()
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_excerpt: Mapped[str] = mapped_column(String(300), default="")


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('queued','running','succeeded','failed','dead')", name="state_valid"),
        Index("ix_integration_jobs_due", "state", "scheduled_at"),
        Index("uq_integration_jobs_live_dedupe", "workspace_id", "dedupe_key", unique=True, sqlite_where=text("state IN ('queued','running')"),
              postgresql_where=text("state IN ('queued','running')")),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    installation_id: Mapped[str | None] = mapped_column(ForeignKey("integration_installations.id", ondelete="CASCADE"), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(40))
    input_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # identifiers only; never credentials
    dedupe_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(10), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    scheduled_at: Mapped[datetime] = _ts()
    lease_until: Mapped[datetime | None] = _opt_ts()
    started_at: Mapped[datetime | None] = _opt_ts()
    finished_at: Mapped[datetime | None] = _opt_ts()
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()


class IdempotencyRecord(Base):
    """Replay protection for retryable writes. Scoped to (organization, user, route); never an authentication credential."""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", "scope", "key"), Index("ix_idempotency_records_expires", "expires_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    user_id: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null while the first request is still in flight
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalLink(Base):
    __tablename__ = "external_links"
    __table_args__ = (
        UniqueConstraint("installation_id", "external_id"),
        CheckConstraint("origin IN ('created','linked')", name="origin_valid"),
        Index("uq_external_links_one_created", "installation_id", "nova_type", "nova_id", unique=True, sqlite_where=text("origin = 'created'"), postgresql_where=text("origin = 'created'")),
        Index("ix_external_links_nova", "workspace_id", "nova_type", "nova_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    installation_id: Mapped[str] = mapped_column(ForeignKey("integration_installations.id", ondelete="CASCADE"))
    nova_type: Mapped[str] = mapped_column(String(40))
    nova_id: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(120))
    external_key: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    external_state: Mapped[str] = mapped_column(String(20), default="")
    origin: Mapped[str] = mapped_column(String(8), default="linked")
    last_synced_at: Mapped[datetime | None] = _opt_ts()
    created_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()


class ExtensionActivation(Base):
    __tablename__ = "extension_activations"
    __table_args__ = (UniqueConstraint("workspace_id", "extension_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = _ws()
    extension_id: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    activated_by: Mapped[str | None] = _user()
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
