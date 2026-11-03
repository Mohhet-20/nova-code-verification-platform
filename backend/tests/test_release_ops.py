# ruff: noqa: E702, E701  (compact test setup lines)
"""Phase 17: production config contract, migration control, backup/restore integrity, rollback planning, manifest, and a real-process smoke test."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import BACKEND_DIR, Settings
from app.release import backup as bk
from app.release import dbtools, deploy, manifest, prodcheck, smoke

STRONG = "Zq8vK2mT9xLpR4wYb7NcJ5hGd3FsA6eU"
KEY = "x" * 0 + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 32 zero bytes, base64


def prod(**kw) -> Settings:
    base = dict(environment="production", api_token=STRONG, cors_origins=["https://nova.example.org"], artifact_dir="/data/a", registration_base_dirs=["/srv/repos"], integration_secret_key=KEY)
    return Settings(**{**base, **kw})


def codes(s: Settings, sev: str = "error") -> set[str]:
    return {f.code for f in prodcheck.check(s) if f.severity == sev}


# ---------------- configuration contract ----------------
def test_valid_production_config_has_no_errors():
    assert codes(prod()) == set()


@pytest.mark.parametrize("kw,code", [
    ({"api_token": ""}, "auth_disabled"),
    ({"api_token": "short"}, "api_token_weak"),
    ({"api_token": "replace-me-with-a-long-random-token-value"}, "api_token_weak"),
    ({"api_token": "<generate: 48+ random characters>"}, "api_token_weak"),  # the literal placeholder in deployment/production.env.example
    ({"cors_origins": ["*"]}, "cors_wildcard"),
    ({"integration_allow_private_destinations": True}, "ssrf_guard_relaxed"),
    ({"ops_metrics_public": True}, "metrics_public"),
    ({"integration_secret_key": "not-base64!!"}, "integration_key_invalid"),
    ({"environment": "staging"}, "environment_unknown"),
])
def test_production_rejects_unsafe_settings(kw, code):
    assert code in codes(prod(**kw))


def test_messages_never_contain_secret_values():
    s = prod(api_token="changeme-changeme-changeme-changeme-123")
    assert "changeme-changeme" not in json.dumps([f.as_dict() for f in prodcheck.check(s)])


def test_development_only_warns():
    assert codes(Settings(environment="development")) == set()
    prodcheck.enforce_startup(Settings(environment="development"))


def test_enforce_startup_refuses_invalid_production():
    with pytest.raises(prodcheck.ConfigurationError, match="auth_disabled"):
        prodcheck.enforce_startup(prod(api_token=""))


def test_enforce_startup_refuses_schema_not_at_head(db_url):
    with pytest.raises(prodcheck.ConfigurationError, match="release step"):
        prodcheck.enforce_startup(prod(database_url=db_url))


def test_enforce_startup_accepts_head(migrated_url):
    prodcheck.enforce_startup(prod(database_url=migrated_url))


def test_sqlite_in_production_is_a_warning_not_a_claim():
    assert "sqlite_in_production" in codes(prod(), "warning")
    assert "database_backend_unverified" in codes(prod(database_url="postgresql://u@h/db"))


# ---------------- migration control ----------------
def test_status_missing_then_head(db_url):
    assert dbtools.schema_status(db_url)["state"] == "missing"
    r = dbtools.migrate(db_url)
    assert r["changed"] and r["after"] == dbtools.head_revision()
    assert dbtools.schema_status(db_url)["at_head"]
    assert dbtools.migrate(db_url)["changed"] is False  # idempotent


def test_unknown_revision_is_refused_and_untouched(migrated_url):
    path = Path(migrated_url.removeprefix("sqlite:///"))
    con = sqlite3.connect(path); con.execute("UPDATE alembic_version SET version_num='zzzz'"); con.commit(); con.close()
    assert dbtools.schema_status(migrated_url)["state"] == "ahead_or_unknown"
    with pytest.raises(dbtools.MigrationError, match="unknown"):
        dbtools.migrate(migrated_url)
    assert path.exists()


def test_migration_lock_blocks_second_runner(db_url):
    with dbtools.migration_lock(db_url):
        with pytest.raises(dbtools.MigrationError, match="Another migration"):
            with dbtools.migration_lock(db_url):
                pass
    with dbtools.migration_lock(db_url):  # released afterwards
        pass


def test_stale_lock_needs_explicit_break(db_url):
    with dbtools.migration_lock(db_url) as lock:
        old = time.time() - dbtools.LOCK_STALE_SECONDS - 10
        os.utime(lock, (old, old))
        with pytest.raises(dbtools.MigrationError):
            with dbtools.migration_lock(db_url):
                pass
        with dbtools.migration_lock(db_url, break_stale=True):
            pass


def test_failed_migration_keeps_database_and_backup(migrated_url, tmp_path, monkeypatch):
    path = Path(migrated_url.removeprefix("sqlite:///"))
    con = sqlite3.connect(path); con.execute("UPDATE alembic_version SET version_num='0015'"); con.commit(); con.close()
    monkeypatch.setattr(dbtools.command, "upgrade", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom SECRET=abc")))
    with pytest.raises(dbtools.MigrationError) as exc:
        dbtools.migrate(migrated_url, backup_dir=tmp_path / "bk")
    assert path.exists() and "SECRET" not in str(exc.value) and "pre-migration backup" in str(exc.value)
    assert any(b["valid"] for b in bk.list_backups(tmp_path / "bk"))
    assert not list(path.parent.glob("*.migrate.lock"))


def test_upgrade_from_previous_revision_preserves_rows_and_takes_backup(db_url, tmp_path):
    from alembic import command

    from tests.conftest import alembic_config
    prev = dbtools.revisions_in_order()[-2]
    command.upgrade(alembic_config(db_url), prev)  # a genuine database at the previous revision
    before = dbtools.row_counts(db_url)
    assert dbtools.schema_status(db_url)["state"] == "behind"
    r = dbtools.migrate(db_url, backup_dir=tmp_path / "bk")
    assert r["before"] == prev and r["after"] == dbtools.head_revision() and r["backup"]
    assert all(dbtools.row_counts(db_url).get(t, 0) >= n for t, n in before.items())
    assert bk.verify_dir(Path(r["backup"]))["schema_revision"] == prev


def test_classifier_reports_every_revision():
    c = dbtools.classify_migrations()
    assert [x["revision"] for x in c] == dbtools.revisions_in_order()
    assert all({"additive", "needs_review"} <= set(x) for x in c)


# ---------------- backup / restore ----------------
@pytest.fixture
def populated(migrated_url, tmp_path):
    art = tmp_path / "artifacts"
    (art / "run1").mkdir(parents=True)
    (art / "run1" / "out.txt").write_text("evidence")
    (art / "staged.py").write_text("print(1)")
    return migrated_url, art


def test_backup_verify_restore_roundtrip(populated, tmp_path):
    url, art = populated
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    m = bk.verify_dir(b["path"])
    assert m["schema_revision"] == dbtools.head_revision() and len(m["artifacts"]) == 2 and m["not_included"]
    r = bk.restore(b["path"], tmp_path / "restored")
    assert r["ok"] and r["integrity"]["ok"] and r["row_counts_ok"]
    assert (tmp_path / "restored" / "artifacts" / "run1" / "out.txt").read_text() == "evidence"
    assert (tmp_path / "restored" / "db.sqlite3").exists() and Path(url.removeprefix("sqlite:///")).exists()


def test_integrity_on_an_unmigrated_database_reports_instead_of_crashing(db_url, tmp_path):
    import sqlite3
    sqlite3.connect(db_url.removeprefix("sqlite:///")).close()  # an empty database file, e.g. from an interrupted first start
    refs = bk.file_reference_check(db_url, tmp_path / "artifacts")
    assert refs["ok"] is False and "test_generation_runs" in refs["tables_missing"]
    out = subprocess.run([sys.executable, "-m", "app.release", "integrity"], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=120,
                         env={**os.environ, "NOVA_DATABASE_URL": db_url})
    assert out.returncode == 1 and "Traceback" not in out.stderr and '"tables_missing"' in out.stdout


def test_empty_artifact_directories_survive_backup_and_restore(populated, tmp_path):
    # A refused test run (e.g. NOVA_TEST_EXECUTION_ENABLED=false) records an artifact directory that stays empty; before 0.19.0 the backup
    # dropped it and every later restore failed its file-reference check.
    url, art = populated
    (art / "repo1" / "executions" / "run-refused").mkdir(parents=True)
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    assert bk.verify_dir(b["path"])["artifact_dirs"] == ["repo1/executions/run-refused"]
    r = bk.restore(b["path"], tmp_path / "restored")
    assert r["ok"] and (tmp_path / "restored" / "artifacts" / "repo1" / "executions" / "run-refused").is_dir()
    (b["path"] / "artifacts" / "repo1" / "executions" / "run-refused").rmdir()
    with pytest.raises(bk.BackupError, match="directory"):
        bk.verify_dir(b["path"])


def test_restore_never_overwrites_existing_target(populated, tmp_path):
    url, art = populated
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    target = tmp_path / "occupied"; target.mkdir(); (target / "keep.txt").write_text("x")
    with pytest.raises(bk.BackupError, match="empty"):
        bk.restore(b["path"], target)
    assert (target / "keep.txt").read_text() == "x"


def test_corrupt_artifact_or_db_fails_verification(populated, tmp_path):
    url, art = populated
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)["path"]
    (b / "artifacts" / "staged.py").write_text("tampered")
    with pytest.raises(bk.BackupError, match="corrupt"):
        bk.verify_dir(b)
    with pytest.raises(bk.BackupError):
        bk.restore(b, tmp_path / "r2")
    b2 = bk.create_backup(url, tmp_path / "bk2", artifact_root=art)["path"]
    (b2 / "db.sqlite3").write_bytes(b"garbage")
    with pytest.raises(bk.BackupError, match="checksum"):
        bk.verify_dir(b2)


def test_interrupted_backup_leaves_no_valid_looking_directory(populated, tmp_path, monkeypatch):
    url, art = populated
    monkeypatch.setattr(bk, "verify_dir", lambda p: (_ for _ in ()).throw(bk.BackupError("simulated")))
    with pytest.raises(bk.BackupError):
        bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    assert list((tmp_path / "bk").iterdir()) == []


def test_listing_flags_partial_and_prune_is_safe(populated, tmp_path):
    url, art = populated
    root = tmp_path / "bk"
    names = [bk.create_backup(url, root, artifact_root=art)["name"] for _ in range(3)]
    (root / ".partial-nova-backup-x").mkdir()
    (root / "nova-backup-broken").mkdir()
    listing = {b["name"]: b["valid"] for b in bk.list_backups(root)}
    assert listing[".partial-nova-backup-x"] is False and listing["nova-backup-broken"] is False
    res = bk.prune(root, keep=1)
    assert res["kept"] == [names[-1]] and set(res["removed"]) == set(names[:2])
    assert (root / "nova-backup-broken").exists() and (root / ".partial-nova-backup-x").exists()  # invalid ones are never auto-deleted
    assert bk.prune(root, keep=1)["removed"] == []  # never deletes the only valid copy
    with pytest.raises(bk.BackupError):
        bk.prune(root, keep=0)


def test_file_reference_check_detects_missing_files(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE refs (p TEXT)"); con.executemany("INSERT INTO refs VALUES (?)", [("a.txt",), ("gone.txt",), ("../escape",)]); con.commit(); con.close()
    root = tmp_path / "art"; root.mkdir(); (root / "a.txt").write_text("x")
    monkeypatch.setattr(bk, "FILE_REFERENCES", (("refs", "p", "file"),))
    r = bk.file_reference_check(f"sqlite:///{db.as_posix()}", root)
    assert r["checked"] == 3 and r["missing_count"] == 2 and not r["ok"]


def test_restore_flags_missing_referenced_files(populated, tmp_path):
    url, art = populated
    path = Path(url.removeprefix("sqlite:///"))
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=OFF")
    cols = [r[1] for r in con.execute("PRAGMA table_info(test_candidates)")]
    notnull = {r[1]: (r[2], r[4]) for r in con.execute("PRAGMA table_info(test_candidates)") if r[3] and r[4] is None}
    vals = {c: ("x" if "CHAR" in t.upper() or "TEXT" in t.upper() else 0 if "INT" in t.upper() or "BOOL" in t.upper() or "FLOAT" in t.upper() else "2026-01-01") for c, (t, _) in notnull.items()}
    vals["staged_path"] = "does/not/exist.py"
    if "id" in cols:
        vals["id"] = "c1"
    for c in ("stat" "us",):
        if c in cols and c not in vals:
            vals[c] = "generated"
    con.execute(f"INSERT INTO test_candidates ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values())); con.commit(); con.close()
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    r = bk.restore(b["path"], tmp_path / "rst")
    assert r["ok"] is False and r["file_references"]["missing_count"] == 1


# ---------------- rollback planning & manifest ----------------
def _prev(head: str) -> dict:
    return {"version": "0.0.0", "migrations": {"head": head}}


def test_rollback_plan_never_downgrades(migrated_url):
    head = dbtools.head_revision()
    same = deploy.rollback_plan(migrated_url, _prev(head))
    assert same["app_rollback"] == "safe" and same["downgrade_executed"] is False
    order = dbtools.revisions_in_order()
    plan = deploy.rollback_plan(migrated_url, _prev(order[-2]))
    assert plan["app_rollback"] in ("likely_safe", "requires_data_decision") and plan["downgrade_executed"] is False
    assert dbtools.schema_status(migrated_url)["at_head"]  # untouched
    assert deploy.rollback_plan(migrated_url, _prev("nope"))["app_rollback"] == "unknown"


def test_manifest_and_sbom_are_honest(tmp_path):
    m = manifest.build()
    assert m["signature"] is None and m["attestation"] is None and m["image"]["built"] is False
    assert m["tests"]["available"] is False and m["migrations"]["head"] == dbtools.head_revision()
    assert any(d["name"].lower() == "fastapi" for d in m["backend_dependencies"])
    s = manifest.sbom()
    assert s["bomFormat"] == "CycloneDX" and s["components"] and all(c["purl"].startswith("pkg:") for c in s["components"])
    junit = tmp_path / "j.xml"; junit.write_text('<testsuite tests="3" failures="1" errors="0" skipped="1"/>')
    assert manifest.build(junit)["tests"] == {"available": True, "tests": 3, "failures": 1, "errors": 0, "skipped": 1}


def test_deployment_record_is_append_only(tmp_path):
    deploy.record(tmp_path, "deploy", "migrated", schema="x")
    deploy.record(tmp_path, "deploy", "failed", error="e")
    assert [h["outcome"] for h in deploy.history(tmp_path)] == ["migrated", "failed"]


def test_preflight_blocks_bad_production_config(db_url, tmp_path):
    assert deploy.preflight(prod(api_token="", database_url=db_url), tmp_path)["ok"] is False
    assert deploy.preflight(prod(database_url=db_url), tmp_path)["ok"] is True


# ---------------- CLI + a real running process ----------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "app.release", *args], cwd=BACKEND_DIR, env={**os.environ, **env}, capture_output=True, text=True, timeout=120)


def test_cli_deploy_backup_restore_smoke_against_real_server(tmp_path):
    db = tmp_path / "live.db"
    art = tmp_path / "art"; art.mkdir(); (art / "f.txt").write_text("x")
    port = _free_port()
    env = {"NOVA_DATABASE_URL": f"sqlite:///{db.as_posix()}", "NOVA_ARTIFACT_DIR": str(art), "NOVA_API_TOKEN": STRONG, "NOVA_LOG_JSON": "false", "NOVA_ENVIRONMENT": "development", "NOVA_REQUIRE_CURRENT_SCHEMA": "true", "NOVA_RELEASE_STATE_DIR": str(tmp_path / "state")}
    # the API refuses to start against an unmigrated database when the schema guard is on
    p = subprocess.run([sys.executable, "-c", "import app.main as m; from fastapi.testclient import TestClient\nwith TestClient(m.create_app()): pass"], cwd=BACKEND_DIR, env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
    assert p.returncode != 0 and "release step" in (p.stdout + p.stderr)
    d = _cli(env, "deploy", "--backup-dir", str(tmp_path / "bk"))
    assert d.returncode == 0, d.stderr and STRONG not in d.stdout
    assert json.loads(_cli(env, "status").stdout)["at_head"]
    assert _cli(env, "backup", "--backup-dir", str(tmp_path / "bk")).returncode == 0
    name = next(b["name"] for b in json.loads(_cli(env, "list-backups", "--backup-dir", str(tmp_path / "bk")).stdout) if b["valid"] and "pre-migration" not in b["name"])
    r = _cli(env, "restore", str(tmp_path / "bk" / name), "--target", str(tmp_path / "restore"))
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(port), "--host", "127.0.0.1"], cwd=BACKEND_DIR, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        assert smoke.wait_ready(base, timeout=60)["ready"]
        res = smoke.run(base, STRONG)
        assert res["ok"], res
        assert STRONG not in json.dumps(res)
        assert smoke.run(base, "wrong-token-wrong-token-wrong-0000")["ok"] is False
        out = _cli(env, "smoke", "--url", base, "--wait", "5")
        assert out.returncode == 0 and json.loads(out.stdout)["authenticated_checks"] is True and STRONG not in out.stdout
    finally:
        server.terminate()
        server.wait(timeout=20)
    hist = deploy.history(tmp_path / "state")
    assert any(h["event"] == "deploy" for h in hist)
