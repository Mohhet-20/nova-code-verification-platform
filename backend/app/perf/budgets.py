"""Baselines and regression policy. A missing baseline is reported as `missing_baseline` — never as a pass."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RATIO = 2.0  # generous: ordinary timing noise on shared machines is well below 2x for medians of >= 3 samples
DEFAULT_FLOOR_MS = 25.0  # differences smaller than this are noise regardless of ratio


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("results"), dict) else {"results": {}}
    except (OSError, ValueError):
        return {"results": {}}


def key_of(result: dict[str, Any]) -> str:
    wl = ",".join(f"{k}={v}" for k, v in sorted(result["workload"].items()))
    return f"{result['operation']}|{wl}|fixture={result['fixture'].get('fixture_version', '?')}:{result['fixture'].get('digest', '?')}|bench={result['benchmark_version']}"


def compare(result: dict[str, Any], baseline: dict[str, Any], *, ratio: float = DEFAULT_RATIO, floor_ms: float = DEFAULT_FLOOR_MS) -> dict[str, Any]:
    """Status: ok | regression | improved | missing_baseline | correctness_failed | error | environment_mismatch."""
    key = key_of(result)
    out = {"key": key, "operation": result["operation"], "current_median_ms": result["stats"]["median_ms"] if result.get("stats") else None, "baseline_median_ms": None, "ratio": None, "threshold_ratio": ratio}
    if result["outcome"] != "ok":
        return {**out, "status": result["outcome"], "explanation": f"Not compared: benchmark outcome is {result['outcome']}."}
    base = baseline.get("results", {}).get(key)
    if base is None:
        return {**out, "status": "missing_baseline", "explanation": "No baseline recorded for this operation/workload/fixture. This is NOT a pass; record a baseline explicitly with --update-baseline."}
    if base.get("environment", {}).get("python") != result["environment"].get("python") or base.get("environment", {}).get("cpu_count") != result["environment"].get("cpu_count"):
        env = "baseline was recorded on a different Python/CPU count; timings are only indicative"
    else:
        env = None
    cur, old = result["stats"]["median_ms"], base["median_ms"]
    r = cur / old if old > 0 else None
    out.update(baseline_median_ms=old, ratio=round(r, 3) if r else None)
    if env:
        return {**out, "status": "environment_mismatch", "explanation": env}
    if cur - old > floor_ms and r is not None and r > ratio:
        return {**out, "status": "regression", "explanation": f"median {cur:.1f} ms vs baseline {old:.1f} ms (x{r:.2f} > x{ratio}); review before accepting."}
    if r is not None and r < 1 / ratio and old - cur > floor_ms:
        return {**out, "status": "improved", "explanation": f"median {cur:.1f} ms vs baseline {old:.1f} ms (x{r:.2f})."}
    return {**out, "status": "ok", "explanation": f"within x{ratio} of the baseline (x{r:.2f})." if r else "within threshold."}


def record(path: Path, results: list[dict[str, Any]]) -> None:
    """Explicit baseline update: only successful, correctness-checked results are stored."""
    data = load(path)
    for r in results:
        if r["outcome"] == "ok" and r.get("stats"):
            data["results"][key_of(r)] = {"median_ms": r["stats"]["median_ms"], "n": r["stats"]["n"], "mad_ms": r["stats"]["mad_ms"], "environment": r["environment"]}
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", "utf-8")
