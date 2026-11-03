"""Filesystem boundary helpers. Every path that touches a repository goes through here."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.errors import InvalidRepositoryError, UnsafePathError


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` (already resolved) is ``root`` or located below it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_repository_root(raw: str, allowed_bases: list[str] | None = None) -> Path:
    """Validate and canonicalize a repository root supplied by a client."""
    if not raw or not raw.strip():
        raise InvalidRepositoryError("Repository path must not be empty.")
    if "\x00" in raw:
        raise UnsafePathError("Repository path contains an invalid character.")
    candidate = Path(os.path.expanduser(raw.strip()))
    if not candidate.is_absolute():
        raise InvalidRepositoryError("Repository path must be absolute.")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise InvalidRepositoryError("Repository path does not exist.") from None
    except (OSError, RuntimeError):
        raise InvalidRepositoryError("Repository path could not be resolved.") from None
    if not resolved.is_dir():
        raise InvalidRepositoryError("Repository path is not a directory.")
    if resolved == Path(resolved.anchor):
        raise InvalidRepositoryError("A filesystem root cannot be registered as a repository.")
    if resolved == Path.home().resolve():
        raise InvalidRepositoryError("A home directory cannot be registered as a repository.")
    if allowed_bases:
        bases = []
        for base in allowed_bases:
            try:
                bases.append(Path(base).expanduser().resolve(strict=True))
            except OSError:
                continue
        if not any(is_within(resolved, base) for base in bases):
            raise UnsafePathError("Repository path is outside the configured registration directories.")
    return resolved


def safe_join(root: Path, relative: str) -> Path:
    """Resolve ``relative`` below ``root`` or raise. Rejects traversal, absolute paths, symlink escapes."""
    if not relative or "\x00" in relative:
        raise UnsafePathError("Invalid relative path.")
    rel = Path(relative.replace("\\", "/"))
    if rel.is_absolute() or rel.drive or ".." in rel.parts:
        raise UnsafePathError("Path escapes the repository root.")
    root_real = root.resolve(strict=True)
    resolved = (root_real / rel).resolve(strict=False)
    if not is_within(resolved, root_real):
        raise UnsafePathError("Path escapes the repository root.")
    return resolved
