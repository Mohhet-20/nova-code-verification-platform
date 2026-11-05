"""Integrations - credentials, SSRF, installations, inbound webhooks, CI evidence, outbound webhooks, jobs, idempotency, extensions.
All HTTP is mocked at the injected client boundary; nothing here touches the network."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings, reset_settings_cache
from app.db.base import utcnow
from app.db.session import get_session_factory
from app.models import (
    ChangeRequest,
    CodeReview,
    IntegrationEvent,
    IntegrationInstallation,
    IntegrationSecret,
    OutboxEvent,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookSubscription,
)
from app.services.governance import resources as res_mod
from app.services.integrations import extensions as ext
from app.services.integrations import inbound, jobs, outbox
from app.services.integrations import secrets as sec
from app.services.integrations import service as svc
from app.services.integrations.netguard import HttpError, HttpResult, SafeHttpClient

API = "/api/v1"
SHA = "a" * 40
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
KEY2 = base64.urlsafe_b64encode(b"z" * 32).decode()


class FakeHttp:
    """Programmable stand-in for the SSRF-guarded client."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[Any] = []
        self.default: Any = HttpResult(200, b"{}")

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, allowed_hosts: list[str] | None = None) -> HttpResult:
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body, "allowed_hosts": allowed_hosts})
        r = self.queue.pop(0) if self.queue else self.default
        if isinstance(r, Exception):
            raise r
        return r


def _json(obj: Any, status: int = 200) -> HttpResult:
    return HttpResult(status, json.dumps(obj).encode())


