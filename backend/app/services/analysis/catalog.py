"""The registry of analyzers and the rule catalog. Adding an analyzer means appending it to ``ANALYZERS``."""
from __future__ import annotations

from app.services.analysis.analyzers import (
    python_correctness,
    python_maintainability,
    python_security,
    python_security_flow,
)
from app.services.analysis.model import Analyzer, RuleSpec

ANALYZERS: list[Analyzer] = [
    python_correctness.analyzer(),
    python_maintainability.analyzer(),
    python_security.analyzer(),
    python_security_flow.analyzer(),
]


def all_rule_specs() -> list[RuleSpec]:
    return [r.spec for a in ANALYZERS for r in a.rules]


def rule_index() -> dict[str, tuple[Analyzer, RuleSpec]]:
    out: dict[str, tuple[Analyzer, RuleSpec]] = {}
    for a in ANALYZERS:
        for r in a.rules:
            if r.spec.id in out:
                raise RuntimeError(f"duplicate rule id {r.spec.id}")  # programming error, caught by tests
            out[r.spec.id] = (a, r.spec)
    return out


def analyzer_versions() -> dict[str, str]:
    versions = {a.id: a.version for a in ANALYZERS}
    versions.update({r.spec.id: r.spec.version for a in ANALYZERS for r in a.rules})
    return versions
