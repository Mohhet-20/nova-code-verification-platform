"""Public-demo guest sessions: off by default, engineer-only, never the owner, throttled."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache


@pytest.fixture
def guest_client(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NOVA_GUEST_ACCESS_ENABLED", "true")
    monkeypatch.setenv("NOVA_API_TOKEN", "owner-secret-token-for-tests-only")
    reset_settings_cache()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_settings_cache()


def test_guest_access_is_off_by_default(client: TestClient) -> None:
    assert client.post("/api/v1/auth/guest").status_code == 404


def test_guest_session_is_an_engineer_not_the_owner(guest_client: TestClient) -> None:
    res = guest_client.post("/api/v1/auth/guest")
    assert res.status_code == 200
    body = res.json()
    assert body["token"].startswith("nova_u_") and body["role"] == "engineer"
    me = guest_client.get("/api/v1/governance/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me["handle"] == "guest" and me["org_role"] == "engineer" and me["auth_kind"] == "user_token"


def test_guest_cannot_administer_members(guest_client: TestClient) -> None:
    token = guest_client.post("/api/v1/auth/guest").json()["token"]
    res = guest_client.post("/api/v1/org/members", json={"handle": "mallory", "display_name": "M", "role": "owner"},
                            headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


def test_without_a_token_the_api_still_refuses(guest_client: TestClient) -> None:
    assert guest_client.get("/api/v1/repositories").status_code == 401


def test_each_session_gets_its_own_token_and_reuses_the_guest(guest_client: TestClient) -> None:
    a = guest_client.post("/api/v1/auth/guest").json()["token"]
    b = guest_client.post("/api/v1/auth/guest").json()["token"]
    assert a != b
    members = guest_client.get("/api/v1/org/members", headers={"Authorization": "Bearer owner-secret-token-for-tests-only"}).json()
    assert sum(1 for m in members["items"] if m.get("handle") == "guest") == 1


def test_guest_sessions_are_throttled_per_address(guest_client: TestClient) -> None:
    codes = [guest_client.post("/api/v1/auth/guest").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429
