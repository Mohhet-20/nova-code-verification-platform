"""Security rules over Python source, reusing NOVA's static-analysis rule engine (``app.services.analysis``). Source is parsed, never executed."""
from __future__ import annotations

import ast
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.services.analysis import catalog
from app.services.analysis.analyzers.py_facts import collect_facts
from app.services.analysis.model import RuleContext
from app.services.analysis.orchestrator import parse_python

# PY-SEC-003 (hardcoded credentials) is handled by the dedicated secrets workflow so one credential is not reported twice.
DELEGATED = {"PY-SEC-003"}


@dataclass
class CodeHit:
    rule_id: str
    rel_path: str
    line: int
    end_line: int
    scope: str
    discriminator: str
    explanation: str
    evidence: str
    severity: str
    confidence: str


def security_rule_ids() -> list[str]:
    return sorted(rid for rid, (_, spec) in catalog.rule_index().items() if spec.category == "security" and rid not in DELEGATED)


def tool_versions() -> dict[str, str]:
    ids = set(security_rule_ids())
    return {k: v for k, v in catalog.analyzer_versions().items() if k in ids or k.startswith("py-security")}


def imported_roots(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module.split(".")[0])
    return out


def scan_source(rel_path: str, data: bytes) -> tuple[list[CodeHit], str | None, set[str]]:
    """Returns (hits, problem, imported top-level modules). A rule that raises is reported in the problem text and does not discard other rules' findings."""
    parsed, reason = parse_python(data, rel_path)
    if parsed is None:
        return [], f"{rel_path}: not analysed ({reason})", set()
    facts = collect_facts(parsed.tree)
    index = catalog.rule_index()
    wanted = set(security_rule_ids())
    hits: list[CodeHit] = []
    errors: list[str] = []
    for analyzer in catalog.ANALYZERS:
        enabled = {r.spec.id: {} for r in analyzer.rules if r.spec.id in wanted}
        if not enabled:
            continue
        res = analyzer.run(lambda options, p=parsed, f=facts: RuleContext(p, options, f), enabled)
        errors.extend(f"{rule}: {exc}" for rule, exc in res.errors)
        for f in res.findings:
            spec = index[f.rule_id][1]
            hits.append(CodeHit(f.rule_id, rel_path, f.line, f.end_line, f.scope, f.discriminator, f.explanation, f.evidence, f.severity or spec.severity, f.confidence or spec.confidence))
    return hits, (f"{rel_path}: rule error(s) {', '.join(errors)}" if errors else None), imported_roots(parsed.tree)


def read_regular(root: Path, rel: str, max_bytes: int) -> tuple[bytes | None, str]:
    """Read a repository file without following symlinks. Returns (data, status)."""
    full = os.path.join(root, rel)
    try:
        st = os.lstat(full)
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, "not_regular"
    if st.st_size > max_bytes:
        return None, "too_large"
    try:
        with open(full, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None, "unreadable"
    return (None, "too_large") if len(data) > max_bytes else (data, "ok")
