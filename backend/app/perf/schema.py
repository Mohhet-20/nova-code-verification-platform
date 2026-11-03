"""Common benchmark-result schema."""
from __future__ import annotations

import os
import platform
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

BENCHMARK_VERSION = "1"
RESULT_SCHEMA = "nova.benchmark/1"


def environment() -> dict[str, Any]:
    """Facts that make a measurement comparable. No hostnames, usernames or paths."""
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(), "platform": platform.system(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version, "kind": "synthetic_benchmark"}


@dataclass
class Stats:
    n: int
    min_ms: float
    median_ms: float
    mean_ms: float
    max_ms: float
    stdev_ms: float
    mad_ms: float  # median absolute deviation: robust variability
    p95_ms: float | None  # only reported with >= 20 samples
    p99_ms: float | None  # only reported with >= 100 samples


@dataclass
class BenchmarkResult:
    operation: str
    workload: dict[str, Any]
    fixture: dict[str, Any]
    stats: Stats | None
    outcome: str  # ok | correctness_failed | error
    correctness: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    queries: dict[str, Any] | None = None
    error: str | None = None
    benchmark_version: str = BENCHMARK_VERSION
    schema: str = RESULT_SCHEMA
    environment: dict[str, Any] = field(default_factory=environment)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
