"""Python structural extraction on top of tree-sitter. Syntax-level facts only."""
from __future__ import annotations

from typing import Any

from app.services.intelligence.extractors.calls import collect_calls
from app.services.intelligence.extractors.common import (
    MAX_SCOPE_DEPTH,
    FileContext,
    SymbolLimitReached,
    first_line,
    syntax_diagnostics,
)
from app.services.intelligence.types import PARSED, PARTIAL, ParsedImport, ParseResult

CONTAINERS = {"if_statement", "elif_clause", "else_clause", "try_statement", "except_clause",
              "except_group_clause", "finally_clause", "with_statement", "for_statement",
              "while_statement", "block", "match_statement", "case_clause"}


def module_name(rel_path: str) -> str:
    parts = rel_path.rsplit(".", 1)[0].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p) or "__init__"


def _docstring(ctx: FileContext, body: Any) -> str | None:
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
        s = first.named_children[0]
        content = "".join(ctx.text(c) for c in s.children if c.type == "string_content")
        return first_line(content)
    return None


def _import_names(ctx: FileContext, node: Any) -> list[dict[str, str | None]]:
    names: list[dict[str, str | None]] = []
    for i, child in enumerate(node.children):
        if node.field_name_for_child(i) != "name":
            continue
        if child.type == "aliased_import":
            n, a = child.child_by_field_name("name"), child.child_by_field_name("alias")
            names.append({"name": ctx.text(n) if n else "", "alias": ctx.text(a) if a else None})
        elif child.type == "wildcard_import":
            names.append({"name": "*", "alias": None})
        else:
            names.append({"name": ctx.text(child), "alias": None})
    return names[:50]


def extract(data: bytes, rel_path: str, parser: Any, max_symbols: int = 5000) -> ParseResult:
    tree = parser.parse(data)
    root = tree.root_node
    ctx = FileContext(data, module_name(rel_path), ".", max_symbols)
    imports: list[ParsedImport] = []
    all_names: list[str] | None = None
    all_line = 1
    diagnostics = syntax_diagnostics(root) if root.has_error else []

    def handle_import(node: Any) -> None:
        line = node.start_point[0] + 1
        if node.type == "import_statement":
            for nm in _import_names(ctx, node):
                imports.append(ParsedImport("import", nm["name"] or "", [{"name": nm["name"], "alias": nm["alias"]}],
                                            line))
            return
        mod = node.child_by_field_name("module_name")
        level, spec = 0, ""
        if mod is not None:
            if mod.type == "relative_import":
                for c in mod.children:
                    if c.type == "import_prefix":
                        level = ctx.text(c).count(".")
                    elif c.type == "dotted_name":
                        spec = ctx.text(c)
            else:
                spec = ctx.text(mod)
        imports.append(ParsedImport("import", spec, _import_names(ctx, node), line, level=level))

    def visit(node: Any, path: list[str], parent_key: str | None, scope: str, depth: int) -> None:
        nonlocal all_names, all_line
        if depth > MAX_SCOPE_DEPTH or node.is_error:
            return
        t = node.type
        if t in ("import_statement", "import_from_statement"):
            handle_import(node)
        elif t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                visit(inner, path, parent_key, scope, depth)
        elif t == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.is_missing:
                return
            body = node.child_by_field_name("body")
            bases: list[str] = []
            sup = node.child_by_field_name("superclasses")
            if sup is not None:
                for c in sup.named_children:
                    if c.type in ("identifier", "attribute"):
                        bases.append(ctx.text(c)[:100])
            name = ctx.text(name_node)
            sym = ctx.add(node, name, "class", path, parent_key, signature=ctx.header(node, body),
                          doc_summary=_docstring(ctx, body), bases=bases[:10],
                          visibility="private" if name.startswith("_") and not name.startswith("__") else "public")
            if body is not None:
                for child in body.named_children:
                    visit(child, path + [name], sym.local_key, "class", depth + 1)
        elif t == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.is_missing:
                return
            body = node.child_by_field_name("body")
            name = ctx.text(name_node)
            kind = "function"
            if scope == "class":
                kind = "constructor" if name == "__init__" else "method"
            sym = ctx.add(node, name, kind, path, parent_key, signature=ctx.header(node, body),
                          doc_summary=_docstring(ctx, body),
                          visibility="private" if name.startswith("_") and not name.startswith("__") else "public")
            if body is not None:
                for child in body.named_children:
                    visit(child, path + [name], sym.local_key, "function", depth + 1)
        elif t == "expression_statement":
            if scope == "function" or not node.named_children:
                return
            expr = node.named_children[0]
            if expr.type != "assignment":
                return
            left = expr.child_by_field_name("left")
            right = expr.child_by_field_name("right")
            if left is None or left.type != "identifier":
                return
            name = ctx.text(left)
            if scope == "module" and name == "__all__" and right is not None and right.type in ("list", "tuple"):
                all_names = [ctx.text(s)[1:-1] for s in right.named_children if s.type == "string"]
                all_line = node.start_point[0] + 1
                return
            kind = "field" if scope == "class" else ("constant" if name.isupper() else "variable")
            ctx.add(node, name, kind, path, parent_key,
                    visibility="private" if name.startswith("_") and not name.startswith("__") else "public")
        elif t in CONTAINERS:
            for child in node.named_children:
                visit(child, path, parent_key, scope, depth + 1)

    status = PARTIAL if root.has_error else PARSED
    try:
        for child in root.named_children:
            visit(child, [], None, "module", 0)
    except SymbolLimitReached:
        status = PARTIAL
        diagnostics.append(f"symbol limit ({max_symbols}) reached; remaining symbols not extracted")

    for sym in ctx.symbols:
        if sym.parent_key is None:  # module level
            sym.exported = (sym.name in all_names) if all_names is not None else (sym.visibility == "public")
    if all_names:
        imports.append(ParsedImport("export", "", [{"name": n, "alias": None} for n in all_names[:50]], all_line))
    calls, dynamic, call_notes = collect_calls(ctx, root, js=False)
    imports.extend(dynamic)
    diagnostics.extend(call_notes)
    return ParseResult(status, "tree-sitter-python", ctx.symbols, imports, diagnostics, calls)
