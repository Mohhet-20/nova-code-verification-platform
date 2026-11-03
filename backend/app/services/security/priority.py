"""Transparent, versioned remediation prioritisation. It ranks; it never reclassifies severity, and it invents no exploitability, exposure, asset value or business impact.

score = severity_base x confidence_factor x evidence_factor + reachability_bonus + fix_bonus + scope_adjustment (clamped to 0-100).
Severity stays as reported. A low-confidence critical or high finding is ranked lower but never below the floor bucket (critical -> P2, high -> P3).
"""
from __future__ import annotations

from typing import Any

MODEL = "nova.priority/1"
DEFAULT_WEIGHTS: dict[str, Any] = {
    "severity_base": {"critical": 100, "high": 75, "medium": 50, "low": 25, "info": 10},
    "confidence_factor": {"high": 1.0, "medium": 0.8, "low": 0.55},
    "evidence_factor": {"dataflow": 1.0, "exact_version": 1.0, "syntactic": 0.9, "heuristic": 0.7, "unknown": 0.6},
    "reachability_bonus": {"request_reachable": 10, "imported": 8, "unknown": 0},
    "fix_bonus": 5,
    "scope_adjustment": {"runtime": 0, "optional": -5, "dev": -10, "unknown": 0},
    "buckets": {"P1": 80, "P2": 60, "P3": 40},
    "floor": {"critical": "P2", "high": "P3"},
}
_ORDER = ["P1", "P2", "P3", "P4"]


def weights(overrides: dict[str, Any] | None) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_WEIGHTS.items()}
    for k, v in (overrides or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k].update({kk: vv for kk, vv in v.items() if kk in out[k] and isinstance(vv, int | float)})
        elif k == "fix_bonus" and isinstance(v, int | float):
            out[k] = v
    return out


def validate_overrides(raw: Any) -> dict[str, Any]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or any(k not in DEFAULT_WEIGHTS or k in ("buckets", "floor") and not isinstance(v, dict) for k, v in raw.items()):
        raise ValueError("priority overrides must be an object with known keys")
    return {k: v for k, v in raw.items()}


def score(finding: dict[str, Any], w: dict[str, Any]) -> dict[str, Any]:
    sev = finding.get("severity", "medium")
    conf = finding.get("confidence", "medium")
    evq = finding.get("evidence_quality", "unknown")
    reach = finding.get("reachability", "unknown")
    scope = finding.get("scope_kind", "unknown")
    fix = bool(finding.get("fix_available"))
    base = w["severity_base"].get(sev, 50)
    cf, ef = w["confidence_factor"].get(conf, 0.8), w["evidence_factor"].get(evq, 0.6)
    rb, sa = w["reachability_bonus"].get(reach, 0), w["scope_adjustment"].get(scope, 0)
    fb = w["fix_bonus"] if fix else 0
    raw = base * cf * ef + rb + fb + sa
    value = round(max(0.0, min(100.0, raw)), 1)
    b = w["buckets"]
    bucket = "P1" if value >= b["P1"] else "P2" if value >= b["P2"] else "P3" if value >= b["P3"] else "P4"
    floor = w["floor"].get(sev)
    floored = False
    if floor and _ORDER.index(bucket) > _ORDER.index(floor):
        bucket, floored = floor, True
    factors = [
        f"severity {sev} -> base {base}",
        f"confidence {conf} -> x{cf}",
        f"evidence quality {evq} -> x{ef}",
        f"reachability {reach} -> {rb:+}" + (" (observed; absence of a signal is never used to lower the score)" if reach == "unknown" else ""),
        f"fix {'available' if fix else 'not established'} -> {fb:+}",
        f"scope {scope} -> {sa:+}",
    ]
    if floored:
        factors.append(f"floor: a {sev} finding is not ranked below {floor}")
    return {"model": MODEL, "score": value, "bucket": bucket, "factors": factors, "unknowns": ["exploitability", "production exposure", "asset criticality", "business impact"],
            "note": "Ranking aid only. Severity and confidence are reported separately and are not changed by this score."}
