"""Render planned cases into pytest source using fixed templates. No model is involved.

All values that originate in repository source (names, annotations, docstring examples) are re-rendered through
``ast.unparse`` / ``repr`` or sanitised, never pasted verbatim.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.testing import facts as facts_mod
from app.services.testing.modules import ModuleInfo
from app.services.testing.planner import GENERATOR_VERSION, Case, plan_function

MAX_CASES_PER_FILE = 60
RESERVED = {"pytest", "_raised"}


def clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).replace('"""', "'''").replace("\\", "/")
    return text.strip()[:limit]


@dataclass
class Built:
    cases: list[Case]
    source: str
    module: str | None
    import_root: str | None
    warnings: list[str] = field(default_factory=list)
    file_stem: str = ""


def _file_stem(rel_path: str, module: str | None, symbol: str | None) -> str:
    base = (module or rel_path[:-3]).replace(".", "_").replace("/", "_")
    base = re.sub(r"[^A-Za-z0-9_]", "_", base)[:60]
    if symbol:
        base += "_" + re.sub(r"[^A-Za-z0-9_]", "_", symbol)[:40]
    return f"test_{base}_nova"


def _unparse_literal(src: str) -> str:
    return ast.unparse(ast.parse(src, mode="eval").body)


def select_functions(all_facts: list[facts_mod.FuncFacts], symbol: str | None) -> tuple[list[facts_mod.FuncFacts], list[str]]:
    warnings: list[str] = []
    if symbol:
        chosen = [f for f in all_facts if f.qualname == symbol or (f.class_name == symbol)]
        return chosen, warnings
    chosen = [f for f in all_facts if f.public]
    skipped = len(all_facts) - len(chosen)
    if skipped:
        warnings.append(f"{skipped} private function(s) were not planned; name a symbol to include one")
    return chosen, warnings


def build(rel_path: str, text: str, symbol: str | None, info: ModuleInfo | None, findings: list[dict[str, Any]],
          *, include_templates: bool = True) -> Built:
    tree = facts_mod.parse_source(text)
    if tree is None:
        raise ValueError("The target file does not parse as Python.")
    chosen, warnings = select_functions(facts_mod.analyze_module(tree), symbol)
    if symbol and not chosen:
        raise LookupError(f"Symbol '{clean(symbol, 80)}' was not found as a function or class in {rel_path}.")
    cases: list[Case] = []
    for f in chosen:
        cases += plan_function(f, findings, include_templates=include_templates)
    if len(cases) > MAX_CASES_PER_FILE:
        warnings.append(f"{len(cases)} cases were planned; only the first {MAX_CASES_PER_FILE} are included")
        cases = cases[:MAX_CASES_PER_FILE]
    importable = info is not None
    if not importable:
        warnings.append("The module cannot be imported by name (not a valid dotted module path); executable cases were "
                        "downgraded to review templates")
        for c in cases:
            if c.runnable:
                c.runnable, c.review_required, c.render = False, True, {"kind": "template", "signature": c.title}
                c.confidence = "low"
                c.assumptions.append("module is not importable by name")
    source = _render(rel_path, symbol, info, cases)
    return Built(cases=cases, source=source, module=info.module if info else None,
                 import_root=info.import_root if info else None, warnings=warnings, file_stem=_file_stem(rel_path, info.module if info else None, symbol))


def _render(rel_path: str, symbol: str | None, info: ModuleInfo | None, cases: list[Case]) -> str:
    runnable = [c for c in cases if c.runnable]
    names: list[str] = []
    for c in runnable:
        n = c.target_symbol
        if "." not in n and n not in names:
            names.append(n)
    kinds = {c.render.get("kind") for c in runnable}
    out: list[str] = [
        f"# NOVA-GENERATED TEST CANDIDATE (generator {GENERATOR_VERSION}). NOT REVIEWED. Do not trust it until you have read it.",
        f"# Target: {clean(rel_path, 200)}{'::' + clean(symbol, 80) if symbol else ''}",
        "# Cases marked TEMPLATE are skipped: they need a developer to decide the expected behaviour first.",
        "import pytest",
    ]
    if info is not None and names:
        imports = ", ".join(f"{n} as _t_{n}" if n in RESERVED else n for n in sorted(names))
        out.append(f"from {info.module} import {imports}")
    out.append("")
    if "not_raises" in kinds:
        out += ["", "def _raised(fn):", "    try:", "        fn()", "    except Exception as exc:  # returned for the assertion, not swallowed",
                "        return exc", "    return None", ""]
    for c in cases:
        out += ["", "", *_render_case(c)]
    return "\n".join(out).rstrip() + "\n"


def _fn(c: Case) -> str:
    return f"_t_{c.target_symbol}" if c.target_symbol in RESERVED else c.target_symbol


def _doc(c: Case) -> list[str]:
    lines = [f'    """{clean(c.title, 160)}', "", f"    Purpose: {clean(c.purpose, 200)}", f"    Expected: {clean(c.expected, 200)}",
             f"    Why: {clean(c.rationale, 240)}", f"    Confidence: {c.confidence}"]
    for a in c.assumptions[:4]:
        lines.append(f"    Assumption: {clean(a, 160)}")
    lines.append('    """')
    return lines


def _render_case(c: Case) -> list[str]:
    kind = c.render.get("kind")
    head = [f"def {c.test_name}():"]
    if kind == "example":
        call = _unparse_literal(c.render["call"])
        expected = _unparse_literal(c.render["expected"])
        return [*head, *_doc(c), f"    assert {call} == {expected}"]
    if kind == "raises":
        args, exc = c.render["args"], c.render["exc"]
        return [*head, *_doc(c), f"    with pytest.raises({_exc(exc)}):", f"        {_fn(c)}({args})"]
    if kind == "not_raises":
        args, exc = c.render["args"], c.render["exc"]
        return [*head, *_doc(c), f"    outcome = _raised(lambda: {_fn(c)}({args}))",
                f"    assert not isinstance(outcome, {_exc(exc)}), f\"unexpected rejection: {{outcome!r}}\""]
    if kind == "contract":
        return [*head, *_doc(c), f"    result = {_fn(c)}({c.render['args']})",
                f"    assert isinstance(result, {c.render['type']}), f\"unexpected type {{type(result).__name__}}\""]
    reason = clean(f"NOVA template: {c.rationale}", 200)
    return [f"@pytest.mark.skip(reason={reason!r})", *head, *_doc(c), f"    # Target: {clean(str(c.render.get('signature', c.target_symbol)), 200)}",
            f"    # Needed: {clean(c.expected, 200)}",
            f"    raise NotImplementedError({clean('define the expected behaviour for ' + c.title, 160)!r})"]


def _exc(name: str) -> str:
    """Exception class name as written in the target. Builtins resolve directly; others are not importable here."""
    import builtins
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and isinstance(getattr(builtins, name, None), type) \
            and issubclass(getattr(builtins, name), BaseException):
        return name
    return "Exception"
