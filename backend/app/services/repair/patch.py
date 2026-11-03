"""A small, strict unified-diff engine: parse, validate and apply text patches without touching the working tree.

Deliberately narrower than ``patch(1)``:
* only modifications of existing text files and creation of new text files (no deletes, renames, mode changes or binary patches);
* hunks must match **exactly** at their stated line (adjusted for earlier hunks): no fuzz and no offset search, so a conflict is never
  resolved silently - it is reported;
* paths are repository-relative, validated and never resolved through symlinks.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import Settings
from app.security.paths import UnsafePathError, safe_join

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MAX_PREVIEW_LINES = 2000
MAX_PREVIEW_LINE_CHARS = 400


class PatchError(Exception):
    """A patch cannot be accepted. ``code`` is stable and used by the API."""

    def __init__(self, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (' ' | '+' | '-', text without line ending)
    old_noeol: bool = False  # "\ No newline at end of file" after the last old line
    new_noeol: bool = False


@dataclass
class FilePatch:
    old_path: str | None  # None when the file is created
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path

    @property
    def creates(self) -> bool:
        return self.old_path is None

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "+")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "-")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


# ------------------------------------------------------------------------------------------------ parsing

def _header_path(raw: str) -> str | None:
    raw = raw.split("\t", 1)[0].rstrip()
    if raw.startswith('"'):
        raise PatchError("invalid_patch", "Quoted file names in a diff header are not supported.")
    return None if raw == "/dev/null" else raw


def _strip_prefix(old: str | None, new: str | None) -> tuple[str | None, str | None]:
    """Remove git's a/ and b/ prefixes when both sides carry them."""
    o_pref = old is not None and old.startswith("a/")
    n_pref = new is not None and new.startswith("b/")
    if (old is None or o_pref) and (new is None or n_pref) and (o_pref or n_pref):
        return (old[2:] if old and o_pref else old), (new[2:] if new and n_pref else new)
    return old, new


def parse(text: str, max_bytes: int, max_files: int) -> list[FilePatch]:
    """Parse a unified diff. Raises ``PatchError`` for anything outside the supported subset."""
    if not isinstance(text, str) or not text.strip():
        raise PatchError("invalid_patch", "The patch is empty.")
    if "\x00" in text:
        raise PatchError("binary_patch", "The patch contains NUL bytes; binary patches are not supported.")
    if len(text.encode("utf-8")) > max_bytes:
        raise PatchError("too_large", f"The patch is larger than the limit of {max_bytes} bytes.")
    parts = text.split("\n")
    if len(parts) > 1 and all(p.endswith("\r") for p in parts[:-1] if p) and any(p.endswith("\r") for p in parts):
        parts = [p[:-1] if p.endswith("\r") else p for p in parts]
    if parts and parts[-1] == "":
        parts.pop()
    files: list[FilePatch] = []
    i = 0
    n = len(parts)
    while i < n:
        line = parts[i]
        if line.startswith(("GIT binary patch", "Binary files ")):
            raise PatchError("binary_patch", "Binary patches are not supported.")
        if line.startswith(("rename from", "rename to", "copy from", "copy to", "similarity index")):
            raise PatchError("unsupported_change", "Renames and copies are not supported; only content changes are.")
        if line.startswith(("deleted file mode",)):
            raise PatchError("unsupported_change", "Deleting files is not supported by repair patches.")
        if line.startswith(("old mode", "new mode")):
            raise PatchError("unsupported_change", "File mode changes are not supported.")
        if line.startswith("--- "):
            if i + 1 >= n or not parts[i + 1].startswith("+++ "):
                raise PatchError("invalid_patch", "A '---' header must be followed by a '+++' header.")
            old_raw, new_raw = _header_path(line[4:]), _header_path(parts[i + 1][4:])
            old_raw, new_raw = _strip_prefix(old_raw, new_raw)
            if new_raw is None:
                raise PatchError("unsupported_change", "Deleting files is not supported by repair patches.")
            if old_raw is not None and old_raw != new_raw:
                raise PatchError("unsupported_change", "Renames are not supported; the old and new path must be identical.")
            fp = FilePatch(old_raw, new_raw)
            i += 2
            while i < n and parts[i].startswith("@@"):
                m = HUNK_RE.match(parts[i])
                if not m:
                    raise PatchError("invalid_patch", f"Malformed hunk header: {parts[i][:60]!r}.")
                h = Hunk(int(m[1]), int(m[2]) if m[2] is not None else 1, int(m[3]), int(m[4]) if m[4] is not None else 1)
                i += 1
                old_seen = new_seen = 0
                last_kind = ""
                while i < n and (old_seen < h.old_len or new_seen < h.new_len):
                    body = parts[i]
                    if body.startswith("\\"):
                        raise PatchError("invalid_patch", "Unexpected '\\ No newline' marker.")
                    kind, txt = (body[0], body[1:]) if body else (" ", "")
                    if kind not in " +-":
                        raise PatchError("invalid_patch", f"Unexpected line in hunk: {body[:60]!r}.")
                    if kind in " -":
                        old_seen += 1
                    if kind in " +":
                        new_seen += 1
                    h.lines.append((kind, txt))
                    last_kind = kind
                    i += 1
                if old_seen != h.old_len or new_seen != h.new_len:
                    raise PatchError("invalid_patch", "A hunk does not contain the number of lines its header declares.")
                if i < n and parts[i].startswith("\\"):
                    if last_kind == "-":
                        h.old_noeol = True
                    elif last_kind == "+":
                        h.new_noeol = True
                    else:
                        h.old_noeol = h.new_noeol = True
                    i += 1
                fp.hunks.append(h)
            if not fp.hunks:
                raise PatchError("invalid_patch", f"The patch for {fp.path!r} has no hunks.")
            if fp.creates and (len(fp.hunks) != 1 or fp.hunks[0].old_len != 0):
                raise PatchError("invalid_patch", "A new file must be a single hunk that only adds lines.")
            files.append(fp)
            if len(files) > max_files:
                raise PatchError("too_many_files", f"A patch may modify at most {max_files} files.")
            continue
        if line.startswith(("diff ", "index ", "new file mode", "# ")) or line.strip() == "":
            i += 1
            continue
        raise PatchError("invalid_patch", f"Unexpected line outside any file section: {line[:60]!r}.")
    if not files:
        raise PatchError("invalid_patch", "No file sections were found in the patch.")
    paths = [f.path for f in files]
    if len(set(paths)) != len(paths):
        raise PatchError("invalid_patch", "A file appears more than once in the patch.")
    return files


