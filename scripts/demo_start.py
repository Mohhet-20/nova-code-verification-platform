"""Start the hosted public demo (deploy/demo/Dockerfile): a fresh copy of the showcase, then the API.

The image build already ran scripts/demo_build.py, which left NOVA's own backend source registered, indexed, analysed,
scanned and reviewed in /app/showcase, so a cold start on a small free instance takes seconds. On every start this:

1. copies the showcase to a fresh working directory (database, artifacts and the demo repository), so each restart is
   a clean demo and nothing a visitor did survives it;
2. runs the API in production mode: NOVA_API_TOKEN (a hosting secret) is the owner's key, the sign-in page's "Explore
   the live demo" button issues a guest engineer token (POST /api/v1/auth/guest), and repository test execution is
   switched off so no visitor can make the server run code;
3. replaces itself with uvicorn on $PORT.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
SHOWCASE = Path(os.environ.get("NOVA_SHOWCASE_DIR", "/app/showcase"))
WORKING = Path(os.environ.get("NOVA_DEMO_ROOT", "/tmp/nova"))


def _force_remove(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)  # git marks object files read-only
    func(path)


def main() -> int:
    if not (SHOWCASE / "nova.db").is_file():
        print(f"[demo] {SHOWCASE} has no nova.db; the image build did not produce the showcase", file=sys.stderr)
        return 2
    if not os.environ.get("NOVA_API_TOKEN"):
        print("[demo] NOVA_API_TOKEN is not set; refusing to start a public API without an owner key", file=sys.stderr)
        return 2
    if WORKING.exists():
        shutil.rmtree(WORKING, onerror=_force_remove)  # the previous run's database, its -wal/-shm journals and any edits
    shutil.copytree(SHOWCASE, WORKING, symlinks=True)
    os.environ.update({
        "NOVA_ENVIRONMENT": "production",
        "NOVA_DATABASE_URL": f"sqlite:///{(WORKING / 'nova.db').as_posix()}",
        "NOVA_ARTIFACT_DIR": str(WORKING / "artifacts"),
        "NOVA_BACKUP_DIR": str(WORKING / "backups"),
        "NOVA_RELEASE_STATE_DIR": str(WORKING / "release-state"),
        "NOVA_REGISTRATION_BASE_DIRS": str(WORKING / "repos"),
        "NOVA_TEST_EXECUTION_ENABLED": "false",
        "NOVA_GUEST_ACCESS_ENABLED": "true",
        "NOVA_AI_PROVIDER": "none",
    })
    os.environ.setdefault("NOVA_LOG_JSON", "true")
    os.environ.setdefault("FORWARDED_ALLOW_IPS", "*")  # behind the host's proxy: client addresses (guest throttle, logs) come from X-Forwarded-For
    print(f"[demo] fresh showcase copied to {WORKING}; starting the API", flush=True)

    os.chdir(APP / "backend")
    cmd = [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0",
           "--port", os.environ.get("PORT", "8000"), "--workers", "1", "--proxy-headers", "--no-server-header", "--no-access-log"]
    if os.name == "nt":  # local trial on Windows, where exec* mangles paths with spaces
        return subprocess.call(cmd)
    os.execvpe(cmd[0], cmd, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
