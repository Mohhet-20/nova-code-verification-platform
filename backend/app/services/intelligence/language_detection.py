"""Deterministic language detection from file names, extensions and (for extensionless scripts) a shebang.

Detection says only what a file *is*. Whether NOVA can parse it is decided by ``parser_registry``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".pyw": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JSX",
    ".ts": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript", ".tsx": "TSX",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".cc": "C++", ".cpp": "C++",
    ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".swift": "Swift", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell", ".bat": "Batch",
    ".cmd": "Batch", ".sql": "SQL", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "CSS", ".sass": "CSS", ".less": "CSS", ".json": "JSON", ".jsonc": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".xml": "XML", ".md": "Markdown",
    ".markdown": "Markdown", ".rst": "Markdown", ".txt": "Text", ".ini": "Config",
    ".cfg": "Config", ".conf": "Config", ".lock": "Lockfile", ".vue": "Vue",
    ".svelte": "Svelte", ".dart": "Dart", ".lua": "Lua", ".ipynb": "Notebook",
    ".tf": "Terraform", ".proto": "Protobuf", ".gradle": "Gradle", ".ex": "Elixir",
    ".exs": "Elixir", ".hs": "Haskell", ".clj": "Clojure", ".groovy": "Groovy",
}

# Extensions that genuinely map to more than one language. The first entry is used; the rest are
# reported as alternatives and the confidence is "low".
AMBIGUOUS_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".h": ("C", "C++"),
    ".m": ("Objective-C", "MATLAB"),
    ".pl": ("Perl", "Prolog"),
    ".r": ("R",),  # case-insensitive mapping of .R / .r; listed so the rule is explicit
    ".v": ("Verilog", "Coq"),
}

LANGUAGE_BY_FILENAME: dict[str, str] = {
    "dockerfile": "Dockerfile", "makefile": "Makefile", "gnumakefile": "Makefile",
    "rakefile": "Ruby", "gemfile": "Ruby", "cmakelists.txt": "CMake", "jenkinsfile": "Groovy",
    "procfile": "Config", ".bashrc": "Shell", ".zshrc": "Shell", ".profile": "Shell",
    ".gitignore": "Config", ".gitattributes": "Config", ".editorconfig": "Config",
}

SHEBANG_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("python", "Python"), ("node", "JavaScript"), ("bash", "Shell"), ("zsh", "Shell"),
    ("/sh", "Shell"), (" sh", "Shell"), ("ruby", "Ruby"), ("perl", "Perl"), ("php", "PHP"),
)

OTHER = "Other"


@dataclass(frozen=True)
class LanguageGuess:
    language: str
    confidence: str  # "high" (name/extension), "medium" (shebang), "low" (ambiguous), "none" (unknown)
    via: str  # "filename" | "extension" | "shebang" | "none"
    alternatives: tuple[str, ...] = ()


def detect_language(filename: str, head: bytes | None = None) -> LanguageGuess:
    """Detect a language. ``head`` (first bytes of the file) is only consulted for a shebang."""
    name = PurePosixPath(filename).name
    lowered = name.lower()
    if lowered in LANGUAGE_BY_FILENAME:
        return LanguageGuess(LANGUAGE_BY_FILENAME[lowered], "high", "filename")
    suffix = PurePosixPath(lowered).suffix
    if suffix in AMBIGUOUS_EXTENSIONS and len(AMBIGUOUS_EXTENSIONS[suffix]) > 1:
        first, *rest = AMBIGUOUS_EXTENSIONS[suffix]
        return LanguageGuess(first, "low", "extension", tuple(rest))
    if suffix in LANGUAGE_BY_EXTENSION:
        return LanguageGuess(LANGUAGE_BY_EXTENSION[suffix], "high", "extension")
    if suffix == ".r":
        return LanguageGuess("R", "high", "extension")
    if not suffix and head and head.startswith(b"#!"):
        line = head.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
        for needle, language in SHEBANG_LANGUAGES:
            if needle in line:
                return LanguageGuess(language, "medium", "shebang")
    return LanguageGuess(OTHER, "none", "none")
