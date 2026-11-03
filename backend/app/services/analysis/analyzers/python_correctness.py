"""Correctness and exception-handling rules for Python. Each rule is small, independent and individually configurable."""
from __future__ import annotations

import ast
from collections.abc import Iterator

from app.services.analysis.analyzers.py_facts import loc
from app.services.analysis.model import Analyzer, RawFinding, Rule, RuleContext, RuleSpec

PY = ("Python",)
RULES: list[Rule] = []


def rule(spec: RuleSpec):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        RULES.append(Rule(spec, fn))
        return fn

    return deco


# ---------------------------------------------------------------------------------------------- PY-COR-001
@rule(RuleSpec(
    id="PY-COR-001", title="Comparison to None with == or !=", category="correctness", severity="low",
    languages=PY, version="1.0",
    description="Equality operators can be overridden by a class, so `x == None` may not mean 'x is None'.",
    remediation="Use `x is None` or `x is not None`. Disable this rule if your project's style allows equality comparison.",
    limitations="Only direct comparisons against the literal None are found; aliases of None are not tracked.",
    references=("PEP 8: Programming Recommendations",)))
def none_comparison(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.Eq, ast.NotEq)) and any(
                isinstance(o, ast.Constant) and o.value is None for o in (operands[i], operands[i + 1])):
                line, end_line, col, end_col = loc(node)
                sym = "==" if isinstance(op, ast.Eq) else "!="
                yield RawFinding(
                    "PY-COR-001", line, end_line, col, end_col, scope, f"{sym}:{ast.unparse(node)}",
                    f"A comparison uses `{sym}` against None.",
                    f"Compare node with operator `{sym}` and a None operand.", def_line=def_line)
                break


# ---------------------------------------------------------------------------------------------- PY-COR-002
_MUTABLE_CALLS = {"list", "dict", "set", "bytearray", "defaultdict", "deque", "OrderedDict", "Counter"}


def _is_mutable_default(node: ast.AST) -> str | None:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return type(node).__name__.lower()
    if isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp)):
        return "comprehension"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MUTABLE_CALLS:
        return f"{node.func.id}() call"
    return None


@rule(RuleSpec(
    id="PY-COR-002", title="Mutable default argument", category="correctness", severity="medium",
    languages=PY, version="1.0",
    description="A default value is created once when the function is defined and shared by every call, so mutations leak between calls.",
    remediation="Default to None and create the container inside the function body.",
    limitations="Only literal containers and calls to common mutable constructors are recognised; "
                "a mutable object returned by another call is not detected.",
    references=("Python docs: Default Argument Values",)))
