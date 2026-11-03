"""Parsing and sanitising of failure evidence. Pure functions: no database, no filesystem, nothing is executed.

Supported inputs: pytest JUnit XML (preferred, structured), the pytest long/short traceback text found inside it,
and standard Python ``Traceback (most recent call last)`` text.
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from app.services.analysis.redaction import scrub

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ABS_POSIX = re.compile(r"(?<![\w:/.~>-])/(?:[\w.@+-]+/)+[\w.@+-]+")
ABS_WIN = re.compile(r"\b[A-Za-z]:[\\/](?:[^\\/\s'\"<>|]+[\\/])*[^\\/\s'\"<>|]+")
FRAME_LINE = re.compile(r'^(?P<path>(?:<[^>\n]+>)|(?:[A-Za-z]:)?[^\s:<>"|?*][^:\n]*?\.[A-Za-z0-9]+):(?P<line>\d+): ?(?P<rest>.*)$')
PY_FILE_LINE = re.compile(r'^\s*File "(?P<path>[^"\n]+)", line (?P<line>\d+)(?:, in (?P<fn>[^\n]+))?\s*$')
EXC_HEAD = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
IN_FUNC = re.compile(r"^in (?P<fn>[\w.<>\[\]$-]+)$")
SITE = re.compile(r"(?:^|/)(?:site-packages|dist-packages)/")
STDLIB = re.compile(r"(?:^|/)lib(?:64)?/python3(?:\.\d+)?/(?!.*(?:site|dist)-packages)|(?:^|/)python3(?:\.\d+)?/")
EXC_NAME = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Timeout)$")


def clean_text(text: str, limit: int, repo_root: str | None = None) -> str:
    """Strip terminal escapes and control characters, redact credentials, and hide absolute paths."""
    text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL.sub("", text)
    if repo_root:
        text = text.replace(repo_root.rstrip("/\\") + "/", "<repo>/").replace(repo_root.rstrip("/\\") + "\\", "<repo>/")
    text = ABS_WIN.sub(lambda m: "<path>/" + re.split(r"[\\/]", m.group(0))[-1], text)
    text = ABS_POSIX.sub(lambda m: "<path>/" + m.group(0).rsplit("/", 1)[-1], text)
    return scrub(text, limit)


@dataclass
class RawFrame:
    path: str
    line: int | None
    function: str | None = None
    col: int | None = None


@dataclass
class ParsedFailure:
    category: str  # assertion | exception | collection_error | test_error | unknown
    exception_type: str | None
    message: str
    frames: list[RawFrame] = field(default_factory=list)
    test_name: str | None = None
    classname: str | None = None
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


def _split_type(first_line: str) -> tuple[str | None, str]:
    m = EXC_HEAD.match(first_line.strip())
    if m and (m.group("msg") is not None or EXC_NAME.match(m.group("type"))):
        return m.group("type"), (m.group("msg") or "")
    return None, first_line.strip()


def parse_pytest_text(text: str, message: str = "", kind: str = "failure") -> ParsedFailure:
    """Parse the traceback text pytest stores in a JUnit ``<failure>``/``<error>`` element (any ``--tb`` style)."""
    frames: list[RawFrame] = []
    last_rest = ""
    e_lines: list[str] = []
    e_frames: list[RawFrame] = []
    for raw in text.split("\n"):
        m = FRAME_LINE.match(raw)
        if m:
            rest = m.group("rest").strip()
            fn = IN_FUNC.match(rest)
            frames.append(RawFrame(m.group("path"), int(m.group("line")), fn.group("fn") if fn else None))
            last_rest = "" if fn or not rest else rest
        elif raw.startswith("E ") or raw == "E":
            body = raw[1:].strip()
            e_lines.append(body)
            fm = PY_FILE_LINE.match(body)
            if fm:  # e.g. collection errors: ``E   File "tests/test_x.py", line 1``
                e_frames.append(RawFrame(fm.group("path"), int(fm.group("line")), None))
    msg_first = (message or "").split("\n", 1)[0]
    etype, emsg = _split_type(msg_first) if msg_first else (None, "")
    if etype is None and last_rest and EXC_HEAD.match(last_rest):
        etype = last_rest
    if etype is None and e_lines:
        for body in reversed(e_lines):  # the exception line is the last ``E`` line that parses as ``Type: message``
            t, _ = _split_type(body)
            if t:
                etype, emsg = t, body.split(":", 1)[-1].strip()
                break
    if etype is None and msg_first.startswith("assert"):
        etype = "AssertionError"
    if e_frames:
        frames.extend(e_frames)
    full_message = message or "\n".join(e_lines)
    if message == "collection failure" and etype and emsg:
        full_message = f"collection failure: {etype}: {emsg}"
    if kind == "error" and ("collection failure" in (message or "") or any("ERROR collecting" in ln for ln in text.split("\n")[:3])):
        category = "collection_error"
    elif etype and etype.split(".")[-1] == "AssertionError":
        category = "assertion"
    elif kind == "error":
        category = "test_error"
    elif etype:
        category = "exception"
    else:
        category = "unknown"
    if etype is None and category == "assertion":
        etype = "AssertionError"
    return ParsedFailure(category, etype, full_message if full_message else emsg, frames)


def parse_python_traceback(text: str) -> ParsedFailure:
    """Parse the *last* ``Traceback (most recent call last)`` block of plain Python output."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("Traceback (most recent call last)")]
    block = lines[starts[-1] + 1:] if starts else lines
    frames: list[RawFrame] = []
    final: str | None = None
    for ln in block:
        m = PY_FILE_LINE.match(ln)
        if m:
            frames.append(RawFrame(m.group("path"), int(m.group("line")), (m.group("fn") or "").strip() or None))
            final = None
        elif ln.strip() and not ln.startswith((" ", "\t")) and frames:
            if ln.startswith(("During handling", "The above exception")):
                continue
            final = ln
    if final is None:
        return ParsedFailure("unknown", None, "", frames, warnings=["No exception line was found after the stack frames."])
    etype, msg = _split_type(final)
    if etype is None:
        return ParsedFailure("unknown", None, final, frames, warnings=["The final line is not an exception."])
    category = "assertion" if etype.split(".")[-1] == "AssertionError" else "exception"
    return ParsedFailure(category, etype, msg, frames)


