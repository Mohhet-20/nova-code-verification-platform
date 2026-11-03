"""CLI: python -m app.perf --profile medium --workloads index_full,api_reads --repeat 3 [--compare|--update-baseline] [--profile-cpu index_full]"""
from __future__ import annotations

import argparse
import cProfile
import json
import logging
import pstats
import sys
from pathlib import Path

from app.core.config import BACKEND_DIR
from app.perf import budgets, fixtures, workloads

BASELINE = BACKEND_DIR / "benchmarks" / "baseline.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.perf", description="NOVA synthetic benchmarks (not production measurements).")
    ap.add_argument("--profile", default="small", choices=sorted(fixtures.PROFILES))
    ap.add_argument("--workloads", default=",".join(workloads.WORKLOADS), help=f"comma list of: {', '.join(workloads.WORKLOADS)}")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--memory", action="store_true", help="also record the tracemalloc Python peak (separate run; slows nothing else)")
    ap.add_argument("--compare", action="store_true", help="compare against benchmarks/baseline.json")
    ap.add_argument("--update-baseline", action="store_true", help="explicitly record results as the new baseline")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--ratio", type=float, default=budgets.DEFAULT_RATIO)
    ap.add_argument("--out", type=Path, help="write the full JSON results here")
    ap.add_argument("--profile-cpu", metavar="WORKLOAD", help="run one workload under cProfile and print the top 25 functions by cumulative time")
    ap.add_argument("--list-fixtures", action="store_true")
    a = ap.parse_args(argv)
    logging.disable(logging.WARNING)  # keep benchmark output readable; errors are still reported through results
    if a.list_fixtures:
        import tempfile

        for name in sorted(fixtures.PROFILES):
            m = fixtures.generate(Path(tempfile.mkdtemp()) / "r", name, a.seed)
            print(json.dumps({k: m[k] for k in ("profile", "files", "bytes", "lines", "languages", "dependencies", "digest")}))
        return 0
    if a.profile_cpu:
        pr = cProfile.Profile()
        pr.enable()
        workloads.run_suite(a.profile, [a.profile_cpu], repeat=1, seed=a.seed)
        pr.disable()
        pstats.Stats(pr).sort_stats("cumulative").print_stats(25)
        return 0
    results = workloads.run_suite(a.profile, [w for w in a.workloads.split(",") if w], a.repeat, a.memory, a.seed)
    data = [r.to_dict() for r in results]
    base = budgets.load(a.baseline)
    rows = [budgets.compare(r, base, ratio=a.ratio) if a.compare else None for r in data]
    for r, c in zip(data, rows, strict=True):
        s = r["stats"]
        line = f"{r['operation']:<32} {r['outcome']:<18}" + (f" median {s['median_ms']:9.1f} ms  min {s['min_ms']:9.1f}  mad {s['mad_ms']:7.1f}  n={s['n']}" if s else "")
        if r["queries"]:
            line += f"  queries={r['queries']['count']} ({r['queries']['total_ms']:.0f} ms)"
        if c:
            line += f"  [{c['status']}]"
        print(line)
    if a.out:
        a.out.write_text(json.dumps({"results": data, "comparison": [c for c in rows if c]}, indent=1), "utf-8")
    if a.update_baseline:
        budgets.record(a.baseline, data)
        print(f"baseline updated: {a.baseline}")
    bad = [r for r in data if r["outcome"] != "ok"]
    regress = [c for c in rows if c and c["status"] == "regression"]
    unverified = [c for c in rows if c and c["status"] in {"missing_baseline", "environment_mismatch"}]
    if unverified:
        print(f"NOT VERIFIED: {len(unverified)} result(s) have no comparable baseline (missing_baseline/environment_mismatch); this is not a pass. Use --update-baseline to record one explicitly.")
    return 1 if bad or regress else (2 if unverified else 0)


if __name__ == "__main__":
    sys.exit(main())
