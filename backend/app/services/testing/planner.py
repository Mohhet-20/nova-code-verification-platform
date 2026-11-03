"""Deterministic test-case planning from signatures, guards, docstring examples and findings.

Rule of thumb: an *executable* case is planned only when the expected outcome is determined by the code itself
(an explicit guard that raises, a boundary of that guard) or by documentation (a docstring example). Every other idea is
planned as a *template* that is skipped when run and says what a developer must decide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.testing.facts import (
    EMPTY_ANNOTATION_VALUES,
    SAFE_ANNOTATION_VALUES,
    FuncFacts,
    Guard,
    Param,
)

GENERATOR_VERSION = "1.0"
CATEGORIES = ("normal", "boundary", "empty", "invalid", "exception", "regression", "invariant", "contract", "property")
# Findings worth a regression template, with the behaviour a developer must pin down.
REGRESSION_RULES = {
    "PY-COR-001": "comparison to None",
    "PY-COR-002": "mutable default argument shared between calls",
    "PY-COR-003": "unreachable code",
    "PY-REL-001": "exception swallowed silently",
    "PY-REL-002": "broad exception handler",
    "PY-SEC-001": "dynamic code execution",
    "PY-SEC-002": "shell execution",
    "PY-SEC-005": "unsafe deserialization",
}


@dataclass
class Case:
    id: str
    test_name: str
    target_symbol: str
    category: str
    title: str
    purpose: str
    preconditions: str
    input_strategy: str
    expected: str
    rationale: str
    confidence: str
    assumptions: list[str] = field(default_factory=list)
    review_required: bool = False
    runnable: bool = True
    # Rendering payload (not persisted verbatim): "kind" selects the template.
    render: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "render"}
        return d


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "x"


def violates(g: Guard, value: Any) -> bool:
    if g.kind == "none":
        return value is None
    if g.kind == "falsy":
        return not value
    if g.kind == "empty_len":
        try:
            return len(value) == 0
        except TypeError:
            return False
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    c = g.value
    return {"<": value < c, "<=": value <= c, ">": value > c, ">=": value >= c, "==": value == c, "!=": value != c}[g.op or "=="]  # type: ignore[operator]


def first_fired(guards: list[Guard], values: dict[str, Any]) -> Guard | None:
    for g in guards:
        if g.param in values and violates(g, values[g.param]):
            return g
    return None


def _num(v: float | int, annotation: str | None) -> float | int:
    return float(v) if annotation == "float" else int(v) if float(v).is_integer() else v


def _ann(p: Param) -> str | None:
    return p.annotation.strip() if p.annotation else None


def baseline_value(p: Param, guards: list[Guard]) -> tuple[bool, Any]:
    """A value that triggers none of ``p``'s guards. (ok, value)."""
    mine = [g for g in guards if g.param == p.name]
    ann = _ann(p)
    numeric = [g for g in mine if g.kind == "compare"]
    if numeric:
        consts = [g.value for g in numeric]
        cands: list[float | int] = [1, 0, -1, 2, 10, 100]
        for c in consts:
            cands += [c - 1, c, c + 1]  # type: ignore[operator]
        for c in cands:
            v = _num(c, ann)
            if not any(violates(g, v) for g in mine):
                return True, v
        return False, None
    if ann in SAFE_ANNOTATION_VALUES:
        v = SAFE_ANNOTATION_VALUES[ann]
        v = type(v)(v) if isinstance(v, (list, dict, set)) else v
        if not any(violates(g, v) for g in mine):
            return True, v
    return False, None


def violating_value(g: Guard, guards: list[Guard], params: dict[str, Param]) -> tuple[bool, Any]:
    p = params[g.param]
    ann = _ann(p)
    if g.kind == "none":
        cands: list[Any] = [None]
    elif g.kind in ("falsy", "empty_len"):
        cands = [EMPTY_ANNOTATION_VALUES[ann]] if ann in EMPTY_ANNOTATION_VALUES else [0] if ann == "int" else \
            [0.0] if ann == "float" else [False] if ann == "bool" else ([None] if g.kind == "falsy" else [])
    else:
        c = g.value
        base = {"<": c - 1, "<=": c, ">": c + 1, ">=": c, "==": c, "!=": c + 1}[g.op or "=="]  # type: ignore[operator]
        cands = [_num(base + d, ann) for d in (0, -1, 1, -2, 2)]
    for v in cands:
        fired = first_fired(guards, {g.param: v})
        if fired is not None and fired.exc == g.exc and violates(g, v):
            return True, v
    return False, None


