"""Shared vocabulary and plain data types for analyzers. No database access here."""
from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}  # lower = more severe
CATEGORIES = ("correctness", "maintainability", "reliability", "security", "complexity")
CONFIDENCES = ("high", "medium", "low")
STATUSES = ("new", "existing", "resolved", "reopened", "suppressed")
OPEN_STATUSES = ("new", "existing", "reopened")
RESOLUTION_REASONS = ("fixed", "file_removed", "rule_disabled", "file_excluded")

# Confidence semantics (documented in docs/architecture/STATIC_ANALYSIS.md):
#   high   - a direct syntactic fact; the construct is present exactly as described.
#   medium - a pattern that is usually a problem but can be intentional or valid in context.
#   low    - an incomplete heuristic that needs manual review.


@dataclass(frozen=True)
class OptionSpec:
    name: str
    default: int | float | bool
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""


@dataclass(frozen=True)
class RuleSpec:
    id: str
    title: str
    description: str
    category: str
    severity: str
    languages: tuple[str, ...]
    version: str
    remediation: str
    limitations: str
    confidence: str = "high"
    options: tuple[OptionSpec, ...] = ()
    references: tuple[str, ...] = ()
    support: str = "implemented"  # implemented | partial


@dataclass
class RawFinding:
    """What a rule reports. Locations are 1-based lines; columns are 1-based character offsets."""

    rule_id: str
    line: int
    end_line: int
    col: int | None
    end_col: int | None
    scope: str  # enclosing "Class.function" path; "" for module level
    discriminator: str  # rule-specific, location-independent identity of the construct
    explanation: str
    evidence: str
    confidence: str | None = None  # None = rule default
    severity: str | None = None  # None = rule default (may still be overridden by configuration)
    metric: float | None = None
    def_line: int | None = None  # line of the enclosing definition, for symbol resolution


@dataclass
class FileInput:
    rel_path: str
    source: str
    tree: ast.Module
    line_count: int

    @property
    def is_init(self) -> bool:
        return self.rel_path.rsplit("/", 1)[-1] == "__init__.py"


@dataclass
class RuleContext:
    file: FileInput
    options: dict[str, Any]
    facts: Any  # FileFacts (python)


Check = Callable[[RuleContext], Iterable[RawFinding]]


@dataclass
class Rule:
    spec: RuleSpec
    check: Check


@dataclass
class AnalyzerResult:
    findings: list[RawFinding] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (rule_id, exception type name)


class Analyzer:
    """A named, versioned group of rules for some languages. Subclass-free: construct with a rule list."""

    def __init__(self, id: str, name: str, version: str, languages: tuple[str, ...], rules: list[Rule]) -> None:  # noqa: A002
        self.id, self.name, self.version, self.languages, self.rules = id, name, version, languages, rules

    def run(self, ctx_factory: Callable[[dict[str, Any]], RuleContext], enabled: dict[str, dict[str, Any]]) -> AnalyzerResult:
        """Run each enabled rule in isolation: one rule raising does not discard the others' findings,
        but it is reported so the file is not treated as fully analysed by this analyzer."""
        result = AnalyzerResult()
        for rule in self.rules:
            options = enabled.get(rule.spec.id)
            if options is None:
                continue
            try:
                result.findings.extend(rule.check(ctx_factory(options)))
            except Exception as exc:  # a rule bug must never take the run down or leak source text
                result.errors.append((rule.spec.id, type(exc).__name__))
        return result
