"""Syntactic call-site and dynamic-import collection (shared by the Python and JS/TS extractors).

This records *what the source says* (a call to the dotted name `a.b` on line N inside symbol X). It does not
decide what the callee is; `call_resolver` does that conservatively. Source is never executed or imported.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.intelligence.extractors.common import FileContext
from app.services.intelligence.types import ParsedCall, ParsedImport

MAX_CALLS_PER_FILE = 5000
_SIMPLE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$#][\w$]*)*$")
PY_DYNAMIC = {"__import__", "importlib.import_module", "import_module"}


def collect_calls(ctx: FileContext, root: Any, js: bool) -> tuple[list[ParsedCall], list[ParsedImport], list[str]]:
    call_types = {"call_expression", "new_expression"} if js else {"call"}
    calls: list[ParsedCall] = []
    dynamic: list[ParsedImport] = []
    notes: list[str] = []
    stack: list[tuple[Any, str | None]] = [(root, None)]
    limited = False
    while stack:
        node, enclosing = stack.pop()
        if node.is_error:
            continue  # do not guess inside syntax-error regions
        enclosing = ctx.node_keys.get(node.id, enclosing)
        if node.type in call_types:
            fn = node.child_by_field_name("constructor" if node.type == "new_expression" else "function")
            line = node.start_point[0] + 1
            if fn is not None:
                if len(calls) < MAX_CALLS_PER_FILE:
                    _record(ctx, node, fn, enclosing, line, js, calls, dynamic)
                if len(calls) >= MAX_CALLS_PER_FILE and not limited:
                    limited = True
                    notes.append(f"call limit ({MAX_CALLS_PER_FILE}) reached; remaining call sites not recorded")
        for child in reversed(node.children):
            stack.append((child, enclosing))
    return calls, dynamic, notes


def _record(ctx: FileContext, call: Any, fn: Any, enclosing: str | None, line: int, js: bool,
            calls: list[ParsedCall], dynamic: list[ParsedImport]) -> None:
    if js and fn.type == "import":
        dynamic.append(ParsedImport("dynamic", "", [], line))
        return
    if js and fn.type == "super":
        return
    text = ctx.text(fn)[:200].replace("?.", ".")
    simple = bool(_SIMPLE.match(text))
    if not simple:
        calls.append(ParsedCall(enclosing, None, line))
        return
    if js and text == "require":
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if first is not None and first.type == "string":
            return  # literal require(): already recorded as an import
        dynamic.append(ParsedImport("dynamic", "", [], line))
        return
    if not js and text in PY_DYNAMIC:
        dynamic.append(ParsedImport("dynamic", "", [], line))
        return
    calls.append(ParsedCall(enclosing, text, line))