def parse_junit(xml_text: str, max_cases: int = 100) -> list[ParsedFailure]:
    """Failing/erroring test cases of a pytest JUnit XML document. DOCTYPE/ENTITY are rejected (no entity expansion)."""
    head = xml_text[:4096].lower()
    if "<!doctype" in head or "<!entity" in head:
        raise ValueError("DOCTYPE and ENTITY declarations are not accepted.")
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - DOCTYPE/ENTITY rejected above
    except ET.ParseError as exc:
        raise ValueError(f"Not valid XML: {str(exc)[:120]}") from None
    out: list[ParsedFailure] = []
    for case in root.iter("testcase"):
        node = case.find("failure")
        kind = "failure"
        if node is None:
            node = case.find("error")
            kind = "error"
        if node is None:
            continue
        pf = parse_pytest_text(node.text or "", node.get("message") or "", kind)
        pf.test_name, pf.classname = case.get("name") or "", case.get("classname") or ""
        out.append(pf)
        if len(out) >= max_cases:
            break
    return out


@dataclass
class ResolvedFrame:
    rel_path: str | None
    display_path: str
    scope: str  # repository | dependency | runtime | unknown
    is_test: bool
    escaped: bool = False


def looks_like_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
            or rel.startswith(("tests/", "test/")) or "/tests/" in rel)


def resolve_frame_path(raw: str, indexed: set[str], repo_root: str | None, test_files: set[str] | None = None,
                       allow_suffix: bool = False) -> ResolvedFrame:
    """Map a reported path to an indexed repository file without touching the file system.

    Absolute paths count only when they sit under the registered root; relative paths that climb out of the
    repository are never mapped (``allow_suffix`` is the one deliberate exception, used for runs whose working
    directory is outside the repository, and it only matches whole trailing path segments of an indexed file).
    """
    p = raw.strip().replace("\\", "/")
    if p.startswith("<repo>/"):
        p = p[len("<repo>/"):]
    elif p.startswith("<"):
        return ResolvedFrame(None, p[:60], "runtime", False)
    root = (repo_root or "").replace("\\", "/").rstrip("/")
    absolute = p.startswith("/") or bool(re.match(r"^[A-Za-z]:/", p))
    if absolute and root and (p == root or p.startswith(root + "/")):
        p, absolute = p[len(root) + 1:], False
    if SITE.search("/" + p):
        tail = re.split(r"(?:site|dist)-packages/", p, maxsplit=1)[-1].split("/")
        return ResolvedFrame(None, "site-packages/" + "/".join(tail[:3]), "dependency", False)
    if STDLIB.search("/" + p):
        return ResolvedFrame(None, "python/" + posixpath.basename(p), "runtime", False)
    if absolute:
        return ResolvedFrame(None, "<external>/" + posixpath.basename(p), "unknown", False, escaped=True)
    norm = posixpath.normpath(p)
    if norm.startswith("../") or norm == "..":
        if allow_suffix:
            stripped = norm
            while stripped.startswith("../"):
                stripped = stripped[3:]
            parts = stripped.split("/")
            hits = [i for i in ("/".join(parts[k:]) for k in range(len(parts))) if i in indexed]
            if hits:
                best = hits[0]  # longest suffix first
                return ResolvedFrame(best, best, "repository", looks_like_test(best) or best in (test_files or set()))
        return ResolvedFrame(None, "<outside-repository>/" + posixpath.basename(norm), "unknown", False, escaped=True)
    if norm in indexed:
        return ResolvedFrame(norm, norm, "repository", norm in (test_files or set()) or looks_like_test(norm))
    return ResolvedFrame(None, norm[:300], "unknown", looks_like_test(norm))
