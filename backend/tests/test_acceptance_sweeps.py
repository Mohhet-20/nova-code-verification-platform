"""Acceptance: route-wide authorization sweeps. These enumerate EVERY operation in the live OpenAPI document, so a route added later without
authentication or role checks fails here instead of slipping through feature-specific tests."""
from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session_factory
from app.models import Membership, User, UserToken, Workspace
from app.security.auth import hash_token
from tests.test_governance import API, World, w  # noqa: F401 - `w` is the shared fixture

# Operations that are intentionally reachable without the bearer token because they authenticate another way (HMAC signature) or are probes.
PUBLIC = {
    ("GET", "/api/v1/health"), ("GET", "/api/v1/ready"),
    ("POST", "/api/v1/auth/guest"),  # public-demo guest session; 404 unless NOVA_GUEST_ACCESS_ENABLED
}
SIGNATURE_AUTHENTICATED_PREFIXES = ("/api/v1/integrations/inbound", "/api/v1/cicd/webhooks", "/api/v1/webhooks")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def operations(client: TestClient) -> list[tuple[str, str, dict[str, Any]]]:
    spec = client.app.openapi()
    return [(m.upper(), p, op) for p, item in spec["paths"].items() for m, op in item.items() if m.lower() in {"get", "post", "put", "patch", "delete"}]


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "nope", path)


def is_public(method: str, path: str) -> bool:
    return (method, path) in PUBLIC or path.startswith(SIGNATURE_AUTHENTICATED_PREFIXES)


def test_every_operation_rejects_missing_or_wrong_token(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "sweep-global-token-0123456789-abcdefghij")
    monkeypatch.setenv("NOVA_AUTH_FAILURE_LIMIT", "0")  # this sweep checks 401 semantics on every route; throttling is tested in test_deployment_hardening
    reset_settings_cache()
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        ops = operations(c)
        assert len(ops) > 250  # guards against the enumeration silently returning nothing
        leaks, bypass = [], []
        for method, path, _ in ops:
            if is_public(method, path):
                continue
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer nova_u_not-a-real-token"}):
                r = c.request(method, concrete(path), headers=headers, json={} if method in MUTATING else None)
                if r.status_code != 401:
                    leaks.append((method, path, tuple(headers), r.status_code))
        assert not leaks, f"operations reachable without valid credentials: {leaks[:10]}"
        # signature-authenticated endpoints must not be usable without their signature either
        for method, path, _ in ops:
            if path.startswith(SIGNATURE_AUTHENTICATED_PREFIXES) and method in MUTATING:
                r = c.request(method, concrete(path), content=b"{}", headers={"Content-Type": "application/json"})
                bypass.append((method, path, r.status_code)) if r.status_code < 400 else None
        assert not bypass, f"unsigned inbound requests accepted: {bypass[:10]}"


def test_read_only_roles_cannot_mutate_anything(w: World) -> None:  # noqa: F811
    """Viewer and auditor tokens must never get a 2xx from a mutating operation, whatever the route."""
    ops = operations(w.c)
    violations = []
    for who in ("viewer", "aud"):
        for method, path, _ in ops:
            if method not in MUTATING or is_public(method, path):
                continue
            r = w.c.request(method, concrete(path), headers={"Authorization": f"Bearer {w.tokens[who]}"}, json={})
            if r.status_code < 300:
                violations.append((who, method, path, r.status_code))
    assert not violations, f"read-only role performed mutations: {violations[:10]}"


def _second_org(w: World) -> str:  # noqa: F811
    with get_session_factory()() as s:
        ws2, u2 = Workspace(name="Other Org", slug="other-sweep"), User(handle="other-sweep-owner", display_name="Other")
        s.add_all([ws2, u2])
        s.flush()
        s.add(Membership(workspace_id=ws2.id, user_id=u2.id, role="owner", status="active"))
        s.add(UserToken(workspace_id=ws2.id, user_id=u2.id, token_hash=hash_token("nova_u_sweep-other-owner-token"), label="t"))
        s.commit()
    return "nova_u_sweep-other-owner-token"


def test_foreign_organization_cannot_read_or_change_repository_scoped_objects(w: World) -> None:  # noqa: F811
    """Object-level authorization sweep: every operation whose only path parameter is a repository id, called with a real repo id of ANOTHER
    organization, must answer 404/403 - never reveal or modify the object."""
    other = {"Authorization": f"Bearer {_second_org(w)}"}
    repo = w.ids["repoA"]
    leaks, probed = [], 0
    for method, path, _ in operations(w.c):
        params = re.findall(r"\{([^}]+)\}", path)
        if params != ["repository_id"] or is_public(method, path):
            continue
        probed += 1
        r = w.c.request(method, path.replace("{repository_id}", repo), headers=other, json={} if method in MUTATING else None)
        if r.status_code not in (401, 403, 404, 405, 422):
            leaks.append((method, path, r.status_code))
    assert probed > 40, f"only {probed} repository-scoped operations were probed"
    assert not leaks, f"cross-tenant access to repository-scoped routes: {leaks[:10]}"
    # the owner of the repository is not locked out by the same call (the sweep is not passing vacuously)
    assert w.get(f"/repositories/{repo}", "lead").status_code == 200


