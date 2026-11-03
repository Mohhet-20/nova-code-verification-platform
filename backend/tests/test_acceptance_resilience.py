"""Phase 18 acceptance: failure injection in isolated databases/directories. Each case checks detection, accurate reporting, no leaked internals,
consistent state and no false success."""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.db.session import reset_engine
from tests.test_acceptance_sweeps import MUTATING, is_public, operations
from tests.test_governance import API, World, w  # noqa: F401

LEAK = re.compile(r"Traceback|sqlalchemy|sqlite3\.|File \"|/sessions/|\.db\b|OperationalError", re.I)


def _app(monkeypatch: pytest.MonkeyPatch, url: str) -> TestClient:
    monkeypatch.setenv("NOVA_DATABASE_URL", url)
    reset_settings_cache()
    reset_engine()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def test_unmigrated_or_missing_database_is_not_ready_and_leaks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for url in (f"sqlite:///{(tmp_path / 'absent' / 'x.db').as_posix()}", f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"):
        with _app(monkeypatch, url) as c:
            assert c.get(f"{API}/health").status_code == 200  # liveness does not depend on the database
            r = c.get(f"{API}/ready")
            assert r.status_code == 503, r.text
            assert not LEAK.search(r.text) and str(tmp_path) not in r.text
            for path in ("/repositories", "/ops/health", "/overview", "/governance/me"):  # F-002: storage problems are 503, never a generic 500
                r2 = c.get(f"{API}{path}")
                assert r2.status_code == 503 and r2.json()["error"]["code"] == "database_unavailable" and not LEAK.search(r2.text), (path, r2.status_code)


def test_corrupted_database_mid_flight_fails_cleanly_and_recovers_after_restore(migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Path(migrated_url.removeprefix("sqlite:///"))
    good = tmp_path / "good.db"
    src, dst = sqlite3.connect(db), sqlite3.connect(good)
    src.backup(dst)
    src.close()
    dst.close()
    with _app(monkeypatch, migrated_url) as c:
        assert c.get(f"{API}/repositories").status_code == 200
        reset_engine()
        db.write_bytes(b"this is not a database" * 100)  # simulate storage corruption under a running process
        bad = c.get(f"{API}/repositories")
        assert bad.status_code == 503 and not LEAK.search(bad.text), bad.text[:300]
        assert c.get(f"{API}/health").status_code == 200
        assert c.get(f"{API}/ready").status_code == 503  # never reports ready on a corrupt database
        reset_engine()
        shutil.copyfile(good, db)  # operator restores a good copy
        assert c.get(f"{API}/ready").status_code == 200
        assert c.get(f"{API}/repositories").status_code == 200


def test_malformed_bodies_never_cause_server_errors(w: World) -> None:  # noqa: F811
    """Fuzz every mutating operation with malformed JSON, wrong JSON types and deeply nested input: 4xx only, no stack traces."""
    bodies = [b"{bad json", b"[]", b"null", b'"str"', b"1e999", b"[" * 5000 + b"]" * 5000, b'{"a":' * 200 + b"1" + b"}" * 200, b"\xff\xfe\x00", b""]
    server_errors = []
    for method, path, _ in operations(w.c):
        if method not in MUTATING or is_public(method, path):
            continue
        url = re.sub(r"\{[^}]+\}", "nope", path)
        for body in bodies:
            r = w.c.request(method, url, content=body, headers={"Authorization": f"Bearer {w.tokens['lead']}", "Content-Type": "application/json"})
            if r.status_code >= 500 or LEAK.search(r.text):
                server_errors.append((method, path, body[:12], r.status_code))
    assert not server_errors, server_errors[:8]


def test_interrupted_backup_and_failed_restore_leave_active_data_untouched(migrated_url: str, tmp_path: Path) -> None:
    from app.release import backup as bk

    db = Path(migrated_url.removeprefix("sqlite:///"))
    before = db.read_bytes()
    b = bk.create_backup(migrated_url, tmp_path / "bk", artifact_root=None)["path"]
    (b / "db.sqlite3").write_bytes(b"corrupt")
    with pytest.raises(bk.BackupError):
        bk.restore(b, tmp_path / "target")
    assert not (tmp_path / "target" / "db.sqlite3").exists()  # nothing half-restored was left behind as a usable database
    assert db.read_bytes() == before
