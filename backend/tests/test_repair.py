"""Phase 08: repair investigations, patch validation/approval/application and verification."""
from __future__ import annotations

import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from tests.conftest import register_and_index
from tests.test_repository_intelligence import write

API = "/api/v1"

CART = textwrap.dedent('''
    def add_item(item, items=[]):
        """Add an item to a cart."""
        items.append(item)
        return items


    def total(items):
        return len(items)
''').lstrip("\n")
OTHER = "def is_missing(value):\n    return value == None\n"
TEST_CART = textwrap.dedent('''
    from pkg.cart import add_item, total


    def test_carts_are_independent():
        add_item("a")
        second = add_item("b")
        assert second == ["b"]


    def test_total():
        assert total(["a", "b"]) == 2
''').lstrip("\n")
FILES = {"pkg/__init__.py": "", "pkg/cart.py": CART, "pkg/other.py": OTHER, "tests/test_cart.py": TEST_CART}
FIXED_CART = CART.replace("def add_item(item, items=[]):", "def add_item(item, items=None):").replace(
    '    items.append(item)', '    if items is None:\n        items = []\n    items.append(item)')


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


@pytest.fixture
def art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOVA_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    reset_settings_cache()
    return tmp_path / "artifacts"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
                          check=True, capture_output=True, text=True).stdout


def setup(client: TestClient, tmp_path: Path, files: dict[str, str] | None = None, use_git: bool = False, name: str = "d",
          dirname: str = "r") -> tuple[str, Path]:
    root = tmp_path / dirname
    write(root, FILES if files is None else files)
    if use_git:
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
    return register_and_index(client, root, name)["repo"]["id"], root


def run_suite(client: TestClient, rid: str) -> dict:
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": False})
    assert r.status_code == 202, r.text
    return client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()


def failure_from_run(client: TestClient, rid: str, run: dict, test_name: str = "test_carts_are_independent") -> dict:
    ing = client.post(f"{base(rid)}/failures/ingest", json={"source": "execution_run", "execution_run_id": run["id"]})
    assert ing.status_code == 201, ing.text
    return next(i for i in ing.json()["items"] if test_name in (i["test_id"] or "") or test_name in i["summary"])


