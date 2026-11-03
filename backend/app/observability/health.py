"""Dependency health. Liveness stays in api/health.py (no dependencies); readiness there checks DB + migrations.

Here: per-dependency state with latency and a safe error class, probe timeouts, a short TTL cache so probes are never run per request,
and a critical/optional distinction (degraded optional != unavailable critical).
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import BACKEND_DIR, Settings
from app.observability import metrics as m

_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nova-probe")


def _db() -> dict[str, Any]:
    from app.db.session import get_engine

    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"detail": "reachable"}


def _migrations() -> dict[str, Any]:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    if current != head:
        raise RuntimeError("migrations_pending")
    return {"detail": f"at head {current}"}


def _storage(settings: Settings) -> dict[str, Any]:
    root = Path(settings.artifact_dir) if settings.artifact_dir else BACKEND_DIR / "data" / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    if not os.access(root, os.W_OK):
        raise PermissionError("artifact_dir_not_writable")
    return {"detail": "artifact directory writable"}


def _workers() -> dict[str, Any]:
    # Jobs run in-process (FastAPI BackgroundTasks); there is no external worker to probe. Report the in-flight count honestly.
    inflight = sum(m.ACTIVE_JOBS.series.values()) if m.ACTIVE_JOBS.series else 0
    return {"detail": f"in-process executor; {int(inflight)} job(s) in flight"}


def _run(name: str, fn, critical: bool, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    state, err = "ok", None
    out: dict[str, Any] = {}
    fut = _POOL.submit(fn)
    try:
        out = fut.result(timeout=timeout)
    except FutureTimeout:
        state, err = "unavailable", "timeout"
    except Exception as exc:  # noqa: BLE001 - only the class is reported, never the message
        state, err = "unavailable", type(exc).__name__ if str(exc) != "migrations_pending" else "migrations_pending"
    ms = round((time.perf_counter() - t0) * 1000, 2)
    if state != "ok" and not critical:
        state = "degraded"
    m.window_add(f"dep:{name}", "good" if state == "ok" else "bad", 1)
    m.window_add("dependency", "total", 1)
    m.window_add("dependency", "good" if state == "ok" else "bad", 1)
    m.DEPENDENCY.set(1.0 if state == "ok" else 0.0, dependency=name)
    return {"name": name, "critical": critical, "state": state, "latency_ms": ms, "error_class": err, **out}


def _optional(settings: Settings) -> list[dict[str, Any]]:
    """Optional integrations are reported as configured/disabled; they are NOT contacted (no expensive or external checks)."""
    return [
        {"name": "osv_advisories", "critical": False, "state": "ok" if settings.security_osv_enabled or settings.security_advisory_dir else "disabled",
         "detail": "live lookups enabled" if settings.security_osv_enabled else ("offline advisory dir configured" if settings.security_advisory_dir else "not configured"), "probed": False},
        {"name": "notifications", "critical": False, "state": "ok" if (settings.ops_webhook_url or settings.ops_slack_webhook_env) else "disabled",
         "detail": "configured" if (settings.ops_webhook_url or settings.ops_slack_webhook_env) else "not configured (alerts are recorded locally only)", "probed": False},
    ]


def dependency_health(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    with _LOCK:
        if not force and _CACHE["value"] is not None and now - _CACHE["at"] < settings.ops_health_cache_seconds:
            return {**_CACHE["value"], "cached": True}
    t = settings.ops_probe_timeout_seconds
    deps = [_run("database", _db, True, t), _run("migrations", _migrations, True, t), _run("storage", lambda: _storage(settings), True, t), _run("workers", _workers, True, t), *_optional(settings)]
    critical_down = [d["name"] for d in deps if d["critical"] and d["state"] != "ok"]
    degraded = [d["name"] for d in deps if d["state"] == "degraded"]
    status = "unavailable" if critical_down else ("degraded" if degraded else "ok")
    value = {"status": status, "critical_unavailable": critical_down, "degraded": degraded, "dependencies": deps, "checked_at": time.time(), "cached": False}
    with _LOCK:
        _CACHE.update(at=time.monotonic(), value=value)
    return value


def reset_cache() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, value=None)
