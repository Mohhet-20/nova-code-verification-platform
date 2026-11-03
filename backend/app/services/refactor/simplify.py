"""Proposal-only conditional simplifications for Python. Each suggestion is behavior-preserving by construction but is never applied
automatically: the engine lists the exact before/after text and states what equivalence argument it relies on."""
from __future__ import annotations

import ast
from typing import Any

from app.services.analysis.redaction import scrub
from app.services.refactor.common import PlanResult, Workspace, loc


def _src(lines: list[str], node: ast.AST) -> str:
    return "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])


def _is_bool(n: ast.stmt, value: bool) -> bool:
    return isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is value


def plan_simplify(ws: Workspace, rel_path: str) -> PlanResult:
    res = PlanResult()
    res.limitations = ["Suggestions only: nothing is applied. Review each one; they assume the condition has no side effects that depend on evaluation count.",
                       "Python only; comments inside a rewritten block are not carried over."]
    got = ws.read(rel_path)
    if got is None or ws.indexed.get(rel_path, ("",))[0] != "python":
        res.add("blocker", "unsupported_language", "Simplification suggestions exist for indexed Python files only.")
        return res
    try:
        tree = ast.parse(got[0])
    except SyntaxError:
        res.add("blocker", "syntax_error", "The file does not parse.")
        return res
    lines = got[0].split("\n")
    out: list[dict[str, Any]] = []

    def add(pattern: str, node: ast.AST, before: str, after: str, why: str) -> None:
        out.append({"pattern": pattern, "rel_path": rel_path, "start_line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
                    "before": scrub(before, 800), "after": scrub(after, 800), "equivalence": why})

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        ind = " " * node.col_offset
        cond = ast.get_source_segment(got[0], node.test) or ""
        if len(node.body) == 1 and len(node.orelse) == 1 and _is_bool(node.body[0], True) and _is_bool(node.orelse[0], False) and cond:
            add("return_bool_condition", node, _src(lines, node), f"{ind}return bool({cond})",
                "`bool(x)` is True exactly when `x` is truthy, which is what the `if` tested; the condition is evaluated once in both forms.")
        elif len(node.body) == 1 and not node.orelse and isinstance(node.body[0], ast.If) and not node.body[0].orelse and cond:
            inner = node.body[0]
            icond = ast.get_source_segment(got[0], inner.test) or ""
            body = "\n".join(lines[inner.body[0].lineno - 1:(inner.end_lineno or inner.lineno)])
            dedented = "\n".join(ln_[4:] if ln_.startswith(ind + "        ") else ln_ for ln_ in body.split("\n"))
            add("merge_nested_if", node, _src(lines, node), f"{ind}if ({cond}) and ({icond}):\n{dedented}",
                "`and` short-circuits in the same order as the nested `if`; neither `if` has an `else`, so no other path exists.")
        elif node.orelse and not (len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)) and isinstance(node.body[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break)):
            els = "\n".join(ln_[4:] if ln_.startswith(ind + "    ") else ln_ for ln_ in lines[node.orelse[0].lineno - 1:(node.end_lineno or node.lineno)])
            head = "\n".join(lines[node.lineno - 1:node.body[-1].end_lineno or node.lineno])
            add("else_after_exit", node, _src(lines, node), f"{head}\n{els}",
                "The `if` body always leaves the block (return/raise/continue/break), so the code after it runs only when the condition was false.")
    out.sort(key=lambda s: (s["start_line"], s["pattern"]))
    res.extra = {"suggestions": out[:50], "total": len(out)}
    res.resolution = {"rel_path": rel_path, "language": "python"}
    if not out:
        res.add("info", "nothing_to_suggest", "No supported simplification pattern was found.")
    for s in out[:50]:
        res.add("info", s["pattern"], f"Line {s['start_line']}: {s['pattern'].replace('_', ' ')}.", [loc(rel_path, s["start_line"])])
    return res
