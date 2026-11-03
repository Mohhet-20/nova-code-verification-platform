"""Optional periodic alert evaluation. Disabled unless `ops_periodic_evaluation` is true; when disabled NOVA makes no continuous-monitoring claim."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import Settings
from app.db.session import get_session_factory
from app.models import Workspace
from app.observability import alerts, notify

log = logging.getLogger("nova.ops.periodic")
STATE = {"running": False, "last_run": None, "runs": 0, "last_error": None}


def evaluate_once(settings: Settings) -> int:
    factory = get_session_factory()
    n = 0
    with factory() as db:
        for ws in db.scalars(select(Workspace)).all():
            alerts.evaluate_all(db, ws.id, settings, notify=lambda d, s, i: notify.dispatch(d, s, i))
            n += 1
    return n


async def loop(settings: Settings) -> None:
    from datetime import UTC, datetime

    STATE["running"] = True
    try:
        while True:
            try:
                await asyncio.to_thread(evaluate_once, settings)
                STATE.update(last_run=datetime.now(UTC).isoformat(), runs=STATE["runs"] + 1, last_error=None)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                STATE["last_error"] = type(exc).__name__
                log.warning("periodic_evaluation_failed", extra={"event": "periodic_evaluation_failed", "error_class": type(exc).__name__})
            await asyncio.sleep(settings.ops_evaluation_interval_seconds)
    finally:
        STATE["running"] = False
