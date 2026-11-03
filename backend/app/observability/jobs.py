"""Job tracking: context propagation, metrics, span and log for a background job. Observability failures never mask the job's own exception."""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.observability import context as ctx
from app.observability import metrics as m
from app.observability import tracing

log = logging.getLogger("nova.jobs")


class JobHandle:
    def __init__(self) -> None:
        self.outcome: str | None = None
        self.retries = 0
        self.reason: str | None = None

    def set_outcome(self, outcome: str, reason: str | None = None) -> None:
        """`reason` must be a short stable class (e.g. 'tests_failed'), never free text or secrets."""
        self.outcome = outcome
        self.reason = reason[:60] if reason else None


def _delay(created_at: datetime | None) -> float | None:
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    d = (datetime.now(UTC) - created_at).total_seconds()
    return d if d >= 0 else None


@contextmanager
def track(kind: str, job_id: str | int | None = None, *, created_at: datetime | None = None, workflow: str | None = None, parent_request_id: str | None = None) -> Iterator[JobHandle]:
    """Wrap a job body. Outcome is `succeeded` unless the body calls `set_outcome` (e.g. `failed`) or raises.

    The parent link is the request id active when the job *starts running* (BackgroundTasks copy the request context); when none is
    active no parent is recorded — links are never invented.
    """
    h = JobHandle()
    t0 = time.perf_counter()
    parent = parent_request_id or ctx.REQUEST_ID.get()
    j1, j2 = ctx.JOB_ID.set(str(job_id) if job_id is not None else None), ctx.JOB_TYPE.set(kind)
    delay = _delay(created_at)
    try:
        m.ACTIVE_JOBS.set(m.ACTIVE_JOBS.series.get((("kind", kind),), 0) + 1, kind=kind)
        if delay is not None:
            m.JOB_QUEUE_DELAY.observe(delay, kind=kind)
            m.record_latency(f"queue:{kind}", delay)
            m.record_latency("queue_all", delay)
    except Exception:  # noqa: BLE001, S110
        pass
    exc_class: str | None = None
    try:
        with tracing.span(f"job.{kind}", job_kind=kind, job_id=str(job_id) if job_id is not None else "") as sp:
            if parent:
                sp.set("parent.request_id", parent)
            if delay is not None:
                sp.set("queue.delay_ms", round(delay * 1000, 1))
            try:
                yield h
            except BaseException as exc:
                exc_class = type(exc).__name__
                h.outcome = "failed"
                raise
            finally:
                sp.set("job.outcome", h.outcome or "succeeded")
    finally:
        dur = time.perf_counter() - t0
        outcome = h.outcome or "succeeded"
        try:
            m.JOBS.inc(kind=kind, outcome=outcome)
            m.JOB_DURATION.observe(dur, kind=kind)
            m.window_add(f"jobs:{kind}", outcome, 1)
            m.window_add("jobs", "total", 1)
            m.window_add("jobs", "good" if outcome in ("succeeded", "cancelled") else "bad", 1)
            if outcome == "failed":
                m.window_add(f"jobfail:{kind}", h.reason or exc_class or "unspecified", 1)
            if h.retries:
                m.JOB_RETRIES.inc(h.retries, kind=kind)
            if workflow:
                m.WORKFLOW.inc(workflow=workflow, outcome=outcome)
                m.window_add(f"workflow:{workflow}", outcome, 1)
            m.ACTIVE_JOBS.set(max(0, m.ACTIVE_JOBS.series.get((("kind", kind),), 1) - 1), kind=kind)
            log.log(logging.ERROR if outcome == "failed" else logging.INFO, "job_finished", extra={"event": "job_finished", "job_kind": kind, "outcome": outcome, "duration_ms": round(dur * 1000, 2), "error_class": exc_class, "parent_request_id": parent, "queue_delay_ms": round(delay * 1000, 1) if delay is not None else None})
        except Exception:  # noqa: BLE001, S110
            pass
        ctx.JOB_ID.reset(j1)
        ctx.JOB_TYPE.reset(j2)


def tracked(kind: str, workflow: str | None = None, *, model: Any = None, id_arg: int = 0, failed: tuple[str, ...] = ("failed", "error", "timed_out"), cancelled: tuple[str, ...] = ("cancelled",)) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a background-job function. When `model` is given, the job's recorded status decides the outcome (execution failure, not the
    verdict on the code under test: e.g. a test run whose tests fail is still a succeeded job). Exceptions always mark the job failed and re-raise."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            rid = args[id_arg] if model is not None and len(args) > id_arg else None
            created = _created_at(model, rid) if model is not None else None
            with track(kind, rid, created_at=created, workflow=workflow) as h:
                result = fn(*args, **kwargs)
                if model is not None and rid is not None:
                    status = _status(model, rid)
                    if status in failed:
                        h.set_outcome("failed", f"status_{status}")
                    elif status in cancelled:
                        h.set_outcome("cancelled")
                return result

        return wrapper

    return deco


def _record(model: Any, rid: Any) -> Any:
    from app.db.session import get_session_factory

    try:
        with get_session_factory()() as s:
            rec = s.get(model, rid)
            if rec is None:
                return None
            return {"status": getattr(rec, "status", None), "created_at": getattr(rec, "created_at", None)}
    except Exception:  # noqa: BLE001, S110 - observability must not break the job
        return None


def _created_at(model: Any, rid: Any) -> datetime | None:
    rec = _record(model, rid)
    return rec["created_at"] if rec else None


def _status(model: Any, rid: Any) -> str | None:
    rec = _record(model, rid)
    return rec["status"] if rec else None
