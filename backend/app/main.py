from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    analysis,
    auth,
    cicd,
    debugging,
    governance,
    graph,
    health,
    integrations,
    intelligence,
    ops,
    refactor,
    repair,
    repositories,
    review,
    security,
    tasks,
    testing,
    verification,
    workspace,
)
from app.core.config import APP_VERSION, get_settings
from app.core.errors import install_error_handlers
from app.core.headers import SecurityHeadersMiddleware
from app.core.limits import BodySizeLimitMiddleware
from app.core.logging import configure_logging
from app.observability import periodic, tracing
from app.observability.middleware import ObservabilityMiddleware
from app.security.process import harden_process
from app.security.throttle import AuthFailureThrottle

logger = logging.getLogger("nova.http")


def recover_interrupted_indexing() -> int:
    """Mark snapshots left 'running' by a previous process as failed so re-indexing is possible."""
    from sqlalchemy import update

    from app.db.base import utcnow
    from app.db.session import get_session_factory
    from app.models import IndexSnapshot

    try:
        with get_session_factory()() as session:
            result = session.execute(
                update(IndexSnapshot).where(IndexSnapshot.status == "running").values(
                    status="failed", finished_at=utcnow(),
                    error_message="Interrupted: the backend stopped before indexing finished."))
            session.commit()
            from app.services.analysis.orchestrator import recover_interrupted_analysis
            from app.services.security.scan import recover_interrupted as recover_security
            from app.services.testing.execution import recover_interrupted_executions
            from app.services.verification.service import recover_interrupted_runs

            return (int(result.rowcount or 0) + recover_interrupted_analysis(session)
                    + recover_interrupted_executions(session) + recover_interrupted_runs(session) + recover_security(session))
    except Exception:  # database not migrated yet; /ready reports that
        return 0


async def integration_worker_loop(settings) -> None:  # type: ignore[no-untyped-def]
    """Optional in-process worker for outbound webhook deliveries and integration jobs. Failures are logged and never stop the API."""
    from app.db.session import get_session_factory
    from app.services.integrations import jobs as int_jobs
    from app.services.integrations import outbox as int_outbox

    while True:
        try:
            def once() -> None:
                with get_session_factory()() as db:
                    int_outbox.run_due(db, settings)
                    int_jobs.run_due(db, settings)
            await asyncio.to_thread(once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("integration_worker_error")
        await asyncio.sleep(settings.integration_worker_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.release import prodcheck

    prodcheck.enforce_startup(get_settings())  # production: refuse invalid config or a schema that is not at head
    logger.info("process_hardening", extra={"result": harden_process()})  # keeps secrets in /proc/<pid>/environ away from test subprocesses
    for finding in prodcheck.check(get_settings()):
        logger.warning("config_finding", extra={"code": finding.code, "severity": finding.severity})
    recovered = recover_interrupted_indexing()
    if recovered:
        logger.warning("recovered_interrupted_indexing", extra={"count": recovered})
    settings = get_settings()
    task = asyncio.create_task(periodic.loop(settings)) if settings.ops_periodic_evaluation else None
    worker = asyncio.create_task(integration_worker_loop(settings)) if settings.integration_worker_enabled else None  # optional; never required for startup
    try:
        yield
    finally:
        pending = [t for t in (task, worker) if t is not None]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # graceful shutdown: let loops unwind before the process exits


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json, environment=settings.environment,
                      ring_size=settings.ops_log_ring_size, field_max=settings.ops_log_field_max)
    tracing.configure(settings.ops_trace_sample_ratio, settings.ops_trace_ring_size)
    app = FastAPI(title="NOVA API", version=APP_VERSION,
                  description="AI Software Engineering and Verification Platform - foundation slice.",
                  lifespan=lifespan,
                  docs_url="/docs" if settings.docs_enabled and settings.environment != "production" else None,
                  redoc_url=None, openapi_url="/openapi.json" if settings.docs_enabled and settings.environment != "production" else None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
                       expose_headers=["X-Request-ID", "Retry-After"])
    app.add_middleware(SecurityHeadersMiddleware)
    install_error_handlers(app)
    app.state.auth_throttle = AuthFailureThrottle(settings.auth_failure_limit, settings.auth_failure_window_seconds)
    app.state.guest_throttle = AuthFailureThrottle(20, 600)  # guest sessions started per client address per 10 minutes

    app.add_middleware(ObservabilityMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

    api = APIRouter(prefix="/api/v1")
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(ops.router)
    api.include_router(workspace.router)
    api.include_router(repositories.router)
    api.include_router(intelligence.router)
    api.include_router(intelligence.caps_router)
    api.include_router(graph.router)
    api.include_router(analysis.router)
    api.include_router(testing.router)
    api.include_router(debugging.router)
    api.include_router(review.router)
    api.include_router(repair.router)
    api.include_router(refactor.router)
    api.include_router(verification.router)
    api.include_router(cicd.router)
    api.include_router(security.router)
    api.include_router(governance.router)
    api.include_router(cicd.webhook_router)
    api.include_router(integrations.router)
    api.include_router(integrations.inbound_router)
    api.include_router(tasks.router)
    app.include_router(api)
    return app


app = create_app()
