"""Read-only change acquisition from a local Git repository.

Only these git subcommands are ever run: ``rev-parse``, ``diff`` (name/numstat/patch), ``cat-file`` and ``ls-files``.
No command mutates the index, the working tree, refs or history. Arguments are passed as an array (never a shell); user-supplied
revisions are validated, resolved to a commit id and then only the id is used; user-supplied paths are never passed to git
(they filter the list git itself reported). Output and time are bounded.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.errors import ConflictError, ValidationFailedError
from app.services import git

MODES = ("revisions", "staged", "unstaged", "working_tree")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@~^+-]{0,199}$")
HEX_RE = re.compile(r"^[0-9a-f]{40,64}$")
ZERO_RE = re.compile(r"^0+$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
STATUS_TYPE = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "added"}


@dataclass
class RawChange:
    status: str                      # git status letter
    path: str                        # new path (old path for deletions)
    old_path: str | None = None
    old_mode: str = ""
    new_mode: str = ""
    old_blob: str | None = None
    new_blob: str | None = None
    added: int | None = None
    removed: int | None = None
    binary: bool = False
    new_ranges: list[list[int]] = field(default_factory=list)
    old_ranges: list[list[int]] = field(default_factory=list)
    deletion_points: list[int] = field(default_factory=list)  # new-side line after which lines were removed (ephemeral)
    hunks_read: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def change_type(self) -> str:
        if self.new_mode == "160000" or self.old_mode == "160000":
            return "unsupported"
        if self.status in ("T", "U", "X") or "120000" in (self.old_mode, self.new_mode):
            return "unsupported"
        return STATUS_TYPE.get(self.status[:1], "unsupported")


@dataclass
class Acquisition:
    mode: str
    base_ref: str | None
    head_ref: str | None
    base_sha: str | None
    head_sha: str | None
    git_head: str | None
    changes: list[RawChange]
    total_files: int
    omitted: int = 0
    patch_bytes: int = 0
    warnings: list[str] = field(default_factory=list)


def validate_ref(ref: str | None, label: str) -> str:
    if not ref or not isinstance(ref, str) or not REF_RE.fullmatch(ref) or ".." in ref or "@{" in ref or ref.endswith(("/", ".lock")):
        raise ValidationFailedError(f"{label} is not a valid revision name. Use a branch, tag, commit id or HEAD~N form.")
    return ref


def validate_paths(paths: list[str] | None) -> list[str]:
    out: list[str] = []
    for p in paths or []:
        if (not isinstance(p, str) or not p or len(p) > 500 or p.startswith("/") or "\\" in p or ":" in p
                or any(ord(c) < 32 for c in p) or any(seg in ("..", "") for seg in p.split("/"))):
            raise ValidationFailedError("A selected path is not a valid repository-relative path.")
        out.append(p)
    if len(out) > 50:
        raise ValidationFailedError("At most 50 paths can be selected.")
    return sorted(set(out))


def _g(root: Path, settings: Settings, args: list[str], max_output: int | None = None) -> git.GitResult:
    return git.run_git(root, ["--literal-pathspecs", *args], settings.git_timeout_seconds,
                       max_output or settings.git_max_output_bytes)


def require_git(root: Path, settings: Settings) -> str | None:
    """Return the current HEAD commit id (None for a repository without commits) or raise a clear 409."""
    if shutil.which("git") is None:
        raise ConflictError("Git is not available on this machine, so changes cannot be read. No diff was fabricated.")
    inside = _g(root, settings, ["rev-parse", "--is-inside-work-tree"])
    if not inside.ok or inside.stdout.strip() != "true":
        raise ConflictError("This repository is not a Git working tree, so there are no changes to review.")
    head = _g(root, settings, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    return head.stdout.strip() if head.ok and HEX_RE.fullmatch(head.stdout.strip()) else None


def resolve_commit(root: Path, settings: Settings, ref: str, label: str) -> str:
    res = _g(root, settings, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"])
    sha = res.stdout.strip()
    if not res.ok or not HEX_RE.fullmatch(sha):
        raise ValidationFailedError(f"{label} '{ref}' does not resolve to a commit in this repository.")
    return sha


def _target(mode: str, base_sha: str | None, head_sha: str | None, git_head: str | None) -> list[str]:
    if mode == "revisions":
        return [base_sha or EMPTY_TREE, head_sha or EMPTY_TREE]
    if mode == "staged":
        return ["--cached", git_head or EMPTY_TREE]
    if mode == "working_tree":
        return [git_head or EMPTY_TREE]
    return []  # unstaged: worktree against the index


def _diff_args(extra: list[str], target: list[str], paths: list[str] | None = None) -> list[str]:
    args = ["-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M", "--relative", *extra, *target]
    if paths:
        args += ["--", *paths]
    return args


def _parse_raw(text: str) -> list[RawChange]:
    parts = text.split("\0")
    out: list[RawChange] = []
    i = 0
    while i < len(parts) and parts[i].startswith(":"):
        meta = parts[i][1:].split(" ")
        i += 1
        if len(meta) < 5:
            continue
        old_mode, new_mode, old_blob, new_blob, status = meta[0], meta[1], meta[2], meta[3], meta[4]
        letter = status[:1]
        if letter in ("R", "C") and i + 1 < len(parts):
            old_path, path = parts[i], parts[i + 1]
            i += 2
        elif i < len(parts):
            path, old_path = parts[i], None
            i += 1
        else:
            break
        ch = RawChange(status=letter, path=path, old_path=old_path if letter in ("R", "C") else None, old_mode=old_mode, new_mode=new_mode,
                       old_blob=None if ZERO_RE.fullmatch(old_blob) or not HEX_RE.fullmatch(old_blob) else old_blob,
                       new_blob=None if ZERO_RE.fullmatch(new_blob) or not HEX_RE.fullmatch(new_blob) else new_blob)
        if letter == "C":
            ch.notes.append(f"Copied from {old_path}; analysed as a new file.")
        out.append(ch)
    return out


def _parse_numstat(text: str) -> dict[str, tuple[int | None, int | None, bool]]:
    parts = text.split("\0")
    out: dict[str, tuple[int | None, int | None, bool]] = {}
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        fields = rec.split("\t", 2)
        if len(fields) < 3:
            continue
        a, r, path = fields
        if path == "":  # rename: the next two NUL-separated fields are old and new
            if i + 1 >= len(parts):
                break
            path = parts[i + 1]
            i += 2
        binary = a == "-" and r == "-"
        out[path] = (None if binary else int(a), None if binary else int(r), binary)
    return out


def _parse_hunks(patch: str, ch: RawChange) -> None:
    for line in patch.splitlines():
        m = HUNK_RE.match(line)
        if not m:
            continue
        os_, ol, ns, nl = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
        if ol:
            ch.old_ranges.append([os_, os_ + ol - 1])
        if nl:
            ch.new_ranges.append([ns, ns + nl - 1])
        else:
            ch.deletion_points.append(ns)
    ch.hunks_read = True


@dataclass
class Prepared:
    mode: str
    base_ref: str | None
    head_ref: str | None
    base_sha: str | None
    head_sha: str | None
    git_head: str | None
    warnings: list[str] = field(default_factory=list)


def prepare(root: Path, settings: Settings, mode: str, base_ref: str | None, head_ref: str | None) -> Prepared:
    """Validate the request and pin revisions to commit ids. Cheap; safe to call synchronously from a request."""
    if mode not in MODES:
        raise ValidationFailedError(f"mode must be one of {', '.join(MODES)}.")
    git_head = require_git(root, settings)
    base_sha = head_sha = None
    if mode == "revisions":
        base_sha = resolve_commit(root, settings, validate_ref(base_ref, "base"), "base")
        head_sha = resolve_commit(root, settings, validate_ref(head_ref or "HEAD", "head"), "head")
        head_ref = head_ref or "HEAD"
    elif base_ref or head_ref:
        raise ValidationFailedError(f"base and head are only used by the 'revisions' mode, not '{mode}'.")
    warnings: list[str] = []
    if mode != "revisions" and git_head is None:
        warnings.append("The repository has no commits yet; changes are compared with an empty tree.")
    return Prepared(mode, base_ref, head_ref, base_sha, head_sha, git_head, warnings)


def acquire(root: Path, settings: Settings, prep: Prepared, selected: list[str], deadline: float, with_hunks: bool = True) -> Acquisition:
    mode, base_sha, head_sha, git_head = prep.mode, prep.base_sha, prep.head_sha, prep.git_head
    target = _target(mode, base_sha, head_sha, git_head)
    warnings: list[str] = list(prep.warnings)

    raw = _g(root, settings, _diff_args(["--raw", "-z", "--no-abbrev"], target))
    if not raw.ok:
        raise ConflictError("Git could not produce the change list for this request.")
    if raw.truncated:
        warnings.append("The change list was larger than the output limit and is incomplete.")
    changes = _parse_raw(raw.stdout)
    ns = _g(root, settings, _diff_args(["--numstat", "-z"], target))
    stats = _parse_numstat(ns.stdout) if ns.ok else {}
    if not ns.ok:
        warnings.append("Line counts could not be read from git; they are shown as unknown.")
    wanted = set(selected)
    if wanted:
        known = {c.path for c in changes}
        missing = sorted(wanted - known)
        if missing:
            raise ValidationFailedError("Selected path(s) are not part of this change: " + ", ".join(missing[:5]))
        changes = [c for c in changes if c.path in wanted]
    changes.sort(key=lambda c: c.path)
    total = len(changes)
    omitted = max(0, total - settings.review_max_files)
    if omitted:
        warnings.append(f"{total} files changed; only the first {settings.review_max_files} (by path) were analysed, {omitted} were not.")
        changes = changes[: settings.review_max_files]

    patch_bytes = 0
    for ch in changes:
        st = stats.get(ch.path)
        if st:
            ch.added, ch.removed, ch.binary = st
        if ch.change_type == "unsupported":
            ch.notes.append("Submodule, symlink, type change or unmerged entry; not analysed.")
            continue
        if ch.binary:
            ch.notes.append("Binary file; no line information.")
            continue
        if not with_hunks or time.monotonic() > deadline or patch_bytes > settings.review_max_diff_bytes:
            continue  # hunks stay unread; the file is reported as "not_read"
        paths = [p for p in (ch.old_path, ch.path) if p]
        res = _g(root, settings, _diff_args(["-U0"], target, paths), settings.review_max_diff_bytes - patch_bytes + 1)
        if not res.ok or res.truncated:
            continue
        patch_bytes += len(res.stdout)
        _parse_hunks(res.stdout, ch)
    not_read = sum(1 for c in changes if not c.hunks_read and not c.binary and c.change_type != "unsupported")
    if not_read and with_hunks:
        warnings.append(f"{not_read} file(s) were not diffed because the diff-size or time limit was reached; their line ranges are unknown.")
    if mode in ("unstaged", "working_tree"):
        un = _g(root, settings, ["ls-files", "--others", "--exclude-standard", "-z"])
        count = len([p for p in un.stdout.split("\0") if p]) if un.ok else 0
        if count:
            warnings.append(f"{count} untracked file(s) exist and are not part of the diff; add them to the index (git add -N) to include them.")
    return Acquisition(mode, prep.base_ref, prep.head_ref, base_sha, head_sha, git_head, changes, total, omitted, patch_bytes, warnings)


def read_blob(root: Path, settings: Settings, blob: str | None) -> tuple[bytes | None, str]:
    """Exact bytes of a git blob. Status: ok | too_large | missing | error."""
    if not blob or not HEX_RE.fullmatch(blob):
        return None, "missing"
    limit = settings.review_max_file_bytes
    env = git.git_env(root)
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True,  # noqa: S603, S607
                              timeout=settings.git_timeout_seconds, env=env, shell=False, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None, "error"
    if proc.returncode != 0:
        return None, "error"
    if len(proc.stdout) > limit:
        return None, "too_large"
    return proc.stdout, "ok"
