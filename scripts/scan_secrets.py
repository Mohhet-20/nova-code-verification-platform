"""Lightweight committed-secret scan (no network, stdlib only). Exit 1 on findings. Not a replacement for a full scanner; it is a deterministic gate
for the common cases: private keys, cloud/VCS tokens and non-placeholder values assigned to secret-looking keys. Findings print file:line and pattern name only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "dist", "data", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache"}
SKIP_SUFFIX = {".png", ".jpg", ".ico", ".svg", ".lock", ".db", ".sqlite3", ".pyc", ".woff2"}
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "assigned-secret": re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|password)\b["']?\s*[:=]\s*["']([A-Za-z0-9+/_\-]{24,})["']"""),
}
PLACEHOLDER = re.compile(r"(?i)(replace|changeme|example|placeholder|generate|your|xxx|<|test|dummy|fake|sample)")
ALLOWLIST = {tuple(x.strip() for x in line.split("|")[:2]) for line in (ROOT / "scripts" / "secret_scan_allowlist.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}


def main() -> int:
    findings = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(ROOT).parts) or p.suffix in SKIP_SUFFIX or p.stat().st_size > 1_000_000:
            continue
        in_tests = "tests" in p.relative_to(ROOT).parts
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            for name, rx in PATTERNS.items():
                m = rx.search(line)
                if not m:
                    continue
                if name == "assigned-secret" and (in_tests or PLACEHOLDER.search(m.group(1)) or PLACEHOLDER.search(line)):
                    continue
                if (p.relative_to(ROOT).as_posix(), name) in ALLOWLIST:
                    continue
                findings.append(f"{p.relative_to(ROOT).as_posix()}:{n}: {name}")
    for f in findings:
        print(f)
    print(f"secret scan: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
