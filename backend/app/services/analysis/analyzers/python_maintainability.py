"""Maintainability and complexity rules for Python."""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Iterator

from app.services.analysis.analyzers.py_facts import loc
from app.services.analysis.complexity import cyclomatic_complexity, max_nesting_depth
from app.services.analysis.model import Analyzer, OptionSpec, RawFinding, Rule, RuleContext, RuleSpec

PY = ("Python",)
RULES: list[Rule] = []


def rule(spec: RuleSpec):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        RULES.append(Rule(spec, fn))
        return fn

    return deco


@rule(RuleSpec(
    id="PY-MNT-001", title="Function is too long", category="maintainability", severity="low", languages=PY, version="1.0",
    description="Very long functions are hard to read, test and change.",
    remediation="Extract cohesive blocks into well-named helper functions.",
    limitations="Length is the physical line span of the definition including comments and blank lines.",
    options=(OptionSpec("max_lines", 80, 10, 5000, "Maximum lines from `def` to the last line"),)))
def long_function(ctx: RuleContext) -> Iterator[RawFinding]:
    limit = int(ctx.options["max_lines"])
    for fn in ctx.facts.functions:
        if fn.line_span > limit:
            line, end_line, col, end_col = loc(fn.node)
            yield RawFinding("PY-MNT-001", line, end_line, col, end_col, fn.qualname, "length",
                             f"`{fn.node.name}` spans {fn.line_span} lines (limit {limit}).",
                             f"Definition spans lines {line}-{end_line}.", metric=float(fn.line_span), def_line=line)


@rule(RuleSpec(
    id="PY-MNT-002", title="File is too long", category="maintainability", severity="low", languages=PY, version="1.0",
    description="Very large modules usually mix responsibilities.",
    remediation="Split the module along responsibility boundaries.",
    limitations="Counts physical lines, including comments and blank lines.",
    options=(OptionSpec("max_lines", 1000, 50, 100000, "Maximum number of lines in a file"),)))
def long_file(ctx: RuleContext) -> Iterator[RawFinding]:
    limit = int(ctx.options["max_lines"])
    if ctx.file.line_count > limit:
        yield RawFinding("PY-MNT-002", 1, ctx.file.line_count, None, None, "", "file",
                         f"The file has {ctx.file.line_count} lines (limit {limit}).",
                         f"Line count {ctx.file.line_count}.", metric=float(ctx.file.line_count))


@rule(RuleSpec(
    id="PY-MNT-003", title="Excessive nesting depth", category="maintainability", severity="low", languages=PY, version="1.0",
    description="Deeply nested if/for/while/try/with blocks are hard to follow.",
    remediation="Use early returns or guard clauses, or extract inner blocks into functions.",
    limitations="Counts nested control structures within one function; an `elif` chain does not deepen the nesting.",
    options=(OptionSpec("max_depth", 4, 2, 20, "Maximum nesting depth of control structures"),)))
def deep_nesting(ctx: RuleContext) -> Iterator[RawFinding]:
    limit = int(ctx.options["max_depth"])
    for fn in ctx.facts.functions:
        depth = max_nesting_depth(fn.node)
        if depth > limit:
            line, end_line, col, end_col = loc(fn.node)
            yield RawFinding("PY-MNT-003", line, end_line, col, end_col, fn.qualname, "nesting",
                             f"`{fn.node.name}` nests control structures {depth} levels deep (limit {limit}).",
                             f"Maximum nesting depth {depth}.", metric=float(depth), def_line=line)


@rule(RuleSpec(
    id="PY-MNT-004", title="Too many parameters", category="maintainability", severity="low", languages=PY, version="1.0",
    description="Functions with many parameters are hard to call correctly.",
    remediation="Group related parameters into a dataclass or configuration object.",
    limitations="`self`, `cls`, `*args` and `**kwargs` are not counted.",
    options=(OptionSpec("max_params", 6, 1, 50, "Maximum number of named parameters"),)))
def many_params(ctx: RuleContext) -> Iterator[RawFinding]:
    limit = int(ctx.options["max_params"])
    for fn in ctx.facts.functions:
        a = fn.node.args
        names = [x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs] if x.arg not in ("self", "cls")]
        if len(names) > limit:
            line, end_line, col, end_col = loc(fn.node)
            yield RawFinding("PY-MNT-004", line, end_line, col, end_col, fn.qualname, "params",
                             f"`{fn.node.name}` takes {len(names)} parameters (limit {limit}).",
                             f"{len(names)} named parameters.", metric=float(len(names)), def_line=line)


@rule(RuleSpec(
    id="PY-CPX-001", title="High cyclomatic complexity", category="complexity", severity="medium", languages=PY, version="1.0",
    description="Many independent paths through a function make it hard to test exhaustively.",
    remediation="Split the function, replace branching with data tables or polymorphism, or simplify conditions.",
    limitations="McCabe-style count of syntactic decision points (see STATIC_ANALYSIS.md); it is not a measure of "
                "cognitive difficulty and does not model exceptions raised by calls.",
    options=(OptionSpec("max_complexity", 10, 2, 200, "Maximum cyclomatic complexity of one function"),)))
