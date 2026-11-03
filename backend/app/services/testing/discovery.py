"""Static discovery of pytest tests. Test files are parsed with ``ast``; nothing is imported or executed."""
from __future__ import annotations

import ast
import configparser
import fnmatch
import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.testing import facts
from app.services.testing.modules import module_index

DEFAULT_FILES = ["test_*.py", "*_test.py"]
DEFAULT_CLASSES = ["Test*"]
DEFAULT_FUNCTIONS = ["test*"]
EXCLUDED_BASENAMES = {"conftest.py", "setup.py", "__init__.py"}


@dataclass
class PytestConfig:
    config_file: str | None = None
    python_files: list[str] = field(default_factory=lambda: list(DEFAULT_FILES))
    python_classes: list[str] = field(default_factory=lambda: list(DEFAULT_CLASSES))
    python_functions: list[str] = field(default_factory=lambda: list(DEFAULT_FUNCTIONS))
    testpaths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"config_file": self.config_file, "python_files": self.python_files, "python_classes": self.python_classes,
                "python_functions": self.python_functions, "testpaths": self.testpaths}


@dataclass
class DiscoveredFile:
    rel_path: str
    sha256: str = ""
    parse_status: str = "ok"
    parse_error: str | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    associated: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _words(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def read_pytest_config(root: Path) -> PytestConfig:
    """Static read of pytest.ini, pyproject.toml, tox.ini or setup.cfg (first match in pytest's own order)."""
    cfg = PytestConfig()
    sources: list[tuple[str, str]] = [("pytest.ini", "pytest"), ("pyproject.toml", "toml"), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")]
    for name, section in sources:
        path = root / name
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 200_000:
                continue
            if section == "toml":
                data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
                table = data.get("tool", {}).get("pytest", {}).get("ini_options")
                if not isinstance(table, dict):
                    continue
                values = {k: table.get(k) for k in ("python_files", "python_classes", "python_functions", "testpaths")}
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
                if not parser.has_section(section):
                    continue
                values = {k: parser.get(section, k, fallback=None) for k in
                          ("python_files", "python_classes", "python_functions", "testpaths")}
        except (OSError, ValueError, configparser.Error, tomllib.TOMLDecodeError):
            cfg.notes.append(f"{name} could not be read; default pytest conventions are assumed")
            continue
        cfg.config_file = name
        for key, value in values.items():
            words = _words(value)
            if words:
                setattr(cfg, key, words)
        return cfg
    return cfg


def _match(name: str, patterns: list[str], *, prefix_ok: bool) -> bool:
    for p in patterns:
        if any(c in p for c in "*?["):
            if fnmatch.fnmatchcase(name, p):
                return True
        elif name.startswith(p) if prefix_ok else name == p:
            return True
    return False


def is_test_file(rel_path: str, cfg: PytestConfig) -> bool:
    base = rel_path.rsplit("/", 1)[-1]
    return base not in EXCLUDED_BASENAMES and any(fnmatch.fnmatchcase(base, p) for p in cfg.python_files)


def _is_unittest_class(node: ast.ClassDef) -> bool:
    for b in node.bases:
        name = ast.unparse(b)
        if name in ("TestCase", "unittest.TestCase") or name.endswith(".TestCase"):
            return True
    return False


def collect_tests(tree: ast.Module, rel_path: str, cfg: PytestConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def parametrized(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any("parametrize" in ast.unparse(d) for d in node.decorator_list)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _match(node.name, cfg.python_functions, prefix_ok=True):
            out.append({"id": f"{rel_path}::{node.name}", "name": node.name, "class": None, "line": node.lineno,
                        "parametrized": parametrized(node)})
        elif isinstance(node, ast.ClassDef):
            unit = _is_unittest_class(node)
            has_init = any(isinstance(b, ast.FunctionDef) and b.name in ("__init__", "__new__") for b in node.body)
            if not unit and (has_init or not _match(node.name, cfg.python_classes, prefix_ok=True)):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        item.name.startswith("test") if unit else _match(item.name, cfg.python_functions, prefix_ok=True)):
                    out.append({"id": f"{rel_path}::{node.name}::{item.name}", "name": item.name, "class": node.name,
                                "line": item.lineno, "parametrized": parametrized(item)})
    return out


def _imports(tree: ast.Module, rel_path: str, index: dict[str, str | None]) -> dict[str, str]:
    """Imported repo modules -> rel_path (only unambiguous matches)."""
    found: dict[str, str] = {}
    pkg_parts = rel_path.split("/")[:-1]

    def hit(name: str) -> None:
        target = index.get(name)
        if target and target != rel_path:
            found[target] = name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                hit(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level - 1 <= len(pkg_parts) else []
                mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                mod = node.module or ""
            if not mod:
                continue
            hit(mod)
            for a in node.names:  # `from pkg import module`
                hit(f"{mod}.{a.name}")
    return found


def discover(root: Path, python_files: dict[str, str], *, max_files: int, max_bytes: int
             ) -> tuple[PytestConfig, list[DiscoveredFile], list[str], list[str], dict[str, int]]:
    """``python_files``: rel_path -> sha256 of the indexed Python files.

    Returns (config, test files, errors, excluded notes, source_stats). Source files are indexed non-test Python files.
    """
    cfg = read_pytest_config(root)
    errors: list[str] = list(cfg.notes)
    excluded: list[str] = []
    index = module_index(python_files)
    candidates = sorted(p for p in python_files if is_test_file(p, cfg))
    if len(candidates) > max_files:
        errors.append(f"{len(candidates)} test files found; only the first {max_files} were inspected")
        candidates = candidates[:max_files]
    test_set = set(candidates)
    sources = sorted(p for p in python_files if p not in test_set and p.rsplit("/", 1)[-1] not in EXCLUDED_BASENAMES)
    stems: dict[str, list[str]] = {}
    for s in sources:
        stems.setdefault(s.rsplit("/", 1)[-1][:-3], []).append(s)
    files: list[DiscoveredFile] = []
    for rel in candidates:
        df = DiscoveredFile(rel)
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file():
                df.parse_status, df.parse_error = "unreadable", "not a regular file"
                excluded.append(f"{rel}: not a regular file")
                files.append(df)
                continue
            if path.stat().st_size > max_bytes:
                df.parse_status, df.parse_error = "too_large", f"larger than {max_bytes} bytes"
                excluded.append(f"{rel}: larger than {max_bytes} bytes")
                files.append(df)
                continue
            data = path.read_bytes()
        except OSError:
            df.parse_status, df.parse_error = "unreadable", "could not be read"
            files.append(df)
            continue
        df.sha256 = hashlib.sha256(data).hexdigest()
        tree = facts.parse_source(data.decode("utf-8", errors="replace"))
        if tree is None:
            df.parse_status, df.parse_error = "syntax_error", "the file does not parse; pytest would report a collection error"
            files.append(df)
            continue
        df.tests = collect_tests(tree, rel, cfg)
        for target, mod in sorted(_imports(tree, rel, index).items()):
            if target in test_set:
                continue
            df.associated.append({"path": target, "via": "import", "confidence": "high", "module": mod})
        base = rel.rsplit("/", 1)[-1][:-3]
        stem = base[5:] if base.startswith("test_") else base[:-5] if base.endswith("_test") else None
        if stem and len(stems.get(stem, [])) == 1 and stems[stem][0] not in {a["path"] for a in df.associated}:
            df.associated.append({"path": stems[stem][0], "via": "name", "confidence": "low", "module": None})
        if not df.tests:
            df.notes.append("no tests matched the collection patterns")
        files.append(df)
    covered = {a["path"] for f in files for a in f.associated if f.tests}
    stats = {"source_files_total": len(sources), "source_files_with_tests": len(covered & set(sources))}
    return cfg, files, errors, excluded, stats
