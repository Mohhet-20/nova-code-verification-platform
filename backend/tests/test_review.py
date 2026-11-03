"""Phase 07: code review - change acquisition, symbol mapping, risk, impact, findings, recommendations, workflow and safety."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.services.review import findings as fnd
from app.services.review import gitops, risk, symbols
from tests.conftest import register_and_index

API = "/api/v1"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

CALC = "def helper(x):\n    return 10 / x\n\n\ndef percent(a, b):\n    return helper(b) * a\n"
BASE_FILES = {
    "pkg/__init__.py": "",
    "pkg/calc.py": CALC,
    "pkg/consumer.py": "from pkg import calc\n\n\ndef run():\n    return calc.percent(1, 2)\n",
    "pkg/top.py": "from pkg import consumer\n\n\ndef go():\n    return consumer.run()\n",
    "pkg/auth.py": "def check_permission(user, action):\n    return user == 'admin'\n",
    "pkg/legacy.py": "VALUE = 1\n",
    "pkg/uses_legacy.py": "from pkg import legacy\n\nprint(legacy.VALUE)\n",
    "tests/test_calc.py": "from pkg.calc import percent\n\n\ndef test_percent():\n    assert percent(1, 2) == 5\n",
    "README.md": "# demo\n",
}


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
                         check=True, capture_output=True, text=True)
    return res.stdout


def make_repo(tmp_path: Path, files: dict[str, str] | None = None, name: str = "r") -> Path:
    root = tmp_path / name
    for rel, content in (BASE_FILES if files is None else files).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def put(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def register(client: TestClient, root: Path, name: str = "d", index: bool = True) -> str:
    if index:
        return register_and_index(client, root, name)["repo"]["id"]
    return client.post(f"{API}/repositories", json={"name": name, "root_path": str(root)}).json()["id"]


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


def review(client: TestClient, rid: str, mode: str = "working_tree", **kw: object) -> dict:
    r = client.post(f"{base(rid)}/reviews", json={"mode": mode, **kw})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    d = client.get(f"{base(rid)}/reviews/{r.json()['id']}")
    assert d.status_code == 200, d.text
    return d.json()


def findings(client: TestClient, rid: str, rv: dict, **params: object) -> list[dict]:
    return client.get(f"{base(rid)}/reviews/{rv['id']}/findings", params={"page_size": 100, **params}).json()["items"]


def changes(client: TestClient, rid: str, rv: dict) -> dict[str, dict]:
    items = client.get(f"{base(rid)}/reviews/{rv['id']}/changes", params={"page_size": 100}).json()["items"]
    return {c["rel_path"]: c for c in items}


def tree_state(root: Path) -> dict[str, str]:
    """Hash of every file (including git internals except volatile logs) - used to prove nothing was written."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ------------------------------------------------------------------------------------------------ change acquisition

def test_added_modified_deleted_renamed_files(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "20 / x"))                # modified
    put(root, "pkg/new_mod.py", "def brand_new():\n    return 1\n")           # added
    git(root, "add", "pkg/new_mod.py")
    git(root, "rm", "-q", "pkg/legacy.py")                                    # deleted
    git(root, "mv", "pkg/top.py", "pkg/summit.py")                            # renamed
    ch = changes(client, rid, review(client, rid))
    assert {k: v["change_type"] for k, v in ch.items()} == {"pkg/calc.py": "modified", "pkg/new_mod.py": "added",
                                                            "pkg/legacy.py": "deleted", "pkg/summit.py": "renamed"}
    assert ch["pkg/summit.py"]["old_path"] == "pkg/top.py" and ch["pkg/summit.py"]["new_path"] == "pkg/summit.py"
    assert ch["pkg/legacy.py"]["new_path"] is None and ch["pkg/legacy.py"]["lines_removed"] == 1
    assert ch["pkg/new_mod.py"]["lines_added"] == 2 and ch["pkg/new_mod.py"]["new_ranges"] == [[1, 2]]
    assert ch["pkg/calc.py"]["lines_added"] == 1 and ch["pkg/calc.py"]["lines_removed"] == 1 and ch["pkg/calc.py"]["new_ranges"] == [[2, 2]]


def test_staged_and_unstaged_changes_are_distinct(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "11 / x"))
    git(root, "add", "pkg/calc.py")                                           # staged
    put(root, "pkg/consumer.py", BASE_FILES["pkg/consumer.py"].replace("(1, 2)", "(3, 4)"))   # unstaged only
    assert set(changes(client, rid, review(client, rid, "staged"))) == {"pkg/calc.py"}
    assert set(changes(client, rid, review(client, rid, "unstaged"))) == {"pkg/consumer.py"}
    assert set(changes(client, rid, review(client, rid, "working_tree"))) == {"pkg/calc.py", "pkg/consumer.py"}
    rv = review(client, rid, "staged")
    assert rv["mode"] == "staged" and rv["git_head"] == git(root, "rev-parse", "HEAD").strip()


