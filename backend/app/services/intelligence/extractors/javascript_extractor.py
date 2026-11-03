"""JavaScript / JSX / TypeScript / TSX structural extraction on top of tree-sitter."""
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

FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
FUNC_DECLS = {"function_declaration", "generator_function_declaration"}


def module_name(rel_path: str) -> str:
    base, _, ext = rel_path.rpartition(".")
    return base if base and ext else rel_path


def _string_value(ctx: FileContext, node: Any) -> str:
    return "".join(ctx.text(c) for c in node.children if c.type == "string_fragment")[:500]


def _doc_before(ctx: FileContext, node: Any) -> str | None:
    if node.parent is not None and node.parent.type == "export_statement":
        node = node.parent
    prev = node.prev_sibling
    if prev is not None and prev.type == "comment" and prev.end_point[0] >= node.start_point[0] - 1:
        text = ctx.text(prev)
        if text.startswith("/**"):
            return first_line(text)
    return None


def extract(data: bytes, rel_path: str, parser: Any, max_symbols: int = 5000) -> ParseResult:
    tree = parser.parse(data)
    root = tree.root_node
    ctx = FileContext(data, module_name(rel_path), "#", max_symbols)
    imports: list[ParsedImport] = []
    exported_names: set[str] = set()
    diagnostics = syntax_diagnostics(root) if root.has_error else []

    def accessibility(member: Any, name: str) -> str:
        for c in member.children:
            if c.type == "accessibility_modifier":
                return "private" if ctx.text(c) == "private" else ("public" if ctx.text(c) == "public" else "protected")
        return "private" if name.startswith("#") else "public"

    def class_like(node: Any, path: list[str], parent_key: str | None, exported: bool, depth: int,
                   name_override: str | None = None) -> None:
        name_node = node.child_by_field_name("name")
        name = name_override or (ctx.text(name_node) if name_node is not None else "")
        if not name:
            return
        body = node.child_by_field_name("body")
        bases: list[str] = []
        for c in node.children:
            if c.type == "class_heritage":
                for h in c.children:
                    if h.type == "extends_clause":
                        v = h.child_by_field_name("value")
                        if v is not None:
                            bases.append(ctx.text(v)[:100])
                    elif h.is_named and h.type not in ("implements_clause", "comment"):
                        bases.append(ctx.text(h)[:100])
        kind = "interface" if node.type == "interface_declaration" else "class"
        sym = ctx.add(node, name, kind, path, parent_key, signature=ctx.header(node, body),
                      doc_summary=_doc_before(ctx, node), bases=bases[:10] if kind == "class" else [],
                      exported=exported)
        if body is None:
            return
        for member in body.named_children:
            m = member.type
            mname_node = member.child_by_field_name("name")
            if mname_node is None:
                continue
            mname = ctx.text(mname_node)
            if m in ("method_definition", "method_signature", "abstract_method_signature"):
                mkind = "constructor" if mname == "constructor" else "method"
                ctx.add(member, mname, mkind, path + [name], sym.local_key,
                        signature=ctx.header(member, member.child_by_field_name("body")),
                        doc_summary=_doc_before(ctx, member), visibility=accessibility(member, mname))
            elif m in ("public_field_definition", "field_definition"):
                ctx.add(member, mname, "field", path + [name], sym.local_key, visibility=accessibility(member, mname))
            elif m == "property_signature":
                ctx.add(member, mname, "property", path + [name], sym.local_key, visibility="public")

    def handle_require(declarator: Any, line: int) -> bool:
        value = declarator.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            return False
        fn, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
        if fn is None or ctx.text(fn) != "require" or args is None or not args.named_children:
            return False
        arg = args.named_children[0]
        if arg.type != "string":
            return False
        target = declarator.child_by_field_name("name")
        names: list[dict[str, str | None]] = []
        if target is not None and target.type == "identifier":
            names.append({"name": "*", "alias": ctx.text(target)})
        elif target is not None and target.type == "object_pattern":
            for c in target.named_children:
                if c.type == "shorthand_property_identifier_pattern":
                    names.append({"name": ctx.text(c), "alias": None})
                elif c.type == "pair_pattern":
                    k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                    names.append({"name": ctx.text(k) if k else "", "alias": ctx.text(v) if v else None})
        imports.append(ParsedImport("require", _string_value(ctx, arg), names[:50], line))
        return True

    def declarations(node: Any, path: list[str], parent_key: str | None, exported: bool, scope: str,
                     depth: int) -> None:
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            line = d.start_point[0] + 1
            if scope == "module" and handle_require(d, line):
                continue
            name_node, value = d.child_by_field_name("name"), d.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or scope == "function":
                continue
            name = ctx.text(name_node)
            if value is not None and value.type in FUNC_VALUES:
                body = value.child_by_field_name("body")
                ctx.add(d, name, "function", path, parent_key, signature=ctx.header(d, body),
                        doc_summary=_doc_before(ctx, node), exported=exported)
            elif value is not None and value.type == "class":
                class_like(value, path, parent_key, exported, depth, name_override=name)
            else:
                is_const = any(c.type == "const" for c in node.children)
                ctx.add(d, name, "constant" if is_const else "variable", path, parent_key, exported=exported,
                        doc_summary=_doc_before(ctx, node))

    def handle(node: Any, path: list[str], parent_key: str | None, exported: bool, scope: str, depth: int) -> None:
        if depth > MAX_SCOPE_DEPTH or node.is_error:
            return
        t = node.type
        if t == "import_statement":
            src = node.child_by_field_name("source")
            names: list[dict[str, str | None]] = []
            for clause in node.named_children:
                if clause.type != "import_clause":
                    continue
                for c in clause.named_children:
                    if c.type == "identifier":
                        names.append({"name": "default", "alias": ctx.text(c)})
                    elif c.type == "namespace_import":
                        ident = next((x for x in c.named_children if x.type == "identifier"), None)
                        names.append({"name": "*", "alias": ctx.text(ident) if ident else None})
                    elif c.type == "named_imports":
                        for spec in c.named_children:
                            n, a = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                            names.append({"name": ctx.text(n) if n else "", "alias": ctx.text(a) if a else None})
            type_only = any(c.type == "type" for c in node.children)
            if src is not None:
                imports.append(ParsedImport("import", _string_value(ctx, src), names[:50],
                                            node.start_point[0] + 1, type_only=type_only))
        elif t == "export_statement":
            decl = node.child_by_field_name("declaration")
            src = node.child_by_field_name("source")
            if decl is not None:
                handle(decl, path, parent_key, True, scope, depth + 1)
                return
            names = []
            for c in node.named_children:
                if c.type == "export_clause":
                    for spec in c.named_children:
                        n, a = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                        nm = ctx.text(n) if n else ""
                        names.append({"name": nm, "alias": ctx.text(a) if a else None})
                        if src is None and nm:
                            exported_names.add(nm)
                elif c.type == "namespace_export":
                    names.append({"name": "*", "alias": None})
            if src is not None:
                if not names:
                    names = [{"name": "*", "alias": None}]
                imports.append(ParsedImport("reexport", _string_value(ctx, src), names[:50], node.start_point[0] + 1))
            elif names:
                imports.append(ParsedImport("export", "", names[:50], node.start_point[0] + 1))
        elif t in FUNC_DECLS:
            nn = node.child_by_field_name("name")
            if nn is None or nn.is_missing:
                return
            body = node.child_by_field_name("body")
            name = ctx.text(nn)
            sym = ctx.add(node, name, "function", path, parent_key, signature=ctx.header(node, body),
                          doc_summary=_doc_before(ctx, node), exported=exported)
            if body is not None:
                for child in body.named_children:
                    if child.type in FUNC_DECLS | CLASS_DECLS:
                        handle(child, path + [name], sym.local_key, False, "function", depth + 1)
        elif t in CLASS_DECLS or t == "interface_declaration":
            class_like(node, path, parent_key, exported, depth)
        elif t == "type_alias_declaration":
            nn = node.child_by_field_name("name")
            if nn is not None:
                ctx.add(node, ctx.text(nn), "type_alias", path, parent_key, signature=ctx.header(node, None),
                        exported=exported, doc_summary=_doc_before(ctx, node))
        elif t == "enum_declaration":
            nn = node.child_by_field_name("name")
            if nn is not None:
                ctx.add(node, ctx.text(nn), "enum", path, parent_key, exported=exported,
                        doc_summary=_doc_before(ctx, node))
        elif t in ("lexical_declaration", "variable_declaration"):
            declarations(node, path, parent_key, exported, scope, depth)
        elif t in ("internal_module", "module"):
            nn = node.child_by_field_name("name")
            if nn is None:
                return
            name = ctx.text(nn)
            sym = ctx.add(node, name, "namespace", path, parent_key, exported=exported)
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children:
                    handle(child, path + [name], sym.local_key, False, "module", depth + 1)
        elif t in ("expression_statement", "ambient_declaration"):
            for child in node.named_children:
                handle(child, path, parent_key, exported, scope, depth + 1)

    status = PARTIAL if root.has_error else PARSED
    try:
        for child in root.named_children:
            handle(child, [], None, False, "module", 0)
    except SymbolLimitReached:
        status = PARTIAL
        diagnostics.append(f"symbol limit ({max_symbols}) reached; remaining symbols not extracted")
    for sym in ctx.symbols:
        if sym.parent_key is None and sym.name in exported_names:
            sym.exported = True
    calls, dynamic, call_notes = collect_calls(ctx, root, js=True)
    imports.extend(dynamic)
    diagnostics.extend(call_notes)
    return ParseResult(status, "tree-sitter-javascript", ctx.symbols, imports, diagnostics, calls)
