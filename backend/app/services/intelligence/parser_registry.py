"""Parser capability registry. Detection != parsing: each language declares what NOVA can really do."""
from __future__ import annotations

import importlib
import importlib.metadata as metadata
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.intelligence.types import ParseResult

logger = logging.getLogger("nova.parsers")

Extractor = Callable[..., ParseResult]


@dataclass(frozen=True)
class LanguageCapability:
    language: str
    backend: str | None            # e.g. "tree-sitter"
    packages: tuple[str, ...]      # python distributions required
    structural: bool               # structural parsing implemented for this language
    semantic: bool = False         # compiler-accurate resolution (never true)
    symbol_kinds: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class CapabilityReport:
    language: str
    parser_available: bool
    structural_supported: bool
    semantic_supported: bool
    backend: str | None
    backend_version: str | None
    symbol_kinds: tuple[str, ...]
    relationships: tuple[str, ...]
    status: str  # "structural" | "unavailable" | "metadata-only"
    notes: str
    unavailable_reason: str | None = None


PY_KINDS = ("class", "function", "method", "constructor", "constant", "variable", "field")
JS_KINDS = ("class", "function", "method", "constructor", "constant", "variable", "field", "property")
TS_KINDS = JS_KINDS + ("interface", "enum", "type_alias", "namespace")
REL = ("IMPORTS_FILE", "DEPENDS_ON_PACKAGE", "EXPORTS_SYMBOL", "DECLARES_SYMBOL", "CONTAINS_SYMBOL",
       "EXTENDS_SYMBOL", "CALLS")

CAPABILITIES: dict[str, LanguageCapability] = {
    "Python": LanguageCapability("Python", "tree-sitter", ("tree-sitter", "tree-sitter-python"), True,
                                 symbol_kinds=PY_KINDS, relationships=REL,
                                 notes="Syntax-level only: no type inference, no call graph."),
    "JavaScript": LanguageCapability("JavaScript", "tree-sitter", ("tree-sitter", "tree-sitter-javascript"), True,
                                     symbol_kinds=JS_KINDS, relationships=REL,
                                     notes="ES modules and CommonJS require(); no bundler/alias resolution."),
    "JSX": LanguageCapability("JSX", "tree-sitter", ("tree-sitter", "tree-sitter-javascript"), True,
                              symbol_kinds=JS_KINDS, relationships=REL, notes="Parsed with the JavaScript grammar."),
    "TypeScript": LanguageCapability("TypeScript", "tree-sitter", ("tree-sitter", "tree-sitter-typescript"), True,
                                     symbol_kinds=TS_KINDS, relationships=REL,
                                     notes="tsconfig path aliases are not resolved; 'implements' not recorded."),
    "TSX": LanguageCapability("TSX", "tree-sitter", ("tree-sitter", "tree-sitter-typescript"), True,
                              symbol_kinds=TS_KINDS, relationships=REL, notes="Parsed with the TSX grammar."),
}

_GRAMMARS: dict[str, tuple[str, str]] = {  # language -> (module, function returning the grammar capsule)
    "Python": ("tree_sitter_python", "language"),
    "JavaScript": ("tree_sitter_javascript", "language"),
    "JSX": ("tree_sitter_javascript", "language"),
    "TypeScript": ("tree_sitter_typescript", "language_typescript"),
    "TSX": ("tree_sitter_typescript", "language_tsx"),
}

_parsers: dict[str, object] = {}
_errors: dict[str, str] = {}
_force_unavailable: set[str] = set()  # test hook


def disable_parser_for_tests(*languages: str) -> None:
    _force_unavailable.update(languages)


def reset_for_tests() -> None:
    _force_unavailable.clear()
    _parsers.clear()
    _errors.clear()


def get_parser(language: str) -> object | None:
    """Return a ready tree-sitter Parser for ``language`` or None if unavailable."""
    if language in _force_unavailable:
        return None
    if language in _parsers:
        return _parsers[language]
    if language not in _GRAMMARS or language in _errors:
        return None
    module_name, func = _GRAMMARS[language]
    try:
        from tree_sitter import Language, Parser

        module = importlib.import_module(module_name)
        parser = Parser(Language(getattr(module, func)()))
    except Exception as exc:  # missing package, ABI mismatch, native load failure
        _errors[language] = f"{type(exc).__name__}"
        logger.warning("parser_unavailable", extra={"language": language, "error_type": type(exc).__name__})
        return None
    _parsers[language] = parser
    return parser


def _version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def capability_for(language: str) -> CapabilityReport:
    cap = CAPABILITIES.get(language)
    if cap is None:
        return CapabilityReport(language, False, False, False, None, None, (), (), "metadata-only",
                                "Detected by name/extension only. No structural parser: indexed as metadata.")
    available = get_parser(language) is not None
    reason = None
    if not available:
        reason = ("disabled for this run" if language in _force_unavailable
                  else f"tree-sitter grammar could not be loaded ({_errors.get(language, 'not installed')})")
    return CapabilityReport(
        language, available, cap.structural, cap.semantic, cap.backend,
        _version(cap.packages[-1]) if available else None, cap.symbol_kinds if available else (),
        cap.relationships if available else (), "structural" if available else "unavailable",
        cap.notes, reason)


def all_capabilities(detected_languages: list[str] | None = None) -> list[CapabilityReport]:
    names = list(CAPABILITIES)
    for lang in detected_languages or []:
        if lang not in CAPABILITIES and lang not in names:
            names.append(lang)
    return [capability_for(n) for n in names]


@dataclass
class _Registry:
    extractors: dict[str, Extractor] = field(default_factory=dict)


registry = _Registry()


def register_extractor(languages: tuple[str, ...], fn: Extractor) -> None:
    for language in languages:
        registry.extractors[language] = fn


def extractor_for(language: str) -> Extractor | None:
    return registry.extractors.get(language)