# ------------------------------------------------------------------------------------------------ paths

def suffixes(settings: Settings) -> set[str]:
    return {s.strip().lower() for s in settings.repair_allowed_suffixes.split(",") if s.strip()}


def check_path(root: Path, rel: str, settings: Settings) -> Path:
    """Return the absolute target path or raise ``PatchError``. Never follows symlinks."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise PatchError("unsafe_path", "A patch path must be a relative, forward-slash path inside the repository.")
    p = PurePosixPath(rel)
    if str(p) != rel or ".." in p.parts or "." in p.parts:
        raise PatchError("unsafe_path", "A patch path must be normalized and must not contain '.' or '..'.")
    if any(part in (".git", ".gitmodules", ".gitattributes") for part in p.parts):
        raise PatchError("unsafe_path", "Patches may not modify Git metadata.")
    if p.suffix.lower() not in suffixes(settings):
        raise PatchError("disallowed_file_type", f"Files of type {p.suffix or '(none)'!r} cannot be patched.")
    try:
        target = safe_join(root, rel)
    except UnsafePathError as exc:
        raise PatchError("unsafe_path", "The path escapes the repository root.") from exc
    # reject any symlink component (safe_join only guarantees the *resolved* path is inside the root)
    cur = root.resolve(strict=True)
    for part in p.parts:
        cur = cur / part
        if os.path.islink(cur):
            raise PatchError("unsafe_path", f"{rel!r} passes through a symbolic link.")
    return target


def read_text_file(path: Path, rel: str, settings: Settings) -> tuple[str, bytes]:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise PatchError("missing_file", f"{rel!r} does not exist, so it cannot be patched.") from exc
    if not os.path.isfile(path) or os.path.islink(path):
        raise PatchError("unsafe_path", f"{rel!r} is not a regular file.")
    if st.st_size > settings.repair_max_file_bytes:
        raise PatchError("too_large", f"{rel!r} is larger than the limit of {settings.repair_max_file_bytes} bytes.")
    data = path.read_bytes()
    if b"\x00" in data:
        raise PatchError("binary_file", f"{rel!r} looks binary.")
    try:
        return data.decode("utf-8"), data
    except UnicodeDecodeError as exc:
        raise PatchError("binary_file", f"{rel!r} is not valid UTF-8 text.") from exc


# ------------------------------------------------------------------------------------------------ applying

def _split(text: str, rel: str) -> tuple[list[str], str, bool]:
    if text == "":
        return [], "\n", True
    crlf = "\r\n" in text
    eol = "\r\n" if crlf else "\n"
    final = text.endswith(eol)
    body = text[: -len(eol)] if final else text
    lines = body.split(eol)
    if any(("\r" in ln) or (crlf and "\n" in ln) for ln in lines):
        raise PatchError("unsupported_change", f"{rel!r} has mixed line endings; it cannot be patched safely.")
    return lines, eol, final


def apply_file(fp: FilePatch, original: str | None) -> str:
    """Apply one file's hunks to ``original`` (None for a new file). Exact matching only."""
    if fp.creates:
        h = fp.hunks[0]
        lines = [t for k, t in h.lines if k == "+"]
        return ("\n".join(lines) + ("" if h.new_noeol else "\n")) if lines else ""
    assert original is not None
    lines, eol, final = _split(original, fp.path)
    out: list[str] = []
    pos = 0  # next unread index in `lines`
    for number, h in enumerate(fp.hunks, 1):
        start = h.old_start - 1 if h.old_len > 0 else h.old_start  # an insertion-only hunk names the line *before* the insertion
        if start < pos:
            raise PatchError("invalid_patch", f"Hunk {number} overlaps or is out of order.")
        old = [t for k, t in h.lines if k in " -"]
        if start + len(old) > len(lines):
            raise PatchError("conflict", f"Hunk {number} extends beyond the end of {fp.path!r}.", [{"hunk": number, "line": start + 1}])
        for off, want in enumerate(old):
            if lines[start + off] != want:
                raise PatchError(
                    "conflict", f"Hunk {number} does not match {fp.path!r} at line {start + off + 1}.",
                    [{"hunk": number, "line": start + off + 1, "expected": want[:120], "found": lines[start + off][:120]}])
        reaches_end = start + len(old) == len(lines)
        if reaches_end and old:
            if h.old_noeol and final:
                raise PatchError("conflict", f"Hunk {number} says {fp.path!r} has no final newline, but it does.", [{"hunk": number}])
            if not h.old_noeol and not final:
                raise PatchError("conflict", f"Hunk {number} assumes a final newline that {fp.path!r} does not have.", [{"hunk": number}])
        out.extend(lines[pos:start])
        out.extend(t for k, t in h.lines if k in " +")
        pos = start + len(old)
        if reaches_end:
            final = not h.new_noeol
    out.extend(lines[pos:])
    if not out:
        return ""
    return eol.join(out) + (eol if final else "")