def boundary_value(g: Guard, guards: list[Guard], params: dict[str, Param]) -> tuple[bool, Any]:
    if g.kind != "compare":
        return False, None
    ann = _ann(params[g.param])
    c = g.value
    base = {"<": c, "<=": c + 1, ">": c, ">=": c - 1, "==": c + 1, "!=": c}[g.op or "=="]  # type: ignore[operator]
    v = _num(base, ann)
    if first_fired(guards, {g.param: v}) is None:
        return True, v
    return False, None


def literal(v: Any) -> str:
    return repr(v)


def call_args(f: FuncFacts, overrides: dict[str, Any]) -> tuple[bool, str, list[str]]:
    """(ok, "a=1, b='x'", missing). Required parameters are passed by keyword so order does not matter."""
    parts: list[str] = []
    missing: list[str] = []
    for p in f.params:
        if p.kind in ("var_positional", "var_keyword"):
            continue
        if p.name in overrides:
            parts.append(f"{p.name}={literal(overrides[p.name])}")
            continue
        if p.has_default:
            continue
        ok, v = baseline_value(p, f.guards)
        if ok:
            parts.append(f"{p.name}={literal(v)}")
        else:
            missing.append(p.name)
    return not missing, ", ".join(parts), missing


def _signature(f: FuncFacts) -> str:
    ps = ", ".join(f"{p.name}: {p.annotation}" if p.annotation else p.name for p in f.params)
    return f"{f.qualname}({ps})" + (f" -> {f.returns}" if f.returns else "")


