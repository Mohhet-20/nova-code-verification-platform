"""Teams, authorization, tenant isolation, policies, workflows, review queue and audit - including abuse cases."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import reset_settings_cache
from app.db.session import get_session_factory, reset_engine
from app.models import (
    ChangeRequest,
    CodeReview,
    CvRun,
    Membership,
    User,
    UserToken,
    WorkflowInstance,
    Workspace,
)
from app.security.auth import hash_token
from tests.conftest import alembic_config

API = "/api/v1"


class World:
    """One organization with a lead, engineers, reviewers, an auditor, a viewer and two teams' worth of resources."""

    def __init__(self, client: TestClient, tmp: Path) -> None:
        self.c, self.tmp = client, tmp
        self.tokens: dict[str, str] = {}
        self.ids: dict[str, str] = {}

    # ---- plumbing
    def call(self, method: str, path: str, who: str | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        headers = {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}
        return self.c.request(method, f"{API}{path}", headers=headers, **kw)

    def get(self, p: str, who: str | None = None, **kw: Any):  # type: ignore[no-untyped-def]
        return self.call("GET", p, who, **kw)

    def post(self, p: str, who: str | None = None, json: Any = None):  # type: ignore[no-untyped-def]
        return self.call("POST", p, who, json=json if json is not None else {})

    def put(self, p: str, who: str | None = None, json: Any = None):  # type: ignore[no-untyped-def]
        return self.call("PUT", p, who, json=json if json is not None else {})

    def member(self, handle: str, role: str) -> str:
        r = self.post("/org/members", json={"handle": handle, "display_name": handle.title(), "role": role})
        assert r.status_code == 201, r.text
        self.tokens[handle] = r.json()["token"]
        self.ids[handle] = r.json()["member"]["user_id"]
        return self.ids[handle]

    def db(self):  # type: ignore[no-untyped-def]
        return get_session_factory()()

    def repo(self, name: str) -> str:
        root = self.tmp / name
        root.mkdir(exist_ok=True)
        (root / "a.py").write_text("x = 1\n")
        r = self.post("/repositories", json={"name": name, "root_path": str(root)})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def review(self, repo_id: str, author: str | None = None, risk: str = "low", head: str = "sha1", status: str = "completed") -> str:
        with self.db() as s:
            r = CodeReview(repository_id=repo_id, mode="standard", status=status, head_sha=head, risk_category=risk, created_by=self.ids.get(author or "", None))
            s.add(r)
            s.commit()
            return r.id

    def change_request(self, repo_id: str, head: str = "sha1") -> str:
        with self.db() as s:
            r = ChangeRequest(repository_id=repo_id, provider="github", external_id=f"pr-{head}", head_sha=head, title="A change", state="open")
            s.add(r)
            s.commit()
            return r.id

    def gate(self, repo_id: str, head: str, outcome: str = "PASS") -> None:
        with self.db() as s:
            s.add(CvRun(repository_id=repo_id, status="completed", gate_outcome=outcome, head_sha=head))
            s.commit()

    def instance(self, who: str, rtype: str, rid: str, **kw: Any) -> dict[str, Any]:
        r = self.post("/workflows/instances", who, {"resource_type": rtype, "resource_id": rid, **kw})
        assert r.status_code == 201, r.text
        return r.json()

    def submit(self, who: str, iid: str):  # type: ignore[no-untyped-def]
        return self.post(f"/workflows/instances/{iid}/transitions", who, {"action": "submit"})

    def decide(self, who: str, iid: str, decision: str = "approve", justification: str = "", **kw: Any):  # type: ignore[no-untyped-def]
        return self.post(f"/workflows/instances/{iid}/decisions", who, {"decision": decision, "justification": justification, **kw})


@pytest.fixture
def w(client: TestClient, tmp_path: Path) -> World:
    w = World(client, tmp_path)
    assert w.post("/governance/seed-defaults").status_code == 200
    w.member("lead", "maintainer")
    for h in ("eng1", "eng2", "outsider"):
        w.member(h, "engineer")
    for h in ("rev1", "rev2", "rev3"):
        w.member(h, "reviewer")
    w.member("aud", "auditor")
    w.member("viewer", "viewer")
    t = w.post("/teams", json={"name": "Alpha", "description": "Alpha team"})
    assert t.status_code == 201, t.text
    w.ids["alpha"] = t.json()["id"]
    for h, role in (("eng1", "admin"), ("eng2", "member"), ("rev1", "reviewer"), ("rev2", "reviewer")):
        assert w.post(f"/teams/{w.ids['alpha']}/members", json={"user_id": w.ids[h], "role": role}).status_code == 201
    w.ids["repoA"] = w.repo("repo-a")
    w.ids["repoFree"] = w.repo("repo-free")
    assert w.put(f"/teams/{w.ids['alpha']}/ownership", json={"repository_id": w.ids["repoA"]}).status_code == 200
    return w


# ===================================================================================================== authentication
def test_user_tokens_and_global_token_gate(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_API_TOKEN", "global-secret")
    reset_settings_cache()
    from app.main import create_app

    with TestClient(create_app()) as c:
        assert c.get(f"{API}/teams").status_code == 401
        assert c.get(f"{API}/teams", headers={"Authorization": "Bearer nova_u_bogus"}).status_code == 401
        assert c.get(f"{API}/teams", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get(f"{API}/teams", headers={"Authorization": "Bearer global-secret"})
        assert ok.status_code == 200
        made = c.post(f"{API}/org/members", headers={"Authorization": "Bearer global-secret"}, json={"handle": "dev1", "role": "engineer"})
        tok = made.json()["token"]
        assert c.get(f"{API}/governance/me", headers={"Authorization": f"Bearer {tok}"}).json()["org_role"] == "engineer"
    with get_session_factory()() as s:  # only a digest is stored, never the token
        row = s.scalar(select(UserToken))
        assert row is not None and row.token_hash == hash_token(tok) and tok not in row.token_hash
    reset_engine()
    reset_settings_cache()


def test_member_lifecycle_and_last_admin_protection(w: World) -> None:
    owner = w.get("/governance/me").json()
    assert owner["org_role"] == "owner" and owner["auth_kind"] == "local"
    # engineers cannot manage membership
    assert w.post("/org/members", "eng1", {"handle": "evil", "role": "owner"}).status_code == 403
    assert w.put(f"/org/members/{w.ids['eng2']}/role", "eng1", {"role": "owner"}).status_code == 403
    assert w.post("/org/members", json={"handle": "Bad Handle!", "role": "viewer"}).status_code == 422
    assert w.post("/org/members", json={"handle": "lead", "role": "viewer"}).status_code == 409  # taken
    assert w.post("/org/members", json={"handle": "newbie", "role": "wizard"}).status_code == 422
    assert w.put(f"/org/members/{w.ids['eng2']}/role", json={"role": "reviewer"}).json()["role"] == "reviewer"
    # the only administrator can neither be demoted nor removed
    me = owner["user_id"]
    assert w.put(f"/org/members/{me}/role", json={"role": "viewer"}).status_code == 409
    assert w.post(f"/org/members/{me}/remove").status_code == 409
    # with a second administrator the first can be demoted
    w.member("admin2", "owner")
    assert w.put(f"/org/members/{me}/role", "admin2", {"role": "maintainer"}).status_code == 200
    assert w.post(f"/org/members/{w.ids['admin2']}/remove", "admin2").status_code == 409  # now the last one
    # removal revokes the token immediately and ends team memberships
    assert w.get("/governance/me", "eng2").status_code == 200
    assert w.post(f"/org/members/{w.ids['eng2']}/remove", "admin2").status_code == 200
    assert w.get("/governance/me", "eng2").status_code == 401
    members = w.get(f"/teams/{w.ids['alpha']}/members", "admin2").json()["items"]
    assert w.ids["eng2"] not in [m["user_id"] for m in members]
    # token rotation invalidates the old token
    old = w.tokens["eng1"]
    new = w.post(f"/org/members/{w.ids['eng1']}/token", "eng1").json()["token"]
    assert w.c.get(f"{API}/governance/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert w.c.get(f"{API}/governance/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_requests_cannot_claim_roles_or_scope(w: World) -> None:
    r = w.post("/teams", "eng1", {"name": "Rogue", "role": "owner", "organization_id": "x"})
    assert r.status_code in (403, 422)
    r = w.post("/workflows/instances", "eng1", {"resource_type": "code_review", "resource_id": "x", "role": "owner"})
    assert r.status_code == 422
    r = w.post("/governance/evaluations", "eng1", {"resource_type": "code_review", "resource_id": "x", "status": "pass"})
    assert r.status_code == 422  # a client can never supply an evaluation result


# ===================================================================================================== teams & isolation
def test_team_crud_search_pagination_and_archive_rules(w: World) -> None:
    assert w.post("/teams", json={"name": "Alpha"}).status_code == 409
    assert w.post("/teams", "eng1", {"name": "Mine"}).status_code == 403
    for n in ("Beta", "Gamma", "Delta"):
        assert w.post("/teams", json={"name": n}).status_code == 201
    page1 = w.get("/teams?page_size=2&page=1").json()
    page2 = w.get("/teams?page_size=2&page=2").json()
    names = [t["name"] for t in page1["items"] + page2["items"]]
    assert page1["total"] == 4 and names == sorted(names) and len(set(names)) == 4
    assert [t["name"] for t in w.get("/teams?q=gam").json()["items"]] == ["Gamma"]
    assert w.put(f"/teams/{w.ids['alpha']}", "eng1", {"description": "new"}).json()["description"] == "new"  # team admin
    assert w.put(f"/teams/{w.ids['alpha']}", "eng2", {"description": "x"}).status_code == 403  # team member
    assert w.post(f"/teams/{w.ids['alpha']}/archive", "eng1").status_code == 403  # archive is organization-level
    assert w.post(f"/teams/{w.ids['alpha']}/archive").status_code == 409  # still owns a repository
    assert w.post(f"/teams/{w.ids['alpha']}/ownership/{w.ids['repoA']}/release").status_code == 204
    assert w.post(f"/teams/{w.ids['alpha']}/archive").status_code == 200
    assert w.post(f"/teams/{w.ids['alpha']}/members", json={"user_id": w.ids["eng2"], "role": "member"}).status_code == 409  # archived
    assert w.get("/teams").json()["total"] == 3 and w.get("/teams?include_archived=true").json()["total"] == 4


def test_membership_rules_and_team_admin_scope(w: World) -> None:
    a = w.ids["alpha"]
    assert w.post(f"/teams/{a}/members", "eng1", {"user_id": w.ids["viewer"], "role": "member"}).status_code == 201  # team admin adds to own team
    assert w.post(f"/teams/{a}/members", "eng1", {"user_id": w.ids["viewer"], "role": "member"}).status_code == 409  # duplicate
    assert w.post(f"/teams/{a}/members", "eng2", {"user_id": w.ids["outsider"], "role": "member"}).status_code == 403  # plain member
    assert w.post(f"/teams/{a}/members", "eng1", {"user_id": w.ids["outsider"], "role": "wizard"}).status_code == 422
    assert w.post(f"/teams/{a}/members", "eng1", {"user_id": "nonexistent", "role": "member"}).status_code == 404
    # a team administrator has no organization powers
    assert w.put(f"/org/members/{w.ids['eng2']}/role", "eng1", {"role": "owner"}).status_code == 403
    assert w.post("/teams", "eng1", {"name": "Another"}).status_code == 403
    assert w.post("/governance/policies", "eng1", {"key": "p-x", "name": "x", "policy_type": "code_review_completed", "severity": "warning"}).status_code == 403
    # a team keeps at least one administrator
    assert w.put(f"/teams/{a}/members/{w.ids['eng1']}", "eng1", {"role": "member"}).status_code == 409
    assert w.post(f"/teams/{a}/members/{w.ids['eng1']}/remove", "eng1").status_code == 409
    # re-adding a removed member reuses the unique row
    assert w.post(f"/teams/{a}/members/{w.ids['rev2']}/remove", "eng1").status_code == 200
    assert w.post(f"/teams/{a}/members", "eng1", {"user_id": w.ids["rev2"], "role": "reviewer"}).status_code == 201
    with w.db() as s:
        n = s.execute(text("SELECT count(*) FROM team_members WHERE team_id=:t AND user_id=:u"), {"t": a, "u": w.ids["rev2"]}).scalar()
        assert n == 1
        with pytest.raises(IntegrityError):  # the database itself rejects duplicates
            s.execute(text("INSERT INTO team_members(id,team_id,user_id,role,status,created_at,updated_at) VALUES('dup',:t,:u,'member','active',datetime('now'),datetime('now'))"), {"t": a, "u": w.ids["rev2"]})


def test_cross_tenant_isolation(w: World) -> None:
    with w.db() as s:  # a second organization with its own owner token
        ws2 = Workspace(name="Other Org", slug="other")
        u2 = User(handle="other-owner", display_name="Other Owner")
        s.add_all([ws2, u2])
        s.flush()
        s.add(Membership(workspace_id=ws2.id, user_id=u2.id, role="owner", status="active"))
        s.add(UserToken(workspace_id=ws2.id, user_id=u2.id, token_hash=hash_token("nova_u_other-owner-token"), label="t"))
        s.commit()
    w.tokens["other"] = "nova_u_other-owner-token"
    rev = w.review(w.ids["repoA"])
    inst = w.instance("eng1", "code_review", rev)
    assert w.get("/teams", "other").json()["total"] == 0
    assert w.get(f"/teams/{w.ids['alpha']}", "other").status_code == 404
    assert w.get(f"/teams/{w.ids['alpha']}/members", "other").status_code == 404
    assert w.get(f"/workflows/instances/{inst['id']}", "other").status_code == 404
    assert w.post(f"/workflows/instances/{inst['id']}/decisions", "other", {"decision": "approve"}).status_code == 404
    assert w.post("/workflows/instances", "other", {"resource_type": "code_review", "resource_id": rev}).status_code == 404
    assert w.get("/review-queue", "other").json()["total"] == 0
    assert w.get("/governance/audit", "other").json()["total"] == 0  # none of the first organization's history is visible
    assert w.get("/governance/policies", "other").json()["total"] == 0
    other_team = w.post("/teams", "other", {"name": "Their team"}).json()["id"]
    assert w.post(f"/teams/{other_team}/members", "other", {"user_id": w.ids["eng1"], "role": "member"}).status_code == 404  # cannot import users from another org
    assert w.put(f"/teams/{other_team}/ownership", "other", {"repository_id": w.ids["repoFree"]}).status_code == 404
    assert w.post("/governance/evaluations", "other", {"resource_type": "code_review", "resource_id": rev}).status_code == 404


def test_restricted_team_resources_are_hidden_from_non_members(w: World) -> None:
    rid = w.ids["repoA"]
    assert w.get(f"/repositories/{rid}", "outsider").status_code == 404  # guessing the id reveals nothing
    ids = [r["id"] for r in w.get("/repositories", "outsider").json()["items"]]
    assert rid not in ids and w.ids["repoFree"] in ids  # unowned repositories stay workspace-visible
    assert w.get(f"/repositories/{rid}", "eng2").status_code == 200  # team member
    assert w.get(f"/repositories/{rid}", "aud").status_code == 200  # auditor: org-wide read
    assert w.get(f"/teams/{w.ids['alpha']}", "outsider").status_code == 404
    assert w.get("/teams", "outsider").json()["total"] == 0 and w.get("/teams", "aud").json()["total"] == 1
    rev = w.review(rid)
    assert w.post("/workflows/instances", "outsider", {"resource_type": "code_review", "resource_id": rev}).status_code == 404
    assert w.post("/review-queue", "outsider", {"resource_type": "code_review", "resource_id": rev}).status_code == 404


def test_ownership_rules(w: World) -> None:
    b = w.post("/teams", json={"name": "Beta"}).json()["id"]
    w.post(f"/teams/{b}/members", json={"user_id": w.ids["rev3"], "role": "admin"})
    a = w.ids["alpha"]
    # accountable owner must belong to the owning team
    assert w.put(f"/teams/{a}/ownership", "eng1", {"repository_id": w.ids["repoFree"], "accountable_user_id": w.ids["outsider"]}).status_code == 422
    ok = w.put(f"/teams/{a}/ownership", "eng1", {"repository_id": w.ids["repoFree"], "accountable_user_id": w.ids["eng2"]})
    assert ok.status_code == 200 and ok.json()["accountable_handle"] == "eng2"
    # a team administrator cannot take over another team's repository; the organization administrator can
    assert w.put(f"/teams/{b}/ownership", "rev3", {"repository_id": w.ids["repoFree"]}).status_code == 404  # hidden from Beta's admin now
    assert w.put(f"/teams/{b}/ownership", json={"repository_id": w.ids["repoFree"]}).status_code == 200
    owned = w.get(f"/teams/{b}/ownership", "rev3").json()
    assert owned["total"] == 1 and owned["items"][0]["repository_id"] == w.ids["repoFree"]
    acts = [e["action"] for e in w.get("/governance/audit?action_prefix=ownership.").json()["items"]]
    assert acts.count("ownership.assigned") == 3


# ===================================================================================================== policies
def test_policy_validation_permissions_and_versioning(w: World) -> None:
    base = {"key": "pol-one", "name": "One", "policy_type": "max_change_risk", "severity": "warning", "config": {"max_level": "moderate"}}
    assert w.post("/governance/policies", json={**base, "policy_type": "run_shell"}).status_code == 422  # unknown type
    assert w.post("/governance/policies", json={**base, "config": {"max_level": "moderate", "cmd": "rm -rf /"}}).status_code == 422  # unknown field
    assert w.post("/governance/policies", json={**base, "config": {"max_level": "catastrophic"}}).status_code == 422
    assert w.post("/governance/policies", json={**base, "severity": "fatal"}).status_code == 422
    assert w.post("/governance/policies", json={**base, "resource_types": ["spaceship"]}).status_code == 422
    assert w.post("/governance/policies", "lead", {**base, "severity": "blocking"}).status_code == 403  # leads cannot create blocking policy
    made = w.post("/governance/policies", "lead", base)
    assert made.status_code == 201 and made.json()["version"] == 1
    pid = made.json()["id"]
    assert w.post("/governance/policies", json=base).status_code == 409  # key unique
    assert w.put(f"/governance/policies/{pid}", "eng1", {"enabled": False}).status_code == 403
    assert w.put(f"/governance/policies/{pid}", "lead", {"severity": "blocking"}).status_code == 403  # cannot escalate to blocking
    up = w.put(f"/governance/policies/{pid}", "lead", {"config": {"max_level": "low"}, "note": "tighten"})
    assert up.status_code == 200 and up.json()["version"] == 2
    assert w.put(f"/governance/policies/{pid}", "lead", {"config": {"max_level": "low"}}).json()["version"] == 2  # no-op does not bump
    versions = w.get(f"/governance/policies/{pid}/versions").json()["items"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["snapshot"]["config"] == {"max_level": "moderate"}
    # blocking policies: only the organization administrator may edit or disable them
    blocking = [p for p in w.get("/governance/policies?severity=blocking").json()["items"] if p["key"] == "independent-approval"][0]
    assert w.put(f"/governance/policies/{blocking['id']}", "lead", {"enabled": False}).status_code == 403
    assert w.put(f"/governance/policies/{blocking['id']}", "lead", {"severity": "informational"}).status_code == 403
    assert w.put(f"/governance/policies/{blocking['id']}", json={"enabled": False}).status_code == 200
    audit = w.get("/governance/audit?action_prefix=policy.").json()["items"]
    assert "policy.disabled" in [e["action"] for e in audit] and all(e["actor_handle"] for e in audit)
    assert any(e["action"] == "authz.denied" for e in w.get("/governance/audit?action_prefix=authz.").json()["items"])


def test_policy_types_listing_exposes_schemas(w: World) -> None:
    types = {t["type"]: t for t in w.get("/governance/policy-types", "viewer").json()}
    assert {"required_reviewers", "independent_approvals", "verification_required", "max_change_risk", "security_checks_required", "evidence_required",
            "prohibited_transition", "release_readiness", "code_review_completed"} <= set(types)
    assert types["max_change_risk"]["config_schema"]["additionalProperties"] is False


def _policy(w: World, key: str, ptype: str, cfg: dict[str, Any], severity: str = "blocking", rtypes: list[str] | None = None) -> str:
    r = w.post("/governance/policies", json={"key": key, "name": key, "policy_type": ptype, "severity": severity, "config": cfg, "resource_types": rtypes or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_evaluation_missing_evidence_is_never_a_pass_and_is_deterministic(w: World) -> None:
    cr = w.change_request(w.ids["repoA"], head="h1")
    _policy(w, "need-ver", "verification_required", {"allowed_outcomes": ["PASS"]}, rtypes=["change_request"])
    _policy(w, "need-sec", "security_checks_required", {}, severity="warning", rtypes=["change_request"])
    _policy(w, "need-risk", "max_change_risk", {"max_level": "moderate"}, rtypes=["change_request"])
    _policy(w, "need-ev", "evidence_required", {"labels": ["design-doc"]}, severity="warning", rtypes=["change_request"])
    out = w.post("/governance/evaluations", json={"resource_type": "change_request", "resource_id": cr}).json()
    by = {r["policy"]["key"]: r for r in out["results"]}
    assert by["need-ver"]["status"] == "unavailable" and by["need-ver"]["blocking"] is True  # no evidence: explicit non-pass
    assert by["need-sec"]["status"] == "warning" and by["need-sec"]["raw_status"] == "unavailable"  # non-blocking, still not "pass"
    assert by["need-risk"]["status"] == "unavailable" and by["need-ev"]["status"] == "warning"
    assert out["blocked"] is True and "need-ver" in out["blocking_policies"]
    assert all("Cannot be evaluated" in by[k]["explanation"] or by[k]["status"] == "warning" for k in ("need-ver",))
    # evidence arrives: the same inputs now pass; two evaluations of unchanged data agree
    w.gate(w.ids["repoA"], "h1", "PASS")
    a = w.post("/governance/evaluations", json={"resource_type": "change_request", "resource_id": cr}).json()
    b = w.post("/governance/evaluations", json={"resource_type": "change_request", "resource_id": cr}).json()
    key = lambda o: sorted((r["policy"]["key"], r["status"], r["raw_status"]) for r in o["results"])  # noqa: E731
    assert key(a) == key(b)
    assert {r["policy"]["key"]: r["status"] for r in a["results"]}["need-ver"] == "pass"
    # evidence for ANOTHER revision does not count
    cr2 = w.change_request(w.ids["repoA"], head="h2")
    other = {r["policy"]["key"]: r["status"] for r in w.post("/governance/evaluations", json={"resource_type": "change_request", "resource_id": cr2}).json()["results"]}
    assert other["need-ver"] == "unavailable"
    w.gate(w.ids["repoA"], "h2", "FAIL")
    failed = {r["policy"]["key"]: r["status"] for r in w.post("/governance/evaluations", json={"resource_type": "change_request", "resource_id": cr2}).json()["results"]}
    assert failed["need-ver"] == "fail"


def test_historical_evaluations_keep_their_policy_version(w: World) -> None:
    pid = _policy(w, "hist", "max_change_risk", {"max_level": "high"}, rtypes=["code_review"])
    rev = w.review(w.ids["repoFree"], risk="critical")
    first = w.post("/governance/evaluations", json={"resource_type": "code_review", "resource_id": rev}).json()
    assert {r["policy"]["key"]: r["status"] for r in first["results"]}["hist"] == "fail"
    assert w.put(f"/governance/policies/{pid}", json={"config": {"max_level": "critical"}}).json()["version"] == 2
    second = w.post("/governance/evaluations", json={"resource_type": "code_review", "resource_id": rev}).json()
    assert {r["policy"]["key"]: r["status"] for r in second["results"]}["hist"] == "pass"
    rows = [e for e in w.get(f"/governance/evaluations?policy_key=hist&resource_id={rev}&resource_type=code_review").json()["items"]]
    assert sorted((e["policy_version"], e["status"]) for e in rows) == [(1, "fail"), (2, "pass")]  # the old decision was not rewritten


def test_prohibited_transition_and_unknown_type_fail_safe(w: World) -> None:
    rev_high = w.review(w.ids["repoFree"], risk="high")
    rev_low = w.review(w.ids["repoFree"], risk="low", head="s2")
    _policy(w, "no-submit-high", "prohibited_transition", {"transition": "submit", "when_risk_at_least": "high"}, rtypes=["code_review"])
    i_low = w.instance("eng1", "code_review", rev_low)
    assert w.submit("eng1", i_low["id"]).status_code == 200
    i_high = w.instance("eng1", "code_review", rev_high)
    blocked = w.submit("eng1", i_high["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["details"]["code"] == "policy_blocked"
    assert w.get(f"/workflows/instances/{i_high['id']}", "eng1").json()["state"] == "DRAFT"  # nothing partially applied
    # a stored policy of an unknown type (e.g. written by a newer version) is reported, never ignored
    with w.db() as s:
        from app.models import GovernancePolicy

        s.add(GovernancePolicy(workspace_id=s.scalar(select(Workspace.id)), key="from-the-future", name="x", policy_type="quantum", severity="blocking", resource_types=[], config={}, version=1))
        s.commit()
    out = w.post("/governance/evaluations", json={"resource_type": "code_review", "resource_id": rev_low}).json()
    fut = [r for r in out["results"] if r["policy"]["key"] == "from-the-future"][0]
    assert fut["status"] == "unavailable" and fut["blocking"] is True


# ===================================================================================================== workflows
def _flow(w: World, repo: str = "repoA", author: str = "eng2", risk: str = "low", head: str = "sha1") -> str:
    rev = w.review(w.ids[repo], author=author, risk=risk, head=head)
    inst = w.instance("eng1", "code_review", rev)
    assert w.submit("eng1", inst["id"]).status_code == 200
    return inst["id"]


def test_happy_path_state_machine_and_timeline(w: World) -> None:
    rev = w.review(w.ids["repoA"], author="eng2")
    inst = w.instance("eng1", "code_review", rev, priority="high", title="Check this")
    iid = inst["id"]
    assert inst["state"] == "DRAFT" and inst["team_id"] == w.ids["alpha"] and inst["definition_key"] == "code-review-review"
    # a second live workflow for the same resource is refused
    assert w.post("/workflows/instances", "eng1", {"resource_type": "code_review", "resource_id": rev}).status_code == 409
    # cannot decide before submission, cannot jump states, cannot "approve" through a transition
    assert w.decide("rev1", iid).status_code == 409
    for bad in ("approve", "reject", "complete", "bogus"):
        assert w.post(f"/workflows/instances/{iid}/transitions", "eng1", {"action": bad}).status_code in (409, 422)
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["state"] == "DRAFT"
    assert w.submit("eng1", iid).json()["state"] == "IN_REVIEW"
    assert w.submit("eng1", iid).status_code == 409  # not resubmittable while in review
    ok = w.decide("rev1", iid, "approve", "Looks right")
    assert ok.status_code == 201 and ok.json()["state"] == "APPROVED"
    assert w.decide("rev2", iid).status_code == 409  # already decided
    assert w.post(f"/workflows/instances/{iid}/transitions", "eng2", {"action": "complete"}).status_code == 403  # a bystander cannot close it
    assert w.post(f"/workflows/instances/{iid}/transitions", "rev2", {"action": "complete"}).status_code == 403
    done = w.post(f"/workflows/instances/{iid}/transitions", "eng1", {"action": "complete"})  # completing only closes an already-approved workflow
    assert done.status_code == 200 and done.json()["state"] == "COMPLETED"
    assert w.post(f"/workflows/instances/{iid}/transitions", "eng1", {"action": "cancel"}).status_code == 409  # terminal
    ev = w.get(f"/workflows/instances/{iid}/events", "eng1").json()["items"]
    types = [e["event_type"] for e in ev]
    assert types[0] == "created" and "decision_recorded" in types and types[-1] == "state_changed" and types.count("state_changed") == 4
    assert ev[-1]["data"].get("action") == "complete" and ev[-1]["to_state"] == "COMPLETED"
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev)
    assert w.get("/review-queue?status=open").json()["total"] == 0  # queue item closed with the workflow


def test_no_self_approval_and_independence(w: World) -> None:
    iid = _flow(w, author="eng2")
    # requester (eng1) is a team admin but cannot approve their own request
    r = w.decide("eng1", iid)
    assert r.status_code == 403
    # the author of the change (eng2, plain member) is not eligible either
    assert w.decide("eng2", iid).status_code == 403
    # an engineer with no reviewing role cannot approve
    assert w.decide("outsider", iid).status_code == 404  # hidden: not on the team
    assert w.decide("viewer", iid).status_code == 404
    # an auditor can see but never decide
    assert w.get(f"/workflows/instances/{iid}", "aud").status_code == 200
    assert w.decide("aud", iid).status_code == 403
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["state"] == "IN_REVIEW"
    assert w.decide("rev1", iid).json()["state"] == "APPROVED"
    # when the requester is also an organization owner the independent flag still holds
    rev = w.review(w.ids["repoFree"], risk="low", head="own")
    inst = w.post("/workflows/instances", json={"resource_type": "code_review", "resource_id": rev}).json()
    w.submit(None, inst["id"])  # type: ignore[arg-type]
    own = w.decide(None, inst["id"])  # type: ignore[arg-type]
    assert own.status_code == 403 and "Independent" in own.json()["error"]["message"]
    audit = w.get("/governance/audit?action_prefix=authz.").json()["items"]
    assert any(e["action"] == "authz.denied" for e in audit)


def test_reject_is_final_and_changes_requested_starts_new_round(w: World) -> None:
    iid = _flow(w)
    assert w.decide("rev1", iid, "reject", "").status_code == 422  # justification required
    assert w.decide("rev1", iid, "request_changes", "please add tests").json()["state"] == "CHANGES_REQUESTED"
    assert w.decide("rev2", iid).status_code == 409
    assert w.post(f"/workflows/instances/{iid}/transitions", "eng2", {"action": "resubmit"}).status_code == 403  # only the requester
    again = w.post(f"/workflows/instances/{iid}/transitions", "eng1", {"action": "resubmit"}).json()
    assert again["state"] == "IN_REVIEW" and again["round"] == 2
    # the previous round's decision does not count; the same reviewer can decide again in the new round
    assert w.decide("rev1", iid, "approve", "ok now").json()["state"] == "APPROVED"
    iid2 = _flow(w, head="r2")
    assert w.decide("rev1", iid2, "reject", "unsafe").json()["state"] == "REJECTED"
    assert w.post(f"/workflows/instances/{iid2}/transitions", "eng1", {"action": "resubmit"}).status_code == 409  # rejection is final
    assert w.decide("rev2", iid2, "approve").status_code == 409
    with w.db() as s:
        rounds = s.execute(text("SELECT round, decision FROM workflow_decisions WHERE instance_id=:i ORDER BY created_at"), {"i": iid}).all()
        assert [r[0] for r in rounds] == [1, 2]


def test_abstain_does_not_approve_and_cancel_rules(w: World) -> None:
    iid = _flow(w)
    assert w.decide("rev1", iid, "abstain").json()["state"] == "IN_REVIEW"
    assert w.decide("rev1", iid, "approve").status_code == 409  # one decision per stage per reviewer
    assert w.post(f"/workflows/instances/{iid}/transitions", "eng2", {"action": "cancel"}).status_code == 403
    assert w.post(f"/workflows/instances/{iid}/transitions", "eng1", {"action": "cancel", "note": "no longer needed"}).json()["state"] == "CANCELLED"
    assert w.decide("rev2", iid).status_code == 409
    # a new workflow may be started once the previous one is no longer live
    rev = w.get(f"/workflows/instances/{iid}", "eng1").json()["resource_id"]
    assert w.post("/workflows/instances", "eng1", {"resource_type": "code_review", "resource_id": rev}).status_code == 201


def test_decision_input_validation(w: World) -> None:
    iid = _flow(w)
    assert w.decide("rev1", iid, "maybe").status_code == 422
    assert w.decide("rev1", iid, "approve", "x" * 2001).status_code == 422
    assert w.post(f"/workflows/instances/{iid}/decisions", "rev1", {"decision": "approve", "user_id": "someone-else"}).status_code == 422
    assert w.post(f"/workflows/instances/{iid}/decisions", "rev1", {"decision": "approve", "state": "APPROVED"}).status_code == 422
    assert w.decide("rev1", "no-such-id").status_code == 404
    assert w.get(f"/workflows/instances/{iid}", "rev1").json()["state"] == "IN_REVIEW"


def test_stale_state_and_double_submit_are_safe(w: World) -> None:
    iid = _flow(w)
    first = w.decide("rev1", iid)
    second = w.decide("rev1", iid)
    assert first.status_code == 201 and second.status_code == 409
    with w.db() as s:
        n = s.execute(text("SELECT count(*) FROM workflow_decisions WHERE instance_id=:i"), {"i": iid}).scalar()
        assert n == 1
        with pytest.raises(IntegrityError):  # the database also enforces one decision per reviewer/round/stage
            s.execute(text("INSERT INTO workflow_decisions(id,instance_id,round,stage_index,user_id,decision,justification,created_at) "
                           "SELECT 'dupdec',instance_id,round,stage_index,user_id,'approve','',created_at FROM workflow_decisions WHERE instance_id=:i"), {"i": iid})


def test_optimistic_locking_detects_concurrent_writers(w: World) -> None:
    iid = _flow(w)
    f = get_session_factory()
    s1, s2 = f(), f()
    a = s1.get(WorkflowInstance, iid)
    b = s2.get(WorkflowInstance, iid)
    a.title = "writer one"
    s1.commit()
    b.title = "writer two"
    with pytest.raises(StaleDataError):
        s2.commit()
    s2.rollback()
    s1.close()
    s2.close()


def test_concurrent_approvals_produce_exactly_one_outcome(w: World) -> None:
    import threading

    iid = _flow(w)
    codes: list[int] = []
    lock = threading.Lock()

    def go(who: str) -> None:
        r = w.decide(who, iid)
        with lock:
            codes.append(r.status_code)

    ts = [threading.Thread(target=go, args=(h,)) for h in ("rev1", "rev2", "lead", "rev3")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409, 403, 404}
    with w.db() as s:
        assert s.scalar(select(WorkflowInstance.state).where(WorkflowInstance.id == iid)) == "APPROVED"
        assert s.execute(text("SELECT count(*) FROM workflow_decisions WHERE instance_id=:i AND decision='approve'"), {"i": iid}).scalar() == 1


def test_multi_stage_sequential_and_quorum(w: World) -> None:
    spec = {"mode": "sequential", "required_policy_keys": [], "stages": [
        {"name": "Peers", "approver_roles": ["reviewer"], "approver_team_roles": ["reviewer"], "min_approvals": 2, "independent": True},
        {"name": "Lead", "approver_roles": ["owner", "maintainer"], "approver_team_roles": [], "min_approvals": 1, "independent": True}]}
    d = w.post("/workflows/definitions", json={"key": "two-stage", "name": "Two stage", "resource_type": "code_review", "spec": spec})
    assert d.status_code == 201, d.text
    assert w.post("/workflows/definitions", "eng1", {"key": "no-access", "name": "x", "resource_type": "code_review", "spec": spec}).status_code == 403
    bad = {**spec, "stages": []}
    assert w.post("/workflows/definitions", json={"key": "bad-one", "name": "bad", "resource_type": "code_review", "spec": bad}).status_code == 422
    bad2 = {**spec, "stages": [{**spec["stages"][0], "min_approvals": 0}]}
    assert w.post("/workflows/definitions", json={"key": "bad-two", "name": "bad", "resource_type": "code_review", "spec": bad2}).status_code == 422
    bad3 = {**spec, "required_policy_keys": ["no-such-policy"]}
    assert w.post("/workflows/definitions", json={"key": "bad-three", "name": "bad", "resource_type": "code_review", "spec": bad3}).status_code == 422
    rev = w.review(w.ids["repoA"], author="eng2")
    iid = w.instance("eng1", "code_review", rev, definition_key="two-stage")["id"]
    w.submit("eng1", iid)
    # the lead stage is not open yet
    assert w.decide("lead", iid).status_code in (403, 409)
    assert w.decide("rev1", iid).json()["state"] == "IN_REVIEW"
    assert w.decide("rev1", iid).status_code == 409  # one person cannot fill the quorum twice
    mid = w.decide("rev2", iid).json()
    assert mid["state"] == "IN_REVIEW" and mid["stage_index"] == 1
    assert w.decide("rev3", iid).status_code == 404  # not on the team, so the workflow is invisible
    assert w.decide("lead", iid).json()["state"] == "APPROVED"
    # definitions are versioned: editing the definition never changes a running instance
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["definition_version"] == 1


def test_definition_snapshot_is_frozen_for_running_instances(w: World) -> None:
    loose = {"mode": "sequential", "required_policy_keys": [], "stages": [{"name": "Any reviewer", "approver_roles": ["reviewer"], "approver_team_roles": ["reviewer"], "min_approvals": 1, "independent": True}]}
    strict = {**loose, "stages": [{"name": "Strict", "approver_roles": ["owner"], "approver_team_roles": [], "min_approvals": 1, "independent": True}]}
    assert w.post("/workflows/definitions", json={"key": "evolving", "name": "Evolving", "resource_type": "code_review", "spec": loose}).json()["version"] == 1
    rev = w.review(w.ids["repoA"], author="eng2")
    iid = w.instance("eng1", "code_review", rev, definition_key="evolving")["id"]
    w.submit("eng1", iid)
    r = w.post("/workflows/definitions", json={"key": "evolving", "name": "Evolving", "resource_type": "code_review", "spec": strict})
    assert r.status_code == 201 and r.json()["version"] == 2
    assert w.decide("rev1", iid).json()["state"] == "APPROVED"  # the instance still follows the snapshot it started with
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["definition_version"] == 1
    defs = [d for d in w.get("/workflows/definitions?resource_type=code_review").json()["items"] if d["key"] == "evolving"]
    assert len(defs) == 1 and defs[0]["version"] == 2


def test_policy_gate_blocks_approval_atomically(w: World) -> None:
    cr = w.change_request(w.ids["repoA"], head="c1")
    inst = w.instance("eng1", "change_request", cr)
    iid = inst["id"]
    assert w.submit("eng1", iid).status_code == 200
    blocked = w.decide("rev1", iid, "approve", "ship it")
    assert blocked.status_code == 409 and blocked.json()["error"]["details"]["code"] == "policy_blocked"
    assert "verification-passed" in str(blocked.json()["error"]["details"])
    # nothing was approved, no decision persisted, but the evaluation and the block were recorded
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["state"] == "IN_REVIEW"
    with w.db() as s:
        assert s.execute(text("SELECT count(*) FROM workflow_decisions WHERE instance_id=:i"), {"i": iid}).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM policy_evaluations WHERE resource_id=:r AND status IN ('unavailable','fail')"), {"r": cr}).scalar() >= 1
    assert any(e["action"] == "workflow.approval_blocked" for e in w.get("/governance/audit?action_prefix=workflow.").json()["items"])
    ps = w.get(f"/workflows/instances/{iid}/policy-status", "eng1").json()
    assert ps["blocked"] is True
    # evidence of a FAILED gate still blocks
    w.gate(w.ids["repoA"], "c1", "FAIL")
    assert w.decide("rev1", iid).status_code == 409
    # a passing gate for the right revision unblocks it, and the reviewer can retry
    w.gate(w.ids["repoA"], "c1", "PASS")
    assert w.decide("rev1", iid, "approve", "verified").json()["state"] == "APPROVED"
    # disabling a protected blocking policy needs the organization administrator, so a lead cannot wave the gate away
    pol = [p for p in w.get("/governance/policies").json()["items"] if p["key"] == "verification-passed"][0]
    assert w.put(f"/governance/policies/{pol['id']}", "lead", {"enabled": False}).status_code == 403


def test_attested_evidence_cannot_satisfy_verification_policy(w: World) -> None:
    cr = w.change_request(w.ids["repoA"], head="c9")
    iid = w.instance("eng1", "change_request", cr)["id"]
    w.submit("eng1", iid)
    assert w.post(f"/workflows/instances/{iid}/evidence", "eng1", {"label": "verification", "note": "trust me"}).status_code in (200, 201)
    assert w.decide("rev1", iid).status_code == 409  # attestation is recorded but never substitutes real verification


def test_prohibited_transition_cannot_be_bypassed_by_other_actions(w: World) -> None:
    _policy(w, "freeze", "prohibited_transition", {"transition": "complete"}, rtypes=["code_review"])
    iid = _flow(w)
    assert w.decide("rev1", iid).json()["state"] == "APPROVED"
    blocked = w.post(f"/workflows/instances/{iid}/transitions", "lead", {"action": "complete"})
    assert blocked.status_code == 409 and blocked.json()["error"]["details"]["code"] == "policy_blocked"
    assert w.get(f"/workflows/instances/{iid}", "lead").json()["state"] == "APPROVED"


# ===================================================================================================== review queue
def test_queue_lifecycle_assignment_claim_release_history(w: World) -> None:
    iid = _flow(w)
    q = w.get("/review-queue?status=open", "rev1").json()
    assert q["total"] == 1
    item = q["items"][0]
    qid = item["id"]
    assert item["instance_id"] == iid and item["resource_available"] is True and item["status"] == "open"
    # outsiders see an empty queue; the requester (not eligible) cannot claim
    assert w.get("/review-queue", "outsider").json()["total"] == 0
    assert w.post(f"/review-queue/{qid}/claim", "eng1").status_code == 403
    assert w.post(f"/review-queue/{qid}/claim", "outsider").status_code == 404
    assert w.post(f"/review-queue/{qid}/claim", "rev1").json()["assignee"] == "rev1"
    assert w.post(f"/review-queue/{qid}/claim", "rev2").status_code == 409  # already claimed
    assert w.post(f"/review-queue/{qid}/release", "rev2").status_code == 403  # only the holder or a lead
    assert w.post(f"/review-queue/{qid}/release", "rev1").json()["assignee"] is None
    assert w.post(f"/review-queue/{qid}/assign", "rev2", {"user_id": w.ids["rev2"]}).status_code == 403  # assigning needs queue.assign
    assert w.post(f"/review-queue/{qid}/assign", "eng1", {"user_id": w.ids["rev2"]}).status_code == 200  # team admin
    assert w.post(f"/review-queue/{qid}/assign", "eng1", {"user_id": w.ids["eng1"]}).status_code in (403, 422)  # cannot assign the requester
    assert w.post(f"/review-queue/{qid}/assign", "eng1", {"user_id": w.ids["outsider"]}).status_code in (403, 404, 422)  # not on the team
    mine = w.get("/review-queue?assignee=me", "rev2").json()
    assert mine["total"] == 1
    hist = w.get(f"/review-queue/{qid}/history", "rev1").json()["items"]
    assert [h["action"] for h in hist][:1] == ["created"] and "assigned" in [h["action"] for h in hist] and "claimed" in [h["action"] for h in hist]
    # deciding closes the item with a recorded reason
    assert w.decide("rev2", iid).status_code == 201
    done = w.get("/review-queue?status=done", "rev1").json()
    assert done["total"] == 1 and done["items"][0]["id"] == qid
    assert w.post(f"/review-queue/{qid}/claim", "rev1").status_code == 409
    assert w.get("/review-queue/summary", "rev1").json()["by_status"].get("open", 0) == 0


def test_queue_filters_pagination_stable_order_and_scoping(w: World) -> None:
    ids = []
    for n in range(5):
        rev = w.review(w.ids["repoA"], author="eng2", head=f"q{n}")
        i = w.instance("eng1", "code_review", rev, priority="urgent" if n == 0 else "normal")["id"]
        w.submit("eng1", i)
        ids.append(i)
    rev = w.review(w.ids["repoFree"], head="free")
    free = w.instance(None, "code_review", rev)["id"] if False else w.post("/workflows/instances", json={"resource_type": "code_review", "resource_id": rev}).json()["id"]
    w.submit(None, free)  # type: ignore[arg-type]
    seen: list[str] = []
    for page in (1, 2, 3):
        r = w.get(f"/review-queue?page_size=2&page={page}", "aud").json()
        assert r["total"] == 6
        seen += [i["id"] for i in r["items"]]
    assert len(seen) == len(set(seen)) == 6
    first = w.get("/review-queue?page_size=1", "aud").json()["items"][0]
    assert first["priority"] == "urgent"  # urgent first
    assert w.get("/review-queue?priority=urgent", "aud").json()["total"] == 1
    assert w.get("/review-queue?team_id=" + w.ids["alpha"], "aud").json()["total"] == 5
    assert w.get("/review-queue?page_size=1000", "aud").status_code == 422  # bounded
    assert w.get("/review-queue?status=weird", "aud").status_code == 422
    # a team member sees only their team's items + unowned; an outsider sees only the unowned repository's item
    assert w.get("/review-queue", "rev1").json()["total"] >= 5
    out = w.get("/review-queue", "outsider").json()
    assert out["total"] == 0 or all(i["team_id"] is None for i in out["items"])


def test_queue_item_for_deleted_resource_is_safe(w: World) -> None:
    iid = _flow(w)
    with w.db() as s:
        s.execute(text("PRAGMA foreign_keys=OFF"))
        s.execute(text("DELETE FROM code_reviews"))
        s.commit()
    q = w.get("/review-queue", "rev1")
    assert q.status_code == 200 and q.json()["items"][0]["resource_available"] is False
    d = w.get(f"/workflows/instances/{iid}", "rev1")
    assert d.status_code == 200 and d.json()["resource_available"] is False
    # the evidence cannot be evaluated, so approval is never silently allowed
    r = w.decide("rev1", iid)
    assert r.status_code in (409, 404)
    assert w.get(f"/workflows/instances/{iid}", "eng1").json()["state"] == "IN_REVIEW"


def test_enqueue_manual_item_requires_visibility_and_is_idempotent(w: World) -> None:
    rev = w.review(w.ids["repoA"])
    first = w.post("/review-queue", "eng1", {"resource_type": "code_review", "resource_id": rev, "reason": "please look"})
    assert first.status_code == 201, first.text
    again = w.post("/review-queue", "eng1", {"resource_type": "code_review", "resource_id": rev})
    assert again.status_code == 409  # no duplicate open item
    assert w.get("/review-queue?status=open", "eng1").json()["total"] == 1
    assert w.post("/review-queue", "eng1", {"resource_type": "spaceship", "resource_id": rev}).status_code == 404


# ===================================================================================================== audit
def test_audit_scoping_filters_and_no_mutation_surface(w: World) -> None:
    iid = _flow(w)
    w.decide("rev1", iid)
    full = w.get("/governance/audit?page_size=100", "aud")
    assert full.status_code == 200 and full.json()["total"] > 10
    assert w.get("/governance/audit", "eng2").status_code == 403  # engineers have no audit access
    assert w.get("/governance/audit", "viewer").status_code == 403
    mine = w.get(f"/governance/audit?team_id={w.ids['alpha']}", "eng1")  # team admin: own team only
    assert mine.status_code == 200 and mine.json()["total"] >= 1
    assert w.get("/governance/audit?team_id=does-not-exist", "eng1").status_code in (403, 404)
    acts = {e["action"] for e in full.json()["items"]}
    assert {"team.created", "workflow.submit", "workflow.decision"} & acts
    assert w.get("/governance/audit?action_prefix=workflow.&page_size=5").json()["total"] >= 2
    assert w.get("/governance/audit?page_size=9999", "aud").status_code == 422
    for method in ("put", "patch", "delete"):  # there is no mutable audit API
        r = w.c.request(method.upper(), f"{API}/governance/audit/{full.json()['items'][0]['id']}")
        assert r.status_code in (404, 405)
    # sensitive values never reach audit details
    blob = str(full.json())
    assert w.tokens["eng1"] not in blob and "token_hash" not in blob


def test_audit_and_decision_rows_are_append_only_in_the_database(w: World) -> None:
    iid = _flow(w)
    w.decide("rev1", iid)
    with w.db() as s:
        for stmt in ("UPDATE audit_events SET action='tampered'", "DELETE FROM audit_events",
                     "UPDATE workflow_decisions SET decision='reject'", "DELETE FROM workflow_decisions",
                     "UPDATE workflow_events SET event_type='x'", "DELETE FROM workflow_events",
                     "UPDATE policy_evaluations SET status='pass'", "DELETE FROM policy_evaluations",
                     "UPDATE governance_policy_versions SET version=99", "DELETE FROM governance_policy_versions",
                     "UPDATE review_queue_history SET action='x'", "DELETE FROM review_queue_history"):
            with pytest.raises(Exception, match="append-only|constraint|abort|RAISE"):
                s.execute(text(stmt))
                s.commit()
            s.rollback()
    # a removed user can still be anonymised without rewriting history
    with w.db() as s:
        s.execute(text("UPDATE audit_events SET actor_user_id=NULL WHERE actor_user_id IS NOT NULL AND id=(SELECT id FROM audit_events WHERE actor_user_id IS NOT NULL LIMIT 1)"))
        s.commit()


def test_denials_are_audited_and_deduplicated(w: World) -> None:
    for _ in range(5):
        assert w.post("/org/members", "eng1", {"handle": "nope", "role": "viewer"}).status_code == 403
    denied = [e for e in w.get("/governance/audit?action_prefix=authz.denied&page_size=100", "aud").json()["items"] if e["actor_handle"] == "eng1"]
    assert 1 <= len(denied) <= 2


# ===================================================================================================== overview, health, performance
def test_overview_and_health_report_real_state(w: World) -> None:
    _flow(w)
    ov = w.get("/governance/overview", "lead").json()
    assert ov["queue"]["by_status"]["open"] >= 1 and ov["teams"] >= 1
    assert "overdue" not in str(ov).lower() or ov.get("overdue") is None
    h = w.get("/governance/health", "aud").json()
    names = {c["name"]: c for c in h["checks"]}
    assert names["workflow_definitions"]["ok"] is True and h["scheduler"] == "not_implemented"
    assert w.get("/governance/health", "eng2").status_code == 403


def test_metrics_have_no_user_or_resource_labels(w: World) -> None:
    iid = _flow(w)
    assert w.decide("rev1", iid).status_code == 201
    r = w.get("/ops/metrics")
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln.startswith("nova_governance")]
    assert any(ln.startswith("nova_governance_transitions_total") for ln in lines)  # the metrics exist (not a vacuous check)
    assert any(ln.startswith("nova_governance_policy_evaluations_total") for ln in lines)
    for ln in lines:
        assert iid not in ln and "rev1" not in ln and w.ids["rev1"] not in ln and "user" not in ln.split("{", 1)[-1]


def test_listing_query_count_is_bounded(w: World) -> None:
    from sqlalchemy import event

    from app.db.session import get_engine

    for n in range(12):
        _flow(w, head=f"p{n}")
    eng = get_engine()
    count = {"n": 0}

    def hook(*_a: Any, **_k: Any) -> None:
        count["n"] += 1

    event.listen(eng, "before_cursor_execute", hook)
    try:
        r = w.get("/workflows/instances?page_size=50", "aud")
        q = w.get("/review-queue?page_size=50", "aud")
    finally:
        event.remove(eng, "before_cursor_execute", hook)
    assert r.json()["total"] == 12 and q.json()["total"] == 12
    assert count["n"] < 40, count["n"]  # not proportional to the number of rows (12 workflows would exceed this if N+1)


# ===================================================================================================== migration safety
def test_migration_preserves_existing_data_and_roundtrips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "mig.db"
    monkeypatch.setenv("NOVA_DATABASE_URL", f"sqlite:///{db}")
    reset_settings_cache()
    reset_engine()
    cfg = alembic_config(f"sqlite:///{db}")
    command.upgrade(cfg, "0014")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO workspaces(id,name,slug,created_at,updated_at) VALUES('w1','Old','old',datetime('now'),datetime('now'))")
    con.execute("INSERT INTO users(id,handle,display_name,created_at,updated_at) VALUES('u1','olduser','Old User',datetime('now'),datetime('now'))")
    con.execute("INSERT INTO memberships(id,workspace_id,user_id,role,created_at,updated_at) VALUES('m1','w1','u1','owner',datetime('now'),datetime('now'))")
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    assert con.execute("SELECT role,status FROM memberships WHERE id='m1'").fetchone() == ("owner", "active")
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_audit_events_no_update" in triggers and "trg_workflow_decisions_no_delete" in triggers
    con.close()
    command.downgrade(cfg, "0014")
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM memberships").fetchone()[0] == 1  # data survives the round trip
    assert not con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'trg_%'").fetchall()
    con.close()
    command.upgrade(cfg, "head")
    reset_engine()
    reset_settings_cache()


# ===================================================================================================== history survives removal/archival
def test_removed_users_and_archived_teams_do_not_break_history(w: World) -> None:
    iid = _flow(w)
    assert w.decide("rev1", iid, "request_changes", "needs tests").status_code == 201
    w.post("/org/members", json={"handle": "backup", "role": "owner"})  # a second administrator exists
    assert w.post(f"/org/members/{w.ids['rev1']}/remove").status_code == 200  # the decider leaves the organization
    assert w.post(f"/org/members/{w.ids['eng1']}/remove").status_code == 200  # ...and so does the requester
    detail = w.get(f"/workflows/instances/{iid}")
    assert detail.status_code == 200 and detail.json()["state"] == "CHANGES_REQUESTED"
    assert detail.json()["decisions"][0]["user_handle"] == "rev1"  # the decision stays attributable
    assert w.get(f"/workflows/instances/{iid}/events").status_code == 200
    assert w.get("/review-queue?status=done").status_code == 200
    assert w.post(f"/teams/{w.ids['alpha']}/ownership/{w.ids['repoA']}/release").status_code == 204
    assert w.post(f"/teams/{w.ids['alpha']}/archive").status_code == 409  # an open workflow still belongs to the team
    assert w.post(f"/workflows/instances/{iid}/transitions", json={"action": "cancel", "note": "team retired"}).status_code == 200
    assert w.post(f"/teams/{w.ids['alpha']}/archive").status_code == 200
    assert w.get(f"/workflows/instances/{iid}").status_code == 200  # history of an archived team's workflow is still readable
    audit = w.get("/governance/audit?page_size=100&action_prefix=workflow.").json()
    assert audit["total"] >= 3 and all(e["actor_handle"] for e in audit["items"] if e["actor_user_id"])
    assert w.get("/governance/audit?action_prefix=team.&page_size=100").status_code == 200
    # a removed member's token no longer works
    assert w.c.get(f"{API}/workflows/instances/{iid}", headers={"Authorization": f"Bearer {w.tokens['rev1']}"}).status_code == 401