@dataclass
class FileResult:
    path: str
    change_type: str  # modify | create
    base_sha256: str | None
    result_sha256: str
    added: int
    removed: int
    original: str | None
    result: str
    warnings: list[str] = field(default_factory=list)


def build(root: Path, settings: Settings, diff_text: str, allowed: set[str] | None = None,
          bases: dict[str, str | None] | None = None, max_files: int | None = None) -> list[FileResult]:
    """Check a diff against the files currently on disk and return what each file would contain. Writes nothing.

    ``allowed``: when given, a path outside it is an unexpected modification. ``bases``: path -> expected sha256 of the current file
    (None = must not exist); a mismatch means the patch is stale.
    """
    fps = parse(diff_text, settings.repair_max_patch_bytes, max_files or settings.repair_max_files)
    results: list[FileResult] = []
    for fp in fps:
        target = check_path(root, fp.path, settings)
        if allowed is not None and fp.path not in allowed:
            raise PatchError("unexpected_file", f"The patch modifies {fp.path!r}, which is not part of this investigation. "
                             "Add it explicitly to the allowed files if that is intended.", [{"path": fp.path}])
        if fp.creates:
            if os.path.lexists(target):
                raise PatchError("stale" if bases is not None else "conflict", f"{fp.path!r} already exists; the patch expects to create it.")
            original, base = None, None
        else:
            original, data = read_text_file(target, fp.path, settings)
            base = sha256_bytes(data)
        if bases is not None and fp.path in bases and bases[fp.path] != base:
            raise PatchError("stale", f"{fp.path!r} has changed since this patch was created.", [{"path": fp.path}])
        result = apply_file(fp, original)
        warnings: list[str] = []
        if len(result.encode("utf-8")) > settings.repair_max_file_bytes:
            raise PatchError("too_large", f"The patched {fp.path!r} would exceed {settings.repair_max_file_bytes} bytes.")
        if fp.path.endswith(".py"):
            try:
                ast.parse(result)
            except SyntaxError as exc:
                was_ok = True
                if original is not None:
                    try:
                        ast.parse(original)
                    except SyntaxError:
                        was_ok = False
                if was_ok:
                    raise PatchError("result_invalid", f"The patched {fp.path!r} would not parse as Python (line {exc.lineno}).",
                                     [{"path": fp.path, "line": exc.lineno or 0}]) from exc
                warnings.append(f"{fp.path} did not parse before the patch and still does not.")
        results.append(FileResult(fp.path, "create" if fp.creates else "modify", base, sha256_text(result), fp.added, fp.removed,
                                  original, result, warnings))
    return results