def plan_function(f: FuncFacts, findings: list[dict[str, Any]], *, include_templates: bool = True) -> list[Case]:
    cases: list[Case] = []
    base_name = _slug(f.qualname.replace(".", "_"))
    counter: dict[str, int] = {}

    def name(kind: str) -> str:
        counter[kind] = counter.get(kind, 0) + 1
        suffix = "" if counter[kind] == 1 else f"_{counter[kind]}"
        return f"test_{base_name}_{kind}{suffix}"

    def template(category: str, title: str, purpose: str, why: str, expected: str, *, strategy: str = "",
                 conf: str = "low", assumptions: list[str] | None = None, extra: dict[str, Any] | None = None) -> None:
        if not include_templates:
            return
        tn = name(category)
        cases.append(Case(
            id=f"{f.qualname}:{tn}", test_name=tn, target_symbol=f.qualname, category=category, title=title, purpose=purpose,
            preconditions="A developer decides the expected behaviour before enabling this test.",
            input_strategy=strategy or "To be chosen by a developer.", expected=expected, rationale=why, confidence=conf,
            assumptions=assumptions or [], review_required=True, runnable=False,
            render={"kind": "template", "signature": _signature(f), **(extra or {})}))

    params = {p.name: p for p in f.params}
    unrunnable = f.skip_reason
    ok_base, base_args, missing = (False, "", []) if unrunnable else call_args(f, {})
    if unrunnable:
        template("normal", f"{f.qualname}: define a representative call", "Exercise the primary behaviour.",
                 unrunnable, "Developer-defined.", assumptions=[unrunnable])
    elif not ok_base and f.params:
        template("normal", f"{f.qualname}: define representative arguments", "Exercise the primary behaviour.",
                 f"No safe argument values can be derived for: {', '.join(missing)} (missing or unsupported type hints).",
                 "Developer-defined.", assumptions=[f"parameters without usable type hints: {', '.join(missing)}"])
    if not unrunnable and (ok_base or not f.params):
        # --- documented examples: the docstring is an independent oracle
        for ex in f.examples:
            tn = name("example")
            cases.append(Case(
                id=f"{f.qualname}:{tn}", test_name=tn, target_symbol=f.qualname, category="normal",
                title=f"{f.qualname}: documented example `{ex.call}`", purpose="Check the example given in the docstring.",
                preconditions="None.", input_strategy=f"Literal arguments from the docstring: {ex.call}",
                expected=f"Result equals {ex.expected} as documented.",
                rationale="The docstring states this result, which is independent of the implementation.", confidence="medium",
                assumptions=["The docstring example is current."], render={"kind": "example", "call": ex.call, "expected": ex.expected}))
        # --- guards: exception + boundary
        for g in f.guards:
            ok, bad = violating_value(g, f.guards, params)
            if ok:
                ok2, args, _ = call_args(f, {g.param: bad})
                if ok2:
                    tn = name("raises")
                    cases.append(Case(
                        id=f"{f.qualname}:{tn}", test_name=tn, target_symbol=f.qualname, category="exception",
                        title=f"{f.qualname}: raises {g.exc} for invalid `{g.param}`",
                        purpose=f"Check the validation guard at line {g.line}.",
                        preconditions="Other arguments are valid values derived from type hints.",
                        input_strategy=f"`{g.param}` = {literal(bad)} (violates the guard); other arguments from type hints.",
                        expected=f"Raises {g.exc}.", rationale=f"The function contains an explicit `raise {g.exc}` guarded by a condition on `{g.param}` (line {g.line}).",
                        confidence="high", assumptions=["The guard is intended behaviour, not a defect."],
                        render={"kind": "raises", "args": args, "exc": g.exc}))
            okb, edge = boundary_value(g, f.guards, params)
            if okb:
                ok2, args, _ = call_args(f, {g.param: edge})
                if ok2:
                    tn = name("boundary")
                    cases.append(Case(
                        id=f"{f.qualname}:{tn}", test_name=tn, target_symbol=f.qualname, category="boundary",
                        title=f"{f.qualname}: `{g.param}` = {literal(edge)} is not rejected as {g.exc}",
                        purpose=f"Check the edge of the condition at line {g.line}.", preconditions="Other arguments are valid.",
                        input_strategy=f"`{g.param}` = {literal(edge)}, the nearest value the guard `{g.param} {g.op} {g.value}` lets through.",
                        expected=f"No {g.exc} is raised for this value (other outcomes are not asserted).",
                        rationale="Boundary of an explicit comparison in the validation preamble.", confidence="high",
                        assumptions=["Behaviour after the guard is outside this test's claim."],
                        render={"kind": "not_raises", "args": args, "exc": g.exc}))
        # --- type contract
        ret = (f.returns or "").strip()
        if ret in SAFE_ANNOTATION_VALUES and ok_base and not f.guards and not f.raises:
            tn = name("contract")
            cases.append(Case(
                id=f"{f.qualname}:{tn}", test_name=tn, target_symbol=f.qualname, category="contract",
                title=f"{f.qualname}: returns {ret}", purpose="Check the declared return type.",
                preconditions="Arguments derived from type hints are valid for this function.",
                input_strategy="One call with simple values chosen from the parameter type hints.",
                expected=f"Result is an instance of {ret}.", rationale="Return annotation is the only documented contract.",
                confidence="low", assumptions=["The derived arguments are acceptable inputs.", "Values are not asserted."],
                review_required=True, render={"kind": "contract", "args": base_args, "type": ret}))
    # --- ideas that need a developer
    guarded = {g.param for g in f.guards}
    for p in f.params:
        ann = _ann(p)
        if ann in EMPTY_ANNOTATION_VALUES and p.name not in guarded and p.kind in ("positional", "keyword_only"):
            template("empty", f"{f.qualname}: empty `{p.name}`", f"Define behaviour for an empty {ann}.",
                     f"`{p.name}` is annotated {ann} and nothing in the code validates emptiness.",
                     "Developer-defined (empty result, default, or error).", strategy=f"`{p.name}` = {literal(EMPTY_ANNOTATION_VALUES[ann])}",
                     extra={"param": p.name})
            break
    if f.raises and len(f.guards) < len(f.raises):
        lines = ", ".join(str(ln) for _, ln in f.raises[:5])
        template("exception", f"{f.qualname}: error paths", "Cover the raise statements not found in the validation preamble.",
                 f"Raise statements at lines {lines} are conditioned on logic that is not a simple parameter check.",
                 "Developer-defined: which inputs reach each raise.", conf="medium")
    if f.handlers:
        template("exception", f"{f.qualname}: exception handling", "Cover the except handlers.",
                 f"{f.handlers} except handler(s); what triggers them depends on collaborators.", "Developer-defined.", conf="medium")
    if f.loops and not f.examples:
        template("invariant", f"{f.qualname}: loop termination and empty iteration", "Cover zero, one and many iterations.",
                 f"{f.loops} loop(s) detected.", "Developer-defined.", conf="low")
    for fd in findings:
        if fd["rule_id"] in REGRESSION_RULES and f.start_line <= fd["start_line"] <= f.end_line:
            template("regression", f"{f.qualname}: regression for {fd['rule_id']}",
                     f"Pin the intended behaviour around: {REGRESSION_RULES[fd['rule_id']]}.",
                     f"Static analysis finding {fd['rule_id']} at line {fd['start_line']} ({fd['title']}).",
                     "Developer-defined; write the failing case first, then fix.", conf="medium",
                     extra={"finding": fd["rule_id"], "line": fd["start_line"]})
    return cases
