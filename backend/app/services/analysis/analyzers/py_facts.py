"""One pass over a Python AST that collects what several rules need (scopes, import aliases, functions)."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class FuncInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str

    @property
    def line_span(self) -> int:
        return (self.node.end_lineno or self.node.lineno) - self.node.lineno + 1


@dataclass
class FileFacts:
    # (node, enclosing scope path, line of the enclosing definition or None)
    nodes: list[tuple[ast.AST, str, int | None]] = field(default_factory=list)
    functions: list[FuncInfo] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # local name -> dotted origin ("sp" -> "subprocess")
    # Per-file memo shared by the rules that analyse this file (cleared with the facts when the file is done); keyed by rule-family name.
    memo: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def resolve(self, node: ast.AST) -> str | None:
        """Dotted name of a Name/Attribute chain with import aliases expanded, or None for anything else."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        head = self.aliases.get(cur.id, cur.id)
        return ".".join([head, *reversed(parts)])


def collect_facts(tree: ast.Module) -> FileFacts:
    facts = FileFacts()
    stack: list[tuple[ast.AST, str, int | None]] = [(tree, "", None)]
    while stack:
        node, scope, def_line = stack.pop()
        facts.nodes.append((node, scope, def_line))
        if isinstance(node, ast.Import):
            for a in node.names:
                facts.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                if a.name != "*":
                    facts.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        child_scope, child_def = scope, def_line
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            child_scope = f"{scope}.{node.name}" if scope else node.name
            child_def = node.lineno
            if isinstance(node, FUNCS):
                facts.functions.append(FuncInfo(node, child_scope))
        for child in reversed(list(ast.iter_child_nodes(node))):
            stack.append((child, child_scope, child_def))
    # Source order makes ordinals (used for fingerprints) deterministic.
    facts.nodes.sort(key=lambda t: (getattr(t[0], "lineno", 0), getattr(t[0], "col_offset", 0)))
    facts.functions.sort(key=lambda f: (f.node.lineno, f.node.col_offset))
    return facts


def loc(node: ast.AST) -> tuple[int, int, int | None, int | None]:
    line = int(getattr(node, "lineno", 1))
    end_line = int(getattr(node, "end_lineno", None) or line)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    return line, end_line, (col + 1 if col is not None else None), (end_col + 1 if end_col is not None else None)