def test_responses_never_contain_credentials(w: World) -> None:  # noqa: F811
    """No GET response body may contain a user token, token hash or the global API token."""
    secrets = list(w.tokens.values())
    leaked = []
    for method, path, _ in operations(w.c):
        if method != "GET" or "{" in path or is_public(method, path):
            continue
        r = w.get(path.removeprefix(API), "lead")
        if r.status_code == 200 and any(s in r.text for s in secrets):
            leaked.append(path)
    assert not leaked, f"credential material in responses: {leaked}"


# ---------------------------------------------------------------------------------------------------- F-001 request size limit
def test_oversized_request_bodies_are_rejected_before_parsing(client: TestClient) -> None:
    from app.core.config import get_settings

    limit = get_settings().max_request_body_bytes
    big = b'{"title":"' + b"a" * (limit + 10) + b'"}'
    r = client.post(f"{API}/tasks", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    def chunks():  # no Content-Length header: the body is counted as it streams
        for _ in range((limit // 65536) + 2):
            yield b"a" * 65536

    r = client.post(f"{API}/tasks", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    ok = client.post(f"{API}/tasks", json={"title": "small"})  # normal requests are unaffected (validation error, not 413)
    assert ok.status_code != 413


# ------------------------------------------------------------------------------------------ git argument-injection sweep
def _schema_props(spec: dict[str, Any], op: dict[str, Any]) -> set[str]:
    body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    ref = body.get("$ref")
    if ref:
        body = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    props = set(body.get("properties", {}))
    for sub in body.get("allOf", []):
        if "$ref" in sub:
            props |= set(spec["components"]["schemas"][sub["$ref"].rsplit("/", 1)[-1]].get("properties", {}))
    return props


def test_revision_arguments_cannot_inject_git_options(w: World, tmp_path) -> None:  # noqa: F811
    """Every operation that accepts a base/head revision must reject option-like values and must never let git write the file named in `--output=`."""
    spec = w.c.app.openapi()
    victim = tmp_path / "pwned.txt"
    payloads = [f"--output={victim}", "-p", "--upload-pack=touch x", "HEAD --output=" + str(victim), "a..b", "x@{1}"]
    probed = 0
    for method, path, op in operations(w.c):
        props = _schema_props(spec, op) if method == "POST" else set()
        keys = props & {"base", "head", "base_ref", "head_ref"}
        if not keys or re.findall(r"\{([^}]+)\}", path) != ["repository_id"]:
            continue
        probed += 1
        for bad in payloads:
            body = {k: bad for k in keys} | ({"source": "revisions", "mode": "revisions"} if "source" in props else {})
            if "source" not in props and "mode" in props:
                body["mode"] = "revisions"
            r = w.post(path.replace("{repository_id}", w.ids["repoA"]).removeprefix(API), "lead", body)
            assert r.status_code >= 400, (path, bad, r.status_code, r.text[:200])
    assert probed >= 2, f"only {probed} revision-accepting operations found"
    assert not victim.exists()


# ------------------------------------------------------------------------------------------------- path traversal sweep
def test_path_parameters_reject_traversal_everywhere(w: World, tmp_path) -> None:  # noqa: F811
    """Every operation exposing a path-like query parameter or body field is probed with traversal payloads; none may return file content from
    outside the repository or answer 5xx."""
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET-OUTSIDE-CONTENT")
    spec = w.c.app.openapi()
    payloads = ["../../outside-secret.txt", str(secret), "..\\..\\outside-secret.txt", "a/../../outside-secret.txt", "%2e%2e/%2e%2e/outside-secret.txt", "\x00", "/etc/passwd"]
    names = {"path", "file", "rel_path", "file_path", "target_path", "paths"}
    probed, bad = 0, []
    for method, path, op in operations(w.c):
        if re.findall(r"\{([^}]+)\}", path) != ["repository_id"] or is_public(method, path):
            continue
        qnames = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"} & names
        bnames = (_schema_props(spec, op) & names) if method in MUTATING else set()
        for name, where in [(n, "query") for n in qnames] + [(n, "body") for n in bnames]:
            probed += 1
            for payload in payloads:
                url = path.replace("{repository_id}", w.ids["repoA"]).removeprefix(API)
                if where == "query":
                    r = w.call(method, url, "lead", params={name: payload}, json={} if method in MUTATING else None)
                else:
                    value: Any = [payload] if name == "paths" else payload
                    r = w.call(method, url, "lead", json={name: value})
                if "TOP-SECRET-OUTSIDE-CONTENT" in r.text or "root:x:" in r.text or r.status_code >= 500:
                    bad.append((method, path, name, payload[:30], r.status_code))
    assert probed >= 5, f"only {probed} path-like parameters found"
    assert not bad, f"traversal or server error: {bad[:8]}"
