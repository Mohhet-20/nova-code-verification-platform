"""Static facts about Python functions that make test planning evidence-based.

Everything here is read from the syntax tree. Nothing is imported or executed.
"""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from typing import Any

from app.services.analysis.complexity import cyclomatic_complexity

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SAFE_ANNOTATION_VALUES: dict[str, Any] = {"int": 1, "float": 1.0, "str": "a", "bool": True, "bytes": b"a",
                                          "list": [1], "dict": {"a": 1}, "tuple": (1,), "set": {1}}
EMPTY_ANNOTATION_VALUES: dict[str, Any] = {"str": "", "list": [], "dict": {}, "tuple": (), "set": set(), "bytes": b""}


@dataclass
class Param:
    name: str
    annotation: str | None
    has_default: bool
    kind: str  # positional | keyword_only | var_positional | var_keyword


@dataclass
class Guard:
    """``if <condition on param>: raise Exc`` found in the validation preamble of a function."""

    param: str
    kind: str  # compare | none | falsy | empty_len
    op: str | None
    value: float | int | None
    exc: str
    line: int


@dataclass
class Example:
    call: str  # source of the call expression
    expected: str  # literal source of the expected value
    line: int


@dataclass
class FuncFacts:
    name: str
    qualname: str
    kind: str  # function | method
    class_name: str | None
    start_line: int
    end_line: int
    params: list[Param]
    returns: str | None
    docstring: str | None
    public: bool
    is_async: bool
    is_generator: bool
    complexity: int
    branches: int
    loops: int
    handlers: int
    raises: list[tuple[str, int]]
    boundary_compares: list[tuple[str, str, float | int, int]]
    guards: list[Guard] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    skip_reason: str | None = None  # why no executable test can be generated for this function


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return None


def parse_source(text: str) -> ast.Module | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None


def _params(args: ast.arguments, skip_first: bool) -> list[Param]:
    out: list[Param] = []
    pos = [*args.posonlyargs, *args.args]
    n_defaults = len(args.defaults)
    for i, a in enumerate(pos):
        if skip_first and i == 0:
            continue
        out.append(Param(a.arg, _unparse(a.annotation), i >= len(pos) - n_defaults, "positional"))
    if args.vararg:
        out.append(Param(args.vararg.arg, _unparse(args.vararg.annotation), True, "var_positional"))
    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(Param(a.arg, _unparse(a.annotation), d is not None, "keyword_only"))
    if args.kwarg:
        out.append(Param(args.kwarg.arg, _unparse(args.kwarg.annotation), True, "var_keyword"))
    return out


def _number(node: ast.AST) -> float | int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _number(node.operand)
        return -inner if inner is not None else None
    return None


_OPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}


def _condition_guards(test: ast.expr, params: set[str], exc: str, line: int) -> list[Guard] | None:
    """Disjuncts of ``test`` as guards, or ``None`` when any part is not understood (then nothing is claimed)."""
    if isinstance(test, ast.BoolOp):
        if not isinstance(test.op, ast.Or):
            return None  # `a and b` raises only when both hold; a single-parameter violation would not trigger it
        out: list[Guard] = []
        for v in test.values:
            g = _condition_guards(v, params, exc, line)
            if g is None:
                return None
            out += g
        return out
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Name) \
            and test.operand.id in params:
        return [Guard(test.operand.id, "falsy", None, None, exc, line)]
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in (*_OPS, ast.Is):
        left, right, op = test.left, test.comparators[0], test.ops[0]
        if isinstance(op, ast.Is) and isinstance(left, ast.Name) and left.id in params \
                and isinstance(right, ast.Constant) and right.value is None:
            return [Guard(left.id, "none", None, None, exc, line)]
        if type(op) in _OPS:
            sym = _OPS[type(op)]
            if isinstance(left, ast.Name) and left.id in params and _number(right) is not None:
                return [Guard(left.id, "compare", sym, _number(right), exc, line)]
            if isinstance(right, ast.Name) and right.id in params and _number(left) is not None:
                return [Guard(right.id, "compare", _FLIP[sym], _number(left), exc, line)]
            if (isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "len"
                    and len(left.args) == 1 and isinstance(left.args[0], ast.Name) and left.args[0].id in params
                    and _number(right) == 0 and sym == "=="):
                return [Guard(left.args[0].id, "empty_len", sym, 0, exc, line)]
    return None


def _raise_name(stmt: ast.stmt) -> str | None:
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return None
    exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
    if isinstance(exc, ast.Name):
        return exc.id
    return None


def extract_guards(fn: ast.FunctionDef | ast.AsyncFunctionDef, params: list[Param]) -> list[Guard]:
    """Validation preamble only: leading ``if cond: raise Exc`` statements (after an optional docstring).

    Stops at the first statement that is not such a guard, because later statements may have changed the parameters.
    """
    names = {p.name for p in params if p.kind in ("positional", "keyword_only")}
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    out: list[Guard] = []
    for stmt in body:
        if not (isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1):
            break
        exc = _raise_name(stmt.body[0])
        if exc is None:
            break
        guards = _condition_guards(stmt.test, names, exc, stmt.lineno)
        if guards is None:
            break
        out += guards
    return out