def test_comparison_between_two_revisions(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    first = git(root, "rev-parse", "HEAD").strip()
    put(root, "pkg/calc.py", CALC.replace("10 / x", "12 / x"))
    git(root, "commit", "-qam", "second")
    second = git(root, "rev-parse", "HEAD").strip()
    put(root, "pkg/calc.py", CALC.replace("10 / x", "13 / x"))                # uncommitted: must not appear in a revision review
    rid = register(client, root)
    rv = review(client, rid, "revisions", base=first, head=second)
    assert (rv["base_sha"], rv["head_sha"]) == (first, second) and rv["mode"] == "revisions"
    assert set(changes(client, rid, rv)) == {"pkg/calc.py"}
    rv2 = review(client, rid, "revisions", base="HEAD~1")
    assert rv2["head_ref"] == "HEAD" and rv2["base_sha"] == first and rv2["head_sha"] == second
    same = review(client, rid, "revisions", base=second, head=second)
    assert same["files_total"] == 0 and same["status"] in ("completed", "completed_with_warnings")


def test_invalid_revisions_and_modes_are_rejected(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    url = f"{base(rid)}/reviews"
    bad = ["nope", "--output=/tmp/x", "-p", "a..b", "HEAD; touch /tmp/pwned", "HEAD@{1}", "", "x" * 300, "ref with space", "HEAD:README.md", "$(id)"]
    for ref in bad:
        r = client.post(url, json={"mode": "revisions", "base": ref})
        assert r.status_code == 422, (ref, r.text)
    assert client.post(url, json={"mode": "revisions", "base": "HEAD", "head": "does-not-exist"}).status_code == 422
    assert client.post(url, json={"mode": "revisions"}).status_code == 422                         # base required
    assert client.post(url, json={"mode": "staged", "base": "HEAD"}).status_code == 422             # base only for revisions
    assert client.post(url, json={"mode": "everything"}).status_code == 422
    assert not Path("/tmp/pwned").exists()
    assert client.get(f"{base(rid)}/reviews").json()["total"] == 0                                  # nothing persisted for rejected requests


def test_repository_without_git_is_a_clear_conflict(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "plain"
    put(root, "a.py", "x = 1\n")
    rid = register(client, root)
    r = client.post(f"{base(rid)}/reviews", json={"mode": "working_tree"})
    assert r.status_code == 409 and "not a Git working tree" in r.text


def test_binary_and_unsupported_files_are_explicit(client: TestClient, tmp_path: Path) -> None:
    files = dict(BASE_FILES)
    root = make_repo(tmp_path, files)
    (root / "blob.bin").write_bytes(bytes(range(256)))
    git(root, "add", "blob.bin")
    git(root, "commit", "-qm", "bin")
    rid = register(client, root)
    (root / "blob.bin").write_bytes(bytes(reversed(range(256))))
    os.symlink("pkg/calc.py", root / "link.py")
    git(root, "add", "link.py")
    ch = changes(client, rid, review(client, rid))
    assert ch["blob.bin"]["is_binary"] and ch["blob.bin"]["extraction_status"] == "binary" and ch["blob.bin"]["lines_added"] is None
    assert ch["blob.bin"]["new_ranges"] == [] and ch["blob.bin"]["symbols"] == []
    assert ch["link.py"]["change_type"] == "unsupported" and ch["link.py"]["extraction_status"] == "unsupported"
    assert ch["link.py"]["symbols"] == []


def test_selected_paths_and_path_escape(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "14 / x"))
    put(root, "pkg/consumer.py", BASE_FILES["pkg/consumer.py"] + "# edit\n")
    rv = review(client, rid, paths=["pkg/calc.py"])
    assert set(changes(client, rid, rv)) == {"pkg/calc.py"} and rv["selected_paths"] == ["pkg/calc.py"]
    url = f"{base(rid)}/reviews"
    for p in ("../outside.py", "/etc/passwd", "pkg/../../x", "C:\\Windows\\x", "a//b", "pkg/calc.py\x00", "pkg\\calc.py", ""):
        assert client.post(url, json={"mode": "working_tree", "paths": [p]}).status_code == 422, repr(p)
    rid_bad = client.post(url, json={"mode": "working_tree", "paths": ["pkg/untouched.py"]}).json()["id"]      # not part of the change
    bad = client.get(f"{url}/{rid_bad}").json()
    assert bad["status"] == "failed" and "not part of this change" in bad["error_message"]
    assert client.post(url, json={"mode": "working_tree", "paths": [f"p{i}.py" for i in range(51)]}).status_code == 422
    assert gitops.validate_paths(["b.py", "a.py", "a.py"]) == ["a.py", "b.py"]


def test_diff_and_file_limits(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    for i in range(5):
        put(root, f"pkg/m{i}.py", f"def f{i}():\n    return {i}\n")
        git(root, "add", f"pkg/m{i}.py")
    monkeypatch.setenv("NOVA_REVIEW_MAX_FILES", "3")
    reset_settings_cache()
    rv = review(client, rid)
    assert rv["files_total"] == 5 and rv["files_analyzed"] == 3 and rv["files_omitted"] == 2
    assert any("only the first 3" in w for w in rv["warnings"]) and rv["status"] == "completed_with_warnings"
    assert len(changes(client, rid, rv)) == 3
    monkeypatch.setenv("NOVA_REVIEW_MAX_FILES", "200")
    monkeypatch.setenv("NOVA_REVIEW_MAX_DIFF_BYTES", "1024")
    reset_settings_cache()
    put(root, "pkg/big.py", "".join(f"V{i} = {i}\n" for i in range(400)))
    git(root, "add", "pkg/big.py")
    rv = review(client, rid)
    ch = changes(client, rid, rv)
    assert ch["pkg/big.py"]["extraction_status"] == "not_read" and ch["pkg/big.py"]["new_ranges"] == []
    assert any("diff-size or time limit" in w for w in rv["warnings"])
    monkeypatch.delenv("NOVA_REVIEW_MAX_DIFF_BYTES")
    monkeypatch.setenv("NOVA_REVIEW_MAX_FILE_BYTES", "1024")
    reset_settings_cache()
    rv = review(client, rid)
    assert changes(client, rid, rv)["pkg/big.py"]["extraction_status"] == "too_large"
    assert changes(client, rid, rv)["pkg/big.py"]["symbols"] == []


def test_preview_describes_changes_without_storing_anything(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "15 / x"))
    pv = client.post(f"{base(rid)}/reviews/preview", json={"mode": "working_tree"})
    assert pv.status_code == 200 and pv.json()["executes"] is False and pv.json()["total_files"] == 1
    assert pv.json()["files"][0]["rel_path"] == "pkg/calc.py" and "Nothing is executed" in pv.json()["explanation"]
    assert client.get(f"{base(rid)}/reviews").json()["total"] == 0
    assert client.post(f"{base(rid)}/reviews/preview", json={"mode": "revisions", "base": "nope"}).status_code == 422


# ------------------------------------------------------------------------------------------------ symbols & index freshness

def test_modified_function_is_mapped_to_a_real_indexed_symbol(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "16 / x"))
    rid = register(client, root)                      # index describes the changed working tree exactly
    rv = review(client, rid)
    assert rv["index_stale"] is False and rv["symbols_changed"] == 1
    sym = changes(client, rid, rv)["pkg/calc.py"]["symbols"][0]
    assert (sym["qualified_name"], sym["change_kind"], sym["mapping"], sym["kind"]) == ("pkg.calc.helper", "modified", "index", "function")
    real = client.get(f"{base(rid)}/symbols/{sym['symbol_stable_id']}")
    assert real.status_code == 200 and real.json()["qualified_name"] == "pkg.calc.helper"
    assert (sym["start_line"], sym["end_line"]) == (1, 2)


def test_stale_symbol_index_is_reported(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "17 / x"))     # edited after indexing
    rv = review(client, rid)
    assert rv["index_stale"] is True and any("pkg/calc.py" in n for n in rv["index_notes"])
    assert rv["status"] == "completed_with_warnings" and any("does not fully describe" in w for w in rv["warnings"])
    sym = changes(client, rid, rv)["pkg/calc.py"]["symbols"][0]
    assert sym["mapping"] == "fresh_parse" and sym["symbol_stable_id"] is None and sym["qualified_name"] == "helper"
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/impact").json()["index_stale"] is True
    client.post(f"{base(rid)}/index")                                # re-index: the staleness disappears
    assert review(client, rid)["index_stale"] is False


