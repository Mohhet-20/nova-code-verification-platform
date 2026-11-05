"""Safe, bounded, local-only repository file discovery (the "file_discovery" role).

The scanner never follows symbolic links, never reads outside the canonical repository root, never
stores file contents, and never records the *names* of skipped credential files.
Hard-coded safety rules (secret names, excluded directories, binaries) cannot be overridden by
repository files such as ``.gitignore``; those can only exclude *more*.
"""
from __future__ import annotations

import codecs
import hashlib
import logging
import os
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pathspec

from app.security.paths import is_within, safe_join
from app.security.secrets import is_secret_filename

logger = logging.getLogger("nova.indexer")


class SkipCode:
    """Concise, stable reason codes for files that are not (fully) indexed."""

    IGNORED_PATTERN = "IGNORED_PATTERN"
    BINARY_FILE = "BINARY_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_ENCODING = "UNSUPPORTED_ENCODING"
    OUTSIDE_REPOSITORY = "OUTSIDE_REPOSITORY"
    SYMLINK_REJECTED = "SYMLINK_REJECTED"
    SECRET_LIKE_PATH = "SECRET_LIKE_PATH"  # noqa: S105  (a reason code, not a credential)
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    UNREADABLE = "UNREADABLE"
    MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"
    DUPLICATE_FILE = "DUPLICATE_FILE"


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", ".env", "virtualenv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache",
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit", "coverage", ".coverage",
    "htmlcov", "site-packages", ".gradle", ".idea", ".vscode", ".terraform", ".ssh", ".aws",
    ".gnupg", "bower_components", ".parcel-cache", ".turbo", ".angular",
})

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".pdf", ".zip", ".gz",
    ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".class", ".exe", ".dll",
    ".so", ".dylib", ".o", ".a", ".obj", ".lib", ".pyc", ".pyo", ".whl", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".webm", ".db",
    ".sqlite", ".sqlite3", ".bin", ".dat", ".pkl", ".npy", ".npz", ".parquet", ".onnx", ".pt",
    ".pth", ".h5", ".ckpt", ".safetensors", ".psd", ".ai", ".iso", ".dmg",
})

MAX_GITIGNORE_BYTES = 256_000
MAX_EXTRA_PATTERNS = 200


@dataclass(frozen=True)
class IndexLimits:
    max_files: int
    max_depth: int
    max_file_bytes: int
    max_total_bytes: int
    time_budget_seconds: float = 300.0
    max_diagnostics: int = 500
    extra_excludes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, int]:
        return {"max_files": self.max_files, "max_depth": self.max_depth,
                "max_file_bytes": self.max_file_bytes, "max_total_bytes": self.max_total_bytes}


@dataclass(frozen=True)
class ScannedFile:
    rel_path: str
    extension: str
    size_bytes: int
    line_count: int
    sha256: str
    depth: int
    mtime_ns: int = 0
    data: bytes = field(default=b"", repr=False, compare=False)  # never logged or persisted


@dataclass(frozen=True)
class SkipDiagnostic:
    code: str
    rel_path: str | None  # None for credential-like paths: their names are never recorded


@dataclass
class ScanResult:
    directories_visited: int = 0
    total_bytes: int = 0
    total_lines: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    diagnostics: list[SkipDiagnostic] = field(default_factory=list)
    truncated: bool = False
    truncation_reason: str | None = None
    files_indexed: int = 0
    seen_paths: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FileRead:
    data: bytes
    sha256: str
    line_count: int
    binary: bool
    bad_encoding: bool


def _is_reparse_point(st: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(st, "st_file_attributes", 0) & flag)


class _IgnoreStack:
    """Per-directory .gitignore specs. Approximates git semantics (no cross-file negation)."""

    def __init__(self, extra: tuple[str, ...]) -> None:
        self._specs: list[tuple[str, pathspec.GitIgnoreSpec]] = []
        lines = [ln for ln in extra[:MAX_EXTRA_PATTERNS] if ln.strip()]
        if lines:
            self._specs.append(("", pathspec.GitIgnoreSpec.from_lines(lines)))
        self._base_count = len(self._specs)

    def push(self, rel_dir: str, directory: Path) -> bool:
        gi = directory / ".gitignore"
        try:
            st = gi.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_GITIGNORE_BYTES:
                return False
            lines = gi.read_text("utf-8", errors="replace").splitlines()
            self._specs.append((rel_dir, pathspec.GitIgnoreSpec.from_lines(lines)))
            return True
        except (OSError, ValueError):
            return False

    def pop(self) -> None:
        self._specs.pop()

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        for base, spec in self._specs:
            sub = rel_path if not base else rel_path[len(base) + 1:]
            if spec.match_file(sub + "/" if is_dir else sub):
                return True
        return False


def _read_file(path: Path, max_bytes: int) -> FileRead:
    """Read one file (bounded), hash it and classify it as text, binary or badly encoded."""
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(65_536):
            read += len(chunk)
            if read > max_bytes:
                raise OSError("file grew beyond the size limit while reading")
            if not chunks and b"\x00" in chunk[:8192]:
                return FileRead(b"", "", 0, True, False)
            digest.update(chunk)
            chunks.append(chunk)
    data = b"".join(chunks)
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoder.decode(data[3:] if data.startswith(codecs.BOM_UTF8) else data, final=True)
    except UnicodeDecodeError:
        return FileRead(b"", digest.hexdigest(), 0, False, True)
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return FileRead(data, digest.hexdigest(), lines, False, False)