def extract_examples(doc: str | None, fn_name: str, first_line: int) -> list[Example]:
    """``>>> name(literal args)`` followed by a literal one-line result. Anything else is ignored."""
    if not doc or ">>>" not in doc:
        return []
    lines = doc.splitlines()
    out: list[Example] = []
    i = 0
    while i < len(lines) - 1:
        s = lines[i].strip()
        if s.startswith(">>> ") and not lines[i + 1].strip().startswith(("...", ">>>")):
            expr, expected = s[4:].strip(), lines[i + 1].strip()
            nxt = lines[i + 2].strip() if i + 2 < len(lines) else ""
            if expected and not expected.startswith("Traceback") and nxt in ("", ) or nxt.startswith(">>>"):
                try:
                    call = ast.parse(expr, mode="eval").body
                    ok = (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == fn_name
                          and all(_literal(a) for a in call.args) and all(k.arg and _literal(k.value) for k in call.keywords))
                    ast.literal_eval(expected)
                except (SyntaxError, ValueError):
                    ok = False
                if ok:
                    out.append(Example(expr, expected, first_line))
        i += 1
    return out[:5]


def _literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False


def _walk_own(fn: ast.AST) -> list[ast.AST]:
    """Nodes of ``fn`` excluding nested function and class bodies."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, (*FUNCS, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


def analyze_module(tree: ast.Module) -> list[FuncFacts]:
    out: list[FuncFacts] = []

    def visit(body: list[ast.stmt], cls: str | None, prefix: str) -> None:
        for node in body:
            if isinstance(node, FUNCS):
                out.append(_facts(node, cls, prefix))
            elif isinstance(node, ast.ClassDef) and cls is None:
                visit(node.body, node.name, f"{node.name}.")

    visit(tree.body, None, "")
    return out


def _facts(fn: ast.FunctionDef | ast.AsyncFunctionDef, cls: str | None, prefix: str) -> FuncFacts:
    decorators = [_unparse(d) or "?" for d in fn.decorator_list]
    static = any(d in ("staticmethod",) for d in decorators)
    skip_first = cls is not None and not static
    params = _params(fn.args, skip_first)
    nodes = _walk_own(fn)
    branches = sum(isinstance(n, (ast.If, ast.IfExp, ast.match_case)) for n in nodes)
    loops = sum(isinstance(n, (ast.For, ast.AsyncFor, ast.While)) for n in nodes)
    handlers = sum(isinstance(n, ast.ExceptHandler) for n in nodes)
    raises = [(_raise_name(n) or "exception", n.lineno) for n in nodes if isinstance(n, ast.Raise)]
    raises.sort(key=lambda r: r[1])
    pnames = {p.name for p in params}
    boundary: list[tuple[str, str, float | int, int]] = []
    for n in nodes:
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _OPS:
            sym = _OPS[type(n.ops[0])]
            if isinstance(n.left, ast.Name) and n.left.id in pnames and _number(n.comparators[0]) is not None:
                boundary.append((n.left.id, sym, _number(n.comparators[0]), n.lineno))  # type: ignore[arg-type]
            elif isinstance(n.comparators[0], ast.Name) and n.comparators[0].id in pnames and _number(n.left) is not None:
                boundary.append((n.comparators[0].id, _FLIP[sym], _number(n.left), n.lineno))  # type: ignore[arg-type]
    doc = ast.get_docstring(fn)
    public = not fn.name.startswith("_") and (cls is None or not cls.startswith("_"))
    f = FuncFacts(
        name=fn.name, qualname=f"{prefix}{fn.name}", kind="method" if cls else "function", class_name=cls,
        start_line=fn.lineno, end_line=getattr(fn, "end_lineno", fn.lineno) or fn.lineno, params=params,
        returns=_unparse(fn.returns), docstring=doc, public=public, is_async=isinstance(fn, ast.AsyncFunctionDef),
        is_generator=any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in nodes),
        complexity=cyclomatic_complexity(fn), branches=branches, loops=loops, handlers=handlers, raises=raises,
        boundary_compares=boundary, decorators=decorators)
    if cls is None:
        f.guards = extract_guards(fn, params)
        f.examples = extract_examples(doc, fn.name, fn.lineno)
    if f.is_async:
        f.skip_reason = "async functions need an async test runner plugin; a template is provided instead"
    elif f.is_generator:
        f.skip_reason = "generator functions need the expected sequence to be defined by a developer"
    elif cls is not None:
        f.skip_reason = "methods need an instance whose construction cannot be determined statically"
    elif any(d.split(".")[-1].split("(")[0] in ("contextmanager", "property", "cached_property", "overload", "fixture")
             for d in decorators):
        f.skip_reason = "decorated function; calling it directly may not match its behaviour"
    return f
