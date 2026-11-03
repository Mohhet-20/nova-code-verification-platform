from __future__ import annotations

from functools import lru_cache

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import APP_VERSION, BACKEND_DIR
from app.db.session import get_engine

router = APIRouter(tags=["health"])


@lru_cache(maxsize=1)
def _migration_head() -> str | None:
    """The head revision is fixed for the lifetime of the process (it comes from the migration files shipped with the code); parsing every
    migration file on each readiness probe cost ~30 ms per call."""
    return ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok", "version": APP_VERSION}


@router.get("/ready")
def ready(response: Response) -> dict[str, object]:
    """Readiness: the database is reachable and migrated to the latest revision."""
    checks: dict[str, object] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            current = MigrationContext.configure(conn).get_current_revision()
        head = _migration_head()
        checks["migrations"] = "ok" if current == head else "pending"
        checks["revision"] = current
    except Exception as exc:
        checks.setdefault("database", f"error:{type(exc).__name__}")
    ok = checks.get("database") == "ok" and checks.get("migrations") == "ok"
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "checks": checks}
