"""Performance framework, fixtures, budgets, and correctness of the performance-motivated changes."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.core.config import Settings
from app.perf import budgets, fixtures
from app.perf.runner import QueryCounter, run_benchmark, summarize
from app.perf.schema import BENCHMARK_VERSION, RESULT_SCHEMA
from app.perf.workloads import Env, env, run_suite

# ---- fixtures ------------------------------------------------------------------------------------------------------


def test_fixtures_are_deterministic_and_counts_are_real(tmp_path: Path) -> None:
    a = fixtures.generate(tmp_path / "a", "small", 7)
    b = fixtures.generate(tmp_path / "b", "small", 7)
    c = fixtures.generate(tmp_path / "c", "small", 8)
    assert a["digest"] == b["digest"] != c["digest"]
    actual = [p for p in (tmp_path / "a").rglob("*") if p.is_file()]
    assert a["files"] == len(actual)
    assert a["bytes"] == sum(p.stat().st_size for p in actual)


@pytest.mark.parametrize("profile", sorted(fixtures.PROFILES))
def test_every_profile_generates(tmp_path: Path, profile: str) -> None:
    if profile in {"large", "large_source_files"}:
        pytest.skip("covered by the benchmark CLI; too slow for the unit suite")
    m = fixtures.generate(tmp_path / profile, profile, 1)
    assert m["files"] > 0 and m["profile"] == profile
    if profile == "malformed":
        assert m["malformed_files"] > 0


# ---- runner / schema -----------------------------------------------------------------------------------------------


def test_summarize_reports_tails_only_with_enough_samples() -> None:
    few = summarize([1.0, 2.0, 3.0])
    assert few.p95_ms is None and few.p99_ms is None and few.median_ms == 2.0
    many = summarize([float(i) for i in range(1, 101)])
    assert many.p95_ms is not None and many.p99_ms is not None


def test_failed_correctness_check_is_not_ok() -> None:
    r = run_benchmark("op", lambda: 1, repeat=2, warmup=0, workload={}, fixture={}, check=lambda v: {"ok": False}).to_dict()
    assert r["outcome"] == "correctness_failed" and r["schema"] == RESULT_SCHEMA and r["benchmark_version"] == BENCHMARK_VERSION


def test_exception_is_reported_as_error_without_raising() -> None:
    def boom() -> None:
        raise RuntimeError("x")

    r = run_benchmark("op", boom, repeat=1, warmup=0, workload={}, fixture={}).to_dict()
    assert r["outcome"] == "error"


def test_setup_is_excluded_from_timing() -> None:
    import time

    r = run_benchmark("op", lambda: 1, setup=lambda: time.sleep(0.05), repeat=3, warmup=0, workload={}, fixture={}).to_dict()
    assert r["outcome"] == "ok" and r["stats"]["median_ms"] < 25


def test_query_counter_counts_without_keeping_parameters(migrated_url: str) -> None:
    from app.db.session import get_engine

    eng = get_engine()
    with QueryCounter(eng) as qc, eng.connect() as c:
        c.execute(text("SELECT :secret"), {"secret": "hunter2-super-secret"})
    rep = qc.report()
    assert rep["count"] >= 1
    assert "hunter2" not in json.dumps(rep)


# ---- budgets -------------------------------------------------------------------------------------------------------


def _result(median: float, outcome: str = "ok", cpu: int = 2) -> dict:
    return {"operation": "op", "workload": {"p": "small"}, "fixture": {"fixture_version": "1", "digest": "d"}, "benchmark_version": "1", "outcome": outcome,
            "stats": {"median_ms": median, "n": 3, "mad_ms": 1.0}, "environment": {"python": "3.12", "cpu_count": cpu}}


def test_missing_baseline_is_never_a_pass() -> None:
    assert budgets.compare(_result(10), {"results": {}})["status"] == "missing_baseline"


def test_regression_ok_improved_and_noise_floor(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    budgets.record(p, [_result(1000)])
    base = budgets.load(p)
    assert budgets.compare(_result(1100), base)["status"] == "ok"
    assert budgets.compare(_result(2600), base)["status"] == "regression"
    assert budgets.compare(_result(300), base)["status"] == "improved"
    budgets.record(p, [_result(2)])
    assert budgets.compare(_result(10), budgets.load(p))["status"] == "ok"  # tiny absolute change is noise


def test_environment_mismatch_and_failed_outcomes_are_not_pass(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    budgets.record(p, [_result(1000, cpu=8)])
    base = budgets.load(p)
    assert budgets.compare(_result(1000, cpu=2), base)["status"] == "environment_mismatch"
    assert budgets.compare(_result(1000, outcome="correctness_failed"), base)["status"] == "correctness_failed"


def test_corrupt_baseline_file_reads_as_empty(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text("{not json")
    assert budgets.load(p) == {"results": {}}
    assert budgets.compare(_result(5), budgets.load(p))["status"] == "missing_baseline"


# ---- migration / query plan ----------------------------------------------------------------------------------------


def test_fk_indexes_exist_and_are_used_for_generation_deletes(migrated_url: str) -> None:
    from app.db.session import get_engine

    eng = get_engine()
    insp = inspect(eng)
    cols = {(t, tuple(i["column_names"])) for t in ("file_imports", "relationships") for i in insp.get_indexes(t)}
    assert ("file_imports", ("resolved_file_id",)) in cols
    assert ("relationships", ("source_file_id",)) in cols and ("relationships", ("target_file_id",)) in cols
    with eng.connect() as c:
        plan = " ".join(str(r) for r in c.execute(text("EXPLAIN QUERY PLAN DELETE FROM relationships WHERE source_file_id = 'x'")))
    assert "ix_relationships_source_file_id" in plan


# ---- incremental vs full parity ------------------------------------------------------------------------------------


def _state(e: Env) -> dict:
    files: list[tuple] = []
    page = 1
    while True:
        j = e.get(f"/files?page_size=200&page={page}")
        files += [(f["rel_path"], f["language"], f["size_bytes"]) for f in j["items"]]
        if len(files) >= j["total"] or not j["items"]:
            break
        page += 1
    snap = e.get("/summary")["snapshot"]
    return {"files": sorted(files), "symbols": snap["symbols_count"], "total": j["total"]}


def test_incremental_equals_full_after_edit_delete_rename_add() -> None:
    with env("small") as e:
        e.index("full")
        root = e.root
        mods = sorted(root.rglob("mod*.py"))
        assert len(mods) >= 4
        fixtures.touch_one(root, e.manifest)  # edit
        mods[1].unlink()  # delete
        mods[2].rename(mods[2].with_name("renamed_mod.py"))  # rename
        (root / "brand_new.py").write_text("def added():\n    return 1\n")  # add
        snap = e.index("incremental")
        assert snap["status"] in {"completed", "completed_with_warnings"}
        incremental = _state(e)
        e.index("full")
        full = _state(e)
        assert incremental == full
        paths = [f[0] for f in full["files"]]
        assert "brand_new.py" in paths and not any(p.endswith("/" + mods[1].name) or p == mods[1].name for p in paths)
        assert any(p.endswith("renamed_mod.py") for p in paths)


def test_noop_incremental_reuses_every_file() -> None:
    with env("small") as e:
        full = e.index("full")
        again = e.index("incremental")
        assert again["files_unchanged"] == full["files_indexed"] and again["files_changed"] == 0 and again["files_new"] == 0


def test_corrupt_source_does_not_block_indexing_and_is_marked_partial() -> None:
    with env("malformed") as e:
        snap = e.index("full")
        assert snap["status"] == "completed_with_warnings"
        assert snap["files_indexed"] == e.manifest["files"]


def test_files_listing_pagination_is_complete_and_disjoint() -> None:
    with env("small") as e:
        e.index("full")
        seen: list[str] = []
        total = None
        for page in range(1, 40):
            j = e.get(f"/files?page_size=7&page={page}")
            total = j["total"]
            seen += [f["rel_path"] for f in j["items"]]
            if not j["items"]:
                break
        assert total == e.manifest["files"] and len(seen) == len(set(seen)) == total


# ---- shared security call cache parity -----------------------------------------------------------------------------


def test_shared_call_cache_matches_uncached_computation() -> None:
    from app.services.analysis.analyzers import python_security_flow as sf
    from app.services.analysis.analyzers.py_facts import collect_facts

    src = "import os\ndef f(x):\n    os.system(x)\n    return eval(x)\ndef g(y):\n    return open(y)\n"
    facts = collect_facts(ast.parse(src))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.facts = facts
    first = [(c.lineno, q, r) for c, _t, q, _l, r in sf._each_call(ctx)]
    cached = [(c.lineno, q, r) for c, _t, q, _l, r in sf._each_call(ctx)]
    facts.memo.clear()
    fresh = [(c.lineno, q, r) for c, _t, q, _l, r in sf._each_call(ctx)]
    assert first == cached == fresh and first


# ---- admission control ---------------------------------------------------------------------------------------------


def test_indexing_admission_limit_returns_429(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import get_session_factory
    from app.models import IndexSnapshot

    roots = []
    for n in range(2):
        r = tmp_path / f"r{n}"
        r.mkdir()
        (r / "a.py").write_text("x = 1\n")
        roots.append(r)
    ids = [client.post("/api/v1/repositories", json={"name": f"r{n}", "root_path": str(r)}).json()["id"] for n, r in enumerate(roots)]
    monkeypatch.setenv("NOVA_INDEX_MAX_CONCURRENT_RUNS", "1")
    reset_settings_cache()
    with get_session_factory()() as s:
        s.add(IndexSnapshot(repository_id=ids[0], status="running", mode="full", limits={}))
        s.commit()
    r = client.post(f"/api/v1/repositories/{ids[1]}/index", json={"mode": "full"})
    assert r.status_code == 429, r.text
    r2 = client.post(f"/api/v1/repositories/{ids[0]}/index", json={"mode": "full"})
    assert r2.status_code == 409  # the per-repository conflict still wins and is reported as such


def test_default_admission_limit_is_bounded() -> None:
    assert Settings().index_max_concurrent_runs >= 1


# ---- benchmark CLI end-to-end --------------------------------------------------------------------------------------


def test_run_suite_small_validates_output_and_carries_no_secrets() -> None:
    results = run_suite("small", ["index_full", "index_noop"], repeat=2)
    assert [r.outcome for r in results] == ["ok", "ok"]
    blob = json.dumps([r.to_dict() for r in results])
    assert "NOVA_API_TOKEN" not in blob and "nova-bench-" not in blob


def test_cli_missing_baseline_fails_in_compare_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.perf.__main__ import main

    code = main(["--profile", "small", "--workloads", "index_noop", "--repeat", "2", "--compare", "--baseline", str(tmp_path / "none.json"), "--out", str(tmp_path / "o.json")])
    assert code != 0
    assert "missing_baseline" in capsys.readouterr().out
