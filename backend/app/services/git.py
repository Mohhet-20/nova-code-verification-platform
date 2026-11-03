"""Controlled git access: argument arrays only, no shell, sanitized env, timeouts, bounded output."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("nova.git")

# A repository's own config can name programs git would run (e.g. core.fsmonitor). Disable them.
_HARDENING = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
              "-c", "protocol.ext.allow=never"]


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str
    truncated: bool = False


def git_env(root: Path) -> dict[str, str]:
    """Sanitized environment for git. GIT_CEILING_DIRECTORIES stops git from searching ABOVE the registered root: without it, a registered
    directory that is not itself a repository but sits inside another one (e.g. a home directory under version control) would make NOVA
    read and report the outer repository's history and changes."""
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "HOME", "USERPROFILE"}}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(Path(root).resolve().parent)})
    return env


def run_git(root: Path, args: list[str], timeout: float, max_output: int) -> GitResult:
    env = git_env(root)
    try:
        proc = subprocess.run(  # noqa: S603 - argument list, shell=False; git resolved via PATH
            ["git", *_HARDENING, "-C", str(root), *args],  # noqa: S607
            capture_output=True, timeout=timeout, env=env, shell=False, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git_failed", extra={"reason": type(exc).__name__})
        return GitResult(False, "")
    out = proc.stdout[:max_output]
    return GitResult(proc.returncode == 0, out.decode("utf-8", "replace"),
                     truncated=len(proc.stdout) > max_output)


@dataclass(frozen=True)
class GitInfo:
    is_git: bool
    branch: str | None = None
    head: str | None = None
    dirty_count: int | None = None


def inspect_repository(root: Path, timeout: float, max_output: int) -> GitInfo:
    if not (root / ".git").exists():
        return GitInfo(False)
    inside = run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout, max_output)
    if not inside.ok or inside.stdout.strip() != "true":
        return GitInfo(False)
    branch = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout, max_output)
    head = run_git(root, ["rev-parse", "HEAD"], timeout, max_output)
    status = run_git(root, ["status", "--porcelain"], timeout, max_output)
    dirty = len([ln for ln in status.stdout.splitlines() if ln.strip()]) if status.ok else None
    return GitInfo(
        True,
        branch.stdout.strip() or None if branch.ok else None,
        head.stdout.strip() or None if head.ok else None,
        dirty,
    )