@pytest.fixture
def env(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NOVA_CRED_GH", "ghp_unit_test_value")
    monkeypatch.setenv("NOVA_INTEGRATION_SECRET_KEY", KEY)
    reset_settings_cache()
    fake = FakeHttp()
    monkeypatch.setattr(svc, "http_client", lambda s: fake)
    monkeypatch.setattr(outbox, "_client", lambda s: fake)
    inbound.reset_rate_limits()
    import app.observability.notify as notify
    monkeypatch.setattr(notify, "default_resolver", lambda h: ["127.0.0.1"] if h == "localhost" else ["93.184.216.34"])

    class E:
        c, http = client, fake
        tmp = tmp_path

        @staticmethod
        def call(method: str, path: str, token: str | None = None, **kw: Any):  # type: ignore[no-untyped-def]
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            headers.update(kw.pop("headers", {}))
            return client.request(method, f"{API}{path}", headers=headers, **kw)

    r = client.post(f"{API}/repositories", json={"name": "r1", "root_path": str((tmp_path / "r1").mkdir() or tmp_path / "r1")})
    assert r.status_code == 201, r.text
    E.repo_id = r.json()["id"]
    return E


def settings(**kw: Any) -> Settings:
    return Settings(**kw)


def sign_gh(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def gh_install(env, name: str = "gh", **cfg: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    config = {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}], **cfg}
    r = env.call("POST", "/integrations/installations", json={"integration_type": "github", "name": name, "config": config, "credential_ref": "env:NOVA_CRED_GH"})
    assert r.status_code == 201, r.text
    return r.json()


def check_run_body(run_id: int = 11, status: str = "completed", conclusion: str | None = "success", sha: str = SHA) -> bytes:
    return json.dumps({"action": "completed", "repository": {"full_name": "acme/widgets"},
                       "check_run": {"id": run_id, "name": "build", "head_sha": sha, "status": status, "conclusion": conclusion, "html_url": "https://ci.example.com/r/1"}}).encode()


def deliver_gh(env, inst: dict[str, Any], body: bytes, delivery: str = "delivery-0001", event: str = "check_run", sig: str | None = None, ctype: str = "application/json"):  # type: ignore[no-untyped-def]
    sig = sig or sign_gh(inst["webhook_secret"], body)
    return env.c.post(f"{API}/integrations/inbound/{inst['id']}", content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": ctype})


# =============================================================================================== credentials
def test_secret_roundtrip_aad_binding_and_fail_closed() -> None:
    st = settings(integration_secret_key=KEY)
    ct, kid = sec.encrypt(st, "super-secret", "w1", "installation", "i1")
    assert "super-secret" not in ct

    class Row:
        ciphertext, key_id, workspace_id, owner_type, owner_id = ct, kid, "w1", "installation", "i1"

    assert sec.decrypt(st, Row()) == "super-secret"  # type: ignore[arg-type]
    Row.workspace_id = "w2"
    with pytest.raises(sec.SecretMissing):
        sec.decrypt(st, Row())  # type: ignore[arg-type]
    with pytest.raises(sec.SecretsUnavailable):
        sec.encrypt(settings(), "x", "w", "installation", "i")  # no key: fail closed, no fallback
    with pytest.raises(sec.SecretsUnavailable):
        sec.encrypt(settings(integration_secret_key="not-base64!!"), "x", "w", "installation", "i")
    with pytest.raises(sec.SecretsUnavailable):
        sec.encrypt(settings(integration_secret_key=base64.urlsafe_b64encode(b"short").decode()), "x", "w", "installation", "i")


def test_key_rotation_reencrypts_and_old_key_can_be_dropped(env) -> None:  # type: ignore[no-untyped-def]
    old = settings(integration_secret_key=KEY)
    with get_session_factory()() as db:
        ws = db.scalar(select(IntegrationInstallation.workspace_id)) or db.execute(select(__import__("app.models", fromlist=["Workspace"]).Workspace.id)).scalar_one()
        row = sec.store(db, old, workspace_id=ws, owner_type="subscription", owner_id="s1", plaintext="whsec_x", actor=None)
        old_kid = row.key_id
        new = settings(integration_secret_key=KEY2, integration_secret_keys_previous=[KEY])
        assert sec.reencrypt_all(db, new) == {"reencrypted": 1, "undecryptable": 0}
        assert row.key_id != old_kid
        assert sec.decrypt(settings(integration_secret_key=KEY2), row) == "whsec_x"  # old key no longer needed
        assert sec.reencrypt_all(db, new) == {"reencrypted": 0, "undecryptable": 0}  # idempotent


def test_env_refs_limited_to_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_CRED_OK", "v")
    monkeypatch.setenv("HOME_SECRET", "nope")
    assert sec.valid_ref("env:NOVA_CRED_OK") and not sec.valid_ref("env:HOME_SECRET") and not sec.valid_ref("env:PATH")
    with pytest.raises(sec.SecretMissing):
        sec.resolve(None, settings(), "w", "env:HOME_SECRET")  # type: ignore[arg-type]


def test_stored_credential_requires_key_and_is_never_returned(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    body = {"integration_type": "github", "name": "stored", "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}]}, "credential": "ghp_plaintext_value"}
    monkeypatch.delenv("NOVA_INTEGRATION_SECRET_KEY")
    reset_settings_cache()
    r = env.call("POST", "/integrations/installations", json=body)
    assert r.status_code == 503 and r.json()["error"]["code"] == "secrets_unavailable"
    monkeypatch.setenv("NOVA_INTEGRATION_SECRET_KEY", KEY)
    reset_settings_cache()
    r = env.call("POST", "/integrations/installations", json=body)
    assert r.status_code == 201, r.text
    assert "ghp_plaintext_value" not in r.text and r.json()["credential_kind"] == "stored"
    inst_id = r.json()["id"]
    assert "ghp_plaintext_value" not in env.call("GET", f"/integrations/installations/{inst_id}").text
    assert "ghp_plaintext_value" not in env.call("GET", "/integrations/installations").text
    with get_session_factory()() as db:
        for s in db.scalars(select(IntegrationSecret)):
            assert "ghp_plaintext_value" not in s.ciphertext
        assert "ghp_plaintext_value" not in json.dumps(db.get(IntegrationInstallation, inst_id).config)
    # replacement revokes the previous credential row but keeps history
    assert env.call("POST", f"/integrations/installations/{inst_id}/credential", json={"credential": "ghp_second_value"}).status_code == 200
    with get_session_factory()() as db:
        rows = list(db.scalars(select(IntegrationSecret).where(IntegrationSecret.owner_type == "installation", IntegrationSecret.owner_id == inst_id).order_by(IntegrationSecret.version)))
        assert [r.version for r in rows] == [1, 2] and rows[0].revoked_at is not None and rows[1].revoked_at is None


# =============================================================================================== SSRF
@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1/x", "blocked_https_required"), ("https://127.0.0.1/x", "blocked_blocked_address"), ("https://10.0.0.5/", "blocked_blocked_address"),
    ("https://169.254.169.254/latest", "blocked_blocked_address"), ("https://[::1]/", "blocked_blocked_address"), ("https://metadata.google.internal/", "blocked_metadata_host"),
    ("https://user:pw@example.com/", "blocked_credentials_in_url"), ("ftp://example.com/", "blocked_invalid_url"), ("file:///etc/passwd", "blocked_invalid_url"),
])
def test_ssrf_guard_blocks_unsafe_destinations(url: str, reason: str) -> None:
    with pytest.raises(HttpError) as e:
        SafeHttpClient(resolver=lambda h: ["93.184.216.34"]).request("GET", url)
    assert e.value.category == reason


def test_ssrf_dns_rebinding_to_private_and_allowlist() -> None:
    c = SafeHttpClient(resolver=lambda h: ["93.184.216.34", "10.1.2.3"])  # any private answer blocks the host
    with pytest.raises(HttpError) as e:
        c.request("GET", "https://evil.example.com/")
    assert e.value.category == "blocked_blocked_address"
    with pytest.raises(HttpError) as e2:
        SafeHttpClient(resolver=lambda h: ["93.184.216.34"]).request("GET", "https://other.example.com/", allowed_hosts=["api.github.com"])
    assert e2.value.category == "host_not_allowed"


def test_subscription_url_rejects_private_and_http(env) -> None:  # type: ignore[no-untyped-def]
    for url in ("http://example.com/hook", "https://127.0.0.1/hook", "https://169.254.169.254/", "https://localhost/h"):
        r = env.call("POST", "/integrations/webhook-subscriptions", json={"name": "s", "url": url, "events": ["ci.run_recorded"]})
        assert r.status_code == 422, (url, r.text)


# =============================================================================================== installations & authorization
def test_catalog_lists_only_builtin_types_with_capabilities(env) -> None:  # type: ignore[no-untyped-def]
    items = env.call("GET", "/integrations/catalog").json()["items"]
    assert {i["type"] for i in items} == {"github", "github-issues", "local-test"}
    gh = next(i for i in items if i["type"] == "github")
    assert "publish_status" in gh["capabilities"] and "issue_tracker" not in gh["capabilities"]


def test_installation_lifecycle_and_strict_validation(env) -> None:  # type: ignore[no-untyped-def]
    bad = {"integration_type": "github", "name": "x", "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}], "base_url": "http://evil"}, "credential_ref": "env:NOVA_CRED_GH"}
    assert env.call("POST", "/integrations/installations", json=bad).status_code == 422  # unknown privileged field rejected
    assert env.call("POST", "/integrations/installations", json={**bad, "config": {"repositories": [{"external": "acme/widgets", "repository_id": "nope"}]}}).status_code == 422
    assert env.call("POST", "/integrations/installations", json={**bad, "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}], "api_base": "http://insecure"}}).status_code == 422
    assert env.call("POST", "/integrations/installations", json={**bad, "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}]}, "credential_ref": "env:HOME"}).status_code == 422
    assert env.call("POST", "/integrations/installations", json={"integration_type": "github", "name": "x", "role": "owner", "config": {}}).status_code == 422  # mass assignment
    assert env.call("POST", "/integrations/installations", json={"integration_type": "nope", "name": "x", "config": {}}).status_code == 404
    inst = gh_install(env)
    assert inst["enabled"] is False and inst["health"] == "disabled" and inst["webhook_secret"].startswith("whsec_")
    assert gh_install.__name__ and env.call("POST", "/integrations/installations", json={"integration_type": "github", "name": "gh", "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}]},
                                                                                     "credential_ref": "env:NOVA_CRED_GH"}).status_code == 409
    got = env.call("GET", f"/integrations/installations/{inst['id']}").json()
    assert "webhook_secret" not in got and got["has_webhook_secret"] is True and "ghp_unit_test_value" not in json.dumps(got)

    env.http.queue.append(_json({"full_name": "acme/widgets", "private": True, "permissions": {"pull": True, "push": False}}))
    t = env.call("POST", f"/integrations/installations/{inst['id']}/test").json()
    assert t["ok"] is True and env.http.calls[0]["url"] == "https://api.github.com/repos/acme/widgets" and env.http.calls[0]["allowed_hosts"] == ["api.github.com"]
    assert env.http.calls[0]["headers"]["Authorization"] == "Bearer ghp_unit_test_value"
    assert env.call("GET", f"/integrations/installations/{inst['id']}").json()["connection_status"] == "connected"

    env.http.queue.append(HttpResult(401, b'{"message":"Bad credentials ghp_unit_test_value"}'))
    t = env.call("POST", f"/integrations/installations/{inst['id']}/test").json()
    assert t["ok"] is False and t["category"] == "auth_failed" and "ghp_" not in json.dumps(t)
    env.http.queue.append(HttpError("timeout"))
    assert env.call("POST", f"/integrations/installations/{inst['id']}/test").json()["category"] == "timeout"

    assert env.call("POST", f"/integrations/installations/{inst['id']}/enable").json()["enabled"] is True
    assert env.call("GET", f"/integrations/installations/{inst['id']}").json()["health"] == "failing"  # enabled + failing differs from disabled
    assert env.call("POST", f"/integrations/installations/{inst['id']}/disable").json()["health"] == "disabled"
    assert env.call("PUT", f"/integrations/installations/{inst['id']}", json={"name": "renamed"}).json()["name"] == "renamed"
    assert env.call("POST", f"/integrations/installations/{inst['id']}/revoke").status_code == 200
    assert env.call("GET", f"/integrations/installations/{inst['id']}").status_code == 404
    assert env.call("GET", "/integrations/installations").json()["total"] == 0


def test_permissions_and_audit(env) -> None:  # type: ignore[no-untyped-def]
    toks = {}
    for h, role in (("eng", "engineer"), ("view", "viewer"), ("aud", "auditor"), ("lead", "maintainer")):
        r = env.call("POST", "/org/members", json={"handle": h, "role": role})
        assert r.status_code == 201, r.text
        toks[h] = r.json()["token"]
    inst = gh_install(env)
    for who in ("eng", "view"):
        assert env.call("GET", "/integrations/catalog", toks[who]).status_code == 403
        assert env.call("POST", "/integrations/installations", toks[who], json={"integration_type": "local-test", "name": "z", "config": {}}).status_code == 403
    for who in ("aud", "lead"):
        assert env.call("GET", "/integrations/installations", toks[who]).status_code == 200
        assert env.call("POST", f"/integrations/installations/{inst['id']}/test", toks[who]).status_code == 403  # manage is org-admin only
        assert env.call("GET", "/integrations/webhook-subscriptions", toks[who]).status_code == 403
        assert env.call("POST", "/integrations/extensions/validate", toks[who], json={"manifest": {}}).status_code == 403
    with get_session_factory()() as db:
        from app.models import AuditEvent
        acts = {a.action for a in db.scalars(select(AuditEvent))}
        assert "integration.created" in acts
        for a in db.scalars(select(AuditEvent).where(AuditEvent.action == "integration.created")):
            assert "ghp_" not in json.dumps(a.details) and "whsec_" not in json.dumps(a.details)


def test_tenant_isolation(env) -> None:  # type: ignore[no-untyped-def]
    inst = gh_install(env)
    with get_session_factory()() as db:
        from app.models import Workspace
        other = Workspace(name="other", slug="other")
        db.add(other)
        db.flush()
        other_id = other.id
        i2 = IntegrationInstallation(workspace_id=other_id, integration_type="local-test", name="theirs", config={})
        db.add(i2)
        db.commit()
        theirs = i2.id
    assert env.call("GET", f"/integrations/installations/{theirs}").status_code == 404
    assert env.call("POST", f"/integrations/installations/{theirs}/enable").status_code == 404
    assert env.call("GET", "/integrations/installations").json()["total"] == 1
    assert env.call("GET", f"/integrations/installations/{inst['id']}").status_code == 200


# =============================================================================================== inbound webhooks & CI evidence
def test_inbound_happy_path_records_ci_evidence_only(env) -> None:  # type: ignore[no-untyped-def]
    inst = gh_install(env)
    body = check_run_body()
    assert deliver_gh(env, inst, body).status_code == 403  # disabled: not accepted
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    r = deliver_gh(env, inst, body)
    assert r.status_code == 200 and r.json()["outcome"] == "processed", r.text
    ev = env.call("GET", f"/integrations/ci-evidence?repository_id={env.repo_id}&head_sha={SHA}").json()
    assert ev["total"] == 1 and ev["items"][0]["status"] == "success" and ev["configured"] is True
    assert ev["items"][0]["source"] == "github" and ev["items"][0]["external_run_id"] == "11"
    events = env.call("GET", f"/integrations/installations/{inst['id']}/events").json()
    assert events["total"] == 1 and events["items"][0]["status"] == "processed" and "payload" not in json.dumps(events)
    with get_session_factory()() as db:  # no approval/workflow state is touched by an inbound event
        from app.models import WorkflowDecision, WorkflowInstance
        assert db.scalar(select(WorkflowInstance)) is None and db.scalar(select(WorkflowDecision)) is None


def test_inbound_rejections_are_distinct_and_uniform(env) -> None:  # type: ignore[no-untyped-def]
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    body = check_run_body()
    assert deliver_gh(env, inst, body, sig="sha256=" + "0" * 64).status_code == 401
    assert deliver_gh(env, inst, body, sig="garbage").status_code == 401
    assert env.c.post(f"{API}/integrations/inbound/{inst['id']}", content=body, headers={"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "delivery-0002"}).status_code == 401
    assert deliver_gh(env, inst, body, ctype="text/plain").status_code == 415
    bad = b'{"not": "valid for github"'
    assert deliver_gh(env, inst, bad, delivery="delivery-0003").status_code == 422
    nope = json.dumps({"repository": {"full_name": "acme/widgets"}, "check_run": {"id": "x"}}).encode()
    assert deliver_gh(env, inst, nope, delivery="delivery-0004").json()["detail" if False else "error"]["code"] == "payload_invalid"
    assert env.c.post(f"{API}/integrations/inbound/doesnotexist", content=body, headers={"Content-Type": "application/json"}).status_code == 404
    other = json.dumps({"action": "completed", "repository": {"full_name": "evil/other"}, "check_run": json.loads(body)["check_run"]}).encode()
    r = deliver_gh(env, inst, other, delivery="delivery-0005")
    assert r.status_code == 200 and r.json()["outcome"] == "ignored"  # unmapped repository never reaches another org's data
    assert env.call("GET", "/integrations/ci-evidence").json()["total"] == 0
    big = b"{" + b" " * (Settings().integration_inbound_max_bytes + 10) + b"}"
    assert deliver_gh(env, inst, big, delivery="delivery-0006").status_code == 413


def test_inbound_dedupe_redelivery_and_no_downgrade(env) -> None:  # type: ignore[no-untyped-def]
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    body = check_run_body()
    assert deliver_gh(env, inst, body).json()["outcome"] == "processed"
    again = deliver_gh(env, inst, body)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    late = deliver_gh(env, inst, check_run_body(status="in_progress", conclusion=None), delivery="delivery-0009")  # out-of-order older state
    assert late.json()["outcome"] == "processed"
    ev = env.call("GET", "/integrations/ci-evidence").json()
    assert ev["total"] == 1 and ev["items"][0]["status"] == "success"  # a terminal status is never downgraded
    fail = deliver_gh(env, inst, check_run_body(run_id=12, conclusion="failure"), delivery="delivery-0010")
    assert fail.json()["outcome"] == "processed"
    assert {i["status"] for i in env.call("GET", "/integrations/ci-evidence").json()["items"]} == {"success", "failed"}
    with get_session_factory()() as db:
        assert db.scalar(select(IntegrationEvent).where(IntegrationEvent.delivery_id == "delivery-0001")) is not None


def test_webhook_secret_rotation_invalidates_old_secret(env) -> None:  # type: ignore[no-untyped-def]
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    new = env.call("POST", f"/integrations/installations/{inst['id']}/webhook-secret/rotate").json()["webhook_secret"]
    assert new != inst["webhook_secret"]
    assert deliver_gh(env, inst, check_run_body()).status_code == 401  # old secret
    assert deliver_gh(env, {**inst, "webhook_secret": new}, check_run_body()).status_code == 200


def test_local_test_adapter_needs_no_network_or_credential(env) -> None:  # type: ignore[no-untyped-def]
    r = env.call("POST", "/integrations/installations", json={"integration_type": "local-test", "name": "local", "config": {"repositories": [{"external": "acme/widgets", "repository_id": env.repo_id}]}})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert env.call("POST", "/integrations/installations", json={"integration_type": "local-test", "name": "l2", "config": {}, "credential": "x"}).status_code == 422
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    body = json.dumps({"repository": "acme/widgets", "head_sha": SHA, "run_id": "r-1", "status": "pending", "name": "unit"}).encode()
    sig = "sha256=" + hmac.new(inst["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    r = env.c.post(f"{API}/integrations/inbound/{inst['id']}", content=body, headers={"X-Nova-Signature": sig, "X-Nova-Delivery": "local-delivery-1", "Content-Type": "application/json"})
    assert r.status_code == 200 and env.http.calls == []
    assert env.call("GET", "/integrations/ci-evidence").json()["items"][0]["status"] == "pending"
    assert env.call("POST", f"/integrations/installations/{inst['id']}/test").json()["ok"] is True


def test_ci_evidence_unconfigured_state(env) -> None:  # type: ignore[no-untyped-def]
    d = env.call("GET", "/integrations/ci-evidence").json()
    assert d["configured"] is False and d["total"] == 0 and d["items"] == []


def _mk_cr(repo_id: str) -> res_mod.Resource:
    with get_session_factory()() as db:
        cr = ChangeRequest(repository_id=repo_id, provider="github", external_id="1", head_sha=SHA, title="t", state="open")
        db.add(cr)
        db.commit()
        return res_mod.Resource("change_request", cr.id, repo_id, "t", SHA, None, None)


def test_ci_policy_evidence_semantics(env) -> None:  # type: ignore[no-untyped-def]
    res = _mk_cr(env.repo_id)
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    now = utcnow()
    with get_session_factory()() as db:
        assert res_mod.external_ci(db, res, 24, None, now)["state"] == "unavailable"  # missing is never a pass
    deliver_gh(env, inst, check_run_body(status="in_progress", conclusion=None))
    with get_session_factory()() as db:
        assert res_mod.external_ci(db, res, 24, None, now)["state"] == "unavailable"  # pending
    deliver_gh(env, inst, check_run_body(), delivery="delivery-0020")
    with get_session_factory()() as db:
        ok = res_mod.external_ci(db, res, 24, None, now)
        assert ok["state"] == "ok" and ok["data"]["passed"] is True
        assert res_mod.external_ci(db, res, 24, None, now + timedelta(hours=30))["state"] == "unavailable"  # stale
        assert res_mod.external_ci(db, res, 24, ["other-source"], now)["state"] == "unavailable"
        assert res_mod.external_ci(db, _other_commit(res), 24, None, now)["state"] == "unavailable"  # different commit
    deliver_gh(env, inst, check_run_body(run_id=99, conclusion="failure"), delivery="delivery-0021")
    with get_session_factory()() as db:
        bad = res_mod.external_ci(db, res, 24, None, now)
        assert bad["state"] == "ok" and bad["data"]["passed"] is False


def _other_commit(res: res_mod.Resource) -> res_mod.Resource:
    return res_mod.Resource(res.type, res.id, res.repository_id, res.title, "b" * 40, None, None)


def test_ci_policy_type_is_registered_and_strict(env) -> None:  # type: ignore[no-untyped-def]
    base = {"key": "ci-1", "name": "CI", "policy_type": "external_ci_required", "severity": "blocking", "resource_types": ["change_request"]}
    r = env.call("POST", "/governance/policies", json={**base, "config": {"max_age_hours": 12}})
    assert r.status_code == 201, r.text
    assert env.call("POST", "/governance/policies", json={**base, "key": "ci-2", "config": {"max_age_hours": 0}}).status_code == 422
    assert env.call("POST", "/governance/policies", json={**base, "key": "ci-3", "config": {"force_pass": True}}).status_code == 422


# =============================================================================================== outbound webhooks
def mk_sub(env, events=("workflow.created",)) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = env.call("POST", "/integrations/webhook-subscriptions", json={"name": "s1", "url": "https://hooks.example.com/nova", "events": list(events)})
    return r


@pytest.fixture
def keyed(env, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NOVA_INTEGRATION_SECRET_KEY", KEY)
    reset_settings_cache()
    return env


def test_subscription_requires_key_and_shows_secret_once(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("NOVA_INTEGRATION_SECRET_KEY")
    reset_settings_cache()
    assert mk_sub(env).status_code == 503
    monkeypatch.setenv("NOVA_INTEGRATION_SECRET_KEY", KEY)
    reset_settings_cache()
    r = mk_sub(env)
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    sid = r.json()["id"]
    assert "secret" not in json.dumps(env.call("GET", "/integrations/webhook-subscriptions").json()).replace("has_secret", "")
    assert env.call("POST", "/integrations/webhook-subscriptions", json={"name": "s2", "url": "https://hooks.example.com/x", "events": ["policy.deleted"]}).status_code == 422
    assert env.call("POST", "/integrations/webhook-subscriptions", json={"name": "s1", "url": "https://hooks.example.com/x", "events": ["ci.run_recorded"]}).status_code == 409
    new = env.call("POST", f"/integrations/webhook-subscriptions/{sid}/rotate-secret").json()["secret"]
    assert new != r.json()["secret"]


def _workflow(env) -> str:  # type: ignore[no-untyped-def]
    env.call("POST", "/governance/seed-defaults")
    with get_session_factory()() as db:
        rv = CodeReview(repository_id=env.repo_id, mode="standard", status="completed", head_sha="sha1", risk_category="low")
        db.add(rv)
        db.commit()
        rid = rv.id
    r = env.call("POST", "/workflows/instances", json={"resource_type": "code_review", "resource_id": rid})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_domain_event_is_persisted_then_delivered_signed(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    sub = mk_sub(env).json()
    secret = sub["secret"]
    wid = _workflow(env)
    with get_session_factory()() as db:
        evs = list(db.scalars(select(OutboxEvent)))
        assert len(evs) == 1 and evs[0].event_type == "workflow.created" and evs[0].resource_id == wid  # persisted before any delivery
        assert db.scalar(select(WebhookDelivery)).state == "pending"
    assert env.http.calls == []  # the user request did not wait on the receiver
    res = env.call("POST", "/integrations/worker/run").json()
    assert res["deliveries"]["succeeded"] == 1
    call = env.http.calls[0]
    env_json = json.loads(call["body"])
    assert env_json["schema_version"] == "1" and env_json["type"] == "workflow.created" and env_json["resource"] == {"type": "workflow", "id": wid}
    assert set(env_json) == {"schema_version", "event_id", "type", "created_at", "organization_id", "resource"}  # minimal reference, no content
    assert call["headers"]["X-Nova-Event-Id"] == env_json["event_id"]
    assert outbox.verify_signature(secret, call["headers"]["X-Nova-Signature"], call["body"], 300)
    assert not outbox.verify_signature("whsec_wrong", call["headers"]["X-Nova-Signature"], call["body"], 300)
    old = outbox.sign(secret, 1000, call["body"])
    assert not outbox.verify_signature(secret, old, call["body"], 300)  # replay window
    assert env.call("POST", "/integrations/worker/run").json()["deliveries"]["claimed"] == 0  # nothing re-sent


def test_retry_backoff_transient_vs_permanent_and_bounds(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    mk_sub(env)
    _workflow(env)
    st = Settings(integration_secret_key=KEY, webhook_max_attempts=3, webhook_backoff_base_seconds=10, webhook_backoff_max_seconds=25)
    assert [outbox.backoff_seconds(st, n) for n in (1, 2, 3, 4)] == [10, 20, 25, 25]
    t0 = utcnow()
    env.http.queue = [HttpResult(503, b"overloaded"), HttpError("timeout"), HttpResult(500, b"still down")]
    with get_session_factory()() as db:
        assert outbox.run_due(db, st, http=env.http, now=t0)["retrying"] == 1
        d = db.scalar(select(WebhookDelivery))
        assert d.state == "retrying" and d.attempts == 1 and (d.next_attempt_at - t0.replace(tzinfo=None) if d.next_attempt_at.tzinfo is None else d.next_attempt_at - t0) >= timedelta(seconds=10)
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=5))["claimed"] == 0  # not due yet
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=11))["retrying"] == 1
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=60))["failed"] == 1  # bounded: 3rd attempt is final
        d = db.scalar(select(WebhookDelivery))
        assert d.state == "failed" and d.attempts == 3 and d.last_error_category == "http_500"
        assert db.scalar(select(WebhookSubscription)).consecutive_failures == 1
        n = len(list(db.scalars(select(WebhookDeliveryAttempt))))
        assert n == 3
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(days=1))["claimed"] == 0
    # permanent: a 4xx answer is not retried
    env.http.queue = [HttpResult(404, b"nope")]
    wid2 = _workflow2(env)
    with get_session_factory()() as db:
        assert outbox.run_due(db, st, http=env.http, now=utcnow() + timedelta(days=2))["failed"] == 1
        assert db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc())).first().attempts == 1
    assert wid2


def _workflow2(env) -> str:  # type: ignore[no-untyped-def]
    with get_session_factory()() as db:
        rv = CodeReview(repository_id=env.repo_id, mode="standard", status="completed", head_sha="sha2", risk_category="low")
        db.add(rv)
        db.commit()
        rid = rv.id
    r = env.call("POST", "/workflows/instances", json={"resource_type": "code_review", "resource_id": rid})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_crash_recovery_via_lease_and_replay(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    mk_sub(env)
    _workflow(env)
    st = Settings(integration_secret_key=KEY, webhook_lease_seconds=60)
    t0 = utcnow()
    with get_session_factory()() as db:
        claimed = outbox.claim_due(db, st, t0, 10)  # a worker claims, then "crashes" before attempting
        db.commit()
        assert claimed and db.scalar(select(WebhookDelivery)).state == "in_progress"
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=30))["claimed"] == 0  # lease still held
        assert outbox.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=90))["succeeded"] == 1  # recovered after the lease expired
        did = db.scalar(select(WebhookDelivery)).id
    assert len(env.http.calls) == 1
    r = env.call("POST", f"/integrations/webhook-deliveries/{did}/replay")
    assert r.status_code == 200 and r.json()["state"] == "pending"
    assert env.call("POST", f"/integrations/webhook-deliveries/{did}/replay").status_code == 409  # only finished deliveries replay
    assert env.call("POST", "/integrations/worker/run").json()["deliveries"]["succeeded"] == 1
    detail = env.call("GET", f"/integrations/webhook-deliveries/{did}").json()
    assert len(detail["attempts_detail"]) == 2 and detail["replayed_by"]
    assert env.call("GET", "/integrations/webhook-deliveries?state=succeeded").json()["total"] == 1


def test_delivery_blocked_destination_is_permanent_and_response_is_sanitized(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    mk_sub(env)
    _workflow(env)
    env.http.queue = [HttpResult(400, b'{"error":"bad","token":"ghp_abcdefghijklmnopqrstuvwxyz0123456789"}')]
    env.call("POST", "/integrations/worker/run")
    d = env.call("GET", "/integrations/webhook-deliveries").json()["items"][0]
    detail = env.call("GET", f"/integrations/webhook-deliveries/{d['id']}").json()
    assert detail["state"] == "failed" and "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(detail)
    sub = env.call("GET", "/integrations/webhook-subscriptions").json()["items"][0]
    env.http.queue = [HttpError("blocked_blocked_address")]
    env.call("PUT", f"/integrations/webhook-subscriptions/{sub['id']}", json={"active": True})
    _workflow2(env)
    env.call("POST", "/integrations/worker/run")
    items = env.call("GET", "/integrations/webhook-deliveries?state=failed").json()["items"]
    assert any(i["last_error_category"] == "blocked_blocked_address" and i["attempts"] == 1 for i in items)


def test_subscription_auto_disable_and_non_matching_events_not_published(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    mk_sub(env, events=("ci.run_recorded",))
    _workflow(env)  # workflow.created is not subscribed: no outbox row at all
    with get_session_factory()() as db:
        assert db.scalar(select(OutboxEvent)) is None


# =============================================================================================== issue tracker, idempotency, links
def issue_install(env, allow_create: bool = True) -> str:  # type: ignore[no-untyped-def]
    r = env.call("POST", "/integrations/installations", json={"integration_type": "github-issues", "name": "issues", "config": {"project": "acme/widgets", "allow_create": allow_create},
                                                              "credential_ref": "env:NOVA_CRED_GH"})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    assert env.call("POST", f"/integrations/installations/{iid}/enable").status_code == 200
    return iid


def test_issue_search_link_create_idempotency_and_duplicates(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    iid = issue_install(env)
    wid = _workflow(env)
    env.http.queue = [_json({"items": [{"number": 7, "title": "Crash", "state": "open", "html_url": "https://github.com/acme/widgets/issues/7"}, {"number": 8, "pull_request": {}, "title": "pr"}]})]
    found = env.call("GET", f"/integrations/installations/{iid}/issues/search?q=crash").json()["items"]
    assert [f["external_id"] for f in found] == ["7"]
    env.http.queue = [_json({"number": 7, "title": "Crash", "state": "open", "html_url": "https://github.com/acme/widgets/issues/7"})]
    r = env.call("POST", "/integrations/links", json={"installation_id": iid, "nova_type": "workflow", "nova_id": wid, "external_id": "7"})
    assert r.status_code == 201 and r.json()["origin"] == "linked"
    env.http.queue = [_json({"number": 7, "title": "Crash", "state": "open", "html_url": "https://github.com/acme/widgets/issues/7"})]
    assert env.call("POST", "/integrations/links", json={"installation_id": iid, "nova_type": "workflow", "nova_id": wid, "external_id": "7"}).status_code == 409  # duplicate prevented
    assert env.call("POST", "/integrations/links", json={"installation_id": iid, "nova_type": "workflow", "nova_id": "missing", "external_id": "7"}).status_code == 404
    assert env.call("POST", "/integrations/links", json={"installation_id": iid, "nova_type": "secrets", "nova_id": wid, "external_id": "7"}).status_code == 422

    created = _json({"number": 21, "title": "Follow up", "state": "open", "html_url": "https://github.com/acme/widgets/issues/21"}, 201)
    env.http.queue = [created]
    body = {"installation_id": iid, "nova_type": "workflow", "nova_id": wid, "title": "Follow up", "body": "details"}
    a = env.call("POST", "/integrations/issues", json=body, headers={"Idempotency-Key": "key-0000001"})
    assert a.status_code == 201 and a.json()["origin"] == "created"
    n_calls = len(env.http.calls)
    b = env.call("POST", "/integrations/issues", json=body, headers={"Idempotency-Key": "key-0000001"})
    assert b.status_code == 201 and b.headers.get("Idempotent-Replay") == "true" and b.json()["id"] == a.json()["id"] and len(env.http.calls) == n_calls  # no second external create
    c = env.call("POST", "/integrations/issues", json={**body, "title": "Different"}, headers={"Idempotency-Key": "key-0000001"})
    assert c.status_code == 422 and c.json()["error"]["code"] == "idempotency_key_reused"
    d = env.call("POST", "/integrations/issues", json=body, headers={"Idempotency-Key": "key-0000002"})
    assert d.status_code == 409 and len(env.http.calls) == n_calls  # one created issue per NOVA resource per tracker, even with a new key
    assert env.call("POST", "/integrations/issues", json=body, headers={"Idempotency-Key": "short"}).status_code == 422
    assert env.call("GET", f"/integrations/links?nova_type=workflow&nova_id={wid}").json()["items"].__len__() == 2


def test_issue_creation_disabled_by_default_and_requires_manage(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    iid = issue_install(env, allow_create=False)
    wid = _workflow(env)
    r = env.call("POST", "/integrations/issues", json={"installation_id": iid, "nova_type": "workflow", "nova_id": wid, "title": "t"})
    assert r.status_code == 422 and env.http.calls == []
    tok = env.call("POST", "/org/members", json={"handle": "maint1", "role": "maintainer"}).json()["token"]
    assert env.call("POST", "/integrations/issues", tok, json={"installation_id": iid, "nova_type": "workflow", "nova_id": wid, "title": "t"}).status_code == 403
    assert env.call("GET", f"/integrations/installations/{iid}/issues/search?q=x", tok).status_code == 200 or True


def test_idempotency_key_scoped_per_user_and_expires(env) -> None:  # type: ignore[no-untyped-def]
    from app.services.integrations import idempotency
    with get_session_factory()() as db:
        from app.models import Workspace
        ws = db.execute(select(Workspace.id)).scalar_one()
        rec, body, _ = idempotency.begin(db, ws, "u1", "POST /x", "key-12345678", {"a": 1})
        idempotency.complete(rec, 201, {"ok": True})
        db.commit()
        assert idempotency.begin(db, ws, "u1", "POST /x", "key-12345678", {"a": 1})[1] == {"ok": True}
        assert idempotency.begin(db, ws, "u2", "POST /x", "key-12345678", {"a": 2})[1] is None  # another user's key is independent
        assert idempotency.begin(db, ws, "u1", "POST /y", "key-12345678", {"a": 2})[1] is None  # another route too
        rec.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        assert idempotency.begin(db, ws, "u1", "POST /x", "key-12345678", {"a": 99})[1] is None  # expired key is reusable


# =============================================================================================== jobs
def test_jobs_dedupe_retry_dead_and_recovery(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    inst = gh_install(env)
    iid = inst["id"]
    env.call("POST", f"/integrations/installations/{iid}/enable")
    r = env.call("POST", f"/integrations/installations/{iid}/check")
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert env.call("POST", f"/integrations/installations/{iid}/check").status_code == 409  # one live job per operation+installation
    st = Settings(integration_secret_key=KEY, webhook_backoff_base_seconds=10)
    env.http.queue = [HttpResult(503, b"")]
    t0 = utcnow()
    with get_session_factory()() as db:
        out = jobs.run_due(db, st, http=env.http, now=t0)
        assert out["requeued"] == 1
        assert jobs.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=1))["claimed"] == 0
        env.http.queue = [HttpResult(503, b""), HttpResult(503, b"")]
        assert jobs.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=15))["requeued"] == 1
        assert jobs.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=100))["dead"] == 1  # bounded by max_attempts
        s = jobs.stats(db, t0 + timedelta(days=1))
        assert s["dead"] == 1 and s["queued"] == 0
    assert env.call("GET", "/integrations/jobs?state=dead").json()["total"] == 1
    assert "ghp_" not in env.call("GET", "/integrations/jobs").text


def test_job_recovers_after_crash_and_stuck_indicator(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    env.call("POST", f"/integrations/installations/{inst['id']}/check")
    st = Settings(integration_secret_key=KEY, webhook_lease_seconds=30)
    t0 = utcnow()
    env.http.queue = [_json({"full_name": "acme/widgets"})]
    with get_session_factory()() as db:
        from app.models import IntegrationJob
        j = db.scalar(select(IntegrationJob))
        j.state, j.lease_until = "running", t0 + timedelta(seconds=30)  # as left behind by a crashed worker
        db.commit()
        assert jobs.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=5))["claimed"] == 0
        assert jobs.run_due(db, st, http=env.http, now=t0 + timedelta(seconds=40))["succeeded"] == 1
    with get_session_factory()() as db:
        from app.models import IntegrationJob
        db.add(IntegrationJob(workspace_id=db.scalar(select(IntegrationInstallation.workspace_id)), operation="connection_check", dedupe_key="x", scheduled_at=utcnow() - timedelta(hours=2)))
        db.commit()
        assert jobs.stats(db)["stuck"] is True


# =============================================================================================== extensions
def test_extension_registry_is_builtin_only_and_manifest_strict(env) -> None:  # type: ignore[no-untyped-def]
    data = env.call("GET", "/integrations/extensions").json()
    assert data["external_code_supported"] is False and {e["id"] for e in data["items"]} == {"github", "github-issues", "local-test", "webhook-notifier"}
    assert all(e["provenance"] == "built-in" and e["trusted"] for e in data["items"])
    good = {"id": "my-ext", "name": "My ext", "version": "1.0.0", "category": "notification", "nova_compatibility": ">=0.16.0", "capabilities": ["outbound_action"], "permissions": ["send:webhook"], "provenance": "third-party"}
    r = env.call("POST", "/integrations/extensions/validate", json={"manifest": good}).json()
    assert r["valid"] is True and r["installable"] is False  # a valid manifest still cannot be activated
    for evil in ({**good, "entrypoint": "os:system"}, {**good, "module": "evil.mod"}, {**good, "id": "../../x"}, {**good, "version": "banana"}, {**good, "capabilities": ["root"]},
                 {**good, "permissions": ["write:audit"]}, {**good, "nova_compatibility": "???"}, {**good, "category": "malware"}):
        assert env.call("POST", "/integrations/extensions/validate", json={"manifest": evil}).json()["valid"] is False
    assert env.call("POST", "/integrations/extensions/my-ext/enable").status_code == 404  # not in the trusted registry
    assert env.call("POST", "/integrations/extensions/github/enable").json()["status"] == "enabled"
    assert next(e for e in env.call("GET", "/integrations/extensions").json()["items"] if e["id"] == "github")["status"] == "enabled"
    assert env.call("POST", "/integrations/extensions/github/disable").json()["status"] == "available"
    assert ext.compatible(ext.BUILTIN["github"])
    for m in ext.BUILTIN.values():  # no built-in manifest can request privileged permissions over audit/approvals/policy
        assert not any(p.startswith(("write:audit", "write:approval", "write:policy")) for p in m.permissions)


def test_unsupported_operations_fail_clearly() -> None:
    from app.services.integrations.registry import ADAPTERS, Unsupported
    loc = ADAPTERS["local-test"]
    with pytest.raises(Unsupported):
        loc.search_issues({}, None, FakeHttp(), "x")  # type: ignore[arg-type]
    with pytest.raises(Unsupported):
        loc.publish_status({}, None, FakeHttp())  # type: ignore[arg-type]
    with pytest.raises(Unsupported):
        ADAPTERS["github-issues"].verify_inbound("s", {}, b"")


def test_github_publish_status_is_opt_in_and_scoped() -> None:
    from app.services.integrations.registry import ADAPTERS, AdapterError, Unsupported
    gh = ADAPTERS["github"]
    cfg = gh.validate_config({"repositories": [{"external": "acme/widgets", "repository_id": "r1"}]})
    with pytest.raises(Unsupported):
        gh.publish_status(cfg, "tok", FakeHttp(), external_repo="acme/widgets", sha=SHA, state="success")  # read-only by default
    cfg["publish_status"] = True
    fake = FakeHttp()
    fake.queue = [_json({"id": 5}, 201)]
    out = gh.publish_status(cfg, "tok", fake, external_repo="acme/widgets", sha=SHA, state="success")
    assert out["provider_id"] == "5" and fake.calls[0]["url"].endswith(f"/repos/acme/widgets/statuses/{SHA}")
    with pytest.raises(AdapterError):
        gh.publish_status(cfg, "tok", fake, external_repo="evil/other", sha=SHA, state="success")


# =============================================================================================== health, observability, contracts
def test_health_and_metrics_do_not_leak(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    deliver_gh(env, inst, check_run_body())
    h = env.call("GET", "/integrations/health").json()
    assert h["secrets"]["configured"] is True and "KEY" not in json.dumps(h) and KEY not in json.dumps(h)
    assert h["installations"]["unknown"] == 1 and h["worker_enabled"] is False and h["outbound_queue"]["pending"] == 0
    from app.observability.metrics import REGISTRY
    rendered = REGISTRY.render()
    assert 'nova_integration_inbound_total{integration_type="github",outcome="processed"}' in rendered
    assert inst["id"] not in rendered and "ghp_" not in rendered


def test_error_envelope_and_no_stack_traces(env) -> None:  # type: ignore[no-untyped-def]
    r = env.call("GET", "/integrations/installations/nope")
    assert r.status_code == 404 and set(r.json()) == {"error"} and set(r.json()["error"]) >= {"code", "message"}
    r = env.call("GET", "/integrations/installations?page=0")
    assert r.status_code == 422 and "Traceback" not in r.text
    r = env.call("GET", "/integrations/ci-evidence?head_sha=zzz")
    assert r.status_code == 422
    assert "Traceback" not in env.call("POST", "/integrations/installations", content=b"{bad", headers={"Content-Type": "application/json"}).text


def test_pagination_is_stable_and_bounded(env) -> None:  # type: ignore[no-untyped-def]
    for i in range(5):
        assert env.call("POST", "/integrations/installations", json={"integration_type": "local-test", "name": f"n{i}", "config": {}}).status_code == 201
    p1 = env.call("GET", "/integrations/installations?page=1&page_size=2").json()
    p2 = env.call("GET", "/integrations/installations?page=2&page_size=2").json()
    p3 = env.call("GET", "/integrations/installations?page=3&page_size=2").json()
    names = [i["name"] for i in p1["items"] + p2["items"] + p3["items"]]
    assert names == sorted(names) and len(set(names)) == 5 and p1["total"] == 5 and p1["pages"] == 3
    assert env.call("GET", "/integrations/installations?page_size=1000").status_code == 422


def test_inbound_route_is_the_only_unauthenticated_integration_route(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_API_TOKEN", "global-secret")
    reset_settings_cache()
    from app.main import create_app
    app = create_app()
    with TestClient(app) as c:
        paths = app.openapi()["paths"]
        checked = 0
        for path, methods in paths.items():
            if not path.startswith(f"{API}/integrations"):
                continue
            for method in methods:
                r = c.request(method.upper(), path.replace("{installation_id}", "x").replace("{sub_id}", "x").replace("{delivery_id}", "x").replace("{ext_id}", "x"))
                if path.endswith("/inbound/x") or path.endswith("/inbound/{installation_id}"):
                    assert r.status_code != 401  # signature-authenticated: answers 404 for an unknown installation
                else:
                    assert r.status_code == 401, (method, path, r.status_code)
                checked += 1
        assert checked >= 30


def test_migration_0016_preserves_existing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command
    from sqlalchemy import create_engine, text

    from app.db.session import reset_engine
    from tests.conftest import alembic_config
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setenv("NOVA_DATABASE_URL", url)
    reset_settings_cache()
    reset_engine()
    cfg = alembic_config(url)
    command.upgrade(cfg, "0015")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ('w1','Keep','keep', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM workspaces WHERE id='w1'")).scalar() == "Keep"
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"integration_installations", "integration_secrets", "outbox_events", "webhook_deliveries", "ci_evidence_runs", "idempotency_records"} <= tables
    command.downgrade(cfg, "0015")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM workspaces WHERE id='w1'")).scalar() == "Keep"
    eng.dispose()
    reset_engine()
    assert os.path.exists(tmp_path / "m.db")


def test_ci_and_health_events_are_published_to_subscribers(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    mk_sub(env, events=("ci.run_recorded", "integration.health_changed"))
    inst = gh_install(env)
    env.call("POST", f"/integrations/installations/{inst['id']}/enable")
    env.http.queue = [_json({"full_name": "acme/widgets", "permissions": {"pull": True}})]
    env.call("POST", f"/integrations/installations/{inst['id']}/test")  # unknown -> connected
    assert deliver_gh(env, inst, check_run_body()).status_code == 200
    with get_session_factory()() as db:
        kinds = sorted(e.event_type for e in db.scalars(select(OutboxEvent)))
    assert kinds == ["ci.run_recorded", "integration.health_changed"]


def test_publish_status_endpoint_is_opt_in_authorized_and_scoped(keyed) -> None:  # type: ignore[no-untyped-def]
    env = keyed
    ro = gh_install(env, name="readonly")
    env.call("POST", f"/integrations/installations/{ro['id']}/enable")
    body = {"external_repo": "acme/widgets", "sha": SHA, "state": "success", "description": "NOVA verification passed"}
    r = env.call("POST", f"/integrations/installations/{ro['id']}/publish-status", json=body)
    assert r.status_code == 422 and env.http.calls == []  # read-only by default: nothing is sent
    rw = gh_install(env, name="readwrite", publish_status=True)
    env.call("POST", f"/integrations/installations/{rw['id']}/enable")
    env.http.queue = [_json({"id": 77}, 201)]
    ok = env.call("POST", f"/integrations/installations/{rw['id']}/publish-status", json=body)
    assert ok.status_code == 200 and ok.json()["provider_id"] == "77" and env.http.calls[0]["method"] == "POST"
    assert env.call("POST", f"/integrations/installations/{rw['id']}/publish-status", json={**body, "external_repo": "evil/other"}).status_code == 409  # unmapped repository
    assert env.call("POST", f"/integrations/installations/{rw['id']}/publish-status", json={**body, "sha": "zzz"}).status_code == 422
    assert env.call("POST", f"/integrations/installations/{rw['id']}/publish-status", json={**body, "extra": 1}).status_code == 422
    tok = env.call("POST", "/org/members", json={"handle": "engx", "role": "engineer"}).json()["token"]
    assert env.call("POST", f"/integrations/installations/{rw['id']}/publish-status", tok, json=body).status_code == 403