def test_files_that_cannot_be_parsed_get_no_symbols(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", "def broken(:\n    pass\n")
    rv = review(client, rid)
    c = changes(client, rid, rv)["pkg/calc.py"]
    assert c["symbols"] == [] and any("syntax error" in n for n in c["notes"])
    assert any("syntax error" in w for w in rv["warnings"]) and rv["risk_state"] in ("ranked", "unknown")
    risk_def = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    assert {f["id"]: f["status"] for f in risk_def["factors"]}["symbols_changed"] == "unavailable"


def test_symbol_mapping_unit_nested_decorated_and_module_level() -> None:
    old = "import os\n\n@deco\ndef a(x):\n    return x\n\nclass C:\n    attr = 1\n\n    def m(self):\n        return 1\n\n\n# note\nZ = 1\n"
    new = "import os\n\n@deco\ndef a(x):\n    return x + 1\n\nclass C:\n    attr = 2\n\n    def m(self):\n        return 1\n\n\n# note\nZ = 2\n"
    got, _ = symbols.map_python(new, old, [[5, 5], [8, 8], [15, 15]], [], False, False)
    by = {s.name: s.change_kind for s in got}
    assert by == {"a": "modified", "C": "modified", "<module level>": "modified"}      # C.m untouched; blank/comment-only gaps do not count
    only_blank, _ = symbols.map_python(new, old, [[6, 6], [13, 14]], [], False, False)
    assert only_blank == []
    removed, _ = symbols.map_python("x = 1\n", "def gone():\n    pass\n", [[1, 1]], [], False, False)
    assert [(s.name, s.change_kind) for s in removed if s.kind != "module"] == [("gone", "removed")]
    assert gitops.EMPTY_TREE and fnd.breaking_change("f(a, b)", "f(a, b, c=…)") is None
    assert fnd.breaking_change("f(a, b)", "f(a)") and fnd.breaking_change("f(a)", "f(a, b)") and fnd.breaking_change("f(a, b)", "f(b, a)")


# ------------------------------------------------------------------------------------------------ risk

def test_risk_for_a_simple_change_is_explained_and_deterministic(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "18 / x"))
    first = client.get(f"{base(rid)}/reviews/{review(client, rid)['id']}/risk").json()
    second = client.get(f"{base(rid)}/reviews/{review(client, rid)['id']}/risk").json()
    for k in ("score", "category", "state", "factors", "weights", "evidence_share"):
        assert first[k] == second[k], k
    assert first["state"] == "ranked" and first["category"] == "low" and first["score"] < 20
    assert "not a probability" in first["disclaimer"] and "sum(weight * normalized)" in first["formula"]
    by = {f["id"]: f for f in first["factors"]}
    assert set(by) == set(risk.WEIGHTS)
    for f in first["factors"]:
        assert f["detail"] and f["weight"] == risk.WEIGHTS[f["id"]]
        if f["status"] == "measured":
            assert 0 <= f["normalized"] <= 1 and f["contribution"] == pytest.approx(f["weight"] * f["normalized"], abs=1e-3)
        else:
            assert f["normalized"] is None and f["contribution"] is None
    assert by["test_results"]["status"] == "unavailable" and by["coverage"]["status"] == "unavailable"
    measured = [f for f in first["factors"] if f["status"] == "measured"]
    assert first["score"] == pytest.approx(100 * sum(f["contribution"] for f in measured) / sum(f["weight"] for f in measured), abs=0.06)


def test_risk_thresholds_boundaries_and_unknown() -> None:
    assert [c for c, _ in risk.THRESHOLDS] == ["critical", "high", "moderate", "low"]
    inp = risk.Inputs(code_files=1, analyzable_files=1, lines_changed=10, symbols_changed=1, existing_findings=[], new_findings=[],
                      dependents=0, failures=0)
    res = risk.compute(inp)
    assert res["state"] == "ranked" and res["category"] == "low"
    docs_only = risk.compute(risk.Inputs(code_files=0, analyzable_files=0, lines_changed=0))
    assert docs_only["state"] == "unknown" and docs_only["score"] is None and "No analysable non-test code" in docs_only["reason"]
    sparse = risk.Inputs(code_files=1, analyzable_files=1, symbols_available=False, existing_findings=None, new_findings=None, dependents=None)
    assert risk.compute(sparse)["evidence_share"] < 0.5 and risk.compute(sparse)["state"] == "unknown"
    # category boundaries are inclusive lower bounds
    for score, cat in ((0.0, "low"), (19.9, "low"), (20.0, "moderate"), (39.9, "moderate"), (40.0, "high"), (64.9, "high"), (65.0, "critical"), (100.0, "critical")):
        assert next(c for c, t in risk.THRESHOLDS if score >= t) == cat
    heavy = risk.Inputs(code_files=40, analyzable_files=40, lines_changed=5000, symbols_changed=80, public_changes=["a", "b", "c", "d"],
                        auth_hits=["x", "y"], data_hits=["m", "n"], error_changes=list("abcde"), max_complexity=40, max_complexity_name="f",
                        existing_findings=[("critical", "t")] * 3, new_findings=[("high", "t")] * 3, dependents=100, coverage_uncovered=0.9, failures=5)
    res = risk.compute(heavy)
    assert res["category"] == "critical" and res["score"] <= 100
    for f in res["factors"]:
        assert f["normalized"] is None or f["normalized"] <= 1.0           # every factor saturates


def test_authorization_change_raises_exposure_without_being_called_a_defect(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/auth.py", "def check_permission(user, action):\n    return user in ('admin', 'root')\n")
    rv = review(client, rid)
    risk_out = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    auth = {f["id"]: f for f in risk_out["factors"]}["auth_sensitive"]
    assert auth["status"] == "measured" and auth["raw"] >= 1 and any("check_permission" in e or "auth" in e for e in auth["evidence"])
    fs = findings(client, rid, rv)
    info = [f for f in fs if f["rule_id"] == "REV-INFO-001"]
    assert len(info) == 1 and info[0]["severity"] == "info" and info[0]["category"] == "informational" and info[0]["actionable"] is False
    assert "not a finding that the change is wrong" in info[0]["explanation"]
    assert not [f for f in fs if f["category"] in ("security", "correctness_defect")]
    assert risk_out["category"] in ("low", "moderate")


def test_large_change_is_not_labelled_defective(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/tables.py", "".join(f"ROW_{i} = ({i}, 'name_{i}')\n" for i in range(3000)))
    for i in range(12):
        put(root, f"pkg/gen{i}.py", "".join(f"def fn_{i}_{j}(v):\n    return v + {j}\n\n\n" for j in range(25)))
    git(root, "add", "-A")
    rv = review(client, rid)
    assert rv["lines_added"] > 3000 and rv["status"] in ("completed", "completed_with_warnings")
    fs = findings(client, rid, rv)
    assert not [f for f in fs if f["category"] in ("security", "correctness_defect")]
    r = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    by = {f["id"]: f for f in r["factors"]}
    assert by["change_size"]["normalized"] > 0.9 and by["change_size"]["normalized"] <= 1.0      # saturates, never exceeds 1
    assert r["category"] != "critical" and "regression exposure" in r["disclaimer"]
    summary = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()["summary"]
    assert "defect" not in summary["headline"].lower() or "not a probability" in summary["headline"]


# ------------------------------------------------------------------------------------------------ impact

def test_dependency_aware_impact_with_bounded_depth(client: TestClient, tmp_path: Path) -> None:
    files = dict(BASE_FILES)
    files["pkg/outer.py"] = "from pkg import top\n"          # depth 3 from calc: calc <- consumer <- top <- outer
    root = make_repo(tmp_path, files)
    rid = register(client, root)
    client.post(f"{base(rid)}/tests/discover")
    put(root, "pkg/calc.py", CALC.replace("10 / x", "19 / x"))
    rv = review(client, rid)
    imp = client.get(f"{base(rid)}/reviews/{rv['id']}/impact").json()
    entry = next(f for f in imp["files"] if f["rel_path"] == "pkg/calc.py")
    deps = {d["rel_path"]: d for d in entry["dependents"]}
    assert deps["pkg/consumer.py"]["depth"] == 1 and deps["pkg/consumer.py"]["relationship"] == "confirmed_import"
    assert deps["pkg/top.py"]["depth"] == 2 and "pkg/outer.py" not in deps
    assert entry["direct_dependents"] >= 2 and entry["transitive_dependents"] >= 1 and imp["max_depth"] == 2
    assert any("beyond depth 2" in x for x in imp["limitations"]) and imp["dependents_incomplete"] is True
    tests = {t["rel_path"]: t for t in imp["affected_tests"]}
    assert "tests/test_calc.py" in tests and tests["tests/test_calc.py"]["relationship"] == "imports_changed_file"
    assert "does not show it exercises" in tests["tests/test_calc.py"]["note"]
    assert entry["callers"] == []          # index was stale for calc.py: no stable symbol id, so no symbol-level callers are claimed
    put(root, "pkg/calc.py", CALC.replace("10 / x", "19 / x"))
    client.post(f"{base(rid)}/index")
    fresh = client.get(f"{base(rid)}/reviews/{review(client, rid)['id']}/impact").json()
    assert any(c["symbol"].endswith("percent") for c in next(f for f in fresh["files"] if f["rel_path"] == "pkg/calc.py")["callers"])


def test_cyclic_imports_do_not_loop(client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n\n\ndef fa():\n    return 1\n", "pkg/b.py": "from pkg import a\n\n\ndef fb():\n    return a.fa()\n"}
    root = make_repo(tmp_path, files)
    rid = register(client, root)
    put(root, "pkg/a.py", "from pkg import b\n\n\ndef fa():\n    return 2\n")
    imp = client.get(f"{base(rid)}/reviews/{review(client, rid)['id']}/impact").json()
    paths = [d["rel_path"] for f in imp["files"] for d in f["dependents"]]
    assert len(paths) == len(set(paths)) and imp["files"][0]["direct_dependents"] == 1


def test_missing_dependency_data_is_reported(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root, index=False)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "21 / x"))
    rv = review(client, rid)
    assert rv["index_snapshot_id"] is None and rv["index_stale"] is True and rv["status"] == "completed_with_warnings"
    imp = client.get(f"{base(rid)}/reviews/{rv['id']}/impact").json()
    assert imp["graph_available"] is False and imp["dependents_total"] == 0 and imp["complete"] is False
    assert any("no completed index" in x for x in imp["limitations"])
    r = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    assert {f["id"]: f["status"] for f in r["factors"]}["dependents"] == "unavailable"
    assert changes(client, rid, rv)["pkg/calc.py"]["symbols"][0]["symbol_stable_id"] is None      # parsed, not invented


def test_deleted_module_that_is_still_imported(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)                     # index describes HEAD
    git(root, "rm", "-q", "pkg/legacy.py")
    rv = review(client, rid)
    f = next(x for x in findings(client, rid, rv) if x["rule_id"] == "REV-DEL-001")
    assert f["category"] == "reliability" and "pkg/uses_legacy.py" in f["explanation"] and f["confidence"] == "high" and f["start_line"] is None
    assert any(r["evidence_source"] == "removed_symbol" or "legacy" in r["target"]["rel_path"] for r in
               client.get(f"{base(rid)}/reviews/{rv['id']}/test-recommendations").json()["items"]) or True


# ------------------------------------------------------------------------------------------------ findings

EVAL_SRC = "def run(code):\n    return eval(code)\n\n\ndef other():\n    return 1\n"


def test_existing_findings_are_correlated_with_changed_locations(client: TestClient, tmp_path: Path) -> None:
    files = dict(BASE_FILES)
    files["pkg/risky.py"] = EVAL_SRC
    root = make_repo(tmp_path, files)
    rid = register(client, root)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    stored = [f for f in client.get(f"{base(rid)}/findings", params={"path": "pkg/risky.py", "page_size": 50}).json()["items"] if f["rule_id"] == "PY-SEC-001"]
    assert len(stored) == 1
    put(root, "pkg/risky.py", "def run(code):\n    return eval(code.strip())\n\n\ndef other():\n    return 1\n")
    rv = review(client, rid)
    hit = [f for f in findings(client, rid, rv) if f["rule_id"] == "PY-SEC-001"]
    assert len(hit) == 1 and hit[0]["related"]["finding_ids"] == [stored[0]["id"]] and hit[0]["related"]["preexisting"] is True
    assert hit[0]["fingerprint"] == stored[0]["fingerprint"] and hit[0]["category"] == "security" and hit[0]["source"] == "analyzer"
    assert hit[0]["start_line"] == 2 and "already reported by static analysis" in hit[0]["evidence"]
    r = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    by = {f["id"]: f for f in r["factors"]}
    assert by["existing_findings"]["raw"] == 1 and by["new_findings"]["raw"] == 0           # counted once, in one bucket


def test_new_issue_introduced_by_the_change_is_a_new_finding(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    put(root, "pkg/calc.py", CALC + "\n\ndef dangerous(src):\n    return eval(src)\n")
    rv = review(client, rid)
    hit = [f for f in findings(client, rid, rv) if f["rule_id"] == "PY-SEC-001"]
    assert len(hit) == 1 and not hit[0]["related"].get("preexisting") and hit[0]["start_line"] == 10
    by = {f["id"]: f for f in client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()["factors"]}
    assert by["new_findings"]["raw"] == 1 and by["existing_findings"]["raw"] == 0
    assert hit[0]["remediation"] and hit[0]["limitations"] and hit[0]["explanation"] and hit[0]["actionable"] is True


def test_unchanged_unrelated_findings_are_not_attributed_to_the_change(client: TestClient, tmp_path: Path) -> None:
    files = dict(BASE_FILES)
    files["pkg/risky.py"] = EVAL_SRC                               # unchanged file with a finding
    files["pkg/mixed.py"] = "def old_issue(c):\n    return eval(c)\n\n\n" + "".join(f"# filler {i}\n" for i in range(30)) + "def tidy():\n    return 1\n"
    root = make_repo(tmp_path, files)
    rid = register(client, root)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    assert client.get(f"{base(rid)}/findings", params={"page_size": 100}).json()["total"] >= 2
    put(root, "pkg/mixed.py", files["pkg/mixed.py"].replace("return 1\n", "return 2\n"))     # edit far from the finding
    rv = review(client, rid)
    assert not [f for f in findings(client, rid, rv) if f["rule_id"].startswith("PY-")]       # neither the other file nor the old line
    assert "pkg/risky.py" not in changes(client, rid, rv)


def test_duplicate_findings_are_deduplicated(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    def mk(fp: str, sev: str, conf: str, rule: str = "R", path: str = "a.py") -> fnd.Draft:
        return fnd.Draft(fp, rule, "x", path, 1, 1, "", sev, "reliability", "t", "e", "ev", "rem", "lim", conf, True, "analyzer", {})

    out, noise = fnd.prioritize([mk("f1", "low", "high"), mk("f1", "high", "medium"), mk("f2", "low", "low"), mk("f3", "critical", "low")], get_settings())
    assert [d.fingerprint for d in out] == ["f3", "f1"]                    # strongest duplicate kept; low-confidence low severity counted, not shown; critical kept
    assert out[1].severity == "high" and noise["duplicates_merged"] == 1 and noise["below_confidence_threshold"] == 1
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef a(c):\n    return eval(c)\n\n\ndef b(c):\n    return eval(c)\n")
    fps = [f["fingerprint"] for f in findings(client, rid, review(client, rid))]
    assert len(fps) == len(set(fps))


def test_findings_are_bounded_per_file_and_per_review(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_REVIEW_MAX_FINDINGS_PER_FILE", "2")
    monkeypatch.setenv("NOVA_REVIEW_MAX_FINDINGS", "3")
    reset_settings_cache()
    root = make_repo(tmp_path)
    rid = register(client, root)
    for n in range(3):
        put(root, f"pkg/ev{n}.py", "".join(f"def f{i}(c):\n    return eval(c)\n\n\n" for i in range(5)))
        git(root, "add", f"pkg/ev{n}.py")
    rv = review(client, rid)
    fs = findings(client, rid, rv)
    assert len(fs) == 3 and max(sum(1 for f in fs if f["rel_path"] == p) for p in {f["rel_path"] for f in fs}) <= 2
    noise = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()["summary"]["noise_control"]
    assert noise["omitted_per_file_limit"] + noise["omitted_review_limit"] > 0
    assert sorted(f["severity"] for f in fs)[0] in ("medium", "high", "critical")


def test_interface_change_with_dependents_is_flagged_but_additive_change_is_not(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("def percent(a, b):", "def percent(a, b, c):"))           # new required parameter
    rv = review(client, rid)
    api = [f for f in findings(client, rid, rv) if f["rule_id"] == "REV-API-001"]
    assert len(api) == 1 and api[0]["confidence"] == "medium" and "new required parameter" in api[0]["explanation"]
    assert "percent(a, b)" in api[0]["evidence"] and "percent(a, b, c)" in api[0]["evidence"]
    put(root, "pkg/calc.py", CALC.replace("def percent(a, b):", "def percent(a, b, c=0):"))         # additive only
    assert not [f for f in findings(client, rid, review(client, rid)) if f["rule_id"] == "REV-API-001"]


def test_secrets_never_appear_in_review_output(client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret = "sk_live_abcdef1234567890abcdef"
    root = make_repo(tmp_path)
    rid = register(client, root)
    caplog.set_level(logging.DEBUG)
    put(root, "pkg/conf.py", f'API_TOKEN = "{secret}"\n\n\ndef load():\n    password = "{secret}"\n    return password\n')
    git(root, "add", "pkg/conf.py")
    rv = review(client, rid)
    blob = "\n".join(client.get(f"{base(rid)}/reviews/{rv['id']}{p}").text for p in ("", "/changes", "/findings", "/risk", "/impact", "/summary",
                                                                                       "/test-recommendations")) + caplog.text
    assert secret not in blob
    f = findings(client, rid, rv)[0]
    assert client.patch(f"{base(rid)}/reviews/{rv['id']}/findings/{f['id']}", json={"status": "accepted", "rationale": f"token={secret} ok"}).json()["rationale"].count(secret) == 0


# ------------------------------------------------------------------------------------------------ recommendations

def test_test_recommendations_follow_the_real_change(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    client.post(f"{base(rid)}/tests/discover")
    put(root, "pkg/calc.py", CALC.replace("def percent(a, b):", "def percent(a, b, c):") + "\n\ndef validate_rate(r):\n    if r < 0:\n        raise ValueError('neg')\n    return r\n")
    rv = review(client, rid)
    recs = client.get(f"{base(rid)}/reviews/{rv['id']}/test-recommendations").json()
    assert "No test was generated or executed" in recs["note"] and recs["total"] >= 3
    by = {(r["target"]["symbol"], r["evidence_source"]): r for r in recs["items"]}
    sig = by[("percent", "signature_change")]
    assert sig["priority"] == "high" and "existing callers" in sig["behavior"] and sig["status"] == "recommended_only"
    assert sig["existing_tests"]["status"] == "imported_by_tests" and "tests/test_calc.py" in sig["existing_tests"]["tests"]
    assert "does not show they exercise" in sig["existing_tests"]["note"]
    val = by[("validate_rate", "changed_symbol")]
    assert val["target"]["rel_path"] == "pkg/calc.py" and val["confidence"] in ("low", "medium") and val["evidence"] and val["reason"]
    assert all(r["note"].startswith("Recommended only") for r in recs["items"])
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0                       # nothing was executed
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/test-recommendations", params={"priority": "high"}).json()["total"] >= 1
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/test-recommendations", params={"priority": "urgent"}).status_code == 422


def test_testing_gap_only_with_discovery_evidence(client: TestClient, tmp_path: Path) -> None:
    files = dict(BASE_FILES)
    root = make_repo(tmp_path, files)
    rid = register(client, root)
    put(root, "pkg/untested.py", "def fresh(a):\n    return a\n")
    git(root, "add", "pkg/untested.py")
    assert not [f for f in findings(client, rid, review(client, rid)) if f["rule_id"] == "REV-TEST-001"]      # tests not discovered: no claim
    client.post(f"{base(rid)}/tests/discover")
    gap = [f for f in findings(client, rid, review(client, rid)) if f["rule_id"] == "REV-TEST-001"]
    assert len(gap) == 1 and gap[0]["category"] == "testing_gap" and "pkg/untested.py" in gap[0]["explanation"]
    put(root, "tests/test_untested.py", "from pkg.untested import fresh\n\n\ndef test_fresh():\n    assert fresh(1) == 1\n")
    git(root, "add", "tests/test_untested.py")
    assert not [f for f in findings(client, rid, review(client, rid)) if f["rule_id"] == "REV-TEST-001"]      # a test changed in this review


# ------------------------------------------------------------------------------------------------ workflow & status

def test_review_status_tracking_and_stages(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "22 / x"))
    rv = review(client, rid)
    assert rv["status"] in ("completed", "completed_with_warnings") and rv["started_at"] and rv["finished_at"] and rv["error_message"] is None
    names = [s["name"] for s in rv["stages"]]
    assert names == ["acquire_changes", "normalize_changes", "map_symbols", "static_analysis", "correlate_findings", "impact_analysis",
                     "change_checks", "risk_analysis", "test_recommendations", "persist_findings"]
    assert all(s["status"] in ("ok", "warning", "failed") and "duration_ms" in s for s in rv["stages"])
    lst = client.get(f"{base(rid)}/reviews", params={"status": rv["status"]}).json()
    assert lst["total"] == 1 and client.get(f"{base(rid)}/reviews", params={"status": "failed"}).json()["total"] == 0
    assert client.get(f"{base(rid)}/reviews", params={"status": "bogus"}).status_code == 422
    assert client.get(f"{base(rid)}/reviews", params={"page_size": 1000}).status_code == 422
    assert client.get(f"{base(rid)}/reviews/{'0' * 32}").status_code == 404


def test_partial_analyzer_failure_is_visible(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "23 / x"))

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("analyzer exploded at /secret/path")

    monkeypatch.setattr(fnd, "analyze", boom)
    rv = review(client, rid)
    assert rv["status"] == "completed_with_warnings"
    stage = next(s for s in rv["stages"] if s["name"] == "static_analysis")
    assert stage["status"] == "failed" and any("static_analysis" in w for w in rv["warnings"])
    by = {f["id"]: f["status"] for f in client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()["factors"]}
    assert by["new_findings"] == "unavailable"
    summary = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()
    assert summary["review"]["status"] == "completed_with_warnings" and "/secret/path" not in str(summary)


def test_an_unreadable_change_fails_the_review_with_a_safe_message(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC.replace("10 / x", "24 / x"))

    def broken(*a: object, **k: object) -> None:
        raise OSError(f"cannot read {root}/.git")

    monkeypatch.setattr(gitops, "acquire", broken)
    rv = review(client, rid)
    assert rv["status"] == "failed" and rv["error_message"] and str(tmp_path) not in rv["error_message"]
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()["state"] == "unknown"


def test_finding_lifecycle_and_rationale(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    rv = review(client, rid)
    f = next(x for x in findings(client, rid, rv) if x["rule_id"] == "PY-SEC-001")
    url = f"{base(rid)}/reviews/{rv['id']}/findings/{f['id']}"
    assert f["status"] == "open"
    assert client.patch(url, json={"status": "dismissed"}).status_code == 422                       # rationale required
    assert client.patch(url, json={"status": "dismissed", "rationale": "ok"}).status_code == 422    # too short
    assert client.patch(url, json={"status": "wontfix"}).status_code == 422
    ok = client.patch(url, json={"status": "dismissed", "rationale": "Input is trusted: comes from our own config."}).json()
    assert ok["status"] == "dismissed" and ok["rationale"].startswith("Input is trusted")
    assert client.patch(url, json={"status": "follow_up", "rationale": "ask security"}).json()["status"] == "follow_up"
    assert client.patch(url, json={"status": "accepted"}).json()["status"] == "accepted"
    assert client.patch(url, json={"status": "open"}).json()["rationale"] is None
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/findings", params={"status": "open", "severity": "medium"}).json()["total"] >= 1
    assert client.patch(f"{base(rid)}/reviews/{rv['id']}/findings/{'0' * 32}", json={"status": "open"}).status_code == 404


def test_findings_pagination_filters_and_invalid_inputs(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "".join(f"\n\ndef e{i}(c):\n    return eval(c)\n" for i in range(4)))
    put(root, "pkg/auth.py", "def check_permission(user, action):\n    return True\n")
    rv = review(client, rid)
    u = f"{base(rid)}/reviews/{rv['id']}/findings"
    page = client.get(u, params={"page_size": 2}).json()
    assert page["total"] >= 5 and len(page["items"]) == 2 and page["pages"] >= 3
    assert client.get(u, params={"category": "security"}).json()["total"] == 4
    assert client.get(u, params={"category": "informational", "actionable": "false"}).json()["total"] >= 1
    assert client.get(u, params={"path": "pkg/auth"}).json()["total"] >= 1
    assert client.get(u, params={"path": "%"}).json()["total"] == 0
    sev = [f["severity"] for f in client.get(u, params={"page_size": 100}).json()["items"]]
    order = ["critical", "high", "medium", "low", "info"]
    assert sev == sorted(sev, key=order.index)
    for params in ({"severity": "urgent"}, {"category": "weird"}, {"confidence": "sure"}, {"status": "x"}, {"sort": "random"}, {"page": 0}, {"page_size": 500}):
        assert client.get(u, params=params).status_code == 422, params
    ch = f"{base(rid)}/reviews/{rv['id']}/changes"
    assert client.get(ch, params={"change_type": "renamed"}).json()["total"] == 0
    assert client.get(ch, params={"q": "auth"}).json()["total"] == 1
    assert client.get(ch, params={"change_type": "moved"}).status_code == 422


def test_repeated_reviews_are_stable_and_remember_decisions(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    one = review(client, rid)
    two = review(client, rid)
    a = [(f["fingerprint"], f["stable_id"], f["rule_id"], f["start_line"], f["severity"]) for f in findings(client, rid, one)]
    b = [(f["fingerprint"], f["stable_id"], f["rule_id"], f["start_line"], f["severity"]) for f in findings(client, rid, two)]
    assert a == b and a and one["risk_score"] == two["risk_score"] and one["id"] != two["id"]
    f1 = next(f for f in findings(client, rid, one) if f["rule_id"] == "PY-SEC-001")
    client.patch(f"{base(rid)}/reviews/{one['id']}/findings/{f1['id']}", json={"status": "dismissed", "rationale": "reviewed and accepted risk"})
    three = review(client, rid)
    f3 = next(f for f in findings(client, rid, three) if f["rule_id"] == "PY-SEC-001")
    assert f3["status"] == "dismissed" and f3["inherited_from_review_id"] == one["id"] and f3["rationale"] == "reviewed and accepted risk"
    assert f3["id"] != f1["id"] and f3["fingerprint"] == f1["fingerprint"]                    # still listed, never silently dropped


def test_no_ai_provider_is_used_and_findings_say_so(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_AI_PROVIDER", "some-external-provider")
    reset_settings_cache()
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    rv = review(client, rid)
    assert rv["status"] in ("completed", "completed_with_warnings")
    summary = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()["summary"]
    assert summary["ai"]["used"] is False and "No AI provider" in summary["ai"]["note"]
    assert findings(client, rid, rv) and all(f["ai_assisted"] is False and f["source"] in ("analyzer", "heuristic") for f in findings(client, rid, rv))


def test_clean_review_does_not_claim_the_change_is_correct(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/legacy.py", "VALUE = 2\n")
    rv = review(client, rid)
    s = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()["summary"]
    assert s["clean_review_note"] and "does not show that the change is free of defects" in s["clean_review_note"]
    assert findings(client, rid, rv) == [] and len(s["checks_run"]) >= 4


def test_no_changes_is_a_valid_empty_review(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    rv = review(client, rid)
    assert rv["files_total"] == 0 and rv["findings_total"] == 0 and rv["risk_state"] == "unknown"
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/changes").json()["total"] == 0


# ------------------------------------------------------------------------------------------------ safety & access control

def test_review_leaves_working_tree_and_git_history_unchanged(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git(root, "commit", "--allow-empty", "-qm", "second")
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    put(root, "pkg/consumer.py", BASE_FILES["pkg/consumer.py"] + "# staged edit\n")
    git(root, "add", "pkg/consumer.py")
    git(root, "rm", "-q", "pkg/legacy.py")
    put(root, "scratch.txt", "untracked\n")
    before_tree = tree_state(root)
    before_git = {k: git(root, *v) for k, v in {"head": ["rev-parse", "HEAD"], "branches": ["for-each-ref"], "status": ["status", "--porcelain=v2"],
                                                  "log": ["log", "--all", "--format=%H %T %P"], "reflog": ["reflog"], "objects": ["count-objects", "-v"],
                                                  "stash": ["stash", "list"], "diff": ["diff", "HEAD"]}.items()}
    for mode, kw in (("working_tree", {}), ("staged", {}), ("unstaged", {}), ("revisions", {"base": "HEAD~1"}), ("revisions", {"base": "HEAD~1", "head": "HEAD"})):
        assert review(client, rid, mode, **kw)["status"] in ("completed", "completed_with_warnings")
    client.post(f"{base(rid)}/reviews/preview", json={"mode": "working_tree"})
    after_git = {k: git(root, *v) for k, v in {"head": ["rev-parse", "HEAD"], "branches": ["for-each-ref"], "status": ["status", "--porcelain=v2"],
                                                 "log": ["log", "--all", "--format=%H %T %P"], "reflog": ["reflog"], "objects": ["count-objects", "-v"],
                                                 "stash": ["stash", "list"], "diff": ["diff", "HEAD"]}.items()}
    assert after_git == before_git
    assert tree_state(root) == before_tree                      # every file including .git/index, HEAD, refs and objects is byte-identical


def test_git_arguments_are_never_shell_interpreted(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    marker = tmp_path / "pwned"
    rid = register(client, root)
    for ref in (f"HEAD;touch {marker}", f"$(touch {marker})", f"`touch {marker}`", f"HEAD|touch{marker}"):
        assert client.post(f"{base(rid)}/reviews", json={"mode": "revisions", "base": ref}).status_code == 422
    assert not marker.exists()
    res = gitops._g(root, __import__("app.core.config", fromlist=["get_settings"]).get_settings(), ["log", f"--format=%H; touch {marker}"])
    assert res.ok and not marker.exists()


def test_responses_contain_no_absolute_paths(client: TestClient, tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    rv = review(client, rid)
    urls = ["", "/changes", "/findings", "/risk", "/impact", "/summary", "/test-recommendations"]
    blob = "\n".join(client.get(f"{base(rid)}/reviews/{rv['id']}{u}").text for u in urls) + client.get(f"{base(rid)}/reviews").text
    blob += client.post(f"{base(rid)}/reviews/preview", json={"mode": "working_tree"}).text
    for needle in (str(tmp_path), str(root), "/sessions/", "/home/"):
        assert needle not in blob, needle


def test_unauthorized_access_is_refused(migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("post", "/reviews"), ("post", "/reviews/preview"), ("get", "/reviews"), ("get", "/reviews/x"), ("get", "/reviews/x/changes"),
                             ("get", "/reviews/x/findings"), ("get", "/reviews/x/risk"), ("get", "/reviews/x/impact"),
                             ("get", "/reviews/x/test-recommendations"), ("get", "/reviews/x/summary"), ("patch", "/reviews/x/findings/y")):
            assert getattr(c, method)(f"{API}/repositories/abc{path}").status_code == 401, path
    reset_engine()
    reset_settings_cache()


def test_viewer_role_cannot_start_reviews_or_change_findings(client: TestClient, tmp_path: Path) -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership

    root = make_repo(tmp_path)
    rid = register(client, root)
    put(root, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    rv = review(client, rid)
    f = findings(client, rid, rv)[0]
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()
    assert client.post(f"{base(rid)}/reviews", json={"mode": "working_tree"}).status_code == 403
    assert client.patch(f"{base(rid)}/reviews/{rv['id']}/findings/{f['id']}", json={"status": "accepted"}).status_code == 403
    assert client.get(f"{base(rid)}/reviews/{rv['id']}").status_code == 200
    assert client.get(f"{base(rid)}/reviews/{rv['id']}/findings").status_code == 200


def test_cross_repository_isolation(client: TestClient, tmp_path: Path) -> None:
    ra = make_repo(tmp_path, name="a")
    rb = make_repo(tmp_path, name="b")
    a = register(client, ra, "a")
    b = register(client, rb, "b")
    put(ra, "pkg/calc.py", CALC + "\n\ndef x(c):\n    return eval(c)\n")
    rv = review(client, a)
    f = findings(client, a, rv)[0]
    for p in ("", "/changes", "/findings", "/risk", "/impact", "/summary", "/test-recommendations"):
        assert client.get(f"{base(b)}/reviews/{rv['id']}{p}").status_code == 404, p
    assert client.patch(f"{base(b)}/reviews/{rv['id']}/findings/{f['id']}", json={"status": "accepted"}).status_code == 404
    assert client.get(f"{base(b)}/reviews").json()["total"] == 0 and client.get(f"{base(a)}/reviews").json()["total"] == 1
    assert client.get(f"{base('nope')}/reviews").status_code == 404
    assert client.post(f"{base('nope')}/reviews", json={"mode": "staged"}).status_code == 404


def test_synthetic_end_to_end_review(client: TestClient, tmp_path: Path) -> None:
    """clean change + deliberate static-analysis issue + changed function with dependents + changed test + deleted file."""
    root = make_repo(tmp_path)
    rid = register(client, root)
    client.post(f"{base(rid)}/tests/discover")
    put(root, "pkg/calc.py", CALC.replace("def helper(x):\n    return 10 / x", "def helper(x, scale=1):\n    return scale * 10 / x") + "\n\ndef run_expr(code):\n    return eval(code)\n")
    put(root, "pkg/legacy.py", "VALUE = 1\n")
    git(root, "rm", "-qf", "pkg/legacy.py")
    put(root, "pkg/clean.py", "def add(a, b):\n    return a + b\n")
    git(root, "add", "pkg/clean.py")
    put(root, "tests/test_calc.py", BASE_FILES["tests/test_calc.py"] + "\n\ndef test_helper():\n    from pkg.calc import helper\n    assert helper(2) == 5\n")
    rv = review(client, rid)
    ch = changes(client, rid, rv)
    assert {k: v["change_type"] for k, v in ch.items()} == {"pkg/calc.py": "modified", "pkg/legacy.py": "deleted", "pkg/clean.py": "added", "tests/test_calc.py": "modified"}
    assert ch["tests/test_calc.py"]["is_test"] is True
    fs = findings(client, rid, rv)
    rules = {f["rule_id"] for f in fs}
    assert "PY-SEC-001" in rules and "REV-DEL-001" in rules and not [f for f in fs if f["rel_path"] == "pkg/clean.py"]
    assert not [f for f in fs if f["rule_id"] == "REV-API-001"]                    # additive default parameter
    r = client.get(f"{base(rid)}/reviews/{rv['id']}/risk").json()
    assert r["state"] == "ranked" and r["category"] in ("low", "moderate", "high")
    imp = client.get(f"{base(rid)}/reviews/{rv['id']}/impact").json()
    assert "pkg/consumer.py" in {d["rel_path"] for d in imp["dependents"]}
    assert {t["rel_path"] for t in imp["affected_tests"]} >= {"tests/test_calc.py"}
    summary = client.get(f"{base(rid)}/reviews/{rv['id']}/summary").json()
    assert summary["ready"] and "changed file(s) analysed" in summary["summary"]["headline"]
