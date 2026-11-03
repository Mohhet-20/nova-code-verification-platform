"""Deterministic fixes for a few static-analysis rules whose repair is mechanical.

A fix is offered only when ALL of these hold; otherwise NOVA recommends and does not fabricate a patch:
1. the finding was produced by the Phase 04 analyzers on the file as it is on disk right now;
2. the exact source construct matches the transformer's narrow pattern (anything else is declined);
3. the transformed file parses, and re-running the analyzers shows the targeted finding is gone and no rule/scope has gained findings.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.analysis.model import RawFinding
from app.services.repair import patch as patchlib
from app.services.review import findings as review_findings

RULES = {
    "PY-COR-001": {
        "title": "Compare to None with 'is' / 'is not'",
        "strategy": "Replace '== None' / '!= None' with 'is None' / 'is not None'.",
        "behavior": "Identity comparison to None is used, so objects that override __eq__ can no longer change the result.",
        "side_effects": ["Objects with a custom __eq__ that deliberately equals None would now compare unequal (rare)."],
    },
    "PY-COR-002": {
        "title": "Replace a mutable default argument with None and create it per call",
        "strategy": "Default the parameter to None and create a fresh container at the start of the function.",
        "behavior": "Each call that omits the argument gets its own new container instead of sharing one across calls.",
        "side_effects": ["Callers that relied on state shared between calls through the default will behave differently.",
                         "An explicit None passed by a caller now also yields a fresh container."],
    },
    "PY-MNT-005": {
        "title": "Remove an unused import",
        "strategy": "Delete the import statement that the analyzer reports as unused.",
        "behavior": "The module no longer imports a name it does not use.",
        "side_effects": ["If the import had side effects, or the name is re-exported or used dynamically, removing it changes behavior."],
    },
}


@dataclass
class Fix:
    rule_id: str
    rel_path: str
    line: int
    scope: str
    new_text: str
    old_text: str
    diff: str
    notes: list[str] = field(default_factory=list)

    def info(self) -> dict[str, Any]:
        meta = RULES[self.rule_id]
        return {"rule_id": self.rule_id, "rel_path": self.rel_path, "line": self.line, "scope": self.scope, "summary": meta["title"],
                "strategy": meta["strategy"], "expected_behavior": meta["behavior"], "side_effects": meta["side_effects"] + self.notes}


def _fix_none_compare(src: str, f: RawFinding) -> str | None:
    lines = src.split("\n")
    if not (1 <= f.line <= len(lines)):
        return None
    line = lines[f.line - 1]
    hits = list(re.finditer(r"(?<![=!<>])\s(==|!=)\s+None\b", line))
    if len(hits) != 1 or line.lstrip().startswith("#"):
        return None
    m = hits[0]
    lines[f.line - 1] = line[: m.start()] + (" is None" if m[1] == "==" else " is not None") + line[m.end():]
    return "\n".join(lines)


def _empty_container(node: ast.expr) -> str | None:
    if isinstance(node, ast.List) and not node.elts:
        return "[]"
    if isinstance(node, ast.Dict) and not node.keys:
        return "{}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("list", "dict", "set") and not node.args and not node.keywords:
        return f"{node.func.id}()"
    return None


def _fix_mutable_default(src: str, f: RawFinding) -> str | None:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= f.line <= node.body[0].lineno:
            if target is None or node.lineno > target.lineno:
                target = node
    if target is None or not target.body or target.body[0].lineno == target.lineno:
        return None  # one-line function bodies are declined
    a = target.args
    pairs = list(zip(a.args[len(a.args) - len(a.defaults):], a.defaults, strict=True)) + [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
    fixes = [(arg, d, _empty_container(d)) for arg, d in pairs if _empty_container(d)]
    if not fixes:
        return None
    if any(arg.annotation is not None for arg, _, _ in fixes):
        return None  # keeping the annotation correct needs a type change NOVA will not guess
    lines = src.split("\n")
    # edit defaults right-to-left so columns stay valid; each default must sit on a single line
    for _arg, d, _ in sorted(fixes, key=lambda t: (t[1].lineno, t[1].col_offset), reverse=True):
        if d.lineno != d.end_lineno:
            return None
        raw = lines[d.lineno - 1].encode("utf-8")
        lines[d.lineno - 1] = (raw[: d.col_offset] + b"None" + raw[d.end_col_offset:]).decode("utf-8")
    first = target.body[0]
    doc_end = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str) else None
    indent = " " * (target.body[1].col_offset if doc_end and len(target.body) > 1 else first.col_offset)
    insert_at = doc_end if doc_end else first.lineno - 1  # index in `lines` after which the guard is inserted
    guard: list[str] = []
    for arg, _, empty in sorted(fixes, key=lambda t: t[1].lineno):
        guard += [f"{indent}if {arg.arg} is None:", f"{indent}    {arg.arg} = {empty}"]
    return "\n".join(lines[:insert_at] + guard + lines[insert_at:])


def _fix_unused_import(src: str, f: RawFinding, rel: str) -> str | None:
    if rel.endswith("__init__.py"):
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    stmt = next((n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno <= f.line <= n.end_lineno), None)
    if stmt is None or len(stmt.names) != 1 or stmt.lineno != stmt.end_lineno:
        return None
    if isinstance(stmt, ast.ImportFrom) and (stmt.module == "__future__" or stmt.names[0].name == "*"):
        return None
    lines = src.split("\n")
    text = lines[stmt.lineno - 1]
    if ";" in text or "#" in text:
        return None
    del lines[stmt.lineno - 1]
    return "\n".join(lines)


def _transform(rule: str, source: str, raw: RawFinding, rel: str) -> str | None:
    if rule == "PY-COR-001":
        return _fix_none_compare(source, raw)
    if rule == "PY-COR-002":
        return _fix_mutable_default(source, raw)
    return _fix_unused_import(source, raw, rel)


def _signature(outcome: Any, rel: str) -> Counter[tuple[str, str, str]]:
    return Counter((r.rule_id, r.scope, r.discriminator) for _, r in outcome.findings.get(rel, []))


def propose(root: Path, settings: Any, rel: str, source: str, wanted_rule: str | None = None, wanted_line: int | None = None) -> tuple[list[Fix], list[str]]:
    """Return verified fixes for one file and the reasons any candidate was declined."""
    fixes: list[Fix] = []
    declined: list[str] = []
    if not source.endswith("\n") or "\r" in source:
        return [], [f"{rel}: only LF files with a final newline are supported for generated patches."]
    before = review_findings.analyze(root, [(rel, source.encode("utf-8"))])
    if rel not in before.analyzed:
        return [], [f"{rel}: not analysed ({'; '.join(before.skipped) or 'unsupported'})."]
    base_sig = _signature(before, rel)
    seen: set[tuple[str, int]] = set()
    for _fp, raw in before.findings[rel]:
        if raw.rule_id not in RULES or (wanted_rule and raw.rule_id != wanted_rule):
            continue
        if wanted_line is not None and not (raw.line <= wanted_line <= max(raw.end_line, raw.line)):
            continue
        if (raw.rule_id, raw.line) in seen:
            continue
        seen.add((raw.rule_id, raw.line))
        new = _transform(raw.rule_id, source, raw, rel)
        if new is None or new == source:
            declined.append(f"{rel}:{raw.line} {raw.rule_id}: the construct is outside the narrow pattern NOVA fixes automatically.")
            continue
        try:
            ast.parse(new)
        except SyntaxError:
            declined.append(f"{rel}:{raw.line} {raw.rule_id}: the transformed file would not parse.")
            continue
        after = review_findings.analyze(root, [(rel, new.encode("utf-8"))])
        after_sig = _signature(after, rel)
        key = (raw.rule_id, raw.scope, raw.discriminator)
        if after_sig[key] >= base_sig[key] or any(after_sig[k] > base_sig[k] for k in after_sig):
            declined.append(f"{rel}:{raw.line} {raw.rule_id}: re-analysis did not confirm the fix, so no patch was produced.")
            continue
        fixes.append(Fix(raw.rule_id, rel, raw.line, raw.scope, new, source, patchlib.make_diff(rel, source, new)))
    return fixes, declined
