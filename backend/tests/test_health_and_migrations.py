from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from fastapi.testclient import TestClient

from tests.conftest import alembic_config

EXPECTED_TABLES = {"workspaces", "users", "memberships", "repositories", "index_snapshots",
                   "indexed_files", "tasks", "task_plans", "audit_events", "alembic_version"}


def test_fresh_database_migrates_and_downgrades(db_url: str, tmp_path: Path) -> None:
    command.upgrade(alembic_config(db_url), "head")
    with sqlite3.connect(tmp_path / "nova-test.db") as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert EXPECTED_TABLES <= tables
    command.downgrade(alembic_config(db_url), "base")
    with sqlite3.connect(tmp_path / "nova-test.db") as conn:
        left = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert not (EXPECTED_TABLES - {"alembic_version"}) & left


def test_models_match_migration(migrated_url: str) -> None:
    """No pending autogenerate differences between ORM models and the migration."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.db.base import Base
    from app.db.session import get_engine

    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_health_does_not_need_database(db_url: str) -> None:
    from app.main import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ready_ok_when_migrated(client: TestClient) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["checks"]["migrations"] == "ok"


def test_ready_reports_pending_migrations(db_url: str) -> None:
    from app.main import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/v1/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
