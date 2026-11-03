"""Static checks on generated test candidates. Defence in depth, NOT a safety proof: a test that passes these checks is
still arbitrary Python and only runs after a human approves it."""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.analysis.redaction import TOKEN_PATTERNS
from app.services.testing import facts as facts_mod

ALLOWED_IMPORTS = {"pytest", "math", "decimal", "fractions", "datetime", "itertools", "collections", "typing", "functools",
                   "operator", "string", "re", "json", "enum", "dataclasses", "statistics"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "exit", "quit", "globals", "locals",
                "vars", "setattr", "delattr"}
BANNED_ATTRS = {"system", "popen", "Popen", "rmtree", "remove", "unlink", "rmdir", "rename", "replace", "chmod", "chown", "kill",
                "fork", "spawnl", "execv", "execl", "makedirs", "mkdir", "write_text", "write_bytes", "touch", "urlopen",
                "connect", "socket", "__subclasses__", "__builtins__", "__globals__", "__code__", "__import__", "__loader__"}
BANNED_NAMES = {"os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "builtins", "pathlib", "tempfile"}
_SHELLISH = re.compile(r"(?i)\b(rm\s+-rf|curl\s|wget\s|powershell|cmd\.exe|/etc/passwd|chmod\s+\d)")
_URL = re.compile(r"(?i)\b(?:https?|ftp)://")


@dataclass
class Check:
    check: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail}


def validate(source: bytes, *, max_bytes: int, expected_module: str | None, recorded_sha: str,
             target_sha_recorded: str, target_sha_now: str | None, other_hashes: set[str],
             path_ok: bool, path_detail: str = "") -> list[Check]:
    checks: list[Check] = [Check("output_path", path_ok, path_detail or "inside the run's staging directory")]
    sha = hashlib.sha256(source).hexdigest()
    checks.append(Check("integrity", sha == recorded_sha, "content matches what was generated" if sha == recorded_sha
                        else "the staged file changed after generation"))
    checks.append(Check("size", len(source) <= max_bytes, f"{len(source)} bytes (limit {max_bytes})"))
    checks.append(Check("target_unchanged", target_sha_now is not None and target_sha_now == target_sha_recorded,
                        "target source is the version the plan was made from" if target_sha_now == target_sha_recorded
                        else "the target file changed or disappeared since generation; regenerate"))
    checks.append(Check("duplicate", sha not in other_hashes,
                        "unique in this run" if sha not in other_hashes else "identical to another candidate in this run"))
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        checks.append(Check("syntax", False, "not valid UTF-8"))
        return checks
    tree = facts_mod.parse_source(text)
    if tree is None:
        checks.append(Check("syntax", False, "does not parse as Python"))
        return checks
    checks.append(Check("syntax", True, "parses"))
    tests = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
    dupes = {n for n in tests if tests.count(n) > 1}
    checks.append(Check("structure", bool(tests) and not dupes,
                        f"{len(tests)} test function(s)" if tests and not dupes else
                        ("no test functions" if not tests else f"duplicate test names: {', '.join(sorted(dupes))}")))
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    bad_imports.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or not (mod in ALLOWED_IMPORTS or mod.split(".")[0] in ALLOWED_IMPORTS or mod == expected_module):
                bad_imports.append("." * node.level + mod)
            if any(a.name == "*" for a in node.names):
                bad_imports.append(f"{mod}.*")
    checks.append(Check("imports", not bad_imports, "only the target module and allow-listed standard modules"
                        if not bad_imports else f"not allowed: {', '.join(sorted(set(bad_imports))[:5])}"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            problems.append(f"call to {node.func.id}() at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            problems.append(f"attribute .{node.attr} at line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"name {node.id} at line {node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SHELLISH.search(node.value) or _URL.search(node.value):
                problems.append(f"shell- or network-like string at line {node.lineno}")
            elif any(p.search(node.value) for p in TOKEN_PATTERNS.values()):
                problems.append(f"credential-shaped string at line {node.lineno}")
    checks.append(Check("prohibited_patterns", not problems, "none found" if not problems else "; ".join(problems[:4])))
    return checks


def failed(checks: list[Check]) -> list[str]:
    return [f"{c.check}: {c.detail}" for c in checks if not c.passed]


def path_inside(path: Path, run_dir: Path) -> tuple[bool, str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False, "staged file is missing"
    if path.is_symlink():
        return False, "staged file is a symbolic link"
    base = run_dir.resolve()
    if base not in resolved.parents:
        return False, "outside the run's staging directory"
    return True, "inside the run's staging directory"
