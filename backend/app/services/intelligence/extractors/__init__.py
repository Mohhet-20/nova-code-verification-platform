from app.services.intelligence.extractors import javascript_extractor, python_extractor
from app.services.intelligence.parser_registry import register_extractor

register_extractor(("Python",), python_extractor.extract)
register_extractor(("JavaScript", "JSX", "TypeScript", "TSX"), javascript_extractor.extract)
