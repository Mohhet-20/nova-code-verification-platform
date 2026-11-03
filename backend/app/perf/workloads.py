"""Benchmark workloads over the real application (API + services + SQLite). Each validates its own output."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.perf import fixtures
from app.perf.runner import run_benchmark
from app.perf.schema import BenchmarkResult

API = "/api/v1"
DONE = {"completed", "completed_with_warnings"}


class Env:
    """A throwaway NOVA instance: own SQLite database, own repository fixture, in-process API client."""

    def __init__(self, profile: str, seed: int = 1234) -> None:
        from alembic import command
        from fastapi.testclient import TestClient

        from app.core.config import BACKEND_DIR, reset_settings_cache
        from app.db.session import get_engine, reset_engine
        from app.main import create_app
        from tests.conftest import alembic_config  # noqa: PLC0415 - reuse the migration helper

        self._tmp = Path(tempfile.mkdtemp(prefix="nova-bench-"))
        self._old = {k: os.environ.get(k) for k in ("NOVA_DATABASE_URL", "NOVA_LOG_JSON", "NOVA_LOG_LEVEL")}
        os.environ["NOVA_DATABASE_URL"] = f"sqlite:///{(self._tmp / 'bench.db').as_posix()}"
        os.environ["NOVA_LOG_JSON"], os.environ["NOVA_LOG_LEVEL"] = "false", "ERROR"
        reset_settings_cache()
        reset_engine()
        command.upgrade(alembic_config(os.environ["NOVA_DATABASE_URL"]), "head")
        _ = BACKEND_DIR
        self.root = self._tmp / "repo"
        self.manifest = fixtures.generate(self.root, profile, seed)
        self.client = TestClient(create_app())
        self.client.__enter__()
        self.engine = get_engine()
        repo = self.client.post(f"{API}/repositories", json={"name": f"bench-{profile}", "root_path": str(self.root)})
        assert repo.status_code == 201, repo.text
        self.repo_id: str = repo.json()["id"]

    def index(self, mode: str = "incremental") -> dict[str, Any]:
        r = self.client.post(f"{API}/repositories/{self.repo_id}/index", json={"mode": mode})
        assert r.status_code == 202, r.text
        return self.client.get(f"{API}/repositories/{self.repo_id}/snapshots/{r.json()['id']}").json()

    def get(self, tail: str) -> Any:
        r = self.client.get(f"{API}/repositories/{self.repo_id}{tail}")
        assert r.status_code == 200, (tail, r.text)
        return r.json()

    def close(self) -> None:
        from app.core.config import reset_settings_cache
        from app.db.session import reset_engine

        self.client.__exit__(None, None, None)
        reset_engine()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)


@contextmanager
def env(profile: str, seed: int = 1234) -> Iterator[Env]:
    e = Env(profile, seed)
    try:
        yield e
    finally:
        e.close()


def _index_check(e: Env) -> Callable[[dict[str, Any]], dict[str, Any]]:
    m = e.manifest

    def check(snap: dict[str, Any]) -> dict[str, Any]:
        ok = snap["status"] in DONE and snap["files_indexed"] == m["files"] and not snap.get("truncated") and snap["symbols_count"] > 0
        return {"ok": ok, "status": snap["status"], "files_indexed": snap["files_indexed"], "files_expected": m["files"], "symbols": snap["symbols_count"], "truncated": snap.get("truncated")}

    return check


def bench_index_full(e: Env, repeat: int, memory: bool) -> BenchmarkResult:
    return run_benchmark("index_full", lambda: e.index("full"), repeat=repeat, warmup=0, workload={"profile": e.manifest["profile"]}, fixture=e.manifest, check=_index_check(e), memory=memory, engine=e.engine)


def bench_index_incremental_one_change(e: Env, repeat: int, memory: bool) -> BenchmarkResult:
    e.index("full")

    def check(snap: dict[str, Any]) -> dict[str, Any]:
        n = e.manifest["files"]
        ok = snap["status"] in DONE and snap["files_changed"] == 1 and snap["files_unchanged"] == n - 1 and snap["files_new"] == 0
        return {"ok": ok, "changed": snap["files_changed"], "unchanged": snap["files_unchanged"], "files": n}

    return run_benchmark("index_incremental_one_change", lambda: e.index("incremental"), setup=lambda: fixtures.touch_one(e.root, e.manifest), repeat=repeat, warmup=0,
                         workload={"profile": e.manifest["profile"], "changed_files": 1}, fixture=e.manifest, check=check, memory=memory, engine=e.engine)


def bench_index_incremental_noop(e: Env, repeat: int, memory: bool) -> BenchmarkResult:
    e.index("full")

    def check(snap: dict[str, Any]) -> dict[str, Any]:
        n = e.manifest["files"]
        return {"ok": snap["status"] in DONE and snap["files_unchanged"] == n and snap["files_changed"] == 0, "unchanged": snap["files_unchanged"], "files": n}

    return run_benchmark("index_incremental_noop", lambda: e.index("incremental"), repeat=repeat, warmup=0, workload={"profile": e.manifest["profile"]}, fixture=e.manifest, check=check, memory=memory, engine=e.engine)


def bench_api_reads(e: Env, repeat: int, memory: bool) -> list[BenchmarkResult]:
    snap = e.index("full")
    out: list[BenchmarkResult] = []
    total_files = snap["files_indexed"]
    first_file = e.get("/files?page_size=1")["items"][0]
    cases: list[tuple[str, str, Callable[[Any], bool]]] = [
        ("api_files_list", "/files?page_size=100", lambda j: len(j["items"]) == min(100, total_files) and j["total"] == total_files),
        ("api_symbols_search", "/symbols/search?q=fn_1&page_size=50", lambda j: len(j["items"]) > 0),
        ("api_summary", "/summary", lambda j: j["snapshot"]["files_indexed"] == total_files),
        ("api_file_detail", f"/files/{first_file['id']}", lambda j: j["id"] == first_file["id"]),
        ("api_dependents", f"/files/{first_file['id']}/dependents?page_size=100", lambda j: isinstance(j, dict)),
        ("api_cycles", "/dependencies/cycles", lambda j: isinstance(j, dict)),
        ("api_impact", f"/dependencies/impact?file_id={first_file['id']}&max_depth=5", lambda j: isinstance(j, dict)),
    ]
    for name, tail, ok in cases:
        def call(t: str = tail) -> Any:
            return e.get(t)

        out.append(run_benchmark(name, call, repeat=repeat, warmup=1, workload={"profile": e.manifest["profile"], "path": tail.split("?")[0].replace(first_file["id"], "{id}")}, fixture=e.manifest,
                                 check=lambda j, ok=ok: {"ok": bool(ok(j))}, memory=memory, engine=e.engine))
    return out


def bench_static_analysis(e: Env, repeat: int, memory: bool) -> BenchmarkResult:
    e.index("full")

    def run() -> dict[str, Any]:
        r = e.client.post(f"{API}/repositories/{e.repo_id}/analysis", json={"mode": "full"})
        assert r.status_code in (200, 202), r.text
        return e.get(f"/analysis/runs/{r.json()['id']}")

    return run_benchmark("static_analysis_full", run, repeat=repeat, warmup=0, workload={"profile": e.manifest["profile"]}, fixture=e.manifest,
                         check=lambda j: {"ok": j["status"] in ("completed", "completed_with_warnings"), "status": j["status"]}, memory=memory, engine=e.engine)


def bench_security_scan(e: Env, repeat: int, memory: bool) -> BenchmarkResult:
    e.index("full")

    def run() -> dict[str, Any]:
        r = e.client.post(f"{API}/repositories/{e.repo_id}/security/scans", json={"scan_types": ["dependencies", "code", "secrets"]})
        assert r.status_code in (200, 202), r.text
        return e.get(f"/security/scans/{r.json()['id']}")

    return run_benchmark("security_scan", run, repeat=repeat, warmup=0, workload={"profile": e.manifest["profile"], "scan_types": "dependencies+code+secrets"}, fixture=e.manifest,
                         check=lambda j: {"ok": j["status"] == "completed", "status": j["status"]}, memory=memory, engine=e.engine)


WORKLOADS: dict[str, Callable[..., Any]] = {
    "index_full": bench_index_full, "index_incremental": bench_index_incremental_one_change, "index_noop": bench_index_incremental_noop,
    "api_reads": bench_api_reads, "static_analysis": bench_static_analysis, "security_scan": bench_security_scan,
}


def run_suite(profile: str, names: list[str], repeat: int = 3, memory: bool = False, seed: int = 1234) -> list[BenchmarkResult]:
    results: list[BenchmarkResult] = []
    for name in names:
        with env(profile, seed) as e:
            r = WORKLOADS[name](e, repeat, memory)
            results.extend(r if isinstance(r, list) else [r])
    return results