def validate_isolated(root: Path, settings: Settings, tmp_base: Path, diff_text: str, allowed: set[str] | None,
                      bases: dict[str, str | None] | None, max_files: int | None = None) -> list[FileResult]:
    """``build`` plus a materialised check in a private temporary directory that is always removed.

    The repository is only read. The temporary copy contains the patched files and is compiled from there.
    """
    tmp_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_base, prefix="validate-") as tmp:
        results = build(root, settings, diff_text, allowed, bases, max_files)
        for r in results:
            dest = Path(tmp) / r.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(r.result, encoding="utf-8", newline="")
            if r.path.endswith(".py"):
                compile(dest.read_text(encoding="utf-8"), r.path, "exec", dont_inherit=True)
        return results


# ------------------------------------------------------------------------------------------------ writing

def _atomic_write(target: Path, text: str, mode: int | None) -> None:
    tmp = target.with_name(f".{target.name}.nova-{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_results(root: Path, settings: Settings, results: list[FileResult]) -> list[str]:
    """Write the results with a final check of each file's current content. Restores everything if any file fails.

    Residual risk: another process can still change a file between the final check and the atomic replace of that one file.
    """
    written: list[FileResult] = []
    try:
        for r in results:
            target = check_path(root, r.path, settings)
            if r.change_type == "create":
                if os.path.lexists(target):
                    raise PatchError("stale", f"{r.path!r} appeared after validation.")
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = None
            else:
                current, data = read_text_file(target, r.path, settings)
                if sha256_bytes(data) != r.base_sha256:
                    raise PatchError("stale", f"{r.path!r} changed after validation; nothing further was written.")
                mode = os.stat(target).st_mode & 0o7777
            _atomic_write(target, r.result, mode)
            written.append(r)
    except Exception:
        restore(root, settings, written)
        raise
    return [r.path for r in written]


def restore(root: Path, settings: Settings, results: list[FileResult]) -> list[str]:
    """Put the original content back (best effort, newest first). Returns the paths restored."""
    done: list[str] = []
    for r in reversed(results):
        try:
            target = check_path(root, r.path, settings)
            if r.change_type == "create":
                target.unlink(missing_ok=True)
            elif r.original is not None:
                _atomic_write(target, r.original, os.stat(target).st_mode & 0o7777 if target.exists() else None)
            done.append(r.path)
        except Exception:  # noqa: BLE001, S112 - restoration must continue for the remaining files
            continue
    return done


# ------------------------------------------------------------------------------------------------ construction & preview

def make_diff(path: str, old: str | None, new: str) -> str:
    """Build a unified diff for one file (LF files with a final newline only)."""
    old_lines = [] if old is None else old.split("\n")[:-1] if old.endswith("\n") else old.split("\n")
    new_lines = new.split("\n")[:-1] if new.endswith("\n") else new.split("\n")
    head_old = "/dev/null" if old is None else f"a/{path}"
    body = list(difflib.unified_diff(old_lines, new_lines, fromfile=head_old, tofile=f"b/{path}", lineterm="", n=3))
    if body and old is None:
        # difflib writes "@@ -0,0 +1,N @@" for empty input, which is what the parser expects
        pass
    return "\n".join(body) + "\n" if body else ""


def reverse_diff(results: list[FileResult]) -> str:
    """A unified diff that undoes the patch (for recovery instructions)."""
    chunks = []
    for r in results:
        if r.change_type == "create":
            continue
        old_lines = r.result.split("\n")[:-1] if r.result.endswith("\n") else r.result.split("\n")
        new_lines = (r.original or "").split("\n")[:-1] if (r.original or "").endswith("\n") else (r.original or "").split("\n")
        chunks.append("\n".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{r.path}", tofile=f"b/{r.path}", lineterm="")))
    return "\n".join(c for c in chunks if c) + "\n" if chunks else ""


def preview(diff_text: str, settings: Settings, max_files: int | None = None) -> dict[str, Any]:
    """A bounded, structured rendering of the diff for the UI. Lines are text only (the UI must escape them)."""
    fps = parse(diff_text, settings.repair_max_patch_bytes, max_files or settings.repair_max_files)
    budget = MAX_PREVIEW_LINES
    files = []
    truncated = False
    for fp in fps:
        hunks = []
        for h in fp.hunks:
            lines = []
            for kind, txt in h.lines:
                if budget <= 0:
                    truncated = True
                    break
                budget -= 1
                lines.append({"kind": {" ": "context", "+": "add", "-": "remove"}[kind],
                              "text": txt[:MAX_PREVIEW_LINE_CHARS] + ("…" if len(txt) > MAX_PREVIEW_LINE_CHARS else "")})
            hunks.append({"header": f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@", "lines": lines})
        files.append({"path": fp.path, "change_type": "create" if fp.creates else "modify", "added": fp.added, "removed": fp.removed,
                      "hunks": hunks})
    return {"files": files, "truncated": truncated}
