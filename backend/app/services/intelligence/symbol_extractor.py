"""Single entry point that turns (language, bytes) into a normalized ParseResult, honestly."""
from __future__ import annotations

import logging

import app.services.intelligence.extractors  # noqa: F401  (registers extractors)
from app.services.intelligence import parser_registry as registry
from app.services.intelligence.types import FAILED, UNAVAILABLE, UNSUPPORTED, ParseResult

logger = logging.getLogger("nova.symbols")


def extract_symbols(language: str, data: bytes, rel_path: str, max_symbols: int = 5000) -> ParseResult:
    """Never raises. Never fabricates symbols; every non-parse outcome is explicit."""
    if language not in registry.CAPABILITIES:
        return ParseResult(UNSUPPORTED, "none")
    parser = registry.get_parser(language)
    extractor = registry.extractor_for(language)
    if parser is None or extractor is None:
        reason = registry.capability_for(language).unavailable_reason or "parser unavailable"
        return ParseResult(UNAVAILABLE, registry.CAPABILITIES[language].backend or "none",
                           diagnostics=[f"structural parser unavailable: {reason}"])
    try:
        return extractor(data, rel_path, parser, max_symbols)
    except Exception as exc:  # parser crash, RecursionError, ...
        logger.warning("extraction_failed", extra={"language": language, "error_type": type(exc).__name__})
        return ParseResult(FAILED, registry.CAPABILITIES[language].backend or "none",
                           diagnostics=[f"extraction failed ({type(exc).__name__})"])