def mutable_default(ctx: RuleContext) -> Iterator[RawFinding]:
    for fn in ctx.facts.functions:
        a = fn.node.args
        pos = [*a.posonlyargs, *a.args]
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults, strict=False))
        pairs += [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
        for arg, default in pairs:
            kind = _is_mutable_default(default)
            if kind:
                line, end_line, col, end_col = loc(default)
                yield RawFinding(
                    "PY-COR-002", line, end_line, col, end_col, fn.qualname, f"param:{arg.arg}",
                    f"Parameter `{arg.arg}` of `{fn.node.name}` has a mutable default value.",
                    f"Default value is a {kind}.", def_line=fn.node.lineno)


# ---------------------------------------------------------------------------------------------- PY-COR-003
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _blocks(node: ast.AST) -> Iterator[list[ast.stmt]]:
    for name in ("body", "orelse", "finalbody"):
        block = getattr(node, name, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            yield block


@rule(RuleSpec(
    id="PY-COR-003", title="Unreachable code after return, raise, break or continue", category="correctness",
    severity="low", languages=PY, version="1.0",
    description="Statements that follow an unconditional return, raise, break or continue in the same block can never run.",
    remediation="Remove the dead statements, or fix the control flow that makes them unreachable.",
    limitations="Only straight-line code in one block is analysed. Reachability that depends on conditions, "
                "`sys.exit()`, infinite loops or NoReturn calls is not considered.", ))
def unreachable_code(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        for block in _blocks(node):
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, _TERMINATORS):
                    nxt = block[i + 1]
                    line, _, col, _ = loc(nxt)
                    end_line = block[-1].end_lineno or line
                    yield RawFinding(
                        "PY-COR-003", line, end_line, col, None, scope,
                        f"{type(stmt).__name__}:{ast.dump(nxt, annotate_fields=False)[:200]}",
                        f"Code after `{type(stmt).__name__.lower()}` cannot be reached.",
                        f"{len(block) - i - 1} statement(s) follow an unconditional {type(stmt).__name__.lower()} "
                        f"on line {stmt.lineno}.", def_line=def_line)
                    break


# ---------------------------------------------------------------------------------------------- exception rules
_BROAD = {"Exception", "BaseException"}


def _handler_catches(h: ast.ExceptHandler) -> list[str] | None:
    """Names of caught exception types, or None for a bare `except:`."""
    if h.type is None:
        return None
    nodes = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
    return [n.id if isinstance(n, ast.Name) else (n.attr if isinstance(n, ast.Attribute) else "?") for n in nodes]


def _reraises(h: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for s in h.body for n in ast.walk(s))


def _trivial_body(h: ast.ExceptHandler) -> bool:
    return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in h.body)


@rule(RuleSpec(
    id="PY-REL-001", title="Empty exception handler", category="reliability", severity="medium",
    languages=PY, version="1.0",
    description="An except block that contains only `pass`, `...` or a constant discards the error without any trace.",
    remediation="Handle the error, log it, or re-raise. If ignoring is intended, narrow the exception type and add a comment "
                "(or use `contextlib.suppress`).",
    limitations="Intentional suppression of a narrow, expected exception (for example `FileNotFoundError`) is "
                "reported at lower confidence, not skipped."))
def empty_handler(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if isinstance(node, ast.ExceptHandler) and _trivial_body(node):
            caught = _handler_catches(node)
            broad = caught is None or any(c in _BROAD for c in caught)
            line, end_line, col, end_col = loc(node)
            what = "everything (bare except)" if caught is None else ", ".join(caught)
            yield RawFinding(
                "PY-REL-001", line, end_line, col, end_col, scope, f"empty:{what}",
                f"An exception handler for {what} does nothing.",
                "Handler body contains only pass/ellipsis/constant statements.",
                confidence="high" if broad else "medium", def_line=def_line)


@rule(RuleSpec(
    id="PY-REL-002", title="Broad exception handler", category="reliability", severity="low",
    languages=PY, version="1.0",
    description="Catching `Exception`, `BaseException` or everything can hide unexpected failures such as programming errors.",
    remediation="Catch the specific exceptions you expect. If a broad handler is required (for example at a process boundary), "
                "log the exception with its traceback and consider re-raising.",
    limitations="Handlers that re-raise are not reported. Whether a broad handler is acceptable at a given boundary "
                "cannot be decided statically.", confidence="medium"))
def broad_handler(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.ExceptHandler) or _reraises(node) or _trivial_body(node):
            continue  # trivial bodies are reported by PY-REL-001
        caught = _handler_catches(node)
        if caught is None or any(c in _BROAD for c in caught):
            line, end_line, col, end_col = loc(node)
            what = "a bare `except:`" if caught is None else f"`{', '.join(caught)}`"
            yield RawFinding(
                "PY-REL-002", line, end_line, col, end_col, scope, f"broad:{'bare' if caught is None else ','.join(caught)}",
                f"The handler catches {what} and does not re-raise.",
                "Broad or bare exception type without a raise statement in the handler body.",
                severity="medium" if caught is None or "BaseException" in caught else None, def_line=def_line)


@rule(RuleSpec(
    id="PY-REL-003", title="Exception handler discards the error silently", category="reliability", severity="low",
    languages=PY, version="1.0",
    description="A handler whose only effect is `continue`, `break` or returning a constant swallows the failure without logging "
                "or using the exception.",
    remediation="Log the exception or surface it to the caller before continuing or returning a default.",
    limitations="A handler may be correct when failure is expected and uninteresting; the analyzer cannot judge intent.",
    confidence="medium"))
def silent_flow_handler(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.ExceptHandler) or _trivial_body(node) or _reraises(node):
            continue
        if all(isinstance(s, (ast.Continue, ast.Break)) or (isinstance(s, ast.Return) and (
                s.value is None or isinstance(s.value, ast.Constant))) for s in node.body):
            caught = _handler_catches(node)
            line, end_line, col, end_col = loc(node)
            yield RawFinding(
                "PY-REL-003", line, end_line, col, end_col, scope, f"silent:{','.join(caught or ['bare'])}",
                "The handler only continues, breaks or returns a constant, so the error is discarded.",
                "Handler body has no call, logging, assignment or raise.", def_line=def_line)


def analyzer() -> Analyzer:
    return Analyzer("py-correctness", "Python correctness and error handling", "1.0.0", PY, RULES)
