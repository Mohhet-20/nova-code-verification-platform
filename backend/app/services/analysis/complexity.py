"""Cyclomatic complexity (McCabe) for Python functions, computed on the standard-library AST.

Definition used (documented in docs/architecture/STATIC_ANALYSIS.md): complexity = 1 + number of decision points inside
the function body, where each of these adds 1:

* ``if`` / ``elif`` (an ``elif`` is an ``If`` in ``orelse``), conditional expression ``a if c else b``
* ``for``, ``async for``, ``while`` (a loop's ``else`` clause adds nothing)
* each ``except`` handler
* each ``case`` of a ``match`` statement other than a bare wildcard ``case _``
* each additional operand of a boolean operator: ``a and b and c`` adds 2
* each ``for`` clause and each ``if`` filter inside a comprehension / generator expression
* ``assert`` is NOT counted, ``with`` is NOT counted, ``try``/``finally`` add nothing by themselves

Nested function and class definitions are separate units: their bodies are not part of the enclosing function's number.
Lambdas are part of the enclosing function. This is the common "extended McCabe without assert" variant, not a formally
verified measurement, and it ignores exceptional control flow that has no syntactic branch.
"""
from __future__ import annotations

import ast

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def cyclomatic_complexity(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    total = 1
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            continue  # separate unit
        if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            total += 1
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
        elif isinstance(node, ast.match_case):
            wildcard = isinstance(node.pattern, ast.MatchAs) and node.pattern.pattern is None and node.guard is None
            if not wildcard:
                total += 1
        stack.extend(ast.iter_child_nodes(node))
    return total


def max_nesting_depth(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Deepest chain of nested control structures (if/for/while/try/with/match) inside the function body."""
    best = 0
    stack: list[tuple[ast.AST, int]] = [(n, 0) for n in func.body]
    block = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
    trystar = getattr(ast, "TryStar", None)
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            continue
        is_block = isinstance(node, block) or (trystar is not None and isinstance(node, trystar))
        d = depth + 1 if is_block else depth
        best = max(best, d)
        for child in ast.iter_child_nodes(node):
            # An ``elif`` is an If nested in ``orelse``; it should not deepen the chain.
            same_level = isinstance(node, ast.If) and isinstance(child, ast.If) and child in node.orelse and len(node.orelse) == 1
            stack.append((child, d - 1 if same_level else d))
    return best
