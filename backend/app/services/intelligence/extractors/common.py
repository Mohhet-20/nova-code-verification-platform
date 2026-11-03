"""Helpers shared by tree-sitter extractors. Source text never leaves these helpers un-bounded."""
from __future__ import annotations

from typing import Any

from app.services.intelligence.types import ParsedSymbol

MAX_SIGNATURE = 200
MAX_DOC = 200
MAX_NAME = 200
MAX_SCOPE_DEPTH = 40
MAX_ERROR_REPORTS = 5
CALLER_KINDS = frozenset({"function", "method", "constructor", "class"})


class SymbolLimitReached(Exception):
    pass


class FileContext:
    """Per-file extraction state: source bytes, symbol list, ordinals, limits."""

    def __init__(self, data: bytes, module_qname: str, separator: str, max_symbols: int) -> None:
        self.data = data
        self.lines: list[bytes] | None = None
        self.module_qname = module_qname
        self.separator = separator  # "." for python, "#" for js/ts
        self.max_symbols = max_symbols
        self.symbols: list[ParsedSymbol] = []
        self._ordinals: dict[str, int] = {}
        self.node_keys: dict[int, str] = {}  # tree-sitter node id -> local_key, for caller attribution

    def text(self, node: Any) -> str:
        return self.data[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def col(self, row: int, byte_col: int) -> int:
        """1-based character column from a tree-sitter byte column."""
        if self.lines is None:
            self.lines = self.data.split(b"\n")
        line = self.lines[row] if row < len(self.lines) else b""
        return len(line[:byte_col].decode("utf-8", "ignore")) + 1

    def add(self, node: Any, name: str, kind: str, path: list[str], parent_key: str | None, **extra: Any
            ) -> ParsedSymbol:
        if len(self.symbols) >= self.max_symbols:
            raise SymbolLimitReached
        name = name[:MAX_NAME]
        nesting = ".".join(path + [name])
        ordinal = self._ordinals.get(nesting, 0)
        self._ordinals[nesting] = ordinal + 1
        qualified = (f"{self.module_qname}{self.separator}{nesting}")
        (sr, sc), (er, ec) = node.start_point, node.end_point
        sym = ParsedSymbol(
            name=name, kind=kind, qualified_name=qualified, start_line=sr + 1,
            start_col=self.col(sr, sc), end_line=er + 1, end_col=self.col(er, ec), parent_key=parent_key,
            local_key=f"{nesting}#{ordinal}",
            confidence="partial" if node.has_error else "observed", **extra)
        self.symbols.append(sym)
        if kind in CALLER_KINDS:
            self.node_keys[node.id] = sym.local_key
        return sym

    def header(self, node: Any, body: Any | None) -> str:
        end = body.start_byte if body is not None else node.end_byte
        raw = self.data[node.start_byte:min(end, node.start_byte + 4 * MAX_SIGNATURE)]
        return " ".join(raw.decode("utf-8", "replace").split())[:MAX_SIGNATURE].rstrip(" :{")


def first_line(text: str, limit: int = MAX_DOC) -> str | None:
    for raw in text.splitlines():
        line = raw.strip().strip("/*#\"' ").strip()
        if line:
            return line[:limit]
    return None


def syntax_diagnostics(root: Any) -> list[str]:
    """Locations (line numbers only, never source text) of syntax errors, bounded."""
    found: list[str] = []
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
            if len(found) < MAX_ERROR_REPORTS:
                kind = "missing token" if node.is_missing else "syntax error"
                found.append(f"{kind} at line {node.start_point[0] + 1}")
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    if count > len(found):
        found.append(f"{count - len(found)} more syntax error region(s)")
    return found
