"""Create a small synthetic repository for trying NOVA safely.

Usage:  python scripts/make_sample_repo.py <empty-or-new-directory> [--git]

The files include fake credentials, a .gitignore, dependency folders and binaries so you can see
NOVA's safety rules in action. Nothing here is real. The script refuses to write into a non-empty
directory, so it can never overwrite your work.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FILES: dict[str, bytes] = {
    "README.md": b"# Sample repository\n\nSynthetic fixture for NOVA.\n",
    "package.json": b'{"name": "sample", "scripts": {"test": "echo ok"}}\n',
    "pyproject.toml": b"[project]\nname = 'sample'\n",
    ".gitignore": b"ignored_dir/\n*.log\n",
    "src/auth/login.py": b"def login(user, password):\n    # BUG: accepts any password\n    return True\n",
    "src/auth/session.py": b"SESSION_TIMEOUT_SECONDS = 600\n",
    "src/billing/invoice.ts": b"export const invoice = 1;\n",
    "tests/test_login.py": b"def test_login():\n    assert True\n",
    "docs/guide.md": b"Guide.\n",
    # Everything below must NOT appear in NOVA's inventory:
    ".env": b"API_KEY=not-a-real-secret\n",
    "deploy/server.pem": b"-----BEGIN PRIVATE KEY-----\nFAKE\n",
    "ignored_dir/skip.py": b"x = 1\n",
    "debug.log": b"log\n",
    "node_modules/lib/index.js": b"module.exports = 1;\n",
    "assets/logo.png": b"\x89PNG\x00\x00",
    # Safe template files ARE indexed:
    ".env.example": b"API_KEY=\n",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path)
    parser.add_argument("--git", action="store_true", help="also run git init and make one commit")
    args = parser.parse_args()
    target: Path = args.target.resolve()
    if target.exists() and any(target.iterdir()):
        print(f"Refusing to write into non-empty directory: {target}", file=sys.stderr)
        return 1
    for rel, data in FILES.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if args.git:
        run = lambda *a: subprocess.run(["git", "-C", str(target), *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("add", "-A")
        run("-c", "user.email=nova@example.com", "-c", "user.name=NOVA Sample", "commit", "-q", "-m", "Initial commit")
    print(f"Sample repository created at: {target}")
    print("Expected NOVA inventory: 10 files (secrets, ignored files, dependencies and binaries are skipped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
