"""Scope-aware Python rename planning (module-level symbols, local variables, and a proposal-only method rename).

Resolution is performed with ``pyscope`` on the files as they are on disk now. A token is edited only when it provably refers to the
renamed binding: a definition, a use that resolves to it, an import of it (following re-exports), a module-attribute access reached through
an import of the defining module, or a literal ``__all__`` / string-annotation entry. Same-spelled names in other scopes, attributes of
unknown objects, comments and unrelated strings are never edited; attributes, strings and external files that may still depend on the old
name are reported as findings so a person can review them.
"""
from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from typing import Any

from app.services.refactor import pyscope as ps
from app.services.refactor.common import IDENT, Edit, PlanResult, Workspace, loc

TEXT_SUFFIXES = (".toml", ".cfg", ".ini", ".json", ".yaml", ".yml", ".md", ".rst", ".txt", ".html")
SERIALIZATION_BASES = ("BaseModel", "Enum", "IntEnum", "StrEnum", "TypedDict", "NamedTuple", "Schema", "Model", "Struct")


def module_names(rel: str) -> list[str]:
    parts = rel[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    out = [".".join(parts)] if parts else []
    if len(parts) > 1 and parts[0] == "src":
        out.append(".".join(parts[1:]))
    return out


def _analyses(ws: Workspace, paths: list[str]) -> tuple[dict[str, ps.FileAnalysis], list[str]]:
    out: dict[str, ps.FileAnalysis] = {}
    unparsed: list[str] = []
    for rel in paths:
        got = ws.read(rel)
        if got is None:
            unparsed.append(rel)
            continue
        fa = ps.analyze(rel, got[0])
        if fa is None:
            unparsed.append(rel)
        else:
            out[rel] = fa
    return out, unparsed


def validate_new_name(res: PlanResult, old: str, new: str) -> bool:
    ok = True
    if not IDENT.match(new) or not new.isidentifier():
        res.add("blocker", "invalid_name", f"{new!r} is not a valid Python identifier.")
        ok = False
    elif keyword.iskeyword(new) or new in ("None", "True", "False"):
        res.add("blocker", "invalid_name", f"{new!r} is a reserved word.")
        ok = False
    if new == old:
        res.add("blocker", "same_name", "The new name is the same as the current name.")
        ok = False
    if ok and new in ps.BUILTINS:
        res.add("warning", "shadows_builtin", f"{new!r} is a Python builtin; code in the renamed scope would stop seeing the builtin.")
    if ok and new.startswith("__") and new.endswith("__"):
        res.add("warning", "dunder_name", "Dunder names have special meaning to Python and libraries.")
    return ok


def _resolves_with(x: ps.Scope, name: str, from_scope: ps.Scope) -> ps.Scope | None:
    had = name in x.bound
    x.bound.add(name)
    try:
        return ps.resolve(from_scope, name)
    finally:
        if not had:
            x.bound.discard(name)


def check_conflicts(res: PlanResult, fa: ps.FileAnalysis, x: ps.Scope, old: str, new: str, renamed: list[ps.Site]) -> None:
    """Refuse a rename that would collide with, be shadowed by, or capture another binding of ``new``."""
    if new in x.bound and new != old:
        where = [s for s in fa.sites if s.name == new and s.scope is x]
        res.add("blocker", "name_conflict", f"{new!r} is already bound in the scope that owns {old!r} in {fa.rel_path}.",
                [loc(fa.rel_path, s.line, s.col) for s in where[:5]] or [loc(fa.rel_path, 1)])
        return
    for s in renamed:
        if s.kind in ("def", "class", "param", "import", "global", "nonlocal"):
            continue
        after = _resolves_with(x, new, s.scope)
        if after is not x:
            res.add("blocker", "shadowed_by_existing_binding", f"In {fa.rel_path} line {s.line}, {new!r} is bound in a nearer scope, so the renamed use would "
                    "refer to a different variable.", [loc(fa.rel_path, s.line, s.col)])
            return
    for s in fa.sites:
        if s.kind == "use" and s.name == new and s.resolved is not x:
            after = _resolves_with(x, new, s.scope)
            if after is x:
                res.add("blocker", "would_capture_existing_use", f"The existing use of {new!r} in {fa.rel_path} line {s.line} would start referring to the "
                        "renamed binding.", [loc(fa.rel_path, s.line, s.col)])
                return


def _scan_mentions(fa: ps.FileAnalysis, name: str, dotted: set[str]) -> tuple[list[dict[str, Any]], int]:
    """String literals that look like references to the name, and the number of comments that mention it."""
    strings: list[dict[str, Any]] = []
    comments = 0
    if not re.search(rf"\b{re.escape(name)}\b", fa.source):
        return strings, comments
    pat = re.compile(rf"(^|[.:/\s]){re.escape(name)}$")
    try:
        for tok in tokenize.generate_tokens(io.StringIO(fa.source).readline):
            if tok.type == tokenize.COMMENT and re.search(rf"\b{re.escape(name)}\b", tok.string):
                comments += 1
            elif tok.type == tokenize.STRING:
                try:
                    val = ast.literal_eval(tok.string)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(val, str) and len(val) < 300 and (val == name or (pat.search(val) and any(d in val for d in dotted))):
                    strings.append(loc(fa.rel_path, tok.start[0], tok.start[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return strings, comments


def _string_annotation_edits(fa: ps.FileAnalysis, name: str, new: str, x: ps.Scope, out: list[Edit], res: PlanResult) -> None:
    for const, scope in fa.string_annotations:
        text = const.value
        if not isinstance(text, str) or name not in text:
            continue
        try:
            sub = ast.parse(text.strip(), mode="eval")
        except SyntaxError:
            continue
        seg = ast.get_source_segment(fa.source, const) or ""
        simple = const.lineno == const.end_lineno and seg[:1] in ("'", '"') and seg[:3] not in ("'''", '"""') and "\\" not in seg and seg[1:-1] == text
        for n in ast.walk(sub):
            if isinstance(n, ast.Name) and n.id == name and ps.resolve(scope, name) is x:
                if simple:
                    lead = len(text) - len(text.lstrip())
                    start = fa.lines[const.lineno - 1]
                    col = ps.char_col(start, const.col_offset) + 1 + lead + n.col_offset
                    out.append(Edit(fa.rel_path, const.lineno, col, col + len(name), name, new, "string_annotation", "certain", "Forward reference in a string annotation."))
                else:
                    res.add("warning", "complex_string_annotation", f"A string annotation in {fa.rel_path} line {const.lineno} mentions {name!r} but is not "
                            "a simple one-line literal; update it by hand.", [loc(fa.rel_path, const.lineno)])


def _external_mentions(ws: Workspace, name: str, dotted: set[str]) -> list[str]:
    found: list[str] = []
    needles = [f"{d}.{name}" for d in dotted] + [f"{d}:{name}" for d in dotted]
    for rel, (_lang, _) in sorted(ws.indexed.items()):
        if not rel.endswith(TEXT_SUFFIXES):
            continue
        got = ws.read(rel)
        if got and any(n in got[0] for n in needles):
            found.append(rel)
        if len(found) >= 10:
            break
    return found


def _module_binding_target(ib: ps.ImportBinding, modules: dict[str, str]) -> str | None:
    """Dotted module path a module-style import binding stands for (None: it is a plain symbol import)."""
    if ib.star:
        return None
    if ib.symbol is None:
        return ib.module if ib.alias else ib.module.split(".")[0]
    dotted = f"{ib.module}.{ib.symbol}" if ib.module else ib.symbol
    return dotted if dotted in modules else None


def plan_symbol_rename(ws: Workspace, rel_path: str, name: str, new_name: str, kind: str | None = None) -> PlanResult:
    res = PlanResult()
    res.limitations = [
        "References are resolved by static scope analysis of Python files known to the index; dynamically built names, reflection and external users "
        "are not seen.",
        "Attribute accesses on objects of unknown type are never edited.",
    ]
    if not validate_new_name(res, name, new_name):
        return res
    paths = ws.paths(("python",))
    if rel_path not in paths:
        res.add("blocker", "file_not_indexed", f"{rel_path} is not an indexed Python file.")
        return res
    ana, unparsed = _analyses(ws, paths)
    modules: dict[str, str] = {}
    for rel in paths:
        for d in module_names(rel):
            modules.setdefault(d, rel)
    tfa = ana.get(rel_path)
    if tfa is None:
        res.add("blocker", "target_unparsed", f"{rel_path} does not parse, so its names cannot be resolved.")
        return res
    mscope = tfa.module_scope
    defs = [s for s in tfa.sites if s.name == name and s.resolved is mscope and s.kind in ("def", "class", "assign", "walrus")]
    imported = [i for i in tfa.imports if i.local == name and i.scope is mscope]
    if not defs and imported:
        res.add("blocker", "defined_elsewhere", f"{name!r} is imported into {rel_path}, not defined there. Rename it where it is defined.",
                [loc(rel_path, imported[0].line)])
        return res
    if not defs:
        res.add("blocker", "symbol_not_found", f"No module-level definition of {name!r} was found in {rel_path} on disk.")
        return res
    res.resolution = {"symbol": name, "rel_path": rel_path, "kind": kind or ("class" if defs[0].kind == "class" else "function" if defs[0].kind == "def" else "variable"),
                      "definitions": [loc(rel_path, d.line, d.col) for d in defs][:10], "scope": "module", "module": module_names(rel_path)[:2],
                      "confidence": "certain" if len(defs) == 1 else "multiple_definitions"}
    if len(defs) > 1:
        res.add("warning", "multiple_definitions", f"{name!r} is bound {len(defs)} times at module level in {rel_path}; all of them are renamed together.",
                [loc(rel_path, d.line, d.col) for d in defs[:5]])
    if tfa.unsupported and any(name in u for u in tfa.unsupported):
        res.add("blocker", "unsupported_syntax", "A match-statement capture binds the same name; it cannot be renamed safely.")
        return res
    # --- files that may mention the name but cannot be analysed
    for rel in unparsed:
        got = ws.read(rel)
        if rel == rel_path or got is None or re.search(rf"\b{re.escape(name)}\b", got[0]):
            res.add("blocker", "unparsed_dependent", f"{rel} cannot be parsed or read and may refer to {name!r}. Fix or exclude it first.", [loc(rel, 1)])
    edits: list[Edit] = []
    variables: list[tuple[ps.FileAnalysis, ps.Scope, list[ps.Site]]] = []

    def collect(fa: ps.FileAnalysis, x: ps.Scope, local: str, first_kind: str) -> list[ps.Site]:
        sites = [s for s in fa.sites if s.name == local and s.resolved is x]
        for s in sites:
            kind_ = "definition" if s.kind in ("def", "class") else ("import_name" if s.kind == "import" else "reference")
            if first_kind == "definition" and s.kind in ("assign", "walrus") and s.scope is x:
                kind_ = "definition"
            edits.append(Edit(fa.rel_path, s.line, s.col, s.end_col, local, new_name, kind_))
        if x.kind == "module":
            for ln_name, ln, a, b in fa.all_exports:
                if ln_name == local:
                    edits.append(Edit(fa.rel_path, ln, a, b, local, new_name, "export_string", "certain", "Entry in a literal __all__."))
        variables.append((fa, x, sites))
        return sites

    # --- the defining file
    collect(tfa, mscope, name, "definition")
    _string_annotation_edits(tfa, name, new_name, mscope, edits, res)
    # --- exporter closure (modules that re-export the name through `from E import name`)
    exporters: set[str] = {rel_path}
    changed = True
    while changed:
        changed = False
        for rel, fa in ana.items():
            if rel in exporters:
                continue
            for ib in fa.imports:
                if ib.scope.kind == "module" and ib.symbol == name and ib.alias is None and ib.module and modules.get(ib.module) in exporters:
                    exporters.add(rel)
                    changed = True
                    break
    exporter_dotted = {d for e in exporters for d in module_names(e)}
    touched_files = {rel_path}
    star_files: list[str] = []
    # --- importers
    for rel, fa in sorted(ana.items()):
        if rel == rel_path:
            continue
        local_changed = False
        modbind: dict[tuple[int, str], list[str]] = {}
        for ib in fa.imports:
            tgt = _module_binding_target(ib, modules)
            if tgt is not None:
                modbind.setdefault((ib.scope.id, ib.local), []).append(tgt)
            if ib.symbol == name and ib.module and modules.get(ib.module) in exporters and ib.name_site is not None:
                if ib.alias is None:
                    collect(fa, ib.scope, name, "reference")
                else:
                    edits.append(Edit(rel, ib.name_site.line, ib.name_site.col, ib.name_site.end_col, name, new_name, "import_name", "certain",
                                      "Imported under an alias; only the imported name changes."))
                local_changed = True
        for node, scope, site in fa.attributes:
            if node.attr != name:
                continue
            chain: list[str] = []
            cur: ast.expr = node.value
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                continue
            x = ps.resolve(scope, cur.id)
            if x is None:
                continue
            for tgt in modbind.get((x.id, cur.id), []):
                dotted = tgt + ("." + ".".join(reversed(chain)) if chain else "")
                if dotted in exporter_dotted:
                    edits.append(Edit(rel, site.line, site.col, site.end_col, name, new_name, "module_attribute", "certain",
                                      f"Attribute of module {dotted} reached through an import."))
                    local_changed = True
                    break
        stars = [i for i in fa.star_imports if modules.get(i.module) in exporters]
        if stars:
            if len(fa.star_imports) > 1:
                res.add("blocker", "ambiguous_star_import", f"{rel} has several wildcard imports, so uses of {name!r} cannot be attributed to one module.",
                        [loc(rel, stars[0].line)])
            else:
                for s in fa.sites:
                    if s.kind == "use" and s.name == name and s.resolved is None:
                        edits.append(Edit(rel, s.line, s.col, s.end_col, name, new_name, "star_import_use", "inferred",
                                          "Name comes from a wildcard import; verify manually."))
                        local_changed = True
                res.add("warning", "wildcard_import", f"{rel} imports from the defining module with '*'. Its uses of {name!r} were inferred, not proven.",
                        [loc(rel, stars[0].line)])
                star_files.append(rel)
        if local_changed:
            touched_files.add(rel)
            _string_annotation_edits(fa, name, new_name, next((x for f2, x, _ in variables if f2 is fa), fa.module_scope), edits, res)
    # --- conflicts (per renamed variable)
    for fa, x, sites in variables:
        check_conflicts(res, fa, x, name, new_name, sites)
    # --- findings about what the engine cannot see
    dynamic_locs: list[dict[str, Any]] = []
    comment_total = 0
    for rel, fa in ana.items():
        strs, cm = _scan_mentions(fa, name, exporter_dotted)
        dynamic_locs += strs
        comment_total += cm
        for d in fa.dynamic:
            if f"'{name}'" in d or f'"{name}"' in d:
                m = re.match(r"line (\d+)", d)
                dynamic_locs.append(loc(rel, int(m.group(1)) if m else 1))
    edited_lines = {(e.rel_path, e.line) for e in edits if e.kind == "export_string"}
    dynamic_locs = [d for d in dynamic_locs if (d["path"], d["line"]) not in edited_lines]
    if dynamic_locs:
        res.add("warning", "dynamic_reference", f"{len(dynamic_locs)} string literal(s) equal or end with {name!r} (for example getattr, registries or "
                "serialization keys). They are not renamed; review them.", dynamic_locs[:20])
    if comment_total:
        res.add("info", "comment_mentions", f"{comment_total} comment(s) mention {name!r}. Comments are not edited.")
    attr_hits = []
    for rel, fa in ana.items():
        for node, _scope, site in fa.attributes:
            if node.attr == name and not any(e.key == (rel, site.line, site.col, site.end_col) for e in edits):
                attr_hits.append(loc(rel, site.line, site.col))
    if attr_hits:
        res.add("warning", "unresolved_attribute_references", f"{len(attr_hits)} attribute access(es) named {name!r} could not be tied to the renamed "
                "symbol (the receiver's type is unknown). They are not edited; review them.", attr_hits[:20])
    ext = _external_mentions(ws, name, exporter_dotted)
    if ext:
        res.add("warning", "external_reference", f"Non-Python files refer to {name!r} by dotted path (entry points, configuration, documentation): "
                + ", ".join(ext[:5]) + ".", [loc(e, 1) for e in ext[:10]])
    in_all = any(n == name for n, *_ in tfa.all_exports)
    reexported = sorted(e for e in exporters if e != rel_path and e.endswith("__init__.py"))
    if in_all or rel_path.endswith("__init__.py") or reexported:
        res.add("warning", "public_api", f"{name!r} is part of a package's public interface ({'listed in __all__' if in_all else 're-exported by ' + ', '.join(reexported) if reexported else 'defined in __init__.py'}). "
                "Code outside this repository may import it under the old name.")
    elif not name.startswith("_"):
        res.add("info", "public_naming", f"{name!r} has a public name (no leading underscore); external users, if any, are not visible here.")
    cls = next((n for n in tfa.tree.body if isinstance(n, ast.ClassDef) and n.name == name), None)
    if cls is not None:
        dec = [ast.unparse(d) for d in cls.decorator_list]
        bases = [ast.unparse(b) for b in cls.bases]
        if any("dataclass" in d for d in dec) or any(b.split(".")[-1] in SERIALIZATION_BASES for b in bases):
            res.add("warning", "serialization_name", f"Class {name!r} looks serialisable (dataclass, enum or schema base); its name may appear in stored or transmitted data.")
        res.side_effects.append("repr() and __name__ of the class change; code or data that matches on the old class name will no longer match.")
    elif res.resolution["kind"] == "function":
        res.side_effects.append("__name__ and tracebacks show the new function name; log or metric labels derived from it change.")
    for fa in ana.values():
        if fa.unsupported and any(name in u or new_name in u for u in fa.unsupported):
            res.add("blocker", "unsupported_syntax", f"{fa.rel_path} uses syntax that scope analysis does not support: {fa.unsupported[0]}.")
    # --- limits
    edits_final = _unique(edits)
    if len(edits_final) > ws.settings.refactor_max_edits:
        res.add("blocker", "too_many_edits", f"The rename needs {len(edits_final)} edits (limit {ws.settings.refactor_max_edits}).")
    if len({e.rel_path for e in edits_final}) > ws.settings.refactor_max_files:
        res.add("blocker", "too_many_files", f"The rename touches more than {ws.settings.refactor_max_files} files.")
    if len(ana) + len(unparsed) >= ws.settings.refactor_max_scan_files:
        res.complete = False
        res.add("warning", "scan_limit_reached", "The scan limit was reached; some files were not examined.")
    res.edits = edits_final
    res.extra = {"exporters": sorted(exporters), "files_scanned": len(ana), "unparsed_files": unparsed[:20], "wildcard_files": star_files}
    return res


def _unique(edits: list[Edit]) -> list[Edit]:
    seen: dict[tuple[str, int, int, int], Edit] = {}
    for e in edits:
        seen.setdefault(e.key, e)
    return sorted(seen.values(), key=lambda e: (e.rel_path, e.line, e.col))


# ------------------------------------------------------------------------------------------------ local variables


def plan_local_rename(ws: Workspace, rel_path: str, function: str, name: str, new_name: str) -> PlanResult:
    res = PlanResult()
    res.limitations = ["Only the named function and the closures nested in it are examined; ``locals()``, ``eval`` and ``exec`` inside it block the rename."]
    if not validate_new_name(res, name, new_name):
        return res
    got = ws.read(rel_path)
    if got is None or rel_path not in ws.indexed:
        res.add("blocker", "file_not_indexed", f"{rel_path} is not an indexed, readable Python file.")
        return res
    fa = ps.analyze(rel_path, got[0])
    if fa is None:
        res.add("blocker", "target_unparsed", f"{rel_path} does not parse.")
        return res
    scopes = [s for s in fa.scopes if s.kind == "function" and s.qualified == function]
    if not scopes:
        res.add("blocker", "function_not_found", f"No function {function!r} was found in {rel_path} on disk.")
        return res
    if len(scopes) > 1:
        res.add("blocker", "ambiguous_function", f"{len(scopes)} functions are called {function!r} in {rel_path}; narrow the target.",
                [loc(rel_path, getattr(s.node, "lineno", 1)) for s in scopes[:5]])
        return res
    fscope = scopes[0]
    node = fscope.node
    res.resolution = {"symbol": name, "rel_path": rel_path, "function": function, "scope": "function", "kind": "local",
                      "confidence": "certain", "definitions": [loc(rel_path, getattr(node, "lineno", 1))]}
    if name in fscope.globals_ or name in fscope.nonlocals or name not in fscope.bound:
        res.add("blocker", "not_a_local", f"{name!r} is not a local variable of {function} (it is global, nonlocal, or not bound there). "
                "Use a symbol rename for module-level names.")
        return res
    if any(s.kind == "param" and s.name == name and s.scope is fscope for s in fa.sites):
        res.add("blocker", "parameter_rename", f"{name!r} is a parameter of {function}. Renaming it changes the call signature (keyword arguments at call sites), "
                "which this engine does not update.")
        return res
    start, end = getattr(node, "lineno", 1), getattr(node, "end_lineno", 1)
    for d in fa.dynamic:
        m = re.match(r"line (\d+): (locals|vars|eval|exec|globals)", d)
        if m and start <= int(m.group(1)) <= end:
            res.add("blocker", "dynamic_scope_access", f"{d} inside {function} can read variables by name, so the rename cannot be proven safe.", [loc(rel_path, int(m.group(1)))])
    if any(name in u for u in fa.unsupported):
        res.add("blocker", "unsupported_syntax", "A match-statement capture binds the same name.")
    sites = [s for s in fa.sites if s.name == name and s.resolved is fscope]
    for s in sites:
        res.edits.append(Edit(rel_path, s.line, s.col, s.end_col, name, new_name, "definition" if s.kind in ("assign", "for", "with", "except", "walrus") and s is sites[0] else "reference"))
    check_conflicts(res, fa, fscope, name, new_name, sites)
    res.edits = _unique(res.edits)
    if not res.edits:
        res.add("blocker", "no_occurrences", f"{name!r} has no occurrences in {function}.")
    return res


# ------------------------------------------------------------------------------------------------ methods (proposal only)


def plan_method_rename(ws: Workspace, rel_path: str, class_name: str, name: str, new_name: str) -> PlanResult:
    """Lists where a method is defined and used, but never produces an applicable patch: the receiver types of ``obj.method()`` are unknown."""
    res = PlanResult()
    res.limitations = ["Method calls on objects whose class is not known statically cannot be attributed, so a method rename is proposal-only."]
    if not validate_new_name(res, name, new_name):
        return res
    got = ws.read(rel_path)
    fa = ps.analyze(rel_path, got[0]) if got else None
    if fa is None:
        res.add("blocker", "target_unparsed", f"{rel_path} cannot be read or parsed.")
        return res
    cls = next((n for n in fa.tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    meth = next((n for n in (cls.body if cls else []) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    if cls is None or meth is None:
        res.add("blocker", "symbol_not_found", f"Method {class_name}.{name} was not found in {rel_path} on disk.")
        return res
    res.resolution = {"symbol": name, "rel_path": rel_path, "class": class_name, "kind": "method", "scope": "class", "confidence": "certain",
                      "definitions": [loc(rel_path, meth.lineno)]}
    res.add("blocker", "proposal_only", "Method renames are proposal-only: calls such as obj.method() cannot be attributed to this class without type information.")
    tok = ps._Tokens(fa.source).after("def", meth.lineno, 0, name)
    if tok:
        res.candidate_edits.append(Edit(rel_path, tok[0], tok[1], tok[2], name, new_name, "definition", "certain", "The method definition."))
    for node, _scope, site in fa.attributes:
        if node.attr == name and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls") and cls.lineno <= site.line <= (cls.end_lineno or site.line):
            res.candidate_edits.append(Edit(rel_path, site.line, site.col, site.end_col, name, new_name, "reference", "inferred", "Call through self/cls inside the class."))
    others = 0
    for rel in ws.paths(("python",)):
        g = ws.read(rel)
        if g and rel != rel_path and re.search(rf"\.{re.escape(name)}\b", g[0]):
            others += 1
    if others:
        res.add("warning", "unresolved_attribute_references", f"{others} other file(s) contain an attribute access named {name!r}; whether it targets this method is unknown.")
    if any(b for b in cls.bases):
        res.add("warning", "inheritance", "The class has base classes; overriding or overridden methods of the same name are not renamed.")
    return res
