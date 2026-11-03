"""Normalized, backend-independent representations produced by parsers and consumed by the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field

# Bump when extraction logic changes so unchanged files are re-analyzed on the next run.
ANALYSIS_VERSION = 2  # 2: call-site extraction added

# Symbol kinds NOVA can emit. A language only emits the kinds its extractor really supports.
SYMBOL_KINDS = (
    "module", "package", "namespace", "class", "struct", "interface", "enum", "function",
    "method", "constructor", "property", "field", "constant", "variable", "type_alias",
)

# analysis_status values (stored per file)
PARSED = "parsed"                 # structural parse completed without syntax errors
PARTIAL = "partial"               # parsed, but the file has syntax errors; only valid parts extracted
FAILED = "failed"                 # a parser exists but crashed / exceeded bounds
UNAVAILABLE = "unavailable"       # a parser is supported but not installed or not loadable
UNSUPPORTED = "unsupported"       # no structural parser for this language; metadata only

RESOLVED, EXTERNAL, UNRESOLVED, UNSUPPORTED_RES = "resolved", "external", "unresolved", "unsupported"


@dataclass
class ParsedSymbol:
    name: str
    kind: str
    qualified_name: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    parent_key: str | None  # local_key of the enclosing symbol in the same file
    local_key: str  # unique inside the file: "<nesting>#<ordinal>"
    visibility: str | None = None  # "public" | "private" | None (unknown)
    exported: bool = False
    signature: str | None = None
    doc_summary: str | None = None
    bases: list[str] = field(default_factory=list)
    confidence: str = "observed"  # "observed" | "partial"


@dataclass
class ParsedImport:
    kind: str  # "import" | "export" | "reexport" | "require"
    specifier: str
    names: list[dict[str, str | None]]  # [{"name": "Foo", "alias": "F"}], "*" for wildcard
    line: int
    level: int = 0  # python relative import depth
    type_only: bool = False


@dataclass
class ParsedCall:
    """A syntactically observed call site. The callee is text only; resolution happens later."""
    caller_key: str | None  # local_key of the enclosing function/method/class, None at module level
    callee: str | None  # dotted name such as "helper", "self.run", "mod.fn"; None when not a simple name
    line: int


@dataclass
class ParseResult:
    status: str
    parser: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)  # short, sanitized; never source text
    calls: list[ParsedCall] = field(default_factory=list)
