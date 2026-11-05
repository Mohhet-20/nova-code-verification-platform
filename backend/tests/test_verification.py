"""Change detection, planning, controlled execution, baselines, regression intelligence, quality gates, policy and API.

TestClient runs background tasks before it returns, so a started run is finished when the POST response has been delivered.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register_and_index
from tests.test_refactor import art, as_viewer, git  # noqa: F401  (art is a fixture)
from tests.test_repository_intelligence import write

API = "/api/v1"

UTIL = textwrap.dedent('''
    def clamp(value, low, high):
        if value < low:
            return low
        if value > high:
            return high
        return value
''').lstrip("\n")
PRICING = textwrap.dedent('''
    from app_pkg.util import clamp


    def discount(price, percent):
        return round(price * (1 - clamp(percent, 0, 100) / 100), 2)
''').lstrip("\n")
T_UTIL = textwrap.dedent('''
    from app_pkg.util import clamp


    def test_clamp_low():
        assert clamp(-1, 0, 10) == 0


    def test_clamp_high():
        assert clamp(11, 0, 10) == 10


    def test_clamp_mid():
        assert clamp(5, 0, 10) == 5
''').lstrip("\n")
T_PRICING = textwrap.dedent('''
    from app_pkg.pricing import discount


    def test_discount():
        assert discount(100, 10) == 90.0
''').lstrip("\n")
FILES = {"app_pkg/__init__.py": "", "app_pkg/util.py": UTIL, "app_pkg/pricing.py": PRICING, "tests/test_util.py": T_UTIL,
         "tests/test_pricing.py": T_PRICING, "README.md": "# demo\n"}
BROKEN_UTIL = UTIL.replace("        return low\n", "        return high\n")


def vb(rid: str) -> str:
    return f"{API}/repositories/{rid}/verification"


def setup(client: TestClient, tmp_path: Path, files: dict[str, str] | None = None, name: str = "v") -> tuple[str, Path]:
    root = tmp_path / "r"
    write(root, FILES if files is None else files)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    rid = register_and_index(client, root, name)["repo"]["id"]
    assert client.post(f"{API}/repositories/{rid}/tests/discover").status_code in (200, 201, 202)
    return rid, root


def edit(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def run(client: TestClient, rid: str, **kw: Any) -> dict[str, Any]:
    body = {"source": "working_tree", "confirm": True, **kw}
    r = client.post(f"{vb(rid)}/runs", json=body)
    assert r.status_code == 202, r.text
    got = client.get(f"{vb(rid)}/runs/{r.json()['id']}")
    assert got.status_code == 200, got.text
    return got.json()


def baseline(client: TestClient, rid: str, label: str = "clean") -> dict[str, Any]:
    r = client.post(f"{vb(rid)}/baselines", json={"label": label, "confirm": True})
    assert r.status_code == 202, r.text
    lst = client.get(f"{vb(rid)}/baselines").json()
    assert lst, "a baseline must be recorded"
    return lst[0]


def rules(d: dict[str, Any]) -> dict[str, str]:
    return {r["id"]: r["status"] for r in d["gate"]["rules"]}


def checks(d: dict[str, Any]) -> dict[str, str]:
    return {c["check_id"]: c["status"] for c in d["checks"]}


def porcelain(root: Path) -> str:
    return git(root, "status", "--porcelain=v1", "--untracked-files=all") + git(root, "rev-parse", "HEAD") + git(root, "stash", "list")


# ------------------------------------------------------------------------------------------------ policy
def test_policy_defaults_versions_and_authorization(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = setup(client, tmp_path)
    p = client.get(f"{vb(rid)}/policy").json()
    assert p["is_default"] and p["version"] == 0 and p["can_edit"] and p["precedence"] == ["FAIL", "BLOCKED", "INCOMPLETE", "PASS"]
    new = {**p["rules"], "default_mode": "standard", "static": {"enforce": True, "max_new_findings": 2}}
    r = client.put(f"{vb(rid)}/policy", json={"rules": new, "note": "relax"})
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["rules"]["static"]["max_new_findings"] == 2
    assert client.put(f"{vb(rid)}/policy", json={"rules": new}).json()["version"] == 2
    hist = client.get(f"{vb(rid)}/policy/history").json()
    assert [h["version"] for h in hist] == [2, 1]
    for bad in ({"rules": {"bogus": 1}}, {"rules": {"tests": {"min_tests_executed": 0}}}, {"rules": {"coverage": {"min_line_percent": 150}}},
                {"rules": {"default_mode": "everything"}}):
        assert client.put(f"{vb(rid)}/policy", json=bad).status_code == 422, bad
    assert client.put(f"{vb(rid)}/policy", json={"rules": {}, "extra": 1}).status_code == 422


def test_policy_change_is_restricted_to_owners(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership
    rid, _ = setup(client, tmp_path)
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="maintainer"))
        s.commit()
    r = client.put(f"{vb(rid)}/policy", json={"rules": {}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "policy_admin_required"
    assert client.get(f"{vb(rid)}/policy").json()["can_edit"] is False
    assert client.get(f"{vb(rid)}/policy/history").json() == []


def test_run_snapshots_the_policy_it_started_with(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    d = run(client, rid)
    assert d["policy_version"] == 0 and d["policy_snapshot"]["static"]["max_new_findings"] == 0
    client.put(f"{vb(rid)}/policy", json={"rules": {"static": {"enforce": False}}})
    again = client.get(f"{vb(rid)}/runs/{d['id']}").json()
    assert again["policy_snapshot"] == d["policy_snapshot"] and again["policy_version"] == 0


# ------------------------------------------------------------------------------------------------ preview, confirmation, safety
def test_preview_never_executes_or_changes_anything(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    before = porcelain(root)
    r = client.post(f"{vb(rid)}/changes/preview", json={"source": "working_tree"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executes"] is False
    assert [f["path"] for f in body["change_set"]["files"]] == ["app_pkg/util.py"]
    plan = {c["check_id"]: c for c in body["plan"]["checks"]}
    assert plan["tests"]["status"] == "planned" and plan["tests"]["reason"] and plan["syntax"]["confidence"] == "high"
    assert body["plan"]["test_targets"] == ["tests/test_util.py"]
    assert body["plan"]["impact"]["selection"]["tests/test_util.py"].startswith("imports changed module")
    assert porcelain(root) == before
    assert client.get(f"{vb(rid)}/runs").json()["total"] == 0


def test_starting_a_run_requires_confirmation(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n# x\n")
    r = client.post(f"{vb(rid)}/runs", json={"source": "working_tree"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirmation_required"
    assert client.post(f"{vb(rid)}/baselines", json={}).json()["error"]["code"] == "confirmation_required"
    assert client.get(f"{vb(rid)}/runs").json()["total"] == 0


def test_run_leaves_git_state_untouched(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    (root / "app_pkg" / "new_mod.py").write_text("X = 1\n")
    before = porcelain(root)
    index_before = (root / ".git" / "index").read_bytes()
    d = run(client, rid)
    assert d["status"] == "completed"
    assert porcelain(root) == before and (root / ".git" / "index").read_bytes() == index_before
    assert {f["path"]: f["status"] for f in d["change_set"]["files"]} == {"app_pkg/util.py": "modified", "app_pkg/new_mod.py": "added"}


# ------------------------------------------------------------------------------------------------ the worked flow
def test_baseline_change_verify_break_fix_flow(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    b = baseline(client, rid)
    assert b["status"] == "valid" and b["integrity_ok"] and b["scope"] == {"kind": "all"} and b["failing"] == []
    assert b["totals"]["collected"] == 4 and b["totals"]["passed"] == 4 and b["head_sha"]

    # a harmless change: focused verification passes, bound to the exact source state
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    ok = run(client, rid)
    assert ok["status"] == "completed" and ok["gate_outcome"] == "PASS", ok["gate"]
    assert checks(ok) == {"syntax": "passed", "static_analysis": "passed", "tests": "passed"}
    assert ok["head_sha"] and ok["tree_fingerprint"] and ok["baseline_id"] == b["id"]
    assert ok["plan"]["baseline"]["selection"].startswith("latest valid baseline")
    assert ok["freshness"]["stale"] is False
    tests = next(c for c in ok["checks"] if c["check_id"] == "tests")
    assert tests["result"]["tests_executed"] == 3, "focused mode runs only the tests linked to the change"

    # a regression: the gate fails and names the new failure
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    bad = run(client, rid)
    assert bad["gate_outcome"] == "FAIL" and checks(bad)["tests"] == "failed"
    reg = bad["regression"]
    assert reg["counts"]["new"] == 1 and reg["counts"]["pre_existing"] == 0
    new = [i for i in reg["items"] if i["category"] == "new"][0]
    assert "test_clamp_low" in new["test"] and new["explanation"] and new["evidence"]["baseline_id"] == b["id"]
    assert rules(bad)["no_new_failures"] == "fail" and bad["gate"]["outcome"] == "FAIL"

    # the fix: re-verification passes and the earlier failure is not carried over
    # reverting to the committed state leaves nothing to verify: that is reported, never a PASS
    edit(root, "app_pkg/util.py", UTIL)
    empty = run(client, rid)
    assert empty["gate_outcome"] == "BLOCKED" and "nothing to verify" in empty["gate"]["rules"][-1]["detail"]
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop2():\n    return 2\n")
    fixed = run(client, rid)
    assert fixed["gate_outcome"] == "PASS" and fixed["regression"]["counts"]["new"] == 0
    assert fixed["tree_fingerprint"] != ok["tree_fingerprint"]
    hist = client.get(f"{vb(rid)}/runs", params={"kind": "verify"}).json()
    assert hist["total"] == 4 and [r["gate_outcome"] for r in hist["items"]] == ["PASS", "BLOCKED", "FAIL", "PASS"]
    assert client.get(f"{vb(rid)}/runs", params={"outcome": "fail"}).json()["total"] == 1
    ov = client.get(f"{vb(rid)}/overview").json()
    assert ov["gate_counts"] == {"PASS": 2, "FAIL": 1, "BLOCKED": 1, "INCOMPLETE": 0}
    assert ov["latest_baseline"]["id"] == b["id"] and ov["latest_run"]["id"] == fixed["id"]


def test_failures_resolved_relative_to_a_failing_baseline(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    git(root, "commit", "-qam", "bug")
    b = baseline(client, rid, "already broken")
    assert any("test_clamp_low" in f for f in b["failing"])
    edit(root, "app_pkg/util.py", BROKEN_UTIL + "\n# touch\n")
    pre = run(client, rid)
    assert pre["gate_outcome"] == "FAIL" and pre["regression"]["counts"]["pre_existing"] == 1 and pre["regression"]["counts"]["new"] == 0
    assert "baseline run failed" in [i for i in pre["regression"]["items"] if i["category"] == "pre_existing"][0]["explanation"]
    assert rules(pre)["no_new_failures"] == "fail", "the default policy does not accept pre-existing failures"
    client.put(f"{vb(rid)}/policy", json={"rules": {"tests": {"allow_preexisting_failures": True}}})
    allowed = run(client, rid)
    assert allowed["gate_outcome"] == "PASS" and rules(allowed)["no_new_failures"] == "pass"
    edit(root, "app_pkg/util.py", UTIL)
    fixed = run(client, rid)
    assert fixed["gate_outcome"] == "PASS"
    assert [i["test"] for i in fixed["regression"]["items"] if i["category"] == "resolved"], fixed["regression"]


def test_no_baseline_means_unclassified_failures_and_never_new(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    d = run(client, rid)
    assert d["gate_outcome"] == "FAIL" and d["baseline_id"] is None
    assert d["regression"]["counts"]["unclassified"] == 1 and d["regression"]["counts"]["new"] == 0
    assert "No compatible baseline" in d["regression"]["items"][0]["explanation"]
    client.put(f"{vb(rid)}/policy", json={"rules": {"baseline": {"required": True}}})
    edit(root, "app_pkg/util.py", UTIL + "\n# c\n")
    blocked = run(client, rid)
    assert blocked["gate_outcome"] == "BLOCKED" and rules(blocked)["baseline"] == "blocked"


# ------------------------------------------------------------------------------------------------ revision binding and staleness
def test_new_commit_or_edit_marks_results_stale(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    d = run(client, rid)
    assert d["gate_outcome"] == "PASS" and d["freshness"]["stale"] is False
    git(root, "commit", "-qam", "commit the change")
    after_commit = client.get(f"{vb(rid)}/runs/{d['id']}").json()
    assert after_commit["freshness"]["stale"] is True and "earlier state" in after_commit["freshness"]["reason"]
    assert after_commit["gate_outcome"] == "PASS", "the recorded decision is kept, but it is flagged as describing an older state"
    gate = client.get(f"{vb(rid)}/runs/{d['id']}/gate").json()
    assert gate["freshness"]["stale"] is True and gate["outcome"] == "PASS"
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 2\n")
    assert client.get(f"{vb(rid)}/runs/{d['id']}").json()["freshness"]["stale"] is True


def test_revisions_source_and_revision_binding(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    base_sha = git(root, "rev-parse", "HEAD").strip()
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    git(root, "commit", "-qam", "c2")
    head = git(root, "rev-parse", "HEAD").strip()
    d = run(client, rid, source="revisions", base=base_sha, head="HEAD")
    assert d["gate_outcome"] == "PASS" and d["base_sha"] == base_sha and d["head_sha"] == head
    assert [f["path"] for f in d["change_set"]["files"]] == ["app_pkg/util.py"]
    # tests run on the checked-out tree, so verifying an older head must be refused, not faked
    old = run(client, rid, source="revisions", base=base_sha, head=base_sha)
    assert [f["path"] for f in old["change_set"]["files"]] == []
    d2 = run(client, rid, source="revisions", base=head, head=base_sha)
    assert d2["gate_outcome"] == "BLOCKED" and checks(d2)["tests"] == "blocked"
    assert "never checks out" in next(c for c in d2["checks"] if c["check_id"] == "tests")["result"]["detail"]
    assert client.post(f"{vb(rid)}/changes/preview", json={"source": "revisions", "base": "nope", "head": "HEAD"}).status_code == 422


def test_staged_source_and_patch_binding(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    git(root, "add", "app_pkg/util.py")
    s = run(client, rid, source="staged")
    assert s["gate_outcome"] == "PASS" and [f["path"] for f in s["change_set"]["files"]] == ["app_pkg/util.py"]
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 99\n")  # unstaged edit on top of the staged content
    drift = run(client, rid, source="staged")
    assert drift["gate_outcome"] == "BLOCKED" and "differs from the working tree" in next(c for c in drift["checks"] if c["check_id"] == "tests")["result"]["detail"]
    edit(root, "app_pkg/util.py", UTIL)
    git(root, "reset", "-q")
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    diff = git(root, "diff", "--", "app_pkg/util.py")
    edit(root, "app_pkg/util.py", UTIL)
    assert diff.startswith("diff --git")
    before = porcelain(root)
    p = run(client, rid, source="patch", patch=diff)
    assert [f["path"] for f in p["change_set"]["files"]] == ["app_pkg/util.py"]
    assert p["gate_outcome"] == "BLOCKED" and "patch" in next(c for c in p["checks"] if c["check_id"] == "tests")["result"]["detail"].lower()
    assert porcelain(root) == before, "a patch is inspected, never applied"
    assert checks(p)["syntax"] == "passed"


# ------------------------------------------------------------------------------------------------ planner and evidence gaps
def test_missing_evidence_is_blocked_never_pass(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    (root / "app_pkg" / "orphan.py").write_text("def lonely():\n    return 1\n")
    d = run(client, rid)
    assert checks(d)["tests"] == "blocked" and d["gate_outcome"] == "BLOCKED"
    assert "No discovered test imports" in next(c for c in d["checks"] if c["check_id"] == "tests")["result"]["detail"]
    assert d["execution_run_id"] is None
    full = run(client, rid, mode="full")
    assert full["gate_outcome"] == "PASS" and next(c for c in full["checks"] if c["check_id"] == "tests")["result"]["tests_executed"] == 4


def test_documentation_only_changes_have_no_evidence_so_not_pass(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "README.md", "# demo\nmore\n")
    d = run(client, rid)
    assert checks(d)["tests"] == "not_applicable" and d["gate_outcome"] == "INCOMPLETE"
    assert "nothing applicable" in d["gate"]["reason"].lower()


def test_changed_files_without_a_verifier_make_the_gate_incomplete(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    (root / "web.js").write_text("export const x = 1;\n")
    d = run(client, rid)
    assert checks(d)["tests"] == "passed" and rules(d)["changed_files_verified"] == "incomplete"
    assert d["gate_outcome"] == "INCOMPLETE" and d["plan"]["unverified_paths"] == ["web.js"]
    only = run(client, rid, source="manifest", paths=["web.js"])
    assert only["gate_outcome"] in ("BLOCKED", "INCOMPLETE") and only["gate_outcome"] != "PASS"


def test_syntax_error_fails_the_gate(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", "def broken(:\n    pass\n")
    d = run(client, rid)
    syn = next(c for c in d["checks"] if c["check_id"] == "syntax")
    assert syn["status"] == "failed" and syn["result"]["errors"][0]["path"] == "app_pkg/util.py"
    assert d["gate_outcome"] == "FAIL"


def test_new_static_findings_are_gated_by_policy(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef check(x):\n    if x == None:\n        return 1\n    return 2\n")
    d = run(client, rid)
    assert len(d["static"]["new_findings"]) >= 1 and rules(d)["static_new_findings"] == "fail" and d["gate_outcome"] == "FAIL"
    client.put(f"{vb(rid)}/policy", json={"rules": {"static": {"enforce": True, "max_new_findings": 10}}})
    lenient = run(client, rid)
    assert rules(lenient)["static_new_findings"] == "pass" and lenient["gate_outcome"] == "PASS"


def test_manifest_cannot_prove_static_changes(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = setup(client, tmp_path)
    d = run(client, rid, source="manifest", paths=["app_pkg/util.py"])
    assert checks(d)["static_analysis"] == "blocked" and d["gate_outcome"] == "BLOCKED"


def test_standard_mode_adds_tests_of_dependents(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    foc = client.post(f"{vb(rid)}/changes/preview", json={"source": "working_tree", "mode": "focused"}).json()["plan"]
    std = client.post(f"{vb(rid)}/changes/preview", json={"source": "working_tree", "mode": "standard"}).json()["plan"]
    assert foc["test_targets"] == ["tests/test_util.py"]
    assert std["test_targets"] == ["tests/test_pricing.py", "tests/test_util.py"]
    assert "depends on a changed module" in std["impact"]["selection"]["tests/test_pricing.py"]
    assert client.post(f"{vb(rid)}/changes/preview", json={"source": "working_tree", "mode": "huge"}).status_code == 422


# ------------------------------------------------------------------------------------------------ coverage
def test_coverage_comes_from_the_tool_or_is_reported_unavailable(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    d = run(client, rid, coverage=True)
    cov = d["coverage"]
    if cov["status"] == "measured":
        assert cov["tool"] and 0 <= cov["line_percent"] <= 100
        assert any(f["rel_path"] == "app_pkg/util.py" for f in cov["changed_files"])
        assert client.get(f"{vb(rid)}/runs/{d['id']}/coverage").json() == cov
    else:
        assert cov["status"] in ("tool_unavailable", "no_data", "failed", "unavailable") and cov["reason"]
    assert run(client, rid)["coverage"] == {"status": "not_requested"}


def test_coverage_policy_without_measurement_blocks(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    client.put(f"{vb(rid)}/policy", json={"rules": {"coverage": {"min_line_percent": 100}}})
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    d = run(client, rid)
    if d["coverage"]["status"] == "measured":
        assert d["coverage"]["line_percent"] < 100 and d["gate_outcome"] == "FAIL" and rules(d)["coverage_min"] == "fail"
    else:
        assert d["gate_outcome"] == "BLOCKED"


# ------------------------------------------------------------------------------------------------ baselines: tamper, compatibility
def test_tampered_baseline_is_not_trusted(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import CvBaseline
    rid, root = setup(client, tmp_path)
    b = baseline(client, rid)
    with get_session_factory()() as s:
        s.execute(update(CvBaseline).where(CvBaseline.id == b["id"]).values(failing=["tests.test_util::test_clamp_low"]))
        s.commit()
    assert client.get(f"{vb(rid)}/baselines/{b['id']}").json()["integrity_ok"] is False
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    d = run(client, rid)
    assert d["regression"]["baseline"]["compatible"] is False
    assert any("integrity" in p for p in d["regression"]["baseline"]["problems"])
    assert d["regression"]["counts"]["new"] == 0 and d["regression"]["counts"]["unclassified"] == 1


def test_baseline_with_different_test_configuration_is_incompatible(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    baseline(client, rid)
    (root / "pytest.ini").write_text("[pytest]\naddopts =\n")
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    d = run(client, rid)
    assert d["regression"]["baseline"]["compatible"] is False
    assert any("configuration" in p for p in d["regression"]["baseline"]["problems"])


def test_compare_endpoint_recomputes_against_a_chosen_baseline(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    b = baseline(client, rid)
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    d = run(client, rid, baseline_id=b["id"])
    cmp = client.get(f"{vb(rid)}/runs/{d['id']}/compare", params={"baseline_id": b["id"]}).json()
    assert cmp["persisted"] is False and cmp["regression"]["counts"]["new"] == 1 and cmp["compatibility"]["compatible"]
    assert client.get(f"{vb(rid)}/runs/{d['id']}/compare", params={"baseline_id": "nope"}).status_code == 404
    assert client.get(f"{vb(rid)}/runs/{d['id']}/regressions").json()["counts"]["new"] == 1


def test_flaky_requires_repeated_evidence(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from app.db.session import get_session_factory
    from app.models import CvRun
    rid, root = setup(client, tmp_path)
    edit(root, "app_pkg/util.py", BROKEN_UTIL)
    first = run(client, rid)
    assert first["regression"]["counts"]["flaky"] == 0, "one failing run is not evidence of flakiness"
    with get_session_factory()() as s:
        s.add(CvRun(repository_id=rid, kind="verify", status="completed", mode="focused", source="working_tree", tree_fingerprint=first["tree_fingerprint"],
                    regression={"failing_tests": [], "scope": {"kind": "all"}}, gate_outcome="PASS"))
        s.commit()
    again = run(client, rid)
    items = [i for i in again["regression"]["items"] if i["category"] == "flaky"]
    assert len(items) == 1 and items[0]["evidence"]["flaky_runs"] and "identical source state" in items[0]["explanation"]
    edit(root, "app_pkg/util.py", BROKEN_UTIL + "\n# different tree\n")
    other = run(client, rid)
    assert other["regression"]["counts"]["flaky"] == 0, "evidence from a different source state must not count"


# ------------------------------------------------------------------------------------------------ cancellation, concurrency, recovery
def test_cancel_queued_run_preserves_state(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import app.api.verification as api
    rid, root = setup(client, tmp_path)
    monkeypatch.setattr(api, "_job", lambda run_id: None)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    r = client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).json()
    assert r["status"] == "queued" and r["execution_run_id"]
    assert client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).status_code == 409
    c = client.post(f"{vb(rid)}/runs/{r['id']}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["gate_outcome"] == "INCOMPLETE"
    assert all(x["status"] == "cancelled" for x in c.json()["checks"])
    ex = client.get(f"{API}/repositories/{rid}/tests/runs/{r['execution_run_id']}").json()
    assert ex["status"] == "rejected", "the pending test run must not stay active"
    assert client.post(f"{vb(rid)}/runs/{r['id']}/cancel").status_code == 409
    assert client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).status_code == 202


def test_cancel_requested_while_running_keeps_partial_results(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from sqlalchemy import update

    import app.api.verification as api

    rid, root = setup(client, tmp_path)
    monkeypatch.setattr(api, "_job", lambda run_id: None)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    r = client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).json()
    from app.db.session import get_session_factory
    from app.models import CvRun
    with get_session_factory()() as s:
        s.execute(update(CvRun).where(CvRun.id == r["id"]).values(cancel_requested=True))
        s.commit()
    from app.core.config import get_settings as gs
    from app.services.verification import engine
    engine.run_job(get_session_factory(), gs(), r["id"])
    d = client.get(f"{vb(rid)}/runs/{r['id']}").json()
    assert d["status"] == "cancelled" and d["gate_outcome"] == "INCOMPLETE" and rules(d)["not_cancelled"] == "incomplete"
    ev = [e["kind"] for e in client.get(f"{vb(rid)}/runs/{r['id']}/events").json()]
    assert "run_started" in ev and ev[-1] == "run_finished"


def test_source_changed_before_start_executes_nothing(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import app.api.verification as api
    from app.core.config import get_settings as gs
    from app.db.session import get_session_factory
    from app.services.verification import engine
    rid, root = setup(client, tmp_path)
    monkeypatch.setattr(api, "_job", lambda run_id: None)
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 1\n")
    r = client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).json()
    edit(root, "app_pkg/util.py", UTIL + "\n\ndef noop():\n    return 2\n")
    engine.run_job(get_session_factory(), gs(), r["id"])
    d = client.get(f"{vb(rid)}/runs/{r['id']}").json()
    assert d["gate_outcome"] == "BLOCKED" and rules(d)["source_unchanged"] == "incomplete", "blocked checks outrank the incomplete rule"
    assert all(c["status"] == "blocked" for c in d["checks"])


def test_interrupted_runs_are_recovered_as_blocked(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import app.api.verification as api
    from app.db.session import get_session_factory
    from app.services.verification.service import recover_interrupted_runs
    rid, root = setup(client, tmp_path)
    monkeypatch.setattr(api, "_job", lambda run_id: None)
    edit(root, "app_pkg/util.py", UTIL + "\n# x\n")
    r = client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}).json()
    with get_session_factory()() as s:
        assert recover_interrupted_runs(s) == 1
    d = client.get(f"{vb(rid)}/runs/{r['id']}").json()
    assert d["status"] == "error" and d["gate_outcome"] == "BLOCKED"


# ------------------------------------------------------------------------------------------------ input security
@pytest.mark.parametrize("payload", [
    {"source": "manifest", "paths": ["../etc/passwd"]}, {"source": "manifest", "paths": ["/etc/passwd"]},
    {"source": "manifest", "paths": [".git/config"]}, {"source": "manifest", "paths": ["a\\b.py"]}, {"source": "manifest", "paths": ["-rf"]},
    {"source": "manifest", "paths": ["a/../../b.py"]}, {"source": "manifest", "paths": []}, {"source": "manifest"},
    {"source": "revisions", "base": "--output=/tmp/x", "head": "HEAD"}, {"source": "revisions", "base": "HEAD; rm -rf /", "head": "HEAD"},
    {"source": "revisions", "base": "$(id)", "head": "HEAD"}, {"source": "patch"}, {"source": "patch", "patch": "not a diff"},
    {"source": "patch", "patch": "--- a/../../etc/x\n+++ b/../../etc/x\n@@ -1 +1 @@\n-a\n+b\n"},
    {"source": "working_tree", "base": "HEAD"}, {"source": "nonsense"}, {"source": "working_tree", "unknown": 1},
])
def test_unsafe_or_malformed_inputs_are_rejected(art: Path, client: TestClient, tmp_path: Path, payload: dict[str, Any]) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    before = porcelain(root)
    r = client.post(f"{vb(rid)}/changes/preview", json=payload)
    assert r.status_code == 422, (payload, r.text)
    assert client.post(f"{vb(rid)}/runs", json={**payload, "confirm": True}).status_code == 422
    assert porcelain(root) == before
    assert client.get(f"{vb(rid)}/runs").json()["total"] == 0


def test_symlink_in_manifest_is_refused(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    outside = tmp_path / "secret.py"
    outside.write_text("TOKEN = 1\n")
    (root / "link.py").symlink_to(outside)
    r = client.post(f"{vb(rid)}/changes/preview", json={"source": "manifest", "paths": ["link.py"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsafe_path"


def test_cross_repository_access_is_rejected(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    other_root = tmp_path / "o"
    write(other_root, FILES)
    git(other_root, "init", "-q", "-b", "main")
    git(other_root, "add", "-A")
    git(other_root, "commit", "-qm", "b")
    other = register_and_index(client, other_root, "other")["repo"]["id"]
    d = run(client, rid, mode="full")
    b = baseline(client, rid)
    assert client.get(f"{vb(other)}/runs/{d['id']}").status_code == 404
    for tail in ("/checks", "/regressions", "/coverage", "/gate", "/events"):
        assert client.get(f"{vb(other)}/runs/{d['id']}{tail}").status_code == 404, tail
    assert client.post(f"{vb(other)}/runs/{d['id']}/cancel").status_code == 404
    assert client.get(f"{vb(other)}/baselines/{b['id']}").status_code == 404
    edit(other_root, "app_pkg/util.py", UTIL + "\n# x\n")
    assert client.post(f"{vb(other)}/runs", json={"source": "working_tree", "confirm": True, "baseline_id": b["id"]}).status_code == 404
    assert client.get(f"{vb('0' * 32)}/overview").status_code == 404


def test_viewer_can_read_but_not_change_anything(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    d = run(client, rid, mode="full")
    as_viewer()
    edit(root, "app_pkg/util.py", UTIL + "\n# x\n")
    assert client.get(f"{vb(rid)}/runs/{d['id']}").status_code == 200 and client.get(f"{vb(rid)}/overview").status_code == 200
    assert client.post(f"{vb(rid)}/changes/preview", json={"source": "working_tree"}).status_code == 200
    for r in (client.post(f"{vb(rid)}/runs", json={"source": "working_tree", "confirm": True}), client.post(f"{vb(rid)}/baselines", json={"confirm": True}),
              client.post(f"{vb(rid)}/runs/{d['id']}/cancel"), client.put(f"{vb(rid)}/policy", json={"rules": {}}),
              client.put(f"{vb(rid)}/runs/{d['id']}/link", json={"type": "repair", "id": "x"})):
        assert r.status_code == 403, r.text


def test_endpoints_require_authentication(art: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from app.core.config import reset_settings_cache
    from app.db.session import reset_engine
    from app.main import create_app
    monkeypatch.setenv("NOVA_API_TOKEN", "t0ken-for-tests-only-123")
    monkeypatch.setenv("NOVA_DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/overview"), ("get", "/policy"), ("put", "/policy"), ("get", "/policy/history"), ("post", "/changes/preview"),
                             ("post", "/runs"), ("get", "/runs"), ("get", "/runs/x"), ("get", "/runs/x/gate"), ("post", "/runs/x/cancel"),
                             ("get", "/runs/x/compare"), ("put", "/runs/x/link"), ("post", "/baselines"), ("get", "/baselines"), ("get", "/baselines/x")):
            assert getattr(c, method)(f"{API}/repositories/abc/verification{path}").status_code == 401, path


# ------------------------------------------------------------------------------------------------ output safety, linking
def test_secrets_and_oversized_output_are_redacted_and_bounded(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    secret = "ghp_" + "A1b2C3d4E5f6" * 4
    edit(root, "tests/test_util.py", T_UTIL + f"\n\ndef test_leak():\n    password = 'hunter2hunter2'\n    assert False, 'token={secret} password=hunter2hunter2 ' + 'x' * 50000\n")
    d = run(client, rid)
    blob = str(d)
    assert secret not in blob and "hunter2hunter2" not in blob
    assert d["gate_outcome"] == "FAIL"
    assert len(blob) < 200_000
    assert str(root) not in blob


def test_link_a_run_to_related_work(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from tests.test_refactor import rename
    rid, root = setup(client, tmp_path)
    t = rename(client, rid, rel="app_pkg/util.py", name="clamp", new="limit_to") if False else None
    edit(root, "app_pkg/util.py", UTIL + "\n# x\n")
    d = run(client, rid)
    assert d["link"] == {}
    bad = client.put(f"{vb(rid)}/runs/{d['id']}/link", json={"type": "repair", "id": "0" * 32})
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "link_not_found"
    assert client.put(f"{vb(rid)}/runs/{d['id']}/link", json={"type": "bogus", "id": "x"}).status_code == 422
    rr = client.post(f"{API}/repositories/{rid}/refactors/rename", json={"type_id": "python.rename_symbol", "rel_path": "app_pkg/util.py", "name": "clamp", "new_name": "limit_to"})
    assert rr.status_code == 201
    ok = client.put(f"{vb(rid)}/runs/{d['id']}/link", json={"type": "refactor", "id": rr.json()["id"]})
    assert ok.status_code == 200 and ok.json()["link"] == {"type": "refactor", "id": rr.json()["id"]}
    assert t is None


def test_history_filters_and_pagination(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = setup(client, tmp_path)
    for i in range(3):
        edit(root, "app_pkg/util.py", UTIL + f"\n# {i}\n")
        run(client, rid)
    p1 = client.get(f"{vb(rid)}/runs", params={"page_size": 2}).json()
    p2 = client.get(f"{vb(rid)}/runs", params={"page_size": 2, "page": 2}).json()
    assert p1["total"] == 3 and len(p1["items"]) == 2 and len(p2["items"]) == 1
    assert {"id", "status", "gate_outcome", "mode", "head_sha", "files_changed", "created_at"} <= set(p1["items"][0])
    assert client.get(f"{vb(rid)}/runs", params={"status": "error"}).json()["total"] == 0
    assert client.get(f"{vb(rid)}/runs", params={"page_size": 500}).status_code == 422
    audit_actions = {e["action"] for e in client.get(f"{API}/repositories/{rid}/audit").json()["items"]} if client.get(f"{API}/repositories/{rid}/audit").status_code == 200 else set()
    assert audit_actions == set() or "verification.verify_requested" in audit_actions