def high_complexity(ctx: RuleContext) -> Iterator[RawFinding]:
    limit = int(ctx.options["max_complexity"])
    for fn in ctx.facts.functions:
        value = cyclomatic_complexity(fn.node)
        if value > limit:
            line, end_line, col, end_col = loc(fn.node)
            yield RawFinding("PY-CPX-001", line, end_line, col, end_col, fn.qualname, "complexity",
                             f"`{fn.node.name}` has cyclomatic complexity {value} (limit {limit}).",
                             f"Counted {value - 1} decision point(s) plus 1.", metric=float(value),
                             severity="high" if value > 2 * limit else None, def_line=line)


# ---------------------------------------------------------------------------------------------- unused imports
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _names_used(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 300:
            # String annotations, `__all__` entries and typing.cast("T", x) refer to names by text.
            used.update(_WORDS.findall(node.value))
    return used


@rule(RuleSpec(
    id="PY-MNT-005", title="Unused import", category="maintainability", severity="low", languages=PY, version="1.0",
    description="An imported name that is never referenced adds noise and can hide dependency problems.",
    remediation="Remove the import, or re-export it explicitly via `__all__` if it is part of the module's interface.",
    limitations="Module-level imports only. Not reported: `__init__.py` files, `from __future__` imports, star imports, "
                "names listed in `__all__` or mentioned in any string literal (string annotations), and imports guarded "
                "by try/except ImportError or `if TYPE_CHECKING`. Side-effect imports are indistinguishable from unused ones.",
    confidence="medium", support="partial"))
def unused_imports(ctx: RuleContext) -> Iterator[RawFinding]:
    if ctx.file.is_init:
        return
    used = _names_used(ctx.file.tree)
    for stmt in ctx.file.tree.body:
        if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.ImportFrom) and (stmt.module == "__future__"):
            continue
        for alias in stmt.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if bound not in used and bound != "_":
                line, end_line, col, end_col = loc(stmt)
                yield RawFinding("PY-MNT-005", line, end_line, col, end_col, "", f"import:{bound}",
                                 f"`{bound}` is imported but never used in this file.",
                                 f"No reference to `{bound}` found in code, annotations or string literals.")


# ---------------------------------------------------------------------------------------------- unused locals
@rule(RuleSpec(
    id="PY-MNT-006", title="Unused local variable", category="maintainability", severity="info", languages=PY, version="1.0",
    description="A local variable is assigned but never read.",
    remediation="Remove the assignment, or use `_` to show the value is intentionally ignored.",
    limitations="Only simple `name = value` assignments in a function are considered (not tuple unpacking, loop or with targets). "
                "Skipped in functions that use `locals()`, `vars()`, `eval`, `exec`, `global` or `nonlocal`.",
    confidence="medium", support="partial"))
def unused_locals(ctx: RuleContext) -> Iterator[RawFinding]:
    for fn in ctx.facts.functions:
        assigned: dict[str, ast.Name] = {}
        loaded: set[str] = set()
        skip = False
        for node in ast.walk(fn.node):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                skip = True
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                    "locals", "vars", "eval", "exec"):
                skip = True
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if len(node.value) < 300:
                    loaded.update(_WORDS.findall(node.value))
        if skip:
            continue
        for stmt in ast.walk(fn.node):
            targets: list[ast.expr] = []
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets = [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    assigned.setdefault(t.id, t)
        for name, node in sorted(assigned.items(), key=lambda kv: kv[1].lineno):
            if name not in loaded:
                line, end_line, col, end_col = loc(node)
                yield RawFinding("PY-MNT-006", line, end_line, col, end_col, fn.qualname, f"local:{name}",
                                 f"Local variable `{name}` in `{fn.node.name}` is assigned but never read.",
                                 f"No load of `{name}` found in the function.", def_line=fn.node.lineno)


# ---------------------------------------------------------------------------------------------- duplicates
@rule(RuleSpec(
    id="PY-MNT-007", title="Duplicate function bodies in one file", category="maintainability", severity="low",
    languages=PY, version="1.0",
    description="Two functions in the same file have structurally identical bodies.",
    remediation="Extract the shared logic, or parameterise one function.",
    limitations="Detects exact structural equality (names, constants and operators identical) within a single file only; "
                "near-duplicates and duplication across files are not detected.",
    confidence="medium", support="partial",
    options=(OptionSpec("min_statements", 4, 2, 100, "Minimum statements in a body before it is compared"),)))
def duplicate_bodies(ctx: RuleContext) -> Iterator[RawFinding]:
    minimum = int(ctx.options["min_statements"])
    groups: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = defaultdict(list)
    names: dict[int, str] = {}
    for fn in ctx.facts.functions:
        body = fn.node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # ignore docstrings
        if len(body) < minimum:
            continue
        key = "|".join(ast.dump(s, annotate_fields=False, include_attributes=False) for s in body)
        groups[key].append(fn.node)
        names[id(fn.node)] = fn.qualname
    for nodes in groups.values():
        for dup in nodes[1:]:
            line, end_line, col, end_col = loc(dup)
            first = nodes[0]
            yield RawFinding("PY-MNT-007", line, end_line, col, end_col, names[id(dup)], f"dup-of:{names[id(first)]}",
                             f"`{dup.name}` has the same body as `{first.name}` (line {first.lineno}).",
                             f"Structurally identical body of {len(dup.body)} statement(s).", def_line=line)


def analyzer() -> Analyzer:
    return Analyzer("py-maintainability", "Python maintainability and complexity", "1.0.0", PY, RULES)