def scan_repository(
    root: Path,
    limits: IndexLimits,
    on_file: Callable[[ScannedFile], None],
    on_progress: Callable[[ScanResult], None] | None = None,
    *,
    read_contents: bool = True,
    on_stat: Callable[[str, int, int], None] | None = None,
) -> ScanResult:
    """Walk ``root`` (canonicalized here) and call ``on_file`` for each indexable file.

    With ``read_contents=False`` nothing is read: ``on_stat(rel_path, size, mtime_ns)`` is called
    instead (used by the cheap freshness check).
    """
    root = root.resolve(strict=True)
    result = ScanResult()
    ignores = _IgnoreStack(limits.extra_excludes)
    seen_inodes: set[tuple[int, int]] = set()
    started = time.perf_counter()  # perf_counter: time.monotonic() ticks every ~15.6 ms on Windows, so a short walk could read 0 elapsed

    def note(code: str, rel: str | None) -> None:
        result.skipped[code] += 1
        if len(result.diagnostics) < limits.max_diagnostics:
            result.diagnostics.append(SkipDiagnostic(code, rel))

    def stop(reason: str) -> None:
        result.truncated = True
        result.truncation_reason = reason

    def walk(directory: Path, rel_dir: str, depth: int) -> Iterator[None]:
        if result.truncated:
            return
        result.directories_visited += 1
        pushed = ignores.push(rel_dir, directory)
        try:
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                note(SkipCode.UNREADABLE, rel_dir or ".")
                return
            for entry in entries:
                if result.truncated:
                    return
                if time.perf_counter() - started > limits.time_budget_seconds:
                    stop("time budget exceeded")
                    return
                name = entry.name
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    note(SkipCode.UNREADABLE, rel)
                    continue
                if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st):
                    note(SkipCode.SYMLINK_REJECTED, rel)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if name.lower() in EXCLUDED_DIRS or ignores.matches(rel, True):
                        note(SkipCode.IGNORED_PATTERN, rel)
                        continue
                    if depth + 1 > limits.max_depth:
                        note(SkipCode.MAX_DEPTH_EXCEEDED, rel)
                        continue
                    child = Path(entry.path)
                    try:
                        if not is_within(child.resolve(strict=True), root):
                            note(SkipCode.OUTSIDE_REPOSITORY, rel)
                            continue
                    except OSError:
                        note(SkipCode.UNREADABLE, rel)
                        continue
                    yield from walk(child, rel, depth + 1)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    note(SkipCode.UNSUPPORTED_FILE_TYPE, rel)
                    continue
                if is_secret_filename(name):
                    note(SkipCode.SECRET_LIKE_PATH, None)
                    continue
                if ignores.matches(rel, False):
                    note(SkipCode.IGNORED_PATTERN, rel)
                    continue
                ext = PurePosixPath(name.lower()).suffix
                if ext in BINARY_EXTENSIONS:
                    note(SkipCode.BINARY_FILE, rel)
                    continue
                if st.st_size > limits.max_file_bytes:
                    note(SkipCode.FILE_TOO_LARGE, rel)
                    continue
                if st.st_ino and (st.st_dev, st.st_ino) in seen_inodes:
                    note(SkipCode.DUPLICATE_FILE, rel)  # hard link to an already indexed file
                    continue
                if result.files_indexed >= limits.max_files:
                    stop("max_files reached")
                    return
                if result.total_bytes + st.st_size > limits.max_total_bytes:
                    stop("max_total_bytes reached")
                    return
                if not read_contents:
                    if on_stat:
                        on_stat(rel, st.st_size, st.st_mtime_ns)
                    result.files_indexed += 1
                    result.total_bytes += st.st_size
                    result.seen_paths.add(rel)
                    continue
                try:
                    real = safe_join(root, rel)  # re-validates the final location
                    read = _read_file(real, limits.max_file_bytes)
                except OSError:
                    note(SkipCode.UNREADABLE, rel)
                    continue
                except Exception:  # unsafe path or similar: skip, never abort the whole index
                    note(SkipCode.OUTSIDE_REPOSITORY, rel)
                    continue
                if read.binary:
                    note(SkipCode.BINARY_FILE, rel)
                    continue
                if read.bad_encoding:
                    note(SkipCode.UNSUPPORTED_ENCODING, rel)
                    continue
                if st.st_ino:
                    seen_inodes.add((st.st_dev, st.st_ino))
                scanned = ScannedFile(rel, ext, st.st_size, read.line_count, read.sha256, depth,
                                      st.st_mtime_ns, read.data)
                on_file(scanned)
                result.files_indexed += 1
                result.total_bytes += st.st_size
                result.total_lines += read.line_count
                result.seen_paths.add(rel)
                if on_progress and result.files_indexed % 250 == 0:
                    on_progress(result)
                yield None
        finally:
            if pushed:
                ignores.pop()

    for _ in walk(root, "", 0):
        pass
    return result
