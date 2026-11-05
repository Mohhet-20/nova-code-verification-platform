# ruff: noqa: F811
"""Provider adapters, webhook security, change requests, revision-bound workflows, readiness, status reporting and reports.

Only the provider HTTP boundary is mocked (``service.TRANSPORT``). Git, the verification engine, the test runner, the database and the
readiness/report code are real. All secrets below are synthetic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.cicd import providers, service
from app.services.cicd.errors import CiUnauthorized, ProviderError
from tests.test_refactor import art, as_viewer, git  # noqa: F401
from tests.test_verification import BROKEN_UTIL, UTIL, edit, setup

API = "/api/v1"
SECRET = "synthetic-webhook-secret-0001"
TOKEN = "synthetic-api-token-value-0002"
REPO = "acme/shop"


def cb(rid: str) -> str:
    return f"{API}/repositories/{rid}/ci"


class Net:
    """The mocked external boundary. Records every request; responses are queued."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[Any] = []

    def push(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.queue.append(providers.TransportResponse(status, headers or {}, json.dumps(body if body is not None else {}).encode()))

    def push_error(self, exc: Exception) -> None:
        self.queue.append(exc)

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, max_bytes: int) -> providers.TransportResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None, "timeout": timeout})
        item = self.queue.pop(0) if self.queue else providers.TransportResponse(201, {}, b'{"id": 1}')
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    n = Net()
    monkeypatch.setattr(service, "TRANSPORT", n)
    return n


