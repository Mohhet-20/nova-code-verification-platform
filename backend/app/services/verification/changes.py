"""Change detection and source fingerprints. Read-only: Git is only ever invoked with fixed argument arrays and never modified.

Sources: ``working_tree`` (tracked changes against HEAD plus untracked files), ``staged`` (the index against HEAD), ``revisions``
(two commits), ``patch`` (a unified diff checked against the files on disk, never applied) and ``manifest`` (an explicit path list).
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import Settings
from app.core.errors import UnsafePathError
from app.security.paths import safe_join
from app.services.repair import patch as patchlib
from app.services.review import gitops
from app.services.verification.errors import CvConflict, CvInvalid

SOURCES = ("working_tree", "staged", "revisions", "patch", "manifest")
NOISE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", "node_modules", ".venv", "venv", ".tox", ".coverage"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}
MAX_MANIFEST = 500


@dataclass
class ChangedFile:
    path: str
    status: str  # added | modified | deleted | renamed
    old_path: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    notes: list[str] = field(default_factory=list)
    unreadable: bool = False

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()

    @property
    def is_python(self) -> bool:
        return self.suffix == ".py"

    @property
    def is_doc(self) -> bool:
        return self.suffix in DOC_SUFFIXES

    @property
    def new_sha(self) -> str | None:
        return hashlib.sha256(self.new_text.encode("utf-8")).hexdigest() if self.new_text is not None else None


@dataclass
class ChangeSet:
    source: str
    git_head: str | None
    base_sha: str | None
    head_sha: str | None
    files: list[ChangedFile]
    warnings: list[str] = field(default_factory=list)
    patch_text: str | None = None

    def paths(self) -> list[str]:
        return sorted(f.path for f in self.files)

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.source}|{self.base_sha}|{self.head_sha}|{self.git_head}".encode())
        for f in sorted(self.files, key=lambda x: x.path):
            h.update(f"\0{f.path}\0{f.status}\0{f.new_sha}".encode())
        return h.hexdigest()

    def public(self) -> dict[str, Any]:
        return {"source": self.source, "base_sha": self.base_sha, "head_sha": self.head_sha, "git_head": self.git_head,
                "fingerprint": self.fingerprint(), "warnings": self.warnings[:20],
                "files": [{"path": f.path, "status": f.status, "old_path": f.old_path, "sha256": f.new_sha, "notes": f.notes[:3]} for f in self.files[:2000]],
                "counts": {"total": len(self.files), "python": sum(f.is_python for f in self.files),
                           "docs": sum(f.is_doc for f in self.files)}}


def _clean_rel(rel: str) -> str:
    if (not isinstance(rel, str) or not rel or len(rel) > 500 or rel.startswith(("/", "-")) or "\\" in rel or ":" in rel
            or any(ord(c) < 32 for c in rel) or any(seg in ("..", ".", "") for seg in rel.split("/"))):
        raise CvInvalid("invalid_path", "A path is not a valid repository-relative path.")
    if ".git" in rel.split("/"):
        raise CvInvalid("invalid_path", "Git metadata cannot be part of a change set.")
    return rel


def _read_disk(root: Path, rel: str, settings: Settings) -> tuple[str | None, str]:
    """Text of a regular, non-symlink file below the root. Status: ok | missing | unsafe | too_large | binary."""
    try:
        p = safe_join(root, rel)
    except UnsafePathError:
        return None, "unsafe"
    cur = root.resolve(strict=True)
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        if os.path.islink(cur):
            return None, "unsafe"
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None, "missing"
    if not os.path.isfile(p):
        return None, "unsafe"
    if st.st_size > settings.cv_max_file_bytes:
        return None, "too_large"
    data = p.read_bytes()
    if b"\0" in data[:8000]:
        return None, "binary"
    return data.decode("utf-8", "replace"), "ok"


def _blob_text(root: Path, settings: Settings, blob: str | None) -> tuple[str | None, str]:
    data, st = gitops.read_blob(root, settings, blob)
    if data is None:
        return None, st
    if b"\0" in data[:8000]:
        return None, "binary"
    return data.decode("utf-8", "replace"), "ok"


def resolve(root: Path, settings: Settings, source: str, *, base: str | None = None, head: str | None = None,
            paths: list[str] | None = None, patch: str | None = None) -> ChangeSet:
    if source not in SOURCES:
        raise CvInvalid("invalid_source", f"source must be one of {', '.join(SOURCES)}.")
    if source in ("working_tree", "staged", "revisions"):
        return _from_git(root, settings, source, base, head)
    if source == "patch":
        return _from_patch(root, settings, patch)
    return _from_manifest(root, settings, paths)


def _from_git(root: Path, settings: Settings, source: str, base: str | None, head: str | None) -> ChangeSet:
    try:
        prep = gitops.prepare(root, settings, source, base, head)
    except CvInvalid:
        raise
    except Exception as exc:  # NovaError subclasses from gitops carry their own message
        raise CvInvalid("git_unavailable", getattr(exc, "message", "Git could not inspect this repository.")) from None
    acq = gitops.acquire(root, settings, prep, [], time.monotonic() + 15, with_hunks=False)
    files: list[ChangedFile] = []
    warnings = [w for w in acq.warnings if "untracked" not in w]
    if acq.omitted:
        raise CvInvalid("too_many_files", f"{acq.total_files} files changed; at most {settings.review_max_files} can be analysed in one run.")
    for ch in acq.changes:
        kind = ch.change_type
        if kind == "unsupported":
            warnings.append(f"{ch.path}: submodule, symlink or type change; not verified.")
            continue
        cf = ChangedFile(ch.path, kind, ch.old_path)
        if kind != "added" and ch.old_blob and not set(ch.old_blob) == {"0"}:
            cf.old_text, st = _blob_text(root, settings, ch.old_blob)
            if st not in ("ok",):
                cf.notes.append(f"old content unavailable ({st})")
        if kind != "deleted":
            if source in ("working_tree",):
                cf.new_text, st = _read_disk(root, ch.path, settings)
            else:
                cf.new_text, st = _blob_text(root, settings, ch.new_blob)
            if st != "ok":
                cf.unreadable = True
                cf.notes.append(f"new content unavailable ({st})")
        files.append(cf)
    if source == "working_tree":
        un = gitops._g(root, settings, ["ls-files", "--others", "--exclude-standard", "-z"])
        for rel in sorted(p for p in (un.stdout.split("\0") if un.ok else []) if p):
            if set(rel.split("/")) & NOISE_PARTS:
                continue
            try:
                _clean_rel(rel)
            except CvInvalid:
                continue
            text, st = _read_disk(root, rel, settings)
            cf = ChangedFile(rel, "added", None, None, text, ["untracked file"], unreadable=st != "ok")
            files.append(cf)
    if len(files) > settings.cv_max_changed_files:
        raise CvInvalid("too_many_files", f"More than {settings.cv_max_changed_files} files changed.")
    files.sort(key=lambda f: f.path)
    return ChangeSet(source, acq.git_head, acq.base_sha, acq.head_sha, files, warnings)


def _from_patch(root: Path, settings: Settings, patch: str | None) -> ChangeSet:
    if not patch or not patch.strip():
        raise CvInvalid("empty_patch", "Provide the unified diff to inspect.")
    if len(patch.encode("utf-8")) > settings.repair_max_patch_bytes:
        raise CvInvalid("patch_too_large", "The patch is larger than the configured limit.")
    try:
        results = patchlib.build(root, settings, patch, None, None, settings.cv_max_changed_files)
    except patchlib.PatchError as exc:
        raise CvInvalid(exc.code, exc.message) from None
    files = [ChangedFile(r.path, "added" if r.change_type == "create" else "modified", None, r.original, r.result) for r in results]
    files.sort(key=lambda f: f.path)
    git_head = None
    if (root / ".git").exists():
        try:
            git_head = gitops.require_git(root, settings)
        except Exception:  # noqa: BLE001 - a patch can be inspected without Git
            git_head = None
    return ChangeSet("patch", git_head, None, None, files, ["The patch is checked against the files on disk and is never applied by verification."], patch)


def _from_manifest(root: Path, settings: Settings, paths: list[str] | None) -> ChangeSet:
    if not paths:
        raise CvInvalid("empty_manifest", "Provide at least one path.")
    if len(paths) > MAX_MANIFEST:
        raise CvInvalid("too_many_files", f"A manifest may list at most {MAX_MANIFEST} paths.")
    files: list[ChangedFile] = []
    for rel in sorted({_clean_rel(p) for p in paths}):
        text, st = _read_disk(root, rel, settings)
        if st == "unsafe":
            raise CvInvalid("unsafe_path", "A manifest path is a symlink or leaves the repository.")
        if st == "missing":
            files.append(ChangedFile(rel, "deleted", None, None, None, ["file does not exist (treated as deleted)"]))
        else:
            files.append(ChangedFile(rel, "modified", None, None, text, ["manifest entry: no previous content available"], unreadable=st != "ok"))
    git_head = None
    if (root / ".git").exists():
        try:
            git_head = gitops.require_git(root, settings)
        except Exception:  # noqa: BLE001
            git_head = None
    return ChangeSet("manifest", git_head, None, None, files, ["A manifest lists files only; their previous content is not known."])


# ----------------------------------------------------------------------------------------- source state

def tree_fingerprint(root: Path, settings: Settings) -> tuple[str, list[str], str | None]:
    """Fingerprint of the working tree the tests run against: HEAD plus the content of every dirty or untracked file.

    Returns (fingerprint, warnings, head). Test side effects (bytecode, caches) are excluded so a verification run does not change it.
    """
    warnings: list[str] = []
    h = hashlib.sha256()
    head = None
    if (root / ".git").exists():
        head = gitops.require_git(root, settings)
        h.update(f"git|{head}".encode())
        st = gitops._g(root, settings, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], 4_000_000)
        if not st.ok:
            raise CvConflict("git_status_failed", "Git could not report the working tree state.")
        toks = st.stdout.split("\0")
        i, rels = 0, []
        while i < len(toks):
            t = toks[i]
            i += 1
            if len(t) < 4:
                continue
            code, rel = t[:2], t[3:]
            if code[0] in "RC":
                i += 1  # the original path follows
            if set(rel.split("/")) & NOISE_PARTS:
                continue
            rels.append(rel)
        for rel in sorted(set(rels)):
            h.update(f"\0{rel}\0".encode())
            _hash_file(root, rel, settings, h, warnings)
    else:
        h.update(b"nogit")
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in NOISE_PARTS)
            for fn in sorted(filenames):
                rel = (Path(dirpath) / fn).relative_to(root).as_posix()
                count += 1
                if count > 5000:
                    warnings.append("The repository has more than 5000 files; the fingerprint covers only the first 5000.")
                    return h.hexdigest(), warnings, head
                h.update(f"\0{rel}\0".encode())
                _hash_file(root, rel, settings, h, warnings)
    return h.hexdigest(), warnings[:5], head


def _hash_file(root: Path, rel: str, settings: Settings, h: Any, warnings: list[str]) -> None:
    try:
        p = root / rel
        if p.is_symlink() or not p.is_file():
            h.update(b"special")
            return
        size = p.stat().st_size
        if size > settings.cv_max_file_bytes * 4:
            h.update(f"large:{size}".encode())
            warnings.append(f"{rel} is large; only its size is part of the fingerprint.")
            return
        h.update(hashlib.sha256(p.read_bytes()).digest())
    except OSError:
        h.update(b"unreadable")


def binding_problems(root: Path, settings: Settings, cs: ChangeSet) -> list[str]:
    """Reasons the tests (which always run on the working tree) cannot describe this change set. Empty = bound."""
    problems: list[str] = []
    if cs.source == "revisions":
        if cs.git_head != cs.head_sha:
            problems.append("The requested head revision is not the checked-out HEAD. NOVA never checks out revisions, so tests would run on a different state.")
        else:
            st = gitops._g(root, settings, ["status", "--porcelain=v1", "--untracked-files=no"])
            if st.ok and st.stdout.strip():
                problems.append("The working tree has uncommitted changes, so tests would not describe the requested revision.")
        return problems
    if cs.source in ("staged", "patch"):
        for f in cs.files:
            if f.status == "deleted":
                continue
            disk, st = _read_disk(root, f.path, settings)
            if st != "ok" or disk != f.new_text:
                label = "staged content" if cs.source == "staged" else "the patch result"
                problems.append(f"{f.path}: {label} differs from the working tree (the patch is not applied or has unstaged edits); tests would not run it.")
    return problems[:10]