def new_repair(client: TestClient, rid: str, **kw: object) -> dict:
    body = {"title": "Carts share state", "description": "Two carts created in a row contain each other's items.", **kw}
    r = client.post(f"{base(rid)}/repairs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def rurl(rid: str, repair: dict | str, tail: str = "") -> str:
    rid_ = repair if isinstance(repair, str) else repair["id"]
    return f"{base(rid)}/repairs/{rid_}{tail}"


def tree_state(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def flow_to_approved(client: TestClient, rid: str, repair: dict, **create: object) -> dict:
    """proposal -> generated patch -> validate -> submit -> approve. Returns the approved patch."""
    assert client.post(rurl(rid, repair, "/evidence")).status_code == 200
    prop = client.post(rurl(rid, repair, "/proposal"))
    assert prop.status_code == 200, prop.text
    p = client.post(rurl(rid, repair, "/patches"), json={"source": "generated", **create})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    assert client.post(rurl(rid, repair, f"/patches/{pid}/validate")).json()["status"] == "validated"
    assert client.post(rurl(rid, repair, f"/patches/{pid}/submit")).json()["status"] == "awaiting_approval"
    ap = client.post(rurl(rid, repair, f"/patches/{pid}/approve"))
    assert ap.status_code == 200 and ap.json()["status"] == "approved", ap.text
    return ap.json()


def test_worked_example_end_to_end(art: Path, client: TestClient, tmp_path: Path) -> None:
    """A real failing test -> evidence -> proposal -> patch -> validate -> approve -> apply -> verify."""
    rid, root = setup(client, tmp_path)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    run = run_suite(client, rid)
    assert run["status"] == "failed" and run["tests_failed"] == 1
    failure = failure_from_run(client, rid, run)
    repair = new_repair(client, rid, failure_id=failure["id"], severity="high")
    assert repair["status"] == "open" and repair["source_kind"] == "failure"
    ev = client.post(rurl(rid, repair, "/evidence")).json()
    top = ev["evidence"]["hypotheses"][0]
    assert (top["rel_path"], top["symbol"]) == ("pkg/cart.py", "pkg.cart.add_item") and ev["evidence"]["state"] == "ranked"
    assert ev["root_cause"]["kind"] == "hypothesis" and "confirmed" in ev["root_cause"]["note"]
    prop = client.post(rurl(rid, repair, "/proposal")).json()["proposal"]
    assert prop["state"] == "ready" and prop["patch_available"] and prop["ai_assisted"] is False
    assert prop["candidate_fixes"][0]["rule_id"] == "PY-COR-002"
    assert any("tests/test_cart.py" in t["target"] for t in prop["tests_to_run"])
    before = tree_state(root)
    created = client.post(rurl(rid, repair, "/patches"), json={"source": "generated"})
    assert created.status_code == 201, created.text
    patch_ = created.json()
    assert patch_["status"] == "draft" and "items=None" in patch_["diff_text"] and patch_["preview"]["files"][0]["path"] == "pkg/cart.py"
    pid = patch_["id"]
    val = client.post(rurl(rid, repair, f"/patches/{pid}/validate")).json()
    assert val["status"] == "validated" and val["validation"]["ok"] and val["validation"]["working_tree_modified"] is False
    assert tree_state(root) == before                                  # nothing written by proposal, creation or validation
    assert client.post(rurl(rid, repair, f"/patches/{pid}/apply"), json={"confirm": True}).status_code == 409   # not approved
    assert client.post(rurl(rid, repair, f"/patches/{pid}/submit")).json()["status"] == "awaiting_approval"
    assert client.post(rurl(rid, repair, f"/patches/{pid}/approve")).json()["status"] == "approved"
    assert tree_state(root) == before                                  # approval alone changes no file
    applied = client.post(rurl(rid, repair, f"/patches/{pid}/apply"), json={"confirm": True})
    assert applied.status_code == 200 and applied.json()["status"] == "applied", applied.text
    assert (root / "pkg/cart.py").read_text() == FIXED_CART
    assert applied.json()["verification_summary"] == {} and client.get(rurl(rid, repair)).json()["verification_outcome"] is None
    assert client.get(rurl(rid, repair)).json()["status"] == "applied"     # applied is not verified
    vurl = rurl(rid, repair, f"/patches/{pid}/verifications")
    pv = client.post(rurl(rid, repair, f"/patches/{pid}/verify/preview"), json={}).json()
    assert pv["executes"] is False and any(t["kind"] == "failing_test" for t in pv["selected"])
    assert client.post(vurl, json={}).status_code == 422                       # confirmation required
    started = client.post(vurl, json={"confirm": True})
    assert started.status_code == 202, started.text
    v = client.get(f"{vurl}/{started.json()['id']}").json()
    assert v["outcome"] == "PASSED", v
    assert {c["id"]: c["status"] for c in v["criteria"]}["original_failure_resolved"] == "pass"
    assert v["run"]["tests_passed"] == 2 and v["run"]["tests_failed"] == 0
    final = client.get(rurl(rid, repair)).json()
    assert final["status"] == "verified" and final["verification_outcome"] == "PASSED"
    assert client.get(rurl(rid, repair, f"/patches/{pid}")).json()["status"] == "verified"
    audit = client.get(rurl(rid, repair, "/audit")).json()["items"]
    actions = [a["action"] for a in audit]
    for a in ("repair.created", "repair.evidence_assembled", "repair.proposal_generated", "repair.patch_created", "repair.patch_validated",
              "repair.patch_submitted", "repair.patch_approved", "repair.patch_applied", "repair.verification_started", "repair.verification_evaluated"):
        assert a in actions, a
    assert actions.index("repair.patch_approved") < actions.index("repair.patch_applied") < actions.index("repair.verification_evaluated")


# ---------------------------------------------------------------------------------------------------- helpers for the rest


def code_of(r: object) -> str:
    return r.json()["error"]["code"]  # type: ignore[attr-defined]


def mrepair(client: TestClient, rid: str) -> dict:
    """An investigation whose evidence names pkg/cart.py, so manual patches to it are in scope."""
    repair = new_repair(client, rid, hint_files=["pkg/cart.py"])
    assert client.post(rurl(rid, repair, "/evidence")).status_code == 200
    return repair


def mdiff(path: str, old: str, new: str) -> str:
    from app.services.repair import patch as pl
    return pl.make_diff(path, old, new)


def manual(client: TestClient, rid: str, repair: dict, diff: str, **kw: object):
    return client.post(rurl(rid, repair, "/patches"), json={"source": "manual", "diff": diff, **kw})


CART_EDIT = mdiff("pkg/cart.py", CART, CART.replace("len(items)", "len(list(items))"))


def as_viewer() -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()


# ---------------------------------------------------------------------------------------------------- evidence


def test_investigation_crud_filters_and_pagination(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    a = new_repair(client, rid, title="Alpha 100% bug", severity="high")
    new_repair(client, rid, title="Beta", severity="low")
    new_repair(client, rid, title="Gamma", severity="low")
    got = client.get(rurl(rid, a)).json()
    assert got["title"] == "Alpha 100% bug" and got["status"] == "open" and got["source_kind"] == "manual"
    lst = client.get(f"{base(rid)}/repairs", params={"page_size": 2}).json()
    assert lst["total"] == 3 and len(lst["items"]) == 2
    assert client.get(f"{base(rid)}/repairs", params={"severity": "low"}).json()["total"] == 2
    assert client.get(f"{base(rid)}/repairs", params={"q": "100%"}).json()["total"] == 1      # % is escaped, not a wildcard
    assert client.get(f"{base(rid)}/repairs", params={"q": "%"}).json()["total"] == 1
    assert client.get(f"{base(rid)}/repairs", params={"status": "verified"}).json()["total"] == 0
    assert client.get(f"{base(rid)}/repairs", params={"status": "bogus"}).status_code == 422
    assert client.patch(rurl(rid, a), json={"status": "closed"}).json()["status"] == "closed"
    assert client.patch(rurl(rid, a), json={"status": "verified"}).status_code == 422
    assert client.get(rurl(rid, "nope")).status_code == 404


def test_evidence_ranks_relevant_files_and_labels_basis(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    repair = mrepair(client, rid)
    ev = client.post(rurl(rid, repair, "/evidence")).json()
    e = ev["evidence"]
    assert e["state"] == "ranked" and e["hypotheses"][0]["rel_path"] == "pkg/cart.py"
    assert all(h["rel_path"] != "pkg/other.py" or h["score"] < e["hypotheses"][0]["score"] for h in e["hypotheses"])
    assert {f["basis"] for f in e["facts"]} <= {"observed", "stated_by_requester"}
    assert any(f["basis"] == "stated_by_requester" for f in e["facts"])
    assert e["limitations"] and "unknowns" in e and "next_steps" in e
    assert ev["root_cause"]["confidence"] in ("low", "medium", "high") and ev["root_cause"]["confidence_rationale"]


def test_insufficient_evidence_is_explicit_and_blocks_generation(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    repair = new_repair(client, rid, title="Something is wrong", description="It breaks sometimes.")
    ev = client.post(rurl(rid, repair, "/evidence")).json()
    assert ev["evidence"]["state"] in ("insufficient_evidence", "ranked")
    prop = client.post(rurl(rid, repair, "/proposal")).json()["proposal"]
    assert prop["state"] in ("insufficient_evidence", "diagnostic_only") and prop["patch_available"] is False
    assert prop["limitations"] and prop["ai_assisted"] is False
    r = client.post(rurl(rid, repair, "/patches"), json={"source": "generated"})
    assert r.status_code == 409 and code_of(r) in ("insufficient_evidence", "ambiguous_fix")


def test_finding_sourced_investigation_scopes_the_fix(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    fl = client.get(f"{base(rid)}/findings", params={"page_size": 100}).json()["items"]
    f = next(x for x in fl if x["rel_path"] == "pkg/other.py")
    repair = new_repair(client, rid, finding_id=f["id"], title="None comparison")
    assert repair["source_kind"] == "finding"
    client.post(rurl(rid, repair, "/evidence"))
    prop = client.post(rurl(rid, repair, "/proposal")).json()["proposal"]
    assert prop["state"] == "ready" and prop["candidate_fixes"][0]["rel_path"] == "pkg/other.py"
    p = client.post(rurl(rid, repair, "/patches"), json={"source": "generated"})
    assert p.status_code == 201 and "is None" in p.json()["diff_text"]


# ---------------------------------------------------------------------------------------------------- patch engine units


def test_patch_engine_units(tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.services.repair import patch as pl
    s = Settings()
    root = tmp_path / "r"
    write(root, {"a.py": "x = 1\ny = 2\nz = 3\n", "crlf.txt": "one\r\ntwo\r\n", "noeol.txt": "last"})
    d = mdiff("a.py", "x = 1\ny = 2\nz = 3\n", "x = 1\ny = 20\nz = 3\n")
    res = pl.build(root, s, d)
    assert res[0].result == "x = 1\ny = 20\nz = 3\n"
    assert pl.sha256_text(d) == hashlib.sha256(d.encode()).hexdigest()
    bad = d.replace(" x = 1", " x = 9")
    with pytest.raises(pl.PatchError) as ei:
        pl.build(root, s, bad)
    assert ei.value.code in ("conflict", "invalid_patch")
    create = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    assert pl.build(root, s, create)[0].result == "hello\nworld\n"
    create_existing = create.replace("new.txt", "a.py")
    with pytest.raises(pl.PatchError):
        pl.build(root, s, create_existing)
    with pytest.raises(pl.PatchError) as e2:
        pl.build(root, s, "GIT binary patch\nliteral 0\n")
    assert e2.value.code == "binary_patch"
    two = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 5\n y = 2\n@@ -2,2 +2,2 @@\n y = 2\n-z = 3\n+z = 7\n"
    with pytest.raises(pl.PatchError):
        pl.build(root, s, two)            # overlapping hunks are refused, never guessed


@pytest.mark.parametrize("src,rule", [
    ("def f(a, b=[]):\n    b.append(a)\n    return b\n", "PY-COR-002"),
    ("def f(v):\n    return v == None\n", "PY-COR-001"),
])
def test_autofix_units(tmp_path: Path, src: str, rule: str) -> None:
    from app.core.config import Settings
    from app.services.repair import autofix
    fixes, declined = autofix.propose(tmp_path, Settings(), "m.py", src, rule, None)
    assert len(fixes) == 1 and fixes[0].rule_id == rule
    import ast
    ast.parse(fixes[0].new_source if hasattr(fixes[0], "new_source") else src)


def test_autofix_declines_what_it_cannot_prove(tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.services.repair import autofix
    src = "def f(a, b=[]):\n    return a\n\nx = eval(input())\n"
    fixes, declined = autofix.propose(tmp_path, Settings(), "m.py", src, None, None)
    assert all(f.rule_id != "PY-SEC-001" for f in fixes)
    assert autofix.propose(tmp_path, Settings(), "m.py", "def (:\n", None, None)[0] == []


# ---------------------------------------------------------------------------------------------------- patch validation


def test_manual_patch_validation_and_tree_untouched(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, use_git=True)
    repair = mrepair(client, rid)
    before, gitbefore = tree_state(root), git(root, "status", "--porcelain")
    r = manual(client, rid, repair, CART_EDIT)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "draft" and len(r.json()["patch_sha256"]) == 64
    v = client.post(rurl(rid, repair, f"/patches/{pid}/validate")).json()
    assert v["status"] == "validated", v["validation"]
    assert v["validation"]["working_tree_modified"] is False
    assert tree_state(root) == before and git(root, "status", "--porcelain") == gitbefore
    assert not list((art / "repair-tmp").glob("*")) if (art / "repair-tmp").exists() else True
    assert client.get(rurl(rid, repair, f"/patches/{pid}")).json()["preview"]["files"][0]["path"] == "pkg/cart.py"


@pytest.mark.parametrize("path,code", [
    ("../evil.py", "unsafe_path"), ("/etc/passwd", "unsafe_path"), (".git/config", "unsafe_path"),
    ("pkg/cart.exe", "disallowed_file_type"),
])
def test_unsafe_paths_are_rejected(art: Path, client: TestClient, tmp_path: Path, path: str, code: str) -> None:
    rid, _ = setup(client, tmp_path)
    repair = mrepair(client, rid)
    r = manual(client, rid, repair, f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n")
    assert r.status_code in (400, 422) and code_of(r) in (code, "invalid_patch", "unsafe_path"), r.text
    assert "/home" not in r.text and str(tmp_path) not in r.text


def test_garbage_binary_delete_rename_and_limits(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    repair = mrepair(client, rid)
    assert manual(client, rid, repair, "this is not a diff").status_code == 422
    assert manual(client, rid, repair, "GIT binary patch\nliteral 3\n").status_code == 422
    assert manual(client, rid, repair, "diff --git a/pkg/cart.py b/pkg/cart.py\ndeleted file mode 100644\n--- a/pkg/cart.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n").status_code == 422
    assert manual(client, rid, repair, "diff --git a/pkg/cart.py b/pkg/new.py\nrename from pkg/cart.py\nrename to pkg/new.py\n").status_code == 422
    many = "".join(f"--- /dev/null\n+++ b/n{i}.txt\n@@ -0,0 +1 @@\n+x\n" for i in range(11))
    r = manual(client, rid, repair, many)
    assert r.status_code == 422 and code_of(r) == "too_many_files"
    assert manual(client, rid, repair, "--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1 @@\n+" + "x" * 300_000 + "\n").status_code == 422
    assert client.get(rurl(rid, repair, "/patches")).json() == []                  # nothing was stored


def test_symlink_escape_is_rejected(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("s = 1\n")
    (root / "pkg" / "link.py").symlink_to(outside / "secret.py")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    repair = mrepair(client, rid)
    assert manual(client, rid, repair, mdiff("pkg/link.py", "s = 1\n", "s = 2\n")).status_code in (400, 422)
    assert manual(client, rid, repair, mdiff("linkdir/secret.py", "s = 1\n", "s = 2\n")).status_code in (400, 422)
    assert (outside / "secret.py").read_text() == "s = 1\n"


def test_stale_conflict_tamper_and_unexpected_file(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    (root / "pkg/cart.py").write_text(CART + "\n# edited by user\n")
    r = client.post(rurl(rid, repair, f"/patches/{pid}/validate"))
    assert r.status_code in (200, 409)
    got = client.get(rurl(rid, repair, f"/patches/{pid}")).json()
    assert got["status"] == "invalid" and got["validation"]["ok"] is False
    assert (root / "pkg/cart.py").read_text().endswith("# edited by user\n")          # never touched
    # tampering with the stored diff is detected
    (root / "pkg/cart.py").write_text(CART)
    pid2 = manual(client, rid, repair, CART_EDIT).json()["id"]
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import RepairPatch
    with get_session_factory()() as s:
        s.execute(update(RepairPatch).where(RepairPatch.id == pid2).values(diff_text=CART_EDIT.replace("list(items)", "items")))
        s.commit()
    t = client.post(rurl(rid, repair, f"/patches/{pid2}/validate"))
    assert t.status_code == 409 and code_of(t) == "patch_tampered"
    # a file outside the investigation's scope is flagged unless declared
    other = mdiff("pkg/other.py", OTHER, OTHER.replace("== None", "is None"))
    ur = manual(client, rid, repair, other)
    if ur.status_code == 201:
        v = client.post(rurl(rid, repair, f"/patches/{ur.json()['id']}/validate")).json()
        assert v["status"] == "invalid" and "unexpected_file" in str(v["validation"])
    else:
        assert code_of(ur) == "unexpected_file"
    allowed = manual(client, rid, repair, other, extra_files=["pkg/other.py"])
    assert allowed.status_code == 201
    assert client.post(rurl(rid, repair, f"/patches/{allowed.json()['id']}/validate")).json()["status"] == "validated"


def test_temp_cleanup_after_validation_failure(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.repair import patch as pl
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    real = pl.apply_file

    def boom(*a: object, **k: object):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(pl, "apply_file", boom)
    before = tree_state(root)
    try:
        client.post(rurl(rid, repair, f"/patches/{pid}/validate"))
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setattr(pl, "apply_file", real)
    assert tree_state(root) == before
    tmp = art / "repair-tmp"
    assert not tmp.exists() or not any(tmp.iterdir())


# ---------------------------------------------------------------------------------------------------- approval and application


def test_permissions_and_state_machine(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    pu = lambda t: rurl(rid, repair, f"/patches/{pid}/{t}")  # noqa: E731
    assert client.post(pu("approve")).status_code == 409                       # draft cannot be approved
    assert client.post(pu("apply"), json={"confirm": True}).status_code == 409
    assert client.post(pu("validate")).json()["status"] == "validated"
    assert client.post(pu("submit")).status_code == 200
    as_viewer()
    for t, body in (("validate", None), ("submit", None), ("approve", None), ("reject", {"reason": "no good"}), ("apply", {"confirm": True})):
        assert client.post(pu(t), json=body).status_code == 403, t
    assert client.get(pu("")[:-1]).status_code == 200
    assert (root / "pkg/cart.py").read_text() == CART


def test_apply_requires_approval_and_confirmation_and_is_exact(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    (root / "pkg/cart.py").chmod(0o750)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    u = lambda t: rurl(rid, repair, f"/patches/{pid}/{t}")  # noqa: E731
    client.post(u("validate")); client.post(u("submit"))  # noqa: E702
    assert code_of(client.post(u("apply"), json={"confirm": True})) == "approval_required"
    assert client.post(u("approve")).status_code == 200
    assert code_of(client.post(u("approve"))) == "already_approved"
    assert client.post(u("apply"), json={"confirm": False}).status_code == 422
    assert (root / "pkg/cart.py").read_text() == CART
    ok = client.post(u("apply"), json={"confirm": True})
    assert ok.status_code == 200 and ok.json()["apply_result"]["originals_retained"] is True
    assert (root / "pkg/cart.py").read_text() == CART.replace("len(items)", "len(list(items))")
    if os.name != "nt":  # POSIX permission bits do not exist on Windows
        assert (root / "pkg/cart.py").stat().st_mode & 0o777 == 0o750
    assert not [p for p in (root / "pkg").iterdir() if p.name.startswith(".") or p.suffix == ".tmp"]
    dup = client.post(u("apply"), json={"confirm": True})
    assert dup.status_code == 409 and code_of(dup) == "already_applied"
    rb = client.get(u("rollback")).json()
    assert "len(items)" in rb["reverse_diff"] and rb["files"]


def test_concurrent_apply_has_exactly_one_winner(art: Path, client: TestClient, tmp_path: Path) -> None:
    import threading
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    u = lambda t: rurl(rid, repair, f"/patches/{pid}/{t}")  # noqa: E731
    client.post(u("validate")); client.post(u("submit")); client.post(u("approve"))  # noqa: E702
    codes: list[int] = []

    def go() -> None:
        codes.append(client.post(u("apply"), json={"confirm": True}).status_code)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]  # noqa: E702
    assert sorted(codes).count(200) == 1 and all(c in (200, 409) for c in codes), codes
    assert (root / "pkg/cart.py").read_text().count("list(items)") == 1


def test_source_modified_after_approval_is_refused(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    u = lambda t: rurl(rid, repair, f"/patches/{pid}/{t}")  # noqa: E731
    client.post(u("validate")); client.post(u("submit")); client.post(u("approve"))  # noqa: E702
    (root / "pkg/cart.py").write_text(CART + "# user edit\n")
    r = client.post(u("apply"), json={"confirm": True})
    assert r.status_code == 409 and code_of(r) in ("stale_source", "patch_conflict")
    assert (root / "pkg/cart.py").read_text() == CART + "# user edit\n"
    assert client.get(u("")[:-1]).json()["status"] == "invalid"


def test_reject_rules_and_audit(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    u = lambda t: rurl(rid, repair, f"/patches/{pid}/{t}")  # noqa: E731
    client.post(u("validate")); client.post(u("submit"))  # noqa: E702
    assert client.post(u("reject"), json={"reason": ""}).status_code == 422
    assert client.post(u("reject"), json={"reason": "x"}).status_code in (409, 422)
    assert client.post(u("reject"), json={"reason": "Changes unrelated behavior"}).json()["status"] == "rejected"
    assert client.post(u("approve")).status_code == 409
    assert client.post(u("apply"), json={"confirm": True}).status_code == 409
    audit = client.get(rurl(rid, repair, "/audit"), params={"page_size": 2}).json()
    assert audit["total"] >= 4 and len(audit["items"]) == 2
    assert "len(list" not in str(audit) and str(tmp_path) not in str(audit)


# ---------------------------------------------------------------------------------------------------- verification


def applied_repair(client: TestClient, tmp_path: Path, diff: str | None = None, files: dict[str, str] | None = None) -> tuple[str, Path, dict, str]:
    """A failing suite -> failure-linked investigation -> approved and applied patch (not verified)."""
    rid, root = setup(client, tmp_path, files)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    run = run_suite(client, rid)
    assert run["status"] == "failed"
    failure = failure_from_run(client, rid, run)
    repair = new_repair(client, rid, failure_id=failure["id"])
    assert client.post(rurl(rid, repair, "/evidence")).status_code == 200
    if diff is None:
        assert client.post(rurl(rid, repair, "/proposal")).status_code == 200
        pr = client.post(rurl(rid, repair, "/patches"), json={"source": "generated"})
    else:
        pr = manual(client, rid, repair, diff)
    assert pr.status_code == 201, pr.text
    pid = pr.json()["id"]
    for step in ("validate", "submit", "approve"):
        r = client.post(rurl(rid, repair, f"/patches/{pid}/{step}"))
        assert r.status_code == 200, (step, r.text, client.get(rurl(rid, repair, f"/patches/{pid}")).json()["validation"])
    assert client.post(rurl(rid, repair, f"/patches/{pid}/apply"), json={"confirm": True}).status_code == 200
    return rid, root, repair, pid


def verify(client: TestClient, rid: str, repair: dict, pid: str, **body: object) -> dict:
    r = client.post(rurl(rid, repair, f"/patches/{pid}/verifications"), json={"confirm": True, **body})
    assert r.status_code == 202, r.text
    return client.get(rurl(rid, repair, f"/patches/{pid}/verifications/{r.json()['id']}")).json()


def test_ineffective_patch_fails_verification_and_keeps_evidence(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root, repair, pid = applied_repair(client, tmp_path, CART_EDIT)
    v = verify(client, rid, repair, pid)
    assert v["outcome"] == "FAILED" and v["run"]["tests_failed"] >= 1
    assert {c["id"]: c["status"] for c in v["criteria"]}["original_failure_resolved"] == "fail"
    assert client.get(rurl(rid, repair, f"/patches/{pid}")).json()["status"] == "verification_failed"
    inv = client.get(rurl(rid, repair)).json()
    assert inv["status"] == "verification_failed" and inv["verification_outcome"] == "FAILED"
    assert v["selected_tests"] and all(t.get("reason") for t in v["selected_tests"])
    assert v["meaning"] and (root / "pkg/cart.py").read_text().count("list(items)") == 1      # applied change is left in place
    hist = client.get(rurl(rid, repair, f"/patches/{pid}/verifications")).json()
    assert len(hist) == 1 and hist[0]["id"] == v["id"]


def test_baseline_separates_pre_existing_failures(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    files["tests/test_legacy.py"] = "def test_legacy_broken():\n    assert 1 == 2\n"
    rid, root, repair, pid = applied_repair(client, tmp_path, files=files)
    v = verify(client, rid, repair, pid, scope="all")
    cmp_ = v["comparison"]
    assert cmp_["baseline_available"] is True and cmp_["new_failures"] == []
    assert any("test_legacy_broken" in n for n in cmp_["pre_existing_failures"])
    assert any("test_carts_are_independent" in n for n in cmp_["no_longer_failing"])
    assert v["outcome"] != "FAILED" and {c["id"]: c["status"] for c in v["criteria"]}["original_failure_resolved"] == "pass"
    assert v["outcome"] != "PASSED" or all(c["status"] != "fail" for c in v["criteria"])


def test_new_failure_after_patch_is_reported_as_new(art: Path, client: TestClient, tmp_path: Path) -> None:
    breaking = mdiff("pkg/cart.py", CART, FIXED_CART.replace("return len(items)", "return len(items) + 1"))
    rid, root, repair, pid = applied_repair(client, tmp_path, breaking)
    v = verify(client, rid, repair, pid, scope="all")
    assert v["outcome"] == "FAILED"
    assert any("test_total" in n for n in v["comparison"]["new_failures"])


def test_timeout_is_incomplete_not_passed(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.testing import runner
    rid, root, repair, pid = applied_repair(client, tmp_path)
    real = runner.execute

    def slow(spec, settings):
        res = real(spec, settings)
        res.status, res.reason, res.exit_code, res.failed, res.passed, res.failures = "timed_out", "Timed out.", None, None, None, []
        return res
    monkeypatch.setattr(runner, "execute", slow)
    v = verify(client, rid, repair, pid)
    assert v["outcome"] == "INCOMPLETE" and v["outcome_reason"]
    assert client.get(rurl(rid, repair, f"/patches/{pid}")).json()["status"] != "verified"
    assert client.get(rurl(rid, repair)).json()["status"] != "verified"


def test_runner_failure_is_not_passed(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rid, root, repair, pid = applied_repair(client, tmp_path)
    monkeypatch.setenv("NOVA_TEST_PYTHON", str(tmp_path / "no-such-python"))
    reset_settings_cache()
    v = verify(client, rid, repair, pid)
    assert v["outcome"] in ("INCOMPLETE", "BLOCKED", "FAILED") and v["outcome"] != "PASSED"


def test_output_is_bounded_and_scrubbed(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.testing import runner
    rid, root, repair, pid = applied_repair(client, tmp_path)
    real = runner.execute

    def noisy(spec, settings):
        res = real(spec, settings)
        res.stdout = "x" * 500_000 + " password=hunter2secret"
        res.stderr = "y" * 500_000
        return res
    monkeypatch.setattr(runner, "execute", noisy)
    v = verify(client, rid, repair, pid)
    out = v["run"]
    for k in ("stdout_tail", "stderr_tail"):
        assert 0 < len(out.get(k) or "") <= 4100
    assert "hunter2secret" not in str(v)


def test_verification_guards(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    vurl = rurl(rid, repair, f"/patches/{pid}/verifications")
    r = client.post(vurl, json={"confirm": True})
    assert r.status_code == 409 and code_of(r) == "patch_not_applied"
    for step in ("validate", "submit", "approve"):
        client.post(rurl(rid, repair, f"/patches/{pid}/{step}"))
    client.post(rurl(rid, repair, f"/patches/{pid}/apply"), json={"confirm": True})
    # tests were never discovered -> a recorded BLOCKED outcome, not a crash and not PASSED
    blocked = client.post(vurl, json={"confirm": True})
    assert blocked.status_code in (202, 409)
    if blocked.status_code == 202:
        got = client.get(f"{vurl}/{blocked.json()['id']}").json()
        assert got["outcome"] == "BLOCKED" and got["outcome_reason"]
    client.post(f"{base(rid)}/tests/discover")
    assert client.post(vurl, json={"confirm": True, "test_ids": ["tests/test_nope.py::test_x"]}).status_code == 422
    assert client.post(vurl, json={"confirm": True, "scope": "bogus"}).status_code == 422
    assert client.post(vurl, json={}).status_code == 422
    assert client.get(f"{vurl}/nope").status_code == 404


def test_api_auth_isolation_and_no_leaks(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, dirname="r1")
    rid2, _ = setup(client, tmp_path, dirname="r2", name="e")
    repair = mrepair(client, rid)
    pid = manual(client, rid, repair, CART_EDIT).json()["id"]
    assert client.get(rurl(rid2, repair)).status_code == 404
    assert client.get(rurl(rid2, repair, f"/patches/{pid}")).status_code == 404
    assert client.post(rurl(rid2, repair, f"/patches/{pid}/approve")).status_code == 404
    assert client.get(rurl(rid, repair, "/patches/nope")).status_code == 404
    assert client.post(f"{base(rid)}/repairs", json={"title": ""}).status_code == 422
    assert client.post(f"{base(rid)}/repairs", json={"title": "x", "severity": "huge"}).status_code == 422
    assert client.post(f"{base(rid)}/repairs", json={"title": "x", "hint_files": ["../../etc/passwd"]}).status_code in (400, 422)
    assert client.post(f"{base(rid)}/repairs", json={"title": "x", "failure_id": "0" * 32}).status_code in (404, 422)
    text = client.get(rurl(rid, repair)).text + client.get(rurl(rid, repair, f"/patches/{pid}")).text + client.get(rurl(rid, repair, "/audit")).text
    assert str(tmp_path) not in text and str(root) not in text


def test_unauthorized_access_is_refused(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("post", "/repairs"), ("get", "/repairs"), ("get", "/repairs/x"), ("post", "/repairs/x/evidence"), ("post", "/repairs/x/proposal"),
                             ("get", "/repairs/x/audit"), ("post", "/repairs/x/patches"), ("get", "/repairs/x/patches/y"),
                             ("post", "/repairs/x/patches/y/approve"), ("post", "/repairs/x/patches/y/apply"),
                             ("post", "/repairs/x/patches/y/verifications"), ("get", "/repairs/x/patches/y/verifications")):
            assert getattr(c, method)(f"{API}/repositories/abc{path}").status_code == 401, path
    reset_engine()


def test_evidence_and_proposal_leave_git_state_untouched(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, use_git=True)
    repair = mrepair(client, rid)
    before, st = tree_state(root), git(root, "status", "--porcelain")
    client.post(rurl(rid, repair, "/evidence"))
    client.post(rurl(rid, repair, "/proposal"))
    client.post(rurl(rid, repair, "/patches"), json={"source": "generated"})
    assert tree_state(root) == before and git(root, "status", "--porcelain") == st == ""
    assert git(root, "log", "--oneline").count("\n") == 1
