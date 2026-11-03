"""Release 0.19 deployment hardening: auth-failure throttling, API security headers, CORS methods, the repository-code kill switch,
process hardening against /proc/<pid>/environ reads, and container-friendly release CLI defaults."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, reset_settings_cache
from app.release import prodcheck
from app.security.throttle import AuthFailureThrottle
from tests.test_release_ops import prod

GOOD = "Zq8vK2mT9xLpR4wYb7NcJ5hGd3FsA6eUjW1oQ"


def _app(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    from app.main import create_app

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return TestClient(create_app(), raise_server_exceptions=False)


# ---------------- throttle unit behaviour ----------------
def test_throttle_blocks_after_limit_and_recovers_after_window():
    now = [1000.0]
    t = AuthFailureThrottle(limit=3, window_seconds=60, clock=lambda: now[0])
    for _ in range(2):
        t.record_failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") == 0
    t.record_failure("1.2.3.4")
    assert 0 < t.retry_after("1.2.3.4") <= 61
    assert t.retry_after("5.6.7.8") == 0  # other clients are unaffected
    now[0] += 61
    assert t.retry_after("1.2.3.4") == 0


def test_throttle_disabled_with_zero_limit_and_memory_is_bounded(monkeypatch: pytest.MonkeyPatch):
    off = AuthFailureThrottle(limit=0, window_seconds=60)
    for _ in range(100):
        off.record_failure("x")
    assert off.retry_after("x") == 0
    import app.security.throttle as th

    monkeypatch.setattr(th, "MAX_TRACKED_CLIENTS", 5)
    t = AuthFailureThrottle(limit=2, window_seconds=60)
    for i in range(50):
        t.record_failure(f"10.0.0.{i}")
    assert len(t._failures) == 5


# ---------------- throttle through the API ----------------
def test_invalid_tokens_are_throttled_per_client_and_missing_tokens_are_not(migrated_url: str, monkeypatch: pytest.MonkeyPatch):
    with _app(monkeypatch, NOVA_API_TOKEN=GOOD, NOVA_AUTH_FAILURE_LIMIT="3") as c:
        for _ in range(10):  # no token: plain 401, never counted
            assert c.get("/api/v1/repositories").status_code == 401
        assert c.get("/api/v1/repositories", headers={"Authorization": f"Bearer {GOOD}"}).status_code == 200
        for _ in range(3):
            assert c.get("/api/v1/repositories", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = c.get("/api/v1/repositories", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_requests"
        assert int(r.headers["Retry-After"]) > 0
        # the valid token is refused too while the client is blocked (otherwise guessing could continue)
        assert c.get("/api/v1/repositories", headers={"Authorization": f"Bearer {GOOD}"}).status_code == 429
        assert c.get("/api/v1/health").status_code == 200  # liveness stays open for orchestrators


def test_invalid_user_tokens_count_as_failures(migrated_url: str, monkeypatch: pytest.MonkeyPatch):
    with _app(monkeypatch, NOVA_AUTH_FAILURE_LIMIT="2") as c:
        for _ in range(2):
            assert c.get("/api/v1/repositories", headers={"Authorization": "Bearer nova_u_not-a-real-token"}).status_code == 401
        assert c.get("/api/v1/repositories", headers={"Authorization": "Bearer nova_u_not-a-real-token"}).status_code == 429


# ---------------- headers and CORS ----------------
def test_api_responses_carry_security_headers_and_keep_endpoint_overrides(client: TestClient):
    r = client.get("/api/v1/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert client.get("/api/v1/does-not-exist").headers["x-content-type-options"] == "nosniff"


def test_cors_preflight_allows_patch_for_configured_origin(client: TestClient):
    r = client.options("/api/v1/repositories/x/findings/y", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PATCH"})
    assert r.status_code == 200 and "PATCH" in r.headers["access-control-allow-methods"]
    bad = client.options("/api/v1/repositories", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


# ---------------- repository-code kill switch ----------------
def test_test_execution_can_be_disabled_and_is_never_reported_as_run(tmp_path: Path):
    from app.services.testing import runner

    spec = runner.RunSpec(kind="existing", repo_root=tmp_path, run_dir=tmp_path / "run", cwd=tmp_path, rootdir=tmp_path,
                          targets=["tests"], import_roots=[tmp_path], coverage=False)
    res = runner.execute(spec, Settings(test_execution_enabled=False))
    assert res.status == "rejected" and "NOVA_TEST_EXECUTION_ENABLED" in res.reason
    assert res.started_at == 0.0 and not (tmp_path / "run" / "junit.xml").exists()  # no process was started


def test_production_contract_reports_new_operational_risks():
    warnings = {f.code for f in prodcheck.check(prod()) if f.severity == "warning"}
    assert "test_execution_unsandboxed" in warnings
    assert "test_execution_unsandboxed" not in {f.code for f in prodcheck.check(prod(test_execution_enabled=False))}
    assert "auth_throttle_disabled" in {f.code for f in prodcheck.check(prod(auth_failure_limit=0)) if f.severity == "warning"}
    assert not [f for f in prodcheck.check(prod()) if f.severity == "error"]


# ---------------- process hardening ----------------
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and prctl are Linux-only")
def test_child_process_cannot_read_parent_environment_after_hardening(tmp_path: Path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import subprocess, sys\n"
        "from app.security.process import harden_process\n"
        "print(harden_process())\n"
        "child = \"import os\\ntry:\\n    d = open(f'/proc/{os.getppid()}/environ','rb').read()\\n    print('LEAKED' if b'canary-7f3a' in d else 'NOT_FOUND')\\n"
        "except OSError:\\n    print('BLOCKED')\\n\"\n"
        "print(subprocess.run([sys.executable, '-c', child], env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True).stdout.strip())\n",
        encoding="utf-8")
    env = {**os.environ, "NOVA_API_TOKEN": "canary-7f3a", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, text=True, timeout=60, check=True).stdout.split()
    assert out == ["nondumpable", "BLOCKED"]


def test_hardening_is_a_noop_off_linux(monkeypatch: pytest.MonkeyPatch):
    from app.security import process

    monkeypatch.setattr(process.sys, "platform", "win32")
    assert process.harden_process() == "unsupported"


# ---------------- release CLI defaults for read-only containers ----------------
def test_release_cli_backup_and_state_dirs_follow_the_environment(tmp_path: Path):
    code = "import app.release.__main__ as m; print(m.DEFAULT_BACKUPS); print(m.DEFAULT_STATE)"
    env = {**os.environ, "NOVA_BACKUP_DIR": str(tmp_path / "bk"), "NOVA_RELEASE_STATE_DIR": str(tmp_path / "st")}
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                         timeout=60, check=True).stdout.splitlines()
    assert [Path(x) for x in out] == [tmp_path / "bk", tmp_path / "st"]


# ---------------- git never escapes the registered root ----------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_does_not_discover_a_repository_above_the_registered_root(tmp_path: Path):
    from app.core.errors import ConflictError
    from app.services import git
    from app.services.review import gitops

    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    (outer / "inner" / "a.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "outer"]):
        subprocess.run(["git", *args], cwd=outer, check=True, capture_output=True)
    settings = Settings()
    # Without the ceiling, `git -C outer/inner rev-parse` answers for the OUTER repository and its changes would be reviewed.
    assert git.run_git(outer, ["rev-parse", "--is-inside-work-tree"], 10, 1000).stdout.strip() == "true"
    assert not git.run_git(outer / "inner", ["rev-parse", "--is-inside-work-tree"], 10, 1000).ok
    with pytest.raises(ConflictError, match="not a Git working tree"):
        gitops.require_git(outer / "inner", settings)
