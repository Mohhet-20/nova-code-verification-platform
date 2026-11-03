from __future__ import annotations

import pytest

from app.services.intelligence import parser_registry
from app.services.intelligence.symbol_extractor import extract_symbols
from app.services.intelligence.types import FAILED, PARSED, PARTIAL, UNAVAILABLE, UNSUPPORTED

PY = b'''"""mod doc"""
import os
from .util import helper as h
from . import sibling
MAX = 3
# def commented_out(): pass
"""
def inside_string(): pass
"""
class Base:
    """Base doc."""
    x: int = 1
    def run(self, a, b=2):
        def inner(): pass
        return a
class Child(Base, object):
    def __init__(self): pass
def run(): pass
def run(): pass
__all__ = ["run"]
'''


@pytest.fixture(autouse=True)
def _reset() -> None:
    parser_registry.reset_for_tests()


def by_name(result, qualified):  # type: ignore[no-untyped-def]
    return [s for s in result.symbols if s.qualified_name == qualified]


def test_python_symbols_and_locations() -> None:
    r = extract_symbols("Python", PY, "pkg/m.py")
    assert r.status == PARSED and r.parser == "tree-sitter-python"
    names = {s.qualified_name for s in r.symbols}
    assert {"pkg.m.Base", "pkg.m.Base.run", "pkg.m.Base.run.inner", "pkg.m.Child.__init__", "pkg.m.MAX"} <= names
    base = by_name(r, "pkg.m.Base")[0]
    assert base.kind == "class" and base.start_line == 10 and base.doc_summary == "Base doc."
    assert by_name(r, "pkg.m.Child")[0].bases == ["Base", "object"]
    assert by_name(r, "pkg.m.Child.__init__")[0].kind == "constructor"


def test_comments_and_strings_are_not_symbols() -> None:
    r = extract_symbols("Python", PY, "pkg/m.py")
    assert not any(s.name in ("commented_out", "inside_string") for s in r.symbols)


def test_duplicate_names_get_distinct_local_keys_and_all_marks_exports() -> None:
    r = extract_symbols("Python", PY, "pkg/m.py")
    runs = by_name(r, "pkg.m.run")
    assert len(runs) == 2 and len({s.local_key for s in runs}) == 2
    assert all(s.exported for s in runs) and not by_name(r, "pkg.m.Base")[0].exported


def test_python_imports_record_level_and_aliases() -> None:
    r = extract_symbols("Python", PY, "pkg/m.py")
    rel = [i for i in r.imports if i.kind == "import" and i.level == 1]
    assert {i.names[0]["name"] for i in rel} == {"helper", "sibling"}
    assert next(i for i in rel if i.names[0]["name"] == "helper").names[0]["alias"] == "h"
    assert next(i for i in r.imports if i.kind == "export").line == 20


def test_syntax_error_is_partial_not_fabricated() -> None:
    r = extract_symbols("Python", b"def broken(:\n  pass\nclass Ok:\n  def m(self): pass\n", "b.py")
    assert r.status == PARTIAL and r.diagnostics
    assert "b.Ok.m" in {s.qualified_name for s in r.symbols}
    assert all("pass" not in d for d in r.diagnostics)  # diagnostics never echo source text


def test_empty_file_parses_with_no_symbols() -> None:
    r = extract_symbols("Python", b"", "e.py")
    assert r.status == PARSED and r.symbols == [] and r.imports == []


def test_typescript_symbols_imports_and_doc_comments() -> None:
    src = b'''import React, {useState as us} from "react";
import type {T} from "./types";
export * from "./other";
const lib = require("lodash");
/** Greets. */
export function greet(name: string): string { return name }
export const arrow = (a) => a;
class A extends B { m() {} static s = 1; constructor(){ super() } }
interface I { x: number }
type U = string | number;
enum E { A, B }
'''
    r = extract_symbols("TypeScript", src, "a.ts")
    assert r.status == PARSED
    kinds = {s.name: s.kind for s in r.symbols}
    assert kinds["greet"] == "function" and kinds["A"] == "class" and kinds["I"] == "interface"
    assert kinds["U"] == "type_alias" and kinds["E"] == "enum"
    greet = next(s for s in r.symbols if s.name == "greet")
    assert greet.exported and greet.doc_summary == "Greets."
    assert next(s for s in r.symbols if s.name == "A").bases == ["B"]
    specs = {(i.kind, i.specifier, i.type_only) for i in r.imports}
    assert ("import", "react", False) in specs and ("import", "./types", True) in specs
    assert ("reexport", "./other", False) in specs and ("require", "lodash", False) in specs


def test_tsx_and_jsx_are_parsed() -> None:
    assert extract_symbols("TSX", b"export const C = () => <div/>;\n", "c.tsx").status == PARSED
    assert extract_symbols("JSX", b"export function C(){ return <div/> }\n", "c.jsx").status == PARSED


def test_unsupported_language_is_metadata_only() -> None:
    r = extract_symbols("Go", b"package main\nfunc main() {}\n", "a.go")
    assert r.status == UNSUPPORTED and r.symbols == [] and r.imports == []


def test_parser_unavailable_is_reported_honestly() -> None:
    parser_registry.disable_parser_for_tests("Python")
    r = extract_symbols("Python", b"def f(): pass\n", "a.py")
    assert r.status == UNAVAILABLE and r.symbols == [] and r.diagnostics
    cap = parser_registry.capability_for("Python")
    assert cap.status == "unavailable" and cap.unavailable_reason


def test_extractor_crash_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RecursionError("deep")

    parser_registry.register_extractor(("Python",), boom)
    try:
        r = extract_symbols("Python", b"x = 1\n", "a.py")
        assert r.status == FAILED and r.symbols == []
    finally:
        import importlib

        import app.services.intelligence.extractors as ex
        importlib.reload(ex)


def test_deeply_nested_input_does_not_crash() -> None:
    src = ("(" * 3000 + "1" + ")" * 3000 + "\n").encode()
    assert extract_symbols("Python", src, "deep.py").status in (PARSED, PARTIAL, FAILED)


def test_symbol_limit_is_enforced() -> None:
    src = "\n".join(f"def f{i}(): pass" for i in range(50)).encode()
    r = extract_symbols("Python", src, "many.py", max_symbols=10)
    assert len(r.symbols) <= 10 and r.status == PARTIAL


def test_unicode_identifiers_and_character_columns() -> None:
    r = extract_symbols("Python", "ü = 1\nclass Ünï:\n    pass\n".encode(), "u.py")
    cls = next(s for s in r.symbols if s.kind == "class")
    assert cls.name == "Ünï" and cls.start_line == 2 and cls.start_col == 1
