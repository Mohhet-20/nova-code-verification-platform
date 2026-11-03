"""Dotted module names and import roots for repository files, derived from the package structure (``__init__.py``)."""
from __future__ import annotations

import keyword
import re
from collections.abc import Iterable
from dataclasses import dataclass

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ModuleInfo:
    module: str  # dotted name as Python would import it
    import_root: str  # repo-relative directory that must be on sys.path ("" = repository root)


def _is_ident(part: str) -> bool:
    return bool(_IDENT.match(part)) and not keyword.iskeyword(part)


def module_info(rel_path: str, python_files: set[str]) -> ModuleInfo | None:
    """``None`` when the file cannot be imported by a plain ``import`` statement (bad identifier, not a .py file)."""
    if not rel_path.endswith(".py"):
        return None
    parts = rel_path[:-3].split("/")
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    if not parts or not all(_is_ident(p) for p in parts):
        return None
    # Walk up while the containing directory is a package (has __init__.py in the index).
    dirs = rel_path.split("/")[:-1]
    top = len(dirs)  # index in `dirs` of the first package directory
    while top > 0 and "/".join(dirs[:top]) + "/__init__.py" in python_files:
        top -= 1
    if is_pkg:
        # file is pkg/__init__.py: the package itself is dirs[top:]
        pkg_parts = dirs[top:]
        if not pkg_parts:
            return None
        return ModuleInfo(".".join(pkg_parts), "/".join(dirs[:top]))
    mod_parts = [*dirs[top:], parts[-1]]
    if not all(_is_ident(p) for p in mod_parts):
        return None
    return ModuleInfo(".".join(mod_parts), "/".join(dirs[:top]))


def module_index(python_files: Iterable[str]) -> dict[str, str | None]:
    """dotted module name -> rel_path (``None`` marks an ambiguous name). Includes the full path-from-root alias."""
    files = set(python_files)
    out: dict[str, str | None] = {}

    def put(name: str, rel: str) -> None:
        if name in out and out[name] != rel:
            out[name] = None
        else:
            out[name] = rel

    for rel in sorted(files):
        info = module_info(rel, files)
        if info is not None:
            put(info.module, rel)
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(_is_ident(p) for p in parts):
            put(".".join(parts), rel)
    return out
