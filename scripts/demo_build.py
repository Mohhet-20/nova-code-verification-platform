"""Build the hosted public demo's showcase data (run once, at image build time, by deploy/demo/Dockerfile).

Produces, under NOVA_DEMO_ROOT (default /tmp/nova):

* ``repos/nova-api`` - a git repository holding NOVA's own backend source and tests, committed, plus one uncommitted change
  (a bearer-token check that drops the constant-time comparison) so the code review has a real diff to assess;
* ``nova.db`` and ``artifacts/`` - that repository registered, fully indexed, statically analysed, security scanned,
  code reviewed, with an ingested failure under investigation and a task, driven through the public HTTP API.

The analysis is heavy for a 512 MB free instance, so it happens here instead of on every cold start;
scripts/demo_start.py copies the result to a fresh working directory each time the service starts.

Usage:  python scripts/demo_build.py            (from the repository root)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
DEMO_ROOT = Path(os.environ.get("NOVA_DEMO_ROOT", "/tmp/nova"))
SHOWCASE = DEMO_ROOT / "repos" / "nova-api"
DONE = {"completed", "completed_with_warnings"}

# The uncommitted change the review assesses: a timing-unsafe token comparison, a classic security regression.
REGRESSION = ("    if not hmac.compare_digest(supplied.encode(), settings.api_token.encode()):",
              "    if supplied != settings.api_token:  # simpler comparison")

TRACEBACK = """Traceback (most recent call last):
  File "app/security/auth.py", line 104, in require_token
    if supplied != settings.api_token:
  File "app/security/auth.py", line 95, in _bearer
    scheme, _, value = authorization.partition(" ")
AttributeError: 'NoneType' object has no attribute 'partition'
"""


def git(*args: str) -> None:
    subprocess.run(["git", *args], cwd=SHOWCASE, check=True, capture_output=True)


def make_repository() -> None:
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(BACKEND / "app", SHOWCASE / "app", ignore=skip)
    shutil.copytree(BACKEND / "tests", SHOWCASE / "tests", ignore=skip)  # discoverable; execution stays disabled on the demo
    for name in ("requirements.txt", "pyproject.toml"):
        if (BACKEND / name).is_file():
            shutil.copy2(BACKEND / name, SHOWCASE / name)
    (SHOWCASE / "README.md").write_text("# nova-api\n\nNOVA's own backend, analysed by NOVA for the public demo.\n",
                                        encoding="utf-8", newline="\n")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.name=NOVA demo", "-c", "user.email=demo@nova.invalid", "commit", "-q", "-m", "Import nova-api")
    auth = SHOWCASE / "app" / "security" / "auth.py"
    text = auth.read_text(encoding="utf-8")
    if REGRESSION[0] not in text:
        raise SystemExit("[demo-build] auth.py no longer contains the line the showcase change edits; update REGRESSION")
    auth.write_text(text.replace(REGRESSION[0], REGRESSION[1], 1), encoding="utf-8", newline="\n")


def main() -> int:
    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)
    SHOWCASE.mkdir(parents=True)
    make_repository()

    os.environ.update({
        "NOVA_ENVIRONMENT": "development",
        "NOVA_DATABASE_URL": f"sqlite:///{(DEMO_ROOT / 'nova.db').as_posix()}",
        "NOVA_ARTIFACT_DIR": str(DEMO_ROOT / "artifacts"),
        "NOVA_REGISTRATION_BASE_DIRS": str(DEMO_ROOT / "repos"),
        "NOVA_TEST_EXECUTION_ENABLED": "false",
        "NOVA_LOG_JSON": "false",
        "NOVA_LOG_LEVEL": "WARNING",
    })
    os.environ.pop("NOVA_API_TOKEN", None)
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))

    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["NOVA_DATABASE_URL"])
    command.upgrade(cfg, "head")

    from app.main import create_app

    with TestClient(create_app()) as c:
        def call(method: str, path: str, body: Any = None, expect: int = 200) -> Any:
            res = c.request(method, f"/api/v1{path}", json=body)
            if res.status_code != expect:
                raise SystemExit(f"[demo-build] {method} {path} -> {res.status_code}: {res.text[:400]}")
            return res.json()

        def wait(path: str) -> dict:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                body = call("GET", path)
                status = body.get("status") or body.get("state")
                if status in DONE:
                    return body
                if status == "failed":
                    raise SystemExit(f"[demo-build] {path} failed: {body.get('error_message')}")
                time.sleep(0.5)
            raise SystemExit(f"[demo-build] timed out waiting for {path}")

        repo = call("POST", "/repositories", {"name": "nova-api", "root_path": str(SHOWCASE),
                                              "description": "NOVA's own backend source, with one uncommitted change to review."}, 201)
        rid = repo["id"]
        snap = wait(f"/repositories/{rid}/snapshots/{call('POST', f'/repositories/{rid}/index', {'mode': 'full'}, 202)['id']}")
        print(f"[demo-build] indexed {snap.get('files_indexed')} files", flush=True)
        run = wait(f"/repositories/{rid}/analysis/runs/{call('POST', f'/repositories/{rid}/analysis', {'mode': 'full'}, 202)['id']}")
        print(f"[demo-build] static analysis: {run.get('findings_open')} open findings", flush=True)
        scan = call("POST", f"/repositories/{rid}/security/scans", {"scan_types": ["code", "secrets"]}, 202)
        wait(f"/repositories/{rid}/security/scans/{scan['id']}")
        review = wait(f"/repositories/{rid}/reviews/{call('POST', f'/repositories/{rid}/reviews', {'mode': 'working_tree'}, 202)['id']}")
        print(f"[demo-build] code review {review.get('status')}", flush=True)
        failures = call("POST", f"/repositories/{rid}/failures/ingest", {"source": "python_traceback", "text": TRACEBACK,
                                                                       "test_id": "tests/test_auth.py::test_missing_header"}, 201)
        if failures["items"]:
            call("POST", f"/repositories/{rid}/failures/{failures['items'][0]['id']}/investigations", None, 201)
        call("POST", "/tasks", {"repository_id": rid, "title": "Restore constant-time token comparison",
                                "problem_statement": "The working-tree change compares bearer tokens with !=, which leaks timing.",
                                "objective": "Compare tokens with hmac.compare_digest again",
                                "acceptance_criteria": ["Review shows no timing-unsafe comparison", "Auth tests pass"]}, 201)
    print(f"[demo-build] showcase ready in {DEMO_ROOT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
