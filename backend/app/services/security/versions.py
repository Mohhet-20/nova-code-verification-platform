"""Ecosystem-aware version semantics. Versions are never compared as strings.

* PyPI: PEP 440 via ``packaging``. * npm: SemVer 2.0.0 (precedence rules, including pre-release identifiers).
An unparseable version yields ``None`` (unknown), never "vulnerable" or "safe".
"""
from __future__ import annotations

import re
from functools import total_ordering
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


@total_ordering
class SemVer:
    __slots__ = ("major", "minor", "patch", "pre")

    def __init__(self, text: str) -> None:
        m = _SEMVER.match(text.strip())
        if not m:
            raise ValueError("not a semantic version")
        self.major, self.minor, self.patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.pre: tuple[str, ...] = tuple(m.group(4).split(".")) if m.group(4) else ()

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SemVer) and self._key() == other._key() and self.pre == other.pre

    def __hash__(self) -> int:
        return hash((self._key(), self.pre))

    def __lt__(self, other: SemVer) -> bool:
        if self._key() != other._key():
            return self._key() < other._key()
        if self.pre == other.pre:
            return False
        if not self.pre:  # a release is greater than any of its pre-releases
            return False
        if not other.pre:
            return True
        for a, b in zip(self.pre, other.pre, strict=False):
            if a == b:
                continue
            an, bn = a.isdigit(), b.isdigit()
            if an and bn:
                return int(a) < int(b)
            if an != bn:
                return an  # numeric identifiers have lower precedence than alphanumeric ones
            return a < b
        return len(self.pre) < len(other.pre)


def parse(ecosystem: str, text: str | None) -> Any | None:
    if not text or not isinstance(text, str):
        return None
    try:
        if ecosystem == "PyPI":
            return Version(text.strip())
        if ecosystem == "npm":
            return SemVer(text)
    except (InvalidVersion, ValueError):
        return None
    return None


def compare(ecosystem: str, a: str, b: str) -> int | None:
    pa, pb = parse(ecosystem, a), parse(ecosystem, b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def is_prerelease(ecosystem: str, text: str) -> bool | None:
    p = parse(ecosystem, text)
    if p is None:
        return None
    return bool(p.is_prerelease) if ecosystem == "PyPI" else bool(p.pre)


def major(ecosystem: str, text: str) -> int | None:
    p = parse(ecosystem, text)
    if p is None:
        return None
    return int(p.major) if ecosystem == "npm" else int(p.release[0]) if p.release else None


def affected(ecosystem: str, version: str | None, ranges: list[dict[str, Any]], versions: list[str]) -> bool | None:
    """OSV range semantics. True = affected, False = not affected, None = cannot be determined (unknown version or no affected data)."""
    v = parse(ecosystem, version)
    if v is None:
        return None
    if not ranges and not versions:
        return None
    for listed in versions:
        lv = parse(ecosystem, listed)
        if lv is not None and lv == v:
            return True
    undecidable = False
    for rng in ranges:
        events = rng.get("events") or []
        parsed: list[tuple[Any, str]] = []
        for ev in events:
            kind = next((k for k in ("introduced", "fixed", "last_affected", "limit") if k in ev), None)
            if kind is None:
                continue
            raw = str(ev[kind])
            pv = parse(ecosystem, "0" if raw == "0" and ecosystem == "PyPI" else ("0.0.0" if raw == "0" else raw))
            if pv is None:
                undecidable = True
                break
            parsed.append((pv, kind))
        else:
            order = {"introduced": 0, "limit": 1, "fixed": 1, "last_affected": 1}
            parsed.sort(key=lambda t: (t[0], order[t[1]]))
            state = False
            for pv, kind in parsed:
                if kind == "introduced" and v >= pv:
                    state = True
                elif kind in ("fixed", "limit") and v >= pv:
                    state = False
                elif kind == "last_affected" and v > pv:
                    state = False
            if state:
                return True
            continue
    if undecidable:
        return None
    return False


# ---- declared constraints (used to judge whether a fixed version fits what the manifest allows) ----
def _npm_atom(spec: str) -> tuple[SemVer, SemVer | None, bool] | None:
    """Return (low, high_exclusive, low_inclusive) for caret/tilde/x-range/exact atoms."""
    s = spec.strip()
    if s in ("", "*", "x", "X", "latest"):
        return SemVer("0.0.0"), None, True
    m = re.match(r"^([~^]?)v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?$", s)
    if not m:
        return None
    op, ma, mi, pa, pre = m.group(1), int(m.group(2)), m.group(3), m.group(4), m.group(5)
    mi_n = None if mi in (None, "x", "X", "*") else int(mi)
    pa_n = None if pa in (None, "x", "X", "*") else int(pa)
    low = SemVer(f"{ma}.{mi_n or 0}.{pa_n or 0}" + (f"-{pre}" if pre else ""))
    if not op:
        if mi_n is None:
            return low, SemVer(f"{ma + 1}.0.0"), True
        if pa_n is None:
            return low, SemVer(f"{ma}.{mi_n + 1}.0"), True
        return low, None, True  # exact: handled by caller
    if op == "~":
        return (low, SemVer(f"{ma}.{mi_n + 1}.0"), True) if mi_n is not None else (low, SemVer(f"{ma + 1}.0.0"), True)
    # caret
    if ma > 0 or mi_n is None:
        return low, SemVer(f"{ma + 1}.0.0"), True
    if mi_n > 0 or pa_n is None:
        return low, SemVer(f"0.{mi_n + 1}.0"), True
    return low, SemVer(f"0.0.{(pa_n or 0) + 1}"), True


def satisfies(ecosystem: str, version: str, spec: str) -> bool | None:
    """Does ``version`` satisfy the declared constraint? ``None`` when the constraint grammar is not understood."""
    pv = parse(ecosystem, version)
    if pv is None or not spec:
        return None
    if ecosystem == "PyPI":
        try:
            return bool(SpecifierSet(spec).contains(pv, prereleases=True))
        except InvalidSpecifier:
            return None
    result_any = False
    for alt in spec.split("||"):
        ok: bool | None = True
        tokens = re.findall(r"(?:>=|<=|>|<|=)?\s*[~^]?v?[0-9xX*][0-9A-Za-z.*+-]*", alt.strip())
        if not tokens or "".join(t.replace(" ", "") for t in tokens) != re.sub(r"\s+", "", alt.strip()).replace(",", ""):
            return None
        for tok in tokens:
            t = tok.replace(" ", "")
            m = re.match(r"^(>=|<=|>|<|=)?(.*)$", t)
            op, rest = m.group(1), m.group(2)  # type: ignore[union-attr]
            if op in (">=", "<=", ">", "<", "="):
                try:
                    bound = SemVer(rest)
                except ValueError:
                    return None
                ok = ok and {">=": pv >= bound, "<=": pv <= bound, ">": pv > bound, "<": pv < bound, "=": pv == bound}[op]
            else:
                atom = _npm_atom(rest)
                if atom is None:
                    return None
                low, high, _ = atom
                if high is None:
                    ok = ok and pv == low
                else:
                    ok = ok and low <= pv < high
        result_any = result_any or bool(ok)
    return result_any
