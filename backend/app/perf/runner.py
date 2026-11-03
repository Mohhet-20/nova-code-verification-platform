"""Measurement helpers: monotonic timing, robust statistics, optional memory/CPU capture and SQL query counting."""
from __future__ import annotations

import statistics
import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.perf.schema import BenchmarkResult, Stats


def summarize(samples_ms: list[float]) -> Stats:
    s = sorted(samples_ms)
    med = statistics.median(s)
    return Stats(n=len(s), min_ms=s[0], median_ms=med, mean_ms=statistics.fmean(s), max_ms=s[-1], stdev_ms=statistics.pstdev(s) if len(s) > 1 else 0.0,
                 mad_ms=statistics.median([abs(x - med) for x in s]),
                 p95_ms=s[min(len(s) - 1, int(0.95 * len(s)))] if len(s) >= 20 else None, p99_ms=s[min(len(s) - 1, int(0.99 * len(s)))] if len(s) >= 100 else None)


class QueryCounter:
    """Counts SQL statements and their total time on an engine. Statement text is normalised and bounded; parameters are never kept."""

    def __init__(self, engine: Engine, top: int = 5) -> None:
        self.engine = engine
        self.count = 0
        self.total_ms = 0.0
        self._by: dict[str, list[float]] = {}
        self.top = top
        self._t0: list[float] = []

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        self._t0.append(time.perf_counter())

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ms = (time.perf_counter() - self._t0.pop()) * 1000 if self._t0 else 0.0
        self.count += 1
        self.total_ms += ms
        key = " ".join(statement.split())[:120]
        agg = self._by.setdefault(key, [0, 0.0])
        agg[0] += 1
        agg[1] += ms

    def __enter__(self) -> QueryCounter:
        event.listen(self.engine, "before_cursor_execute", self._before)
        event.listen(self.engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._before)
        event.remove(self.engine, "after_cursor_execute", self._after)

    def report(self) -> dict[str, Any]:
        top = sorted(self._by.items(), key=lambda kv: kv[1][1], reverse=True)[: self.top]
        return {"count": self.count, "total_ms": round(self.total_ms, 3),
                "top_statements": [{"statement": k, "calls": int(v[0]), "total_ms": round(v[1], 3)} for k, v in top]}


@contextmanager
def measured_memory(enabled: bool) -> Iterator[dict[str, Any]]:
    """tracemalloc peak of Python allocations (not RSS; it slows the code, so it is only enabled for a dedicated run)."""
    out: dict[str, Any] = {}
    if not enabled:
        yield out
        return
    tracemalloc.start()
    try:
        yield out
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        out["python_peak_bytes"] = peak


def run_benchmark(operation: str, fn: Callable[[], Any], *, repeat: int = 5, warmup: int = 1, workload: dict[str, Any] | None = None, fixture: dict[str, Any] | None = None,
                  check: Callable[[Any], dict[str, Any]] | None = None, setup: Callable[[], None] | None = None, memory: bool = False,
                  engine: Engine | None = None) -> BenchmarkResult:
    """Time `fn` (`setup` is excluded from the timing). `check(result)` must return {"ok": bool, ...}; a fast wrong answer is a failed benchmark."""
    samples: list[float] = []
    correctness: dict[str, Any] = {}
    res: dict[str, Any] = {}
    try:
        for _ in range(warmup):
            if setup:
                setup()
            fn()
        cpu0 = time.process_time()
        for _ in range(repeat):
            if setup:
                setup()
            t0 = time.perf_counter()
            value = fn()
            samples.append((time.perf_counter() - t0) * 1000)
            if check is not None:
                correctness = check(value)
                if not correctness.get("ok", False):
                    return BenchmarkResult(operation, workload or {}, fixture or {}, summarize(samples), "correctness_failed", correctness)
        res["cpu_seconds"] = round(time.process_time() - cpu0, 4)
        queries = None
        if engine is not None:  # one extra, separate run so counting does not perturb the timings
            if setup:
                setup()
            with QueryCounter(engine) as qc:
                fn()
            queries = qc.report()
        if memory:
            with measured_memory(True) as mem:
                if setup:
                    setup()
                fn()
            res.update(mem)
        return BenchmarkResult(operation, workload or {}, fixture or {}, summarize(samples), "ok", correctness, res, queries)
    except Exception as exc:  # noqa: BLE001 - reported as an error result, not raised
        return BenchmarkResult(operation, workload or {}, fixture or {}, summarize(samples) if samples else None, "error", correctness, res, None, type(exc).__name__)
