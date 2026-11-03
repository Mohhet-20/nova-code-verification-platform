"""Conservative JavaScript / TypeScript rename planning on top of the tree-sitter parsers NOVA already loads.

Scope resolution for JS/TS is *not* implemented. The engine therefore only accepts a rename when the name is unambiguous by construction:
the target is a top-level declaration, and the name is bound exactly once in the defining file and once in every importing file. Any
other binding of the same spelling (parameter, local, catch variable, pattern, import) is a blocker rather than something guessed.
Importers are taken from the index (resolved ES-module imports and re-exports); CommonJS ``require`` users are a blocker. Property
names (``obj.name``), object keys and JSX attributes are never treated as references.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.services.refactor.common import Edit, PlanResult, Workspace, loc, parser_for
from app.services.refactor.pyscope import char_col

JS_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
RESERVED = frozenset("""break case catch class const continue debugger default delete do else enum export extends false finally for function if import in
instanceof new null return super switch this throw true try typeof var void while with yield let static await async of implements interface package private
protected public type declare namespace abstract readonly undefined""".split())
DECL_TYPES = {
    "function_declaration": "function", "generator_function_declaration": "function", "function_signature": "function", "class_declaration": "class",
    "abstract_class_declaration": "class", "interface_declaration": "interface", "type_alias_declaration": "type_alias", "enum_declaration": "enum",
}
NAME_OWNERS = {"function_declaration", "generator_function_declaration", "function_expression", "class_declaration", "class", "abstract_class_declaration",
               "interface_declaration", "type_alias_declaration", "enum_declaration", "function_signature", "method_definition", "type_parameter",
               "generator_function"}
PATTERN_TYPES = {"formal_parameters", "required_parameter", "optional_parameter", "object_pattern", "array_pattern", "rest_pattern", "pair_pattern",
                 "catch_clause", "shorthand_property_identifier_pattern"}
ImporterRows = Callable[[str], list[dict[str, Any]]]


def _text(n: Any) -> str:
    return n.text.decode("utf-8", errors="replace") if n is not None else ""


def _walk(n: Any):
    stack = [n]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _binding_like(node: Any) -> bool:
    cur = node
    while cur.parent is not None:
        p = cur.parent
        t = p.type
        if t in ("import_specifier", "import_clause", "namespace_import", "import_statement"):
            return True
        if t == "variable_declarator":
            return cur == p.child_by_field_name("name") or cur.type.endswith("pattern")
        if t in NAME_OWNERS:
            return cur == p.child_by_field_name("name")
        if t == "arrow_function":
            return cur == p.child_by_field_name("parameter")
        if t == "for_in_statement":
            return cur == p.child_by_field_name("left")
        if t == "assignment_pattern":
            if cur == p.child_by_field_name("right"):
                return False
            cur = p
            continue
        if t in ("required_parameter", "optional_parameter"):
            if cur == p.child_by_field_name("value") or cur.type == "type_annotation":
                return False
            cur = p
            continue
        if t in PATTERN_TYPES:
            cur = p
            continue
        return False
    return False


class _Src:
    def __init__(self, rel: str, text: str, language: str) -> None:
        self.rel = rel
        self.text = text
        self.lines = text.split("\n")
        parser = parser_for(language)
        self.tree = parser.parse(text.encode("utf-8")) if parser is not None else None  # type: ignore[attr-defined]

    def col(self, row: int, byte_col: int) -> int:
        return char_col(self.lines[row], byte_col) if row < len(self.lines) else byte_col

    def edit(self, node: Any, old: str, new: str, kind: str, confidence: str = "certain", note: str = "") -> Edit:
        row = node.start_point[0]
        a = self.col(row, node.start_point[1])
        return Edit(self.rel, row + 1, a, a + len(old), old, new, kind, confidence, note)

    def ident_nodes(self, name: str) -> list[Any]:
        return [n for n in _walk(self.tree.root_node) if n.type in ("identifier", "type_identifier") and _text(n) == name] if self.tree else []

    def shorthand(self, name: str) -> list[Any]:
        return [n for n in _walk(self.tree.root_node) if n.type in ("shorthand_property_identifier", "shorthand_property_identifier_pattern") and _text(n) == name]


def _definitions(src: _Src, name: str) -> list[tuple[Any, str]]:
    out: list[tuple[Any, str]] = []
    for top in src.tree.root_node.children:
        node = top
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration") or next((c for c in node.children if c.type in DECL_TYPES or c.type.endswith("_declaration")), node)
        if node.type in DECL_TYPES:
            nm = node.child_by_field_name("name")
            if nm is not None and _text(nm) == name:
                out.append((nm, DECL_TYPES[node.type]))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    if nm is not None and nm.type == "identifier" and _text(nm) == name:
                        out.append((nm, "variable"))
    return out


def _exported_specifiers(src: _Src, name: str) -> list[Any]:
    return [n for n in _walk(src.tree.root_node) if n.type == "export_specifier" and _text(n.child_by_field_name("name")) == name]


def _mentions(src: _Src, name: str) -> tuple[list[dict[str, Any]], int]:
    strings, comments = [], 0
    for n in _walk(src.tree.root_node):
        if n.type == "string" and _text(n).strip("'\"`") == name:
            strings.append(loc(src.rel, n.start_point[0] + 1, n.start_point[1]))
        elif n.type == "comment" and re.search(rf"\b{re.escape(name)}\b", _text(n)):
            comments += 1
    return strings, comments


def plan_symbol_rename(ws: Workspace, rel_path: str, name: str, new_name: str, language: str, importers_of: ImporterRows) -> PlanResult:
    res = PlanResult()
    res.limitations = [
        "No JavaScript/TypeScript scope resolution: a rename is accepted only when the name is bound exactly once in each affected file.",
        "tsconfig path aliases and bundler resolution are not followed; only imports the index resolved are updated.",
    ]
    if not JS_IDENT.match(new_name) or new_name in RESERVED:
        res.add("blocker", "invalid_name", f"{new_name!r} is not a valid JavaScript identifier.")
        return res
    if new_name == name:
        res.add("blocker", "same_name", "The new name is the same as the current name.")
        return res
    got = ws.read(rel_path)
    if got is None or rel_path not in ws.indexed:
        res.add("blocker", "file_not_indexed", f"{rel_path} is not an indexed, readable file.")
        return res
    if parser_for(language) is None:
        res.add("blocker", "parser_unavailable", f"No {language} parser is available, so references cannot be resolved.")
        return res
    src = _Src(rel_path, got[0], language)
    if src.tree is None or src.tree.root_node.has_error:
        res.add("blocker", "target_unparsed", f"{rel_path} has syntax errors, so its names cannot be resolved reliably.")
        return res
    defs = _definitions(src, name)
    if not defs:
        res.add("blocker", "symbol_not_found", f"No top-level declaration of {name!r} was found in {rel_path} on disk.")
        return res
    res.resolution = {"symbol": name, "rel_path": rel_path, "kind": defs[0][1], "scope": "module", "language": language,
                      "definitions": [loc(rel_path, d.start_point[0] + 1, d.start_point[1]) for d, _ in defs][:10],
                      "confidence": "certain" if len(defs) == 1 else "multiple_declarations"}
    def_ids = {d.id for d, _ in defs}
    edits: list[Edit] = []

    def local_rename(s: _Src, extra_ok_ids: set[int], what: str) -> bool:
        idents = s.ident_nodes(name)
        bind = [n for n in idents if _binding_like(n) and n.id not in extra_ok_ids]
        if bind:
            res.add("blocker", "shadowed_or_redeclared", f"{what}: {name!r} is also bound by a parameter, local, pattern or import ({len(bind)} place(s)), and "
                    "JavaScript scope resolution is not implemented. Rename the other binding first or narrow the scope.",
                    [loc(s.rel, n.start_point[0] + 1, n.start_point[1]) for n in bind[:5]])
            return False
        short = s.shorthand(name)
        if short:
            res.add("blocker", "shorthand_property", f"{what}: {name!r} is used as a shorthand property; renaming would change the property key.",
                    [loc(s.rel, n.start_point[0] + 1, n.start_point[1]) for n in short[:5]])
            return False
        if any(_text(n) == new_name for n in _walk(s.tree.root_node) if n.type in ("identifier", "type_identifier")):
            res.add("blocker", "name_conflict", f"{new_name!r} is already used as an identifier in {s.rel}.", [loc(s.rel, 1)])
            return False
        for n in idents:
            if n.parent is not None and n.parent.type == "export_specifier" and n == n.parent.child_by_field_name("alias"):
                continue
            if n.parent is not None and n.parent.type == "export_specifier" and n.parent.child_by_field_name("alias") is not None \
                    and n == n.parent.child_by_field_name("name") and s.rel != rel_path:
                continue
            kind = "definition" if n.id in def_ids else "reference"
            edits.append(s.edit(n, name, new_name, kind))
        return True

    ok = local_rename(src, def_ids, rel_path)
    exp = _exported_specifiers(src, name)
    for sp in exp:
        if sp.child_by_field_name("alias") is None:
            res.add("warning", "public_api", f"{name!r} is exported from {rel_path}; code outside this repository may import it by name.")
            break
    exported_direct = any(n.parent is not None and n.parent.type == "export_statement" for d, _ in defs for n in [d.parent])
    if exported_direct and not any(f.code == "public_api" for f in res.findings):
        res.add("warning", "public_api", f"{name!r} is exported from {rel_path}; code outside this repository may import it by name.")
    # ---- importers (ES modules, followed through re-exports)
    exporters = {rel_path}
    work = [rel_path]
    handled: set[tuple[str, int]] = set()
    files_touched = {rel_path}
    srcs: dict[str, _Src] = {rel_path: src}
    while work:
        cur = work.pop()
        for row in importers_of(cur):
            importer, kind, line, names = row["rel_path"], row["kind"], row["line"], row.get("names") or []
            if (importer, line) in handled or importer == rel_path:
                continue
            handled.add((importer, line))
            mentions = [n for n in names if n.get("name") in (name, "*")]
            if not mentions:
                continue
            if kind == "require":
                res.add("blocker", "commonjs_dependent", f"{importer} loads the module with require(); CommonJS destructuring and property access are not updated.",
                        [loc(importer, line)])
                continue
            g = ws.read(importer)
            if g is None:
                res.add("blocker", "unreadable_dependent", f"{importer} cannot be read.", [loc(importer, line)])
                continue
            lang = ws.indexed[importer][0]
            s = srcs.get(importer) or _Src(importer, g[0], lang)
            srcs[importer] = s
            if s.tree is None or s.tree.root_node.has_error:
                res.add("blocker", "unparsed_dependent", f"{importer} has syntax errors, so its uses of {name!r} cannot be resolved.", [loc(importer, line)])
                continue
            stmt = next((n for n in s.tree.root_node.children if n.start_point[0] + 1 == line and n.type in ("import_statement", "export_statement")), None)
            if stmt is None:
                res.add("warning", "import_not_located", f"The import in {importer} line {line} could not be matched to a statement; its uses of {name!r} are not edited.",
                        [loc(importer, line)])
                continue
            specs = [n for n in _walk(stmt) if n.type in ("import_specifier", "export_specifier") and _text(n.child_by_field_name("name")) == name]
            for sp in specs:
                nm = sp.child_by_field_name("name")
                alias = sp.child_by_field_name("alias")
                edits.append(s.edit(nm, name, new_name, "import_name", "certain", "Imported name." if stmt.type == "import_statement" else "Re-exported name."))
                files_touched.add(importer)
                if stmt.type == "export_statement" and alias is None:
                    exporters.add(importer)
                    work.append(importer)
                    res.add("warning", "reexport_changed", f"{importer} re-exports {name!r} without an alias; its own importers are updated too.", [loc(importer, line)])
                elif stmt.type == "import_statement" and alias is None:
                    local_rename(s, {nm.id}, importer)
            ns = [n for n in _walk(stmt) if n.type == "namespace_import"]
            for n in ns:
                ident = next((c for c in n.children if c.type == "identifier"), None)
                if ident is None:
                    continue
                local = _text(ident)
                bound = [i for i in s.ident_nodes(local) if _binding_like(i)]
                if len(bound) != 1:
                    res.add("blocker", "shadowed_or_redeclared", f"{importer}: the namespace import {local!r} is bound more than once.", [loc(importer, line)])
                    continue
                for m in _walk(s.tree.root_node):
                    if m.type == "member_expression" and _text(m.child_by_field_name("object")) == local and _text(m.child_by_field_name("property")) == name:
                        edits.append(s.edit(m.child_by_field_name("property"), name, new_name, "module_attribute", "certain", f"Property of namespace import {local}."))
                        files_touched.add(importer)
                    elif m.type == "nested_type_identifier" and _text(m.child_by_field_name("module")) == local and _text(m.child_by_field_name("name")) == name:
                        edits.append(s.edit(m.child_by_field_name("name"), name, new_name, "module_attribute", "certain", f"Type of namespace import {local}."))
                        files_touched.add(importer)
            if any(n.get("name") == "*" and n.get("alias") is None for n in names) and stmt.type == "export_statement":
                exporters.add(importer)
                work.append(importer)
                res.add("info", "star_reexport", f"{importer} re-exports everything from the module; its importers were examined.", [loc(importer, line)])
    strings, comments = [], 0
    for s in srcs.values():
        a, b = _mentions(s, name)
        strings += a
        comments += b
    if strings:
        res.add("warning", "dynamic_reference", f"{len(strings)} string literal(s) equal {name!r} (registries, dynamic imports, serialization). They are not renamed.", strings[:20])
    if comments:
        res.add("info", "comment_mentions", f"{comments} comment(s) mention {name!r}. Comments are not edited.")
    seen: dict[tuple[str, int, int, int], Edit] = {}
    for e in edits:
        seen.setdefault(e.key, e)
    res.edits = sorted(seen.values(), key=lambda e: (e.rel_path, e.line, e.col))
    if not ok:
        res.candidate_edits, res.edits = res.edits, []
    if len(res.edits) > ws.settings.refactor_max_edits or len({e.rel_path for e in res.edits}) > ws.settings.refactor_max_files:
        res.add("blocker", "too_many_edits", "The rename exceeds the edit or file limits.")
    res.extra = {"exporters": sorted(exporters), "files_examined": len(srcs)}
    return res