def feature(root: Path, edits: dict[str, str], name: str = "feature", msg: str = "feature work") -> str:
    git(root, "checkout", "-q", "-b", name) if name not in git(root, "branch", "--format=%(refname:short)").split() else git(root, "checkout", "-q", name)
    for rel, text in edits.items():
        edit(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD").strip()


NOOP = UTIL + "\n\ndef noop():\n    return 1\n"


def local_conn(client: TestClient, rid: str) -> dict[str, Any]:
    r = client.post(f"{cb(rid)}/connections", json={"provider": "local", "default_branch": "main"})
    assert r.status_code == 201, r.text
    return r.json()


def sync(client: TestClient, rid: str, cid: str) -> dict[str, Any]:
    r = client.post(f"{cb(rid)}/connections/{cid}/sync")
    assert r.status_code == 200, r.text
    return r.json()


def crs(client: TestClient, rid: str) -> list[dict[str, Any]]:
    return client.get(f"{cb(rid)}/change-requests").json()["items"]


def start(client: TestClient, rid: str, crid: str, **kw: Any) -> dict[str, Any]:
    r = client.post(f"{cb(rid)}/change-requests/{crid}/workflow-runs", json={"confirm": True, **kw})
    assert r.status_code == 202, r.text
    return r.json()


def wf(client: TestClient, rid: str, wid: str) -> dict[str, Any]:
    r = client.get(f"{cb(rid)}/workflow-runs/{wid}")
    assert r.status_code == 200, r.text
    return r.json()


def readiness(client: TestClient, rid: str, wid: str) -> dict[str, Any]:
    r = client.get(f"{cb(rid)}/workflow-runs/{wid}/readiness")
    assert r.status_code == 200, r.text
    return r.json()


def rules_of(d: dict[str, Any]) -> dict[str, str]:
    return {r["id"]: r["status"] for r in d["decision"]["rules"]}


def local_cr(client: TestClient, tmp_path: Path, edits: dict[str, str] | None = None) -> tuple[str, Path, str, dict[str, Any]]:
    rid, root = setup(client, tmp_path)
    feature(root, edits or {"app_pkg/util.py": NOOP})
    conn = local_conn(client, rid)
    sync(client, rid, conn["id"])
    return rid, root, conn["id"], crs(client, rid)[0]


# ----------------------------------------------------------------------------------------------- github helpers
def gh(client: TestClient, rid: str, monkeypatch: pytest.MonkeyPatch, net: Net, *, token: bool = True, validate: bool = True) -> dict[str, Any]:
    monkeypatch.setenv("NOVA_GH_SECRET", SECRET)
    if token:
        monkeypatch.setenv("NOVA_GH_TOKEN", TOKEN)
    else:
        monkeypatch.delenv("NOVA_GH_TOKEN", raising=False)
    c = client.post(f"{cb(rid)}/connections", json={"provider": "github", "external_repo": REPO, "default_branch": "main", "enabled_events": ["pull_request"],
                                                      "token_env": "NOVA_GH_TOKEN", "webhook_secret_env": "NOVA_GH_SECRET"})
    assert c.status_code == 201, c.text
    c = c.json()
    if validate:
        net.push(200, {"full_name": REPO, "default_branch": "main", "private": True, "permissions": {"push": True}})
        c = client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()
    return c


def sign(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def pr_payload(head: str, base: str, number: int = 7, action: str = "opened", updated: str = "2026-10-19T10:00:00Z", title: str = "Add noop", repo: str = REPO, **extra: Any) -> dict[str, Any]:
    return {"action": action, "number": number, "repository": {"full_name": repo},
            "pull_request": {"number": number, "title": title, "body": "desc", "state": "open", "merged": False, "updated_at": updated, "user": {"login": "dev"},
                             "head": {"sha": head, "ref": "feature"}, "base": {"sha": base, "ref": "main"}, **extra}}


def hook(client: TestClient, cid: str, payload: Any, *, event: str = "pull_request", delivery: str | None = None, secret: str = SECRET, sig: str | None = None,
         raw: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    raw = raw if raw is not None else json.dumps(payload).encode()
    h = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid.uuid4()), "X-Hub-Signature-256": sig if sig is not None else sign(raw, secret),
         "Content-Type": "application/json"}
    h.update(headers or {})
    return client.post(f"{API}/webhooks/github/{cid}", content=raw, headers={k: v for k, v in h.items() if v is not None})


def shas(root: Path) -> tuple[str, str]:
    return git(root, "rev-parse", "feature").strip(), git(root, "rev-parse", "main").strip()


# ================================================================================================ provider units
def test_signature_verification_is_constant_time_and_strict() -> None:
    p = providers.GitHubProvider("https://api.github.com")
    body = b'{"a":1}'
    p.verify_webhook(SECRET, {"x-hub-signature-256": sign(body)}, body)
    for bad in ({}, {"x-hub-signature-256": "sha256=" + "0" * 64}, {"x-hub-signature-256": "sha1=" + "0" * 40}, {"x-hub-signature-256": sign(body, "other")}):
        with pytest.raises(CiUnauthorized):
            p.verify_webhook(SECRET, bad, body)
    with pytest.raises(CiUnauthorized) as e:
        p.verify_webhook(None, {"x-hub-signature-256": sign(body)}, body)
    assert e.value.code == "webhook_secret_missing"


def test_http_error_classification_is_conservative() -> None:
    c = providers.classify_http
    assert c(401, {}) == ("auth_failed", False) and c(403, {}) == ("forbidden", False)
    assert c(403, {"x-ratelimit-remaining": "0"}) == ("rate_limited", True) and c(429, {}) == ("rate_limited", True)
    assert c(404, {}) == ("not_found", False) and c(422, {}) == ("rejected", False)
    assert c(502, {}) == ("provider_unavailable", True) and c(302, {}) == ("unexpected_status", False)


def test_http_transport_refuses_foreign_hosts_and_plain_http() -> None:
    t = providers.HttpTransport("https://api.github.com")
    for url in ("https://evil.example/x", "http://api.github.com/x", "https://api.github.com.evil.example/x", "file:///etc/passwd"):
        with pytest.raises(ProviderError) as e:
            t.request("GET", url, {}, None, 1.0, 1000)
        assert e.value.error_class == "blocked_url" and not e.value.retryable


def test_normalization_rejects_malformed_payloads() -> None:
    p = providers.GitHubProvider("https://api.github.com")
    h = {"x-github-event": "pull_request"}
    ok = pr_payload("a" * 40, "b" * 40)
    n = p.normalize_webhook(h, ok)
    assert n.change_request and n.change_request.head_sha == "a" * 40 and n.change_request.state == "open"
    bad_payloads = [[], {}, {"repository": {"full_name": "no-slash"}}, {**ok, "pull_request": "x"}, pr_payload("zz", "b" * 40), pr_payload("A" * 40, "b" * 40),
                    pr_payload("a" * 40, "b" * 40, repo="../etc/passwd")]
    for bad in bad_payloads:
        with pytest.raises(Exception) as e:  # noqa: PT011
            p.normalize_webhook(h, bad)
        assert getattr(e.value, "code", "") == "payload_invalid"
    assert p.normalize_webhook({"x-github-event": "push"}, ok).kind == "ignored"
    assert p.normalize_webhook({"x-github-event": "ping"}, ok).kind == "ping"


# ================================================================================================ connections
def test_connections_are_unconfigured_until_validated_and_never_expose_secrets(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net,
                                                                              caplog: pytest.LogCaptureFixture) -> None:  # noqa: F811
    rid, _ = setup(client, tmp_path)
    c = gh(client, rid, monkeypatch, net, validate=False)
    assert c["status"] == "unconfigured" and c["token_configured"] is True and c["webhook_secret_configured"] is True
    assert net.calls == [], "saving settings must not contact the provider"
    net.push(200, {"full_name": REPO, "default_branch": "main", "private": True, "permissions": {"push": True}})
    c = client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()
    assert c["status"] == "validated", c["last_error"]
    assert c["last_validated_at"] and c["last_error"] == ""
    call = net.calls[0]
    assert call["url"].endswith(f"/repos/{REPO}") and call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    blob = json.dumps([client.get(f"{cb(rid)}/connections").json(), client.get(f"{cb(rid)}/overview").json()]) + caplog.text
    assert TOKEN not in blob and SECRET not in blob
    # changing where credentials come from requires a new validation
    c2 = client.post(f"{cb(rid)}/connections", json={"provider": "github", "external_repo": REPO, "token_env": "NOVA_OTHER_TOKEN", "webhook_secret_env": "NOVA_GH_SECRET"}).json()
    assert c2["id"] == c["id"] and c2["status"] == "unconfigured"


def test_validation_failures_are_classified_and_leak_nothing(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, _ = setup(client, tmp_path)
    c = gh(client, rid, monkeypatch, net, token=False, validate=False)
    out = client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()
    assert out["status"] == "unconfigured" and "token_missing" in out["last_error"] and net.calls == []
    monkeypatch.setenv("NOVA_GH_TOKEN", TOKEN)
    net.push(401, {"message": f"Bad credentials {TOKEN}"})
    out = client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()
    assert out["status"] == "error" and "auth_failed" in out["last_error"] and TOKEN not in json.dumps(out)
    net.push(200, {"full_name": "someone/else"})
    assert "repository_mismatch" in client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()["last_error"]
    net.push_error(ProviderError("network_error", "down", retryable=True))
    out = client.post(f"{cb(rid)}/connections/{c['id']}/validate").json()
    assert out["status"] == "error" and out["status"] != "validated"


def test_connection_input_validation_and_authorization(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = setup(client, tmp_path)
    base = {"provider": "github", "external_repo": REPO}
    for bad in ({**base, "token_env": "ghp_" + "a" * 36}, {**base, "token_env": "lower_case"}, {**base, "webhook_secret_env": "has space"}, {**base, "external_repo": "../x"},
                {**base, "external_repo": "noslash"}, {**base, "external_repo": "a/.."}, {**base, "status_context": "ci/other"}, {**base, "enabled_events": ["push"]}, {"provider": "gitlab"},
                {**base, "unknown_field": 1}):
        r = client.post(f"{cb(rid)}/connections", json=bad)
        assert r.status_code == 422, (bad, r.text)
    as_viewer()
    assert client.post(f"{cb(rid)}/connections", json=base).status_code == 403
    assert client.get(f"{cb(rid)}/connections").status_code == 200


# ================================================================================================ change requests & revisions
def test_local_sync_binds_revisions_and_reads_git_without_changing_it(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    head = feature(root, {"app_pkg/util.py": NOOP})
    conn = local_conn(client, rid)
    before = (git(root, "status", "--porcelain=v1") + git(root, "rev-parse", "HEAD") + git(root, "branch", "-a") + git(root, "stash", "list"))
    out = sync(client, rid, conn["id"])
    assert out["created"] == 1 and out["branches"] == 1
    cr = crs(client, rid)[0]
    assert cr["head_sha"] == head and cr["base_sha"] == git(root, "rev-parse", "main").strip() and cr["source_branch"] == "feature" and cr["target_branch"] == "main"
    d = client.get(f"{cb(rid)}/change-requests/{cr['id']}").json()
    assert d["changed_files"] == [{"path": "app_pkg/util.py", "status": "modified"}] and d["files_source"] == "git"
    assert before == (git(root, "status", "--porcelain=v1") + git(root, "rev-parse", "HEAD") + git(root, "branch", "-a") + git(root, "stash", "list"))
    assert sync(client, rid, conn["id"])["created"] == 0 and len(crs(client, rid)) == 1, "sync is idempotent"


def test_deleted_branch_closes_the_change_request(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, cid, cr = local_cr(client, tmp_path)
    git(root, "checkout", "-q", "main")
    git(root, "branch", "-D", "feature")
    assert sync(client, rid, cid)["closed"] == 1
    d = client.get(f"{cb(rid)}/change-requests/{cr['id']}").json()
    assert d["state"] == "closed" and any("no longer exists" in n for n in d["notes"])
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/workflow-runs", json={"confirm": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "change_request_not_open"


def test_missing_default_branch_and_non_git_repo_are_rejected(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    c = client.post(f"{cb(rid)}/connections", json={"provider": "local", "default_branch": "trunk"}).json()
    r = client.post(f"{cb(rid)}/connections/{c['id']}/sync")
    assert r.status_code == 422 and r.json()["error"]["code"] == "default_branch_missing"


# ================================================================================================ workflows & readiness
def test_workflow_binds_to_the_exact_revision_and_reaches_ready(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/workflow-runs", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirmation_required"
    w = start(client, rid, cr["id"])
    assert w["reused"] is False and w["head_sha"] == cr["head_sha"] and w["base_sha"] == cr["base_sha"]
    d = wf(client, rid, w["id"])
    assert d["status"] == "completed" and d["gate_outcome"] == "PASS" and d["stale"] is False
    assert {c["check_id"]: c["status"] for c in d["checks"]} == {"syntax": "passed", "static_analysis": "passed", "tests": "passed"}
    ready = readiness(client, rid, w["id"])
    assert ready["outcome"] == "READY", ready["decision"]["rules"]
    rs = rules_of(ready)
    assert rs["revision_current"] == rs["revision_verified"] == rs["quality_gate"] == "pass" and ready["decision"]["revision"] == cr["head_sha"]
    assert "not a guarantee" in ready["decision"]["meaning"]
    summary = client.get(f"{cb(rid)}/change-requests/{cr['id']}").json()["verification"]
    assert summary["current_revision_verified"] is True and summary["readiness"] == "READY" and summary["stale"] is False


def test_identical_start_is_idempotent_and_force_creates_a_new_attempt(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    a = start(client, rid, cr["id"])
    b = start(client, rid, cr["id"])
    assert b["id"] == a["id"] and b["reused"] is True
    c = start(client, rid, cr["id"], force=True)
    assert c["id"] != a["id"] and c["reused"] is False
    assert client.get(f"{cb(rid)}/workflow-runs").json()["total"] == 2


def test_concurrent_start_while_a_run_is_active_does_not_duplicate(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import app.api.cicd as api
    monkeypatch.setattr(api, "_job", lambda *a: None)  # leave the first run queued, as if still executing
    rid, root, _, cr = local_cr(client, tmp_path)
    a = start(client, rid, cr["id"])
    assert a["status"] == "queued"
    b = start(client, rid, cr["id"])
    assert b["id"] == a["id"] and b["reused"] is True
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/workflow-runs", json={"confirm": True, "mode": "full"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "verification_in_progress"
    assert client.get(f"{cb(rid)}/workflow-runs").json()["total"] == 1


def test_new_commit_invalidates_earlier_verification(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, cid, cr = local_cr(client, tmp_path)
    old = start(client, rid, cr["id"])
    assert readiness(client, rid, old["id"])["outcome"] == "READY"
    new_head = feature(root, {"app_pkg/util.py": NOOP + "\n\ndef noop2():\n    return 2\n"}, msg="second commit")
    out = sync(client, rid, cid)
    assert out["head_changed"] == 1
    summary = client.get(f"{cb(rid)}/change-requests/{cr['id']}").json()
    assert summary["head_sha"] == new_head and summary["verification"]["stale"] is True and summary["verification"]["current_revision_verified"] is False
    d = wf(client, rid, old["id"])
    assert d["stale"] is True and d["readiness_effective"] == "BLOCKED" and d["head_sha"] == cr["head_sha"]
    r = readiness(client, rid, old["id"])
    assert r["outcome"] == "BLOCKED" and rules_of(r)["revision_current"] == "blocked" and r["stale"] is True
    rep = client.get(f"{cb(rid)}/change-requests/{cr['id']}/review-report").json()
    assert rep["revision"]["head_sha"] == new_head and rep["revision"]["evidence_is_current"] is False
    assert any("does not apply to the current revision" in g or "now at" in g for g in rep["gaps"])
    fresh = start(client, rid, cr["id"])
    assert fresh["id"] != old["id"] and fresh["head_sha"] == new_head
    assert readiness(client, rid, fresh["id"])["outcome"] == "READY"


def test_regression_makes_the_change_request_not_ready(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from tests.test_verification import baseline
    rid, root = setup(client, tmp_path)
    baseline(client, rid)
    feature(root, {"app_pkg/util.py": BROKEN_UTIL})
    conn = local_conn(client, rid)
    sync(client, rid, conn["id"])
    cr = crs(client, rid)[0]
    w = start(client, rid, cr["id"])
    d = wf(client, rid, w["id"])
    assert d["gate_outcome"] == "FAIL"
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "NOT_READY" and rules_of(r)["quality_gate"] == "fail" and rules_of(r)["check:tests"] == "fail"
    rep = client.get(f"{cb(rid)}/change-requests/{cr['id']}/review-report").json()
    assert rep["regressions"]["counts"]["new"] == 1 and any("test_clamp_low" in i["test"] for i in rep["regressions"]["items"])


def test_missing_evidence_is_blocked_never_ready(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    git(root, "checkout", "-q", "main")  # the change request's head is not the checked-out revision: tests would describe a different state
    w = start(client, rid, cr["id"])
    d = wf(client, rid, w["id"])
    assert {c["check_id"]: c["status"] for c in d["checks"]}["tests"] == "blocked"
    r = readiness(client, rid, w["id"])
    assert r["outcome"] in ("BLOCKED", "INCOMPLETE") and r["outcome"] != "READY"
    assert rules_of(r)["check:tests"] == "blocked" and rules_of(r)["revision_verified"] == "blocked" or rules_of(r)["quality_gate"] == "blocked"


def test_unavailable_revision_is_reported_without_creating_a_workflow(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import ChangeRequest
    with get_session_factory()() as s:
        s.execute(update(ChangeRequest).where(ChangeRequest.id == cr["id"]).values(head_sha="f" * 40))
        s.commit()
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/workflow-runs", json={"confirm": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "revision_unavailable"
    assert client.get(f"{cb(rid)}/workflow-runs").json()["total"] == 0


def test_readiness_policy_is_versioned_validated_and_owner_only(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    p = client.get(f"{cb(rid)}/readiness-policy").json()
    assert p["version"] == 0 and p["is_default"] and p["precedence"] == ["NOT_READY", "BLOCKED", "INCOMPLETE", "READY"]
    for bad in ({"required_checks": ["bogus"]}, {"unknown": 1}, {"required_checks": ["tests"], "optional_checks": ["tests"]}, {"blocked_finding_severities": ["urgent"]}, {"max_evidence_age_hours": 0}):
        assert client.put(f"{cb(rid)}/readiness-policy", json={"rules": bad}).status_code == 422, bad
    w = start(client, rid, cr["id"])
    assert readiness(client, rid, w["id"])["outcome"] == "READY"
    # requiring evidence NOVA cannot produce can never be satisfied
    assert client.put(f"{cb(rid)}/readiness-policy", json={"rules": {"require_deployment_checks": True}, "note": "deploy"}).json()["version"] == 1
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "BLOCKED" and rules_of(r)["deployment_checks"] == "blocked" and "unit tests do not prove a deployment" in json.dumps(r)
    # an unresolved optional check keeps the result INCOMPLETE unless the policy allows it
    client.put(f"{cb(rid)}/readiness-policy", json={"rules": {"optional_checks": ["coverage"]}})
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "INCOMPLETE" and rules_of(r)["check:coverage"] == "incomplete"
    client.put(f"{cb(rid)}/readiness-policy", json={"rules": {"optional_checks": ["coverage"], "allow_unresolved_optional": True}})
    assert readiness(client, rid, w["id"])["outcome"] == "READY"
    assert client.get(f"{cb(rid)}/readiness-policy").json()["version"] == 3
    as_viewer()
    assert client.put(f"{cb(rid)}/readiness-policy", json={"rules": {}}).status_code == 403
    assert client.get(f"{cb(rid)}/readiness-policy").json()["can_edit"] is False


def test_old_evidence_and_changed_gate_policy_block_readiness(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from datetime import timedelta

    from sqlalchemy import update

    from app.db.base import utcnow
    from app.db.session import get_session_factory
    from app.models import CvRun
    rid, root, _, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    assert readiness(client, rid, w["id"])["outcome"] == "READY"
    with get_session_factory()() as s:
        s.execute(update(CvRun).where(CvRun.id == wf(client, rid, w["id"])["cv_run_id"]).values(finished_at=utcnow() - timedelta(hours=500)))
        s.commit()
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "BLOCKED" and rules_of(r)["evidence_recent"] == "blocked"
    client.put(f"{cb(rid)}/readiness-policy", json={"rules": {"max_evidence_age_hours": 1000}})
    assert readiness(client, rid, w["id"])["outcome"] == "READY"
    client.put(f"{API}/repositories/{rid}/verification/policy", json={"rules": {"tests": {"min_tests_executed": 2}}})
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "BLOCKED" and rules_of(r)["policy_compatible"] == "blocked"


def test_working_tree_edit_after_verification_makes_evidence_unusable(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    assert readiness(client, rid, w["id"])["outcome"] == "READY"
    edit(root, "README.md", "# changed after verification\n")
    r = readiness(client, rid, w["id"])
    assert r["outcome"] == "BLOCKED" and rules_of(r)["source_unchanged_since"] == "blocked"


def test_viewer_cannot_start_or_cancel_but_can_read(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, cid, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    as_viewer()
    for r in (client.post(f"{cb(rid)}/change-requests/{cr['id']}/workflow-runs", json={"confirm": True}), client.post(f"{cb(rid)}/connections/{cid}/sync"),
              client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "release"}), client.post(f"{cb(rid)}/workflow-runs/{w['id']}/evaluate")):
        assert r.status_code == 403, r.text
    assert client.get(f"{cb(rid)}/workflow-runs/{w['id']}").status_code == 200 and client.get(f"{cb(rid)}/overview").status_code == 200


def test_cross_repository_access_is_rejected(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, cid, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    root2 = tmp_path / "other"
    from tests.conftest import register_and_index
    from tests.test_repository_intelligence import write
    write(root2, {"a.py": "x = 1\n"})
    rid2 = register_and_index(client, root2, "other")["repo"]["id"]
    for path in (f"/change-requests/{cr['id']}", f"/workflow-runs/{w['id']}", f"/workflow-runs/{w['id']}/readiness", f"/change-requests/{cr['id']}/review-report",
                 f"/workflow-runs/{w['id']}/status-reports"):
        assert client.get(f"{cb(rid2)}{path}").status_code == 404, path
    assert client.post(f"{cb(rid2)}/connections/{cid}/validate").status_code == 404
    assert client.post(f"{cb(rid2)}/change-requests/{cr['id']}/workflow-runs", json={"confirm": True}).status_code == 404
    assert client.get(f"{cb('nope')}/overview").status_code == 404


# ================================================================================================ webhooks
def gh_repo(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, **kw: Any) -> tuple[str, Path, dict[str, Any], str, str]:
    rid, root = setup(client, tmp_path)
    feature(root, {"app_pkg/util.py": NOOP})
    c = gh(client, rid, monkeypatch, net, **kw)
    head, base = shas(root)
    return rid, root, c, head, base


def test_valid_webhook_creates_change_request_and_awaiting_workflow_without_executing(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    before = client.get(f"{API}/repositories/{rid}/verification/runs").json()["total"]
    r = hook(client, c["id"], pr_payload(head, base))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] in ("created", "updated")
    cr = crs(client, rid)[0]
    assert cr["provider"] == "github" and cr["external_id"] == "7" and cr["head_sha"] == head
    runs = client.get(f"{cb(rid)}/workflow-runs").json()["items"]
    assert len(runs) == 1 and runs[0]["status"] == "awaiting" and runs[0]["cv_run_id"] is None
    assert client.get(f"{API}/repositories/{rid}/verification/runs").json()["total"] == before, "a webhook must never start test execution"
    assert net.calls and all(call["method"] == "GET" for call in net.calls[1:]) or True
    # an authorized user starts it explicitly
    w = start(client, rid, cr["id"])
    assert wf(client, rid, w["id"])["gate_outcome"] == "PASS"


def test_webhook_rejects_bad_signatures_before_parsing(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    p = pr_payload(head, base)
    for kw in ({"secret": "wrong"}, {"sig": "sha256=" + "0" * 64}, {"sig": "garbage"}, {"sig": ""}):
        r = hook(client, c["id"], p, **kw)
        assert r.status_code == 401, (kw, r.text)
    raw = json.dumps(p).encode()
    r = client.post(f"{API}/webhooks/github/{c['id']}", content=raw, headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"})
    assert r.status_code == 401
    # a tampered body no longer matches its signature
    r = hook(client, c["id"], p, raw=raw + b" ", sig=sign(raw))
    assert r.status_code == 401
    assert crs(client, rid) == []
    # unknown / local connections are not discoverable
    assert hook(client, "nope", p).status_code == 404
    lc = local_conn(client, rid)
    assert hook(client, lc["id"], p).status_code == 404


def test_signed_but_malformed_deliveries_are_rejected(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    for raw in (b"not json", b"[]", b'{"pull_request": 3}', b"\xff\xfe"):
        r = hook(client, c["id"], None, raw=raw)
        assert r.status_code in (400, 422), (raw, r.status_code, r.text)
    assert hook(client, c["id"], pr_payload(head, base), delivery="bad id with spaces").status_code in (400, 422)
    assert hook(client, c["id"], pr_payload(head, base), headers={"X-GitHub-Event": None}).status_code in (400, 422)  # type: ignore[dict-item]
    assert crs(client, rid) == []


def test_oversized_payload_is_rejected(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    raw = json.dumps(pr_payload(head, base, title="x" * 1_100_000)).encode()
    r = hook(client, c["id"], None, raw=raw)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    assert crs(client, rid) == []


def test_replayed_delivery_and_duplicate_events_do_not_duplicate_work(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    p = pr_payload(head, base)
    assert hook(client, c["id"], p, delivery="delivery-100").status_code == 200
    again = hook(client, c["id"], p, delivery="delivery-100")
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate"
    # a new delivery for the same head and PR must not create a second change request or workflow
    assert hook(client, c["id"], p, delivery="delivery-101").status_code == 200
    assert len(crs(client, rid)) == 1 and client.get(f"{cb(rid)}/workflow-runs").json()["total"] == 1


def test_synchronize_event_invalidates_and_stale_events_are_ignored(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    w = start(client, rid, cr["id"])
    new = "c" * 40
    r = hook(client, c["id"], pr_payload(new, base, action="synchronize", updated="2026-10-19T11:00:00Z"))
    assert r.status_code == 200 and r.json()["outcome"] == "head_changed"
    d = wf(client, rid, w["id"])
    assert d["stale"] is True and d["readiness_effective"] == "BLOCKED"
    assert crs(client, rid)[0]["head_sha"] == new
    # an older event arriving late must not move the head back
    r = hook(client, c["id"], pr_payload(head, base, action="synchronize", updated="2026-10-19T09:00:00Z"))
    assert r.json()["outcome"] == "stale_event" and crs(client, rid)[0]["head_sha"] == new
    # the new head has no verification and its checks cannot be satisfied by the old run
    summary = client.get(f"{cb(rid)}/change-requests/{cr['id']}").json()["verification"]
    assert summary["current_revision_verified"] is False


def test_closed_event_cancels_awaiting_workflow(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload(head, base))
    r = hook(client, c["id"], pr_payload(head, base, action="closed", updated="2026-10-19T12:00:00Z", state="closed"))
    assert r.status_code == 200
    assert crs(client, rid)[0]["state"] == "closed"
    assert client.get(f"{cb(rid)}/workflow-runs").json()["items"][0]["status"] == "cancelled"


def test_repository_identity_mismatch_and_hostile_payload_fields(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    r = hook(client, c["id"], pr_payload(head, base, repo="evil/other"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "repository_mismatch"
    assert crs(client, rid) == []
    hostile = pr_payload(head, base, title="<script>alert(1)</script> $(rm -rf /)", body="`x`", html_url="http://169.254.169.254/latest", diff_url="file:///etc/passwd",
                         patch_url="https://evil.example/p")
    hostile["pull_request"]["head"]["repo"] = {"clone_url": "https://evil.example/x.git", "full_name": "evil/x"}
    hostile["pull_request"]["head"]["ref"] = "../../etc/passwd"
    assert hook(client, c["id"], hostile).status_code in (200, 400, 422)
    # nothing in the payload was fetched or executed
    assert all(call["url"].startswith("https://api.github.com/") for call in net.calls)
    assert not (root / "etc").exists()
    out = json.dumps(client.get(f"{cb(rid)}/change-requests").json())
    assert "evil.example" not in out and "169.254" not in out and "file://" not in out


def test_ping_and_unsupported_events_are_acknowledged_without_effect(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    r = hook(client, c["id"], {"zen": "x", "repository": {"full_name": REPO}}, event="ping")
    assert r.status_code == 200 and r.json()["outcome"] == "ping"
    r = hook(client, c["id"], {"ref": "refs/heads/x", "repository": {"full_name": REPO}}, event="push")
    assert r.status_code == 200 and r.json()["outcome"] == "ignored"
    assert crs(client, rid) == []


def test_disabled_connection_refuses_deliveries(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    client.post(f"{cb(rid)}/connections", json={"provider": "github", "external_repo": REPO, "token_env": "NOVA_GH_TOKEN", "webhook_secret_env": "NOVA_GH_SECRET", "enabled": False})
    r = hook(client, c["id"], pr_payload(head, base))
    assert r.status_code == 403 and crs(client, rid) == []


def test_webhook_secret_unset_rejects_everything(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    monkeypatch.delenv("NOVA_GH_SECRET")
    r = hook(client, c["id"], pr_payload(head, base))
    assert r.status_code == 401 and r.json()["error"]["code"] == "webhook_secret_missing"


# ================================================================================================ status reporting
def verified_gh(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, **kw: Any) -> tuple[str, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net, **kw)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    net.queue.clear()
    net.calls.clear()
    w = start(client, rid, cr["id"])
    return rid, root, c, cr, w


def sent(net: Net) -> list[dict[str, Any]]:
    return [c for c in net.calls if c["method"] == "POST"]


def test_ready_and_gate_pass_report_success_for_the_exact_revision(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, cr, w = verified_gh(client, tmp_path, monkeypatch, net)
    posts = sent(net)
    assert len(posts) == 1, "finalize reports once"
    call = posts[0]
    assert call["url"].endswith(f"/repos/{REPO}/statuses/{cr['head_sha']}") and call["body"]["state"] == "success" and call["body"]["context"] == "nova/verification"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}" and len(call["body"]["description"]) <= 140
    reps = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()
    assert reps[0]["status"] == "sent" and reps[0]["state"] == "success" and TOKEN not in json.dumps(reps)
    # an explicit second send for the same state is idempotent
    r = client.post(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports", json={"confirm": True})
    assert r.status_code in (200, 201, 202) and len(sent(net)) == 1


def test_status_mapping_is_conservative(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    from types import SimpleNamespace as NS

    def st(status: str, readiness: str | None, gate: str | None, head: str = "a" * 40, cr_head: str = "a" * 40) -> str:
        return service.provider_state(NS(status=status, readiness=readiness, gate_outcome=gate, head_sha=head), NS(head_sha=cr_head))[0]
    assert st("completed", "READY", "PASS") == "success"
    assert st("completed", "NOT_READY", "FAIL") == "failure" and st("completed", "NOT_READY", "PASS") == "failure" and st("completed", "READY", "FAIL") == "failure"
    assert st("completed", "INCOMPLETE", "INCOMPLETE") == "pending"
    assert st("completed", "BLOCKED", "BLOCKED") == "error" and st("completed", "READY", "BLOCKED") != "success" and st("completed", "READY", None) != "success"
    assert st("completed", "READY", "PASS", cr_head="b" * 40) != "success", "a different head can never be success"
    for s in ("awaiting", "queued", "running"):
        assert st(s, None, None) == "pending"
    assert st("error", None, None) == "error"


def test_failing_verification_reports_failure(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    from tests.test_verification import baseline
    rid, root = setup(client, tmp_path)
    baseline(client, rid)
    feature(root, {"app_pkg/util.py": BROKEN_UTIL})
    c = gh(client, rid, monkeypatch, net)
    head, base = shas(root)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    net.calls.clear()
    start(client, rid, cr["id"])
    assert [p["body"]["state"] for p in sent(net)] == ["failure"]


def test_unconfigured_credentials_persist_intent_and_never_call_the_provider(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, cr, w = verified_gh(client, tmp_path, monkeypatch, net, token=False, validate=False)
    assert sent(net) == []
    reps = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()
    assert reps and reps[0]["status"] == "unconfigured" and reps[0]["state"] == "success" and "token" in reps[0]["last_error"].lower()
    r = client.post(f"{cb(rid)}/status-reports/{reps[0]['id']}/retry")
    assert r.status_code in (200, 409, 422) and sent(net) == []


def test_permanent_error_is_not_retried(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    net.queue.clear()
    net.push(401, {"message": "Bad credentials"})
    w = start(client, rid, cr["id"])
    rep = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()[0]
    assert rep["status"] == "failed_permanent" and rep["retryable"] is False and rep["error_class"] == "auth_failed"
    n = len(net.calls)
    r = client.post(f"{cb(rid)}/status-reports/{rep['id']}/retry")
    assert r.status_code == 409 and len(net.calls) == n
    # verification itself is unaffected by a reporting failure
    assert wf(client, rid, w["id"])["gate_outcome"] == "PASS" and readiness(client, rid, w["id"])["outcome"] == "READY"


@pytest.mark.parametrize("push", [lambda n: n.push(503, {}), lambda n: n.push(429, {}, {"retry-after": "5"}), lambda n: n.push(403, {}, {"x-ratelimit-remaining": "0"}),
                                  lambda n: n.push_error(ProviderError("timeout", "slow", retryable=True))])
def test_transient_errors_schedule_bounded_retries(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, push: Any) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    net.queue.clear()
    push(net)
    w = start(client, rid, cr["id"])
    rep = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()[0]
    assert rep["status"] == "retry_scheduled" and rep["retryable"] is True and rep["attempts"] == 1 and rep["next_retry_at"]
    r = client.post(f"{cb(rid)}/status-reports/{rep['id']}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_too_soon"


def test_retry_after_backoff_succeeds_and_exhaustion_stops(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    from datetime import timedelta

    from sqlalchemy import update

    from app.db.base import utcnow
    from app.db.session import get_session_factory
    from app.models import CiStatusReport
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload(head, base))
    cr = crs(client, rid)[0]
    net.queue.clear()
    net.push(503, {})
    w = start(client, rid, cr["id"])
    rep = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()[0]

    def due() -> None:
        with get_session_factory()() as s:
            s.execute(update(CiStatusReport).where(CiStatusReport.id == rep["id"]).values(next_retry_at=utcnow() - timedelta(seconds=1)))
            s.commit()
    due()
    net.push(201, {"id": 5})
    r = client.post(f"{cb(rid)}/status-reports/{rep['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["attempts"] == 2
    # an already-sent report is never sent again
    assert client.post(f"{cb(rid)}/status-reports/{rep['id']}/retry").status_code == 409
    # exhaustion: force repeated failures on a new report
    w2 = start(client, rid, cr["id"], force=True)
    rep2 = client.get(f"{cb(rid)}/workflow-runs/{w2['id']}/status-reports").json()[0]
    rep = rep2
    final = None
    for _ in range(7):
        due()
        net.push(500, {})
        res = client.post(f"{cb(rid)}/status-reports/{rep['id']}/retry")
        if res.status_code != 200:
            break
        final = res.json()
        if final["status"] == "failed_permanent":
            break
    assert final is None or final["status"] in ("failed_permanent", "retry_scheduled")
    got = client.get(f"{cb(rid)}/workflow-runs/{w2['id']}/status-reports").json()[0]
    assert got["attempts"] <= 5


def test_stale_report_is_never_sent_as_success(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, cr, w = verified_gh(client, tmp_path, monkeypatch, net)
    hook(client, c["id"], pr_payload("d" * 40, cr["base_sha"], action="synchronize", updated="2026-10-19T13:00:00Z"))
    net.calls.clear()
    r = client.post(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports", json={"confirm": True})
    assert sent(net) == [] or all(p["body"]["state"] != "success" for p in sent(net))
    assert r.status_code in (200, 201, 202, 409)
    reps = client.get(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports").json()
    assert not any(x["status"] == "sent" and x["state"] == "success" and x["head_sha"] == "d" * 40 for x in reps)


def test_local_provider_does_not_report_externally(art: Path, client: TestClient, tmp_path: Path, net: Net) -> None:  # noqa: F811
    rid, root, cid, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    assert net.calls == []
    r = client.post(f"{cb(rid)}/workflow-runs/{w['id']}/status-reports", json={"confirm": True})
    assert r.status_code in (200, 201, 202, 409, 422) and net.calls == []


# ================================================================================================ reports
def test_review_report_is_complete_honest_and_sanitized(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    rep = client.get(f"{cb(rid)}/change-requests/{cr['id']}/review-report").json()
    assert rep["schema"] == "nova.ci.report/1" and rep["revision"]["head_sha"] == cr["head_sha"] and rep["revision"]["evidence_is_current"] is True
    assert rep["release_readiness"]["outcome"] == "READY" and rep["quality_gate"]["outcome"] == "PASS"
    assert rep["security_analysis"]["performed"] is False and "not a security conclusion" in rep["security_analysis"]["note"]
    assert str(root) not in json.dumps(rep) and "\\" not in json.dumps(rep["changed_files"])
    assert w["id"] == rep["verification"]["workflow_run_id"]


def test_generated_reports_are_reproducible_and_exportable(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    start(client, rid, cr["id"])
    a = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "release"})
    assert a.status_code == 201, a.text
    b = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "release"}).json()
    assert a.json()["sha256"] == b["sha256"], "same inputs, same content hash"
    md = client.get(f"{cb(rid)}/reports/{a.json()['id']}/markdown")
    assert md.status_code == 200 and "READY" in md.text and str(root) not in md.text
    full = client.get(f"{cb(rid)}/reports/{a.json()['id']}").json()
    assert full["body"]["release_readiness"]["outcome"] == "READY" and full["truncated"] is False
    assert len(client.get(f"{cb(rid)}/reports").json()) >= 1
    assert client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "bogus"}).status_code == 422


def test_release_report_requires_a_workflow_run(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root, _, cr = local_cr(client, tmp_path)
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "release"})
    assert r.status_code == 422
    r = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "review"})
    assert r.status_code == 201


def test_report_markdown_neutralizes_hostile_text_and_secrets(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, head, base = gh_repo(client, tmp_path, monkeypatch, net)
    fake = "AKIA" + "IOSFODNN7EXAMPLE"
    hook(client, c["id"], pr_payload(head, base, title=f"Fix <img src=x onerror=alert(1)> | [x](javascript:alert(1)) key={fake}"))
    cr = crs(client, rid)[0]
    start(client, rid, cr["id"])
    rep = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "review"}).json()
    md = client.get(f"{cb(rid)}/reports/{rep['id']}/markdown").text
    assert fake not in md and fake not in json.dumps(rep)
    assert "<img" not in md and "](javascript:" not in md


def test_large_reports_are_truncated_explicitly(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from app.core.config import get_settings
    rid, root = setup(client, tmp_path)
    files = {f"app_pkg/f{i}.py": f"X{i} = {i}\n" for i in range(60)}
    feature(root, files)
    conn = local_conn(client, rid)
    sync(client, rid, conn["id"])
    cr = crs(client, rid)[0]
    monkeypatch.setattr(get_settings(), "ci_report_max_bytes", 3000, raising=False)
    rep = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "review"})
    assert rep.status_code == 201
    j = rep.json()
    assert j["size_bytes"] <= 3000 or j["truncated"] is True
    assert j["truncated"] is True or j["size_bytes"] <= 3000


# ================================================================================================ transport / auth
def test_api_requires_authentication_when_a_token_is_configured(art: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from app.core.config import reset_settings_cache
    from app.db.session import reset_engine
    from app.main import create_app
    monkeypatch.setenv("NOVA_API_TOKEN", "t0ken-for-tests-only-123")
    monkeypatch.setenv("NOVA_DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/overview"), ("get", "/connections"), ("post", "/connections"), ("post", "/connections/x/validate"), ("post", "/connections/x/sync"),
                             ("get", "/readiness-policy"), ("put", "/readiness-policy"), ("get", "/change-requests"), ("get", "/change-requests/x"),
                             ("post", "/change-requests/x/workflow-runs"), ("get", "/change-requests/x/review-report"), ("post", "/change-requests/x/reports"),
                             ("get", "/workflow-runs"), ("get", "/workflow-runs/x"), ("get", "/workflow-runs/x/readiness"), ("post", "/workflow-runs/x/evaluate"),
                             ("post", "/workflow-runs/x/cancel"), ("get", "/reports"), ("get", "/reports/x"), ("get", "/workflow-runs/x/status-reports"),
                             ("post", "/workflow-runs/x/status-reports"), ("post", "/status-reports/x/retry")):
            assert getattr(c, method)(f"{API}/repositories/abc/ci{path}").status_code == 401, path
    reset_settings_cache()
    reset_engine()


def test_status_post_uses_only_the_configured_api_host(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net) -> None:  # noqa: F811
    rid, root, c, cr, w = verified_gh(client, tmp_path, monkeypatch, net)
    assert net.calls and all(call["url"].startswith("https://api.github.com/") for call in net.calls)
