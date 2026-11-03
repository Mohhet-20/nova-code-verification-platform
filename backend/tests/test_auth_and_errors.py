from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


@pytest.fixture
def secured(migrated_url: str, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "s3cret-token")
    reset_settings_cache()
    with TestClient(create_app()) as c:
        yield c


def test_token_required_when_configured(secured: TestClient) -> None:
    assert secured.get(f"{API}/health").status_code == 200  # liveness stays open
    r = secured.get(f"{API}/repositories")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert secured.get(f"{API}/repositories", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert secured.get(f"{API}/repositories", headers={"Authorization": "Basic s3cret-token"}).status_code == 401
    ok = secured.get(f"{API}/repositories", headers={"Authorization": "Bearer s3cret-token"})
    assert ok.status_code == 200
    assert secured.get(f"{API}/workspace", headers={"Authorization": "Bearer s3cret-token"}).json()[
        "auth_mode"] == "bearer-token"


def test_writes_are_blocked_without_token(secured: TestClient, tmp_path: Path) -> None:
    r = secured.post(f"{API}/repositories", json={"name": "x", "root_path": str(tmp_path)})
    assert r.status_code == 401
    assert secured.post(f"{API}/tasks", json={}).status_code == 401


def test_viewer_role_cannot_write(client: TestClient, tmp_path: Path) -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership

    client.get(f"{API}/workspace")  # bootstraps the local identity
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()
    assert client.get(f"{API}/repositories").status_code == 200
    r = client.post(f"{API}/repositories", json={"name": "x", "root_path": str(tmp_path)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert client.post(f"{API}/tasks", json={}).status_code in (403, 422)


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    r = client.get(f"{API}/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    r = client.post(f"{API}/repositories", json={"name": "SENSITIVE-VALUE-123", "root_path": 5})
    body = r.text
    assert r.status_code == 422 and "SENSITIVE-VALUE-123" not in body
    assert r.json()["error"]["details"][0]["field"] == "root_path"


def test_unhandled_error_hides_internals(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import workspace

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /home/x")

    monkeypatch.setattr(workspace, "limits_from", boom)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    r = quiet.get(f"{API}/workspace")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in r.text


def test_logs_do_not_contain_request_bodies(client: TestClient, tmp_path: Path,
                                            caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    client.post(f"{API}/repositories", json={"name": "LOGSECRET", "root_path": str(tmp_path)})
    assert "LOGSECRET" not in caplog.text
