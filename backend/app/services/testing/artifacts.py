"""Where generated tests and run artifacts live. Never inside a repository."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.core.config import BACKEND_DIR, Settings
from app.core.errors import UnsafePathError


def artifact_root(settings: Settings) -> Path:
    root = Path(settings.artifact_dir).expanduser() if settings.artifact_dir else BACKEND_DIR / "data" / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def run_dir(settings: Settings, repository_id: str, kind: str, run_id: str) -> tuple[Path, str]:
    """(absolute dir, relative path stored in the database). ids are hex so no traversal is possible; checked anyway."""
    for part in (repository_id, kind, run_id):
        if not part.isalnum():
            raise UnsafePathError("Invalid artifact identifier.")
    rel = f"{repository_id}/{kind}/{run_id}"
    base = artifact_root(settings)
    path = (base / rel).resolve()
    if base not in path.parents:
        raise UnsafePathError("Artifact path escapes the artifact root.")
    return path, rel


def resolve_rel(settings: Settings, rel: str) -> Path:
    """Resolve a stored artifact-relative path, refusing anything outside the artifact root."""
    base = artifact_root(settings)
    path = (base / rel).resolve()
    if not rel or base not in path.parents:
        raise UnsafePathError("Artifact path escapes the artifact root.")
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_new(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (never overwrite)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
