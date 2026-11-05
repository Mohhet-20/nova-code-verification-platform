"""Refactoring planning, multi-file rename, duplicates, extract function, lifecycle, verification and API."""
from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from tests.conftest import register_and_index
from tests.test_repository_intelligence import write

API = "/api/v1"

INIT = textwrap.dedent('''
    from pkg.cart import add_item

    __all__ = ["add_item"]
''').lstrip("\n")
CART = textwrap.dedent('''
    """Cart helpers."""


    def add_item(item, items=None):
        # add_item keeps the list it is given
        items = [] if items is None else items
        items.append(item)
        return items


    class Cart:
        def __init__(self):
            self.items = []

        def add_item(self, item):
            return add_item(item, self.items)


    def other():
        add_item = 3
        return add_item


    LABEL = "add_item"
''').lstrip("\n")
USE = textwrap.dedent('''
    import pkg.cart
    from pkg import add_item as put
    from pkg.cart import Cart, add_item
    from pkg import cart


    def run():
        c = Cart()
        c.add_item(1)
        put(2)
        pkg.cart.add_item(3)
        cart.add_item(4)
        return add_item(5)
''').lstrip("\n")
TEST_CART = textwrap.dedent('''
    from pkg.cart import add_item


    def test_add():
        assert add_item("a") == ["a"]


    def test_independent():
        assert add_item("b") == ["b"]
''').lstrip("\n")
FILES = {"pkg/__init__.py": INIT, "pkg/cart.py": CART, "pkg/use.py": USE, "tests/test_cart.py": TEST_CART}


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


def rf(rid: str, tid: str | dict | None = None, tail: str = "") -> str:
    t = tid["id"] if isinstance(tid, dict) else tid
    return f"{base(rid)}/refactors" + (f"/{t}" if t else "") + tail


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


def tree_state(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink() and ".git" not in p.parts:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def code_of(r: object) -> str:
    return r.json()["error"]["code"]  # type: ignore[attr-defined]


def as_viewer() -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()


def rename(client: TestClient, rid: str, rel: str = "pkg/cart.py", name: str = "add_item", new: str = "append_item", **kw: object) -> dict:
    r = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_symbol", "rel_path": rel, "name": name, "new_name": new, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def to_approved(client: TestClient, rid: str, t: dict) -> dict:
    assert client.post(rf(rid, t, "/validate")).json()["status"] == "validated"
    assert client.post(rf(rid, t, "/submit")).json()["status"] == "awaiting_approval"
    ack = client.get(rf(rid, t, "/plan")).json()["plan"]["acknowledgements_required"]
    ap = client.post(rf(rid, t, "/approve"), json={"acknowledged_warnings": ack})
    assert ap.status_code == 200 and ap.json()["status"] == "approved", ap.text
    return ap.json()


def edits_by_file(t: dict) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for e in t["plan"]["edits"]:
        out.setdefault(e["rel_path"], []).append((e["line"], e["kind"]))
    return out


# ---------------------------------------------------------------------------------------------------- catalog
def test_catalog_distinguishes_support_levels(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    r = client.get(rf(rid, tail="/types"))
    assert r.status_code == 200
    types = {t["id"]: t for t in r.json()["types"]}
    assert types["python.rename_symbol"]["support"] == "applicable"
    assert types["js.rename_symbol"]["support"] == "applicable"
    assert types["python.extract_function"]["support"] == "applicable"
    assert types["analysis.duplicates"]["support"] == "proposal_only"
    assert types["python.rename_method"]["support"] == "proposal_only"
    assert types["python.rename_module"]["support"] == "unsupported" and types["python.rename_module"]["reason"]
    for t in types.values():
        assert {"id", "languages", "inputs", "preconditions", "scope", "semantic_requirement", "edit_types", "validation", "verification", "limitations"} <= set(t)
    assert types["python.rename_symbol"]["inputs"] and types["python.rename_symbol"]["limitations"]


def test_unsupported_and_wrong_endpoint_types_are_rejected(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    r = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_module", "rel_path": "pkg/cart.py", "name": "a", "new_name": "b"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------------------------------- python rename
def test_python_rename_is_scope_correct_across_files(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    before = tree_state(root)
    t = rename(client, rid)
    assert t["status"] == "generated", [(f["code"], f["message"]) for f in t["plan"]["findings"] if f["severity"] == "blocker"]
    assert t["support_level"] == "applicable"
    assert tree_state(root) == before, "planning must not touch the working tree"
    changed = {f["path"] for f in t["files"]}
    assert changed == {"pkg/__init__.py", "pkg/cart.py", "pkg/use.py", "tests/test_cart.py"}
    diff = client.get(rf(rid, t, "/diff")).json()
    assert diff["has_diff"] and {f["path"] for f in diff["preview"]["files"]} == changed
    # apply on a copy of the text to inspect exact results without writing
    ack = t["plan"]["acknowledgements_required"]
    assert "public_api" in ack or any(f["code"] == "public_api" for f in t["plan"]["findings"])
    kinds = edits_by_file(t)
    assert (4, "definition") in kinds["pkg/cart.py"]
    assert not any(line == 5 for line, _ in kinds["pkg/cart.py"]), "comment must not be edited"
    texts = {e["rel_path"] + ":" + str(e["line"]): e for e in t["plan"]["edits"]}
    assert all(e["old"] == "add_item" and e["new"] == "append_item" for e in texts.values())
    assert "pkg/cart.py:15" not in texts, "the method definition must not be renamed"
    assert "pkg/cart.py:21" not in texts and "pkg/cart.py:22" not in texts, "local shadow must not be renamed"
    assert "pkg/cart.py:25" not in texts, "string must not be renamed"


def test_rename_end_to_end_apply_and_verify(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, use_git=True)
    client.post(f"{base(rid)}/tests/discover")
    t = rename(client, rid)
    to_approved(client, rid, t)
    # applying without confirm is refused, and nothing changes
    before = tree_state(root)
    assert client.post(rf(rid, t, "/apply"), json={}).status_code == 422
    assert tree_state(root) == before
    done = client.post(rf(rid, t, "/apply"), json={"confirm": True})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "applied" and body["apply_result"]["ok"] and body["apply_result"]["originals_retained"]
    assert (root / "pkg/cart.py").read_text().count("append_item") == 2
    use = (root / "pkg/use.py").read_text()
    assert "from pkg import add_item as put" not in use and "from pkg import append_item as put" in use
    assert "pkg.cart.append_item(3)" in use and "cart.append_item(4)" in use and "c.add_item(1)" in use
    cart = (root / "pkg/cart.py").read_text()
    assert "add_item = 3" in cart and 'LABEL = "add_item"' in cart and "# add_item keeps" in cart and "def add_item(self, item):" in cart
    assert "append_item" in (root / "pkg/__init__.py").read_text()
    # no git commit was made; the other changes are plain working-tree edits
    assert git(root, "log", "--oneline").count("\n") == 1
    assert "append_item" in git(root, "diff")
    # duplicate apply is refused
    again = client.post(rf(rid, t, "/apply"), json={"confirm": True})
    assert again.status_code == 409 and code_of(again) == "already_applied"
    # rollback guidance is available and correct
    rb = client.get(rf(rid, t, "/rollback")).json()
    assert rb["applied"] and rb["current_matches_refactored_result"] and "append_item" in rb["reverse_diff"]
    # audit trail
    actions = [e["action"] for e in client.get(rf(rid, t, "/audit")).json()["items"]]
    assert actions[0] == "refactor.created" and "refactor.approved" in actions and actions[-1] == "refactor.applied"
    # verification: a real controlled test run, outcome never claims equivalence
    prev = client.post(rf(rid, t, "/verify/preview"), json={}).json()
    assert prev["executes"] is False and prev["selected"] and prev["not_proven"]
    assert client.post(rf(rid, t, "/verifications"), json={}).status_code == 422
    v = client.post(rf(rid, t, "/verifications"), json={"confirm": True})
    assert v.status_code == 202, v.text
    got = client.get(rf(rid, t, f"/verifications/{v.json()['id']}")).json()
    assert got["outcome"] in ("PASSED", "INCOMPLETE"), got
    ids = {c["id"]: c["status"] for c in got["criteria"]}
    assert ids["run_completed"] == "pass" and ids["behavior_equivalence"] == "unavailable"
    final = client.get(rf(rid, t)).json()
    assert final["status"] in ("verified", "applied")
    if got["outcome"] == "PASSED":
        assert "not proven" in got["outcome_reason"].lower() or "evidence" in got["meaning"].lower()


# ---------------------------------------------------------------------------------------------------- rename edge cases
def blockers(t: dict) -> set[str]:
    return {f["code"] for f in t["plan"]["findings"] if f["severity"] == "blocker"}


def warnings_of(t: dict) -> set[str]:
    return {f["code"] for f in t["plan"]["findings"] if f["severity"] == "warning"}


@pytest.mark.parametrize(("name", "new", "expected"), [
    ("add_item", "add_item", "same_name"), ("add_item", "class", "invalid_name"), ("add_item", "not valid", "invalid_name"),
    ("add_item", "Cart", "name_conflict"), ("missing_symbol", "x", "symbol_not_found"), ("add_item", "int", None),
])
def test_rename_validation_and_ambiguity(art: Path, client: TestClient, tmp_path: Path, name: str, new: str, expected: str | None) -> None:
    rid, root = setup(client, tmp_path)
    before = tree_state(root)
    t = rename(client, rid, name=name, new=new)
    if expected:
        assert t["status"] == "blocked" and expected in blockers(t), t["plan"]["findings"]
        assert t["diff_text" if False else "has_diff"] is False
    else:
        assert t["status"] == "generated" and "shadows_builtin" in warnings_of(t)
    assert tree_state(root) == before


def test_rename_warns_about_dynamic_serialization_and_wildcards(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/m.py": "from dataclasses import dataclass\n\n\ndef build(x):\n    return x\n\n\n@dataclass\nclass Order:\n    total: int = 0\n",
             "pkg/dyn.py": 'import pkg.m as m\nfrom pkg.m import *\n\n\ndef go():\n    f = getattr(m, "build")\n    return f(1), build(2)\n',
             "pkg/ser.py": 'import json\nfrom pkg.m import Order\n\n\ndef dump(o: Order):\n    return json.dumps({"total": o.total})\n'}
    rid, _ = setup(client, tmp_path, files)
    t = rename(client, rid, rel="pkg/m.py", name="build", new="construct")
    assert t["status"] == "generated"
    assert {"dynamic_reference", "wildcard_import"} <= warnings_of(t)
    assert set(t["plan"]["acknowledgements_required"]) >= {"dynamic_reference", "wildcard_import"}
    # the wildcard user's call is updated, the string is not
    assert any(e["rel_path"] == "pkg/dyn.py" and e["line"] == 7 for e in t["plan"]["edits"])
    assert not any(e["rel_path"] == "pkg/dyn.py" and e["line"] == 6 for e in t["plan"]["edits"])
    t2 = rename(client, rid, rel="pkg/m.py", name="Order", new="Purchase")
    assert t2["status"] == "generated" and "serialization_name" in warnings_of(t2)


def test_approval_needs_warning_acknowledgement(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    t = rename(client, rid)
    assert client.post(rf(rid, t, "/validate")).json()["status"] == "validated"
    assert client.post(rf(rid, t, "/submit")).status_code == 200
    ack = client.get(rf(rid, t, "/plan")).json()["plan"]["acknowledgements_required"]
    assert ack, "the rename touches an exported name"
    r = client.post(rf(rid, t, "/approve"), json={"acknowledged_warnings": []})
    assert r.status_code == 422 and code_of(r) == "acknowledgement_required"
    assert client.get(rf(rid, t)).json()["status"] == "awaiting_approval"


def test_unsupported_python_syntax_blocks(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/m.py": "def f(x):\n    return x\n\n\ndef g(cmd):\n    match cmd:\n        case [f, *rest]:\n            return rest\n    return f\n"}
    rid, _ = setup(client, tmp_path, files)
    t = rename(client, rid, rel="pkg/m.py", name="f", new="h")
    assert t["status"] == "blocked" and "unsupported_syntax" in blockers(t)


def test_local_rename_only_touches_its_function(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/m.py": "def a(x):\n    tmp = x + 1\n    return tmp\n\n\ndef b(x):\n    tmp = x * 2\n    return tmp\n"}
    rid, root = setup(client, tmp_path, files)
    r = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_local", "rel_path": "pkg/m.py", "function": "a", "name": "tmp", "new_name": "bumped"})
    t = r.json()
    assert r.status_code == 201 and t["status"] == "generated"
    assert {e["line"] for e in t["plan"]["edits"]} == {2, 3}
    missing = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_local", "rel_path": "pkg/m.py", "name": "tmp", "new_name": "z"})
    assert missing.status_code == 422 and code_of(missing) == "function_required"


def test_method_rename_is_proposal_only(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    r = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_method", "rel_path": "pkg/cart.py", "class_name": "Cart", "name": "add_item", "new_name": "put"})
    t = r.json()
    assert r.status_code == 201 and t["support_level"] == "proposal_only" and t["status"] in ("analyzed", "blocked") and not t["has_diff"]
    assert t["plan"]["candidate_edits"] or t["plan"]["findings"]
    assert client.post(rf(rid, t, "/validate")).status_code == 409


# ---------------------------------------------------------------------------------------------------- JS / TS rename
JS_FILES = {
    "src/lib.ts": "export function add(a: number, b: number): number {\n  return a + b;\n}\n\nexport const other = 1;\n",
    "src/use.ts": "import { add } from './lib';\nimport * as L from './lib';\n\nconst o = { add: 1 };\nexport const total = add(1, 2) + L.add(3, 4) + o.add;\nexport const name = 'add';\n",
    "src/shadow.ts": "import { add } from './lib';\n\nexport function f(add: number) {\n  return add + 1;\n}\nexport const g = add(1, 2);\n",
    "src/cjs.js": "const { add } = require('./lib');\nmodule.exports = add(1, 2);\n",
}


def test_js_rename_updates_imports_namespace_and_leaves_properties(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {k: v for k, v in JS_FILES.items() if k in ("src/lib.ts", "src/use.ts")}
    rid, root = setup(client, tmp_path, files)
    r = client.post(rf(rid, tail="/rename"), json={"type_id": "js.rename_symbol", "rel_path": "src/lib.ts", "name": "add", "new_name": "sum"})
    t = r.json()
    assert r.status_code == 201 and t["status"] == "generated", t["plan"]["findings"]
    assert {f["path"] for f in t["files"]} == {"src/lib.ts", "src/use.ts"}
    to_approved(client, rid, t)
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).json()["status"] == "applied"
    use = (root / "src/use.ts").read_text()
    assert "import { sum } from './lib'" in use and "sum(1, 2)" in use and "L.sum(3, 4)" in use
    assert "const o = { add: 1 }" in use and "o.add" in use and "'add'" in use
    assert "function sum" in (root / "src/lib.ts").read_text()


def test_js_rename_blocks_on_shadowing_and_commonjs(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path, JS_FILES)
    t = client.post(rf(rid, tail="/rename"), json={"type_id": "js.rename_symbol", "rel_path": "src/lib.ts", "name": "add", "new_name": "sum"}).json()
    assert t["status"] == "blocked"
    assert {"shadowed_or_redeclared", "commonjs_dependent"} & blockers(t)
    mismatch = client.post(rf(rid, tail="/rename"), json={"type_id": "js.rename_symbol", "rel_path": "src/lib.ts", "name": "add", "new_name": "class"}).json()
    assert "invalid_name" in blockers(mismatch)
    wrong = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_symbol", "rel_path": "src/lib.ts", "name": "add", "new_name": "q"}).json()
    assert "language_mismatch" in blockers(wrong)


# ---------------------------------------------------------------------------------------------------- stale index / sources
def test_new_file_after_index_blocks_and_content_change_warns(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    (root / "pkg" / "late.py").write_text("from pkg.cart import add_item\n")
    t = rename(client, rid)
    assert t["status"] == "blocked" and "stale_index" in blockers(t) and t["plan"]["snapshot"]["complete"] is False
    assert client.post(f"{base(rid)}/index", json={"mode": "incremental"}).status_code == 202
    t2 = rename(client, rid)
    assert t2["status"] == "generated" and any(e["rel_path"] == "pkg/late.py" for e in t2["plan"]["edits"])
    # content-only change after indexing: allowed, flagged, read from disk
    (root / "pkg" / "late.py").write_text("from pkg.cart import add_item\n\n\nvalue = add_item(1)\n")
    t3 = rename(client, rid)
    assert t3["status"] == "generated" and "stale_index_content" in warnings_of(t3) and t3["plan"]["snapshot"]["complete"] is False
    assert any(e["rel_path"] == "pkg/late.py" and e["line"] == 4 for e in t3["plan"]["edits"])
    assert t3["impact"]["index_complete"] is False and t3["impact"]["caveats"]


def test_source_changed_after_plan_is_detected_and_refused(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    to_approved(client, rid, t)
    (root / "pkg/use.py").write_text((root / "pkg/use.py").read_text() + "\n# edited by a human\n")
    before = tree_state(root)
    r = client.post(rf(rid, t, "/apply"), json={"confirm": True})
    assert r.status_code == 409 and code_of(r) == "stale_source"
    assert tree_state(root) == before, "a refused apply must not write anything"
    got = client.get(rf(rid, t)).json()
    assert got["status"] == "invalid" and got["validation"]["stale"] is True
    assert client.post(rf(rid, t, "/approve"), json={}).status_code == 409


def test_tampered_diff_is_refused(art: Path, client: TestClient, tmp_path: Path) -> None:
    from app.db.session import get_session_factory
    from app.models import RefactorTransformation

    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    to_approved(client, rid, t)
    with get_session_factory()() as s:
        row = s.get(RefactorTransformation, t["id"])
        row.diff_text = row.diff_text.replace("append_item", "evil_item")
        s.commit()
    before = tree_state(root)
    r = client.post(rf(rid, t, "/apply"), json={"confirm": True})
    assert r.status_code == 409 and code_of(r) == "patch_tampered"
    assert tree_state(root) == before


def test_invalid_syntax_result_blocks_validation(art: Path, client: TestClient, tmp_path: Path) -> None:
    from app.db.session import get_session_factory
    from app.models import RefactorTransformation
    from app.services.repair import patch as patchlib

    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    old = (root / "pkg/cart.py").read_text()
    broken = old.replace("def add_item(item, items=None):", "def append_item(item, items=None)")
    diff = patchlib.make_diff("pkg/cart.py", old, broken)
    with get_session_factory()() as s:
        row = s.get(RefactorTransformation, t["id"])
        row.diff_text, row.patch_sha256 = diff, hashlib.sha256(diff.encode()).hexdigest()
        row.files = [{"path": "pkg/cart.py", "change_type": "modify", "base_sha256": hashlib.sha256(old.encode()).hexdigest(), "result_sha256": None, "added": 1, "removed": 1}]
        s.commit()
    r = client.post(rf(rid, t, "/validate"))
    assert r.status_code == 200 and r.json()["status"] == "invalid", r.text
    assert (root / "pkg/cart.py").read_text() == old
    assert client.post(rf(rid, t, "/submit")).status_code == 409


def test_planning_and_validation_leave_git_state_untouched(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, use_git=True)
    status, files = git(root, "status", "--porcelain"), tree_state(root)
    t = rename(client, rid)
    client.post(rf(rid, t, "/validate"))
    client.post(rf(rid, tail="/duplicates"), json={})
    assert git(root, "status", "--porcelain") == status and tree_state(root) == files
    assert not list(root.rglob("*.orig")) and not list(root.rglob("*.rej"))


def test_symlink_and_traversal_are_never_followed(art: Path, client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("from pkg.cart import add_item\n")
    rid, root = setup(client, tmp_path)
    (root / "pkg" / "link.py").symlink_to(outside)
    t = rename(client, rid)
    assert not any(e["rel_path"] == "pkg/link.py" for e in t["plan"]["edits"])
    assert outside.read_text() == "from pkg.cart import add_item\n"
    bad = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_symbol", "rel_path": "../outside.py", "name": "x", "new_name": "y"}).json()
    assert bad["status"] == "blocked"
    abs_ = client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_symbol", "rel_path": str(outside), "name": "x", "new_name": "y"}).json()
    assert abs_["status"] == "blocked"


def test_crlf_files_are_refused_not_corrupted(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    rid, root = setup(client, tmp_path, files)
    (root / "pkg/use.py").write_bytes(USE.replace("\n", "\r\n").encode())
    client.post(f"{base(rid)}/index", json={"mode": "incremental"})
    t = rename(client, rid)
    assert t["status"] == "blocked" and "unsupported_line_endings" in blockers(t)


def test_concurrent_apply_is_refused(art: Path, client: TestClient, tmp_path: Path) -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import RefactorTransformation

    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    to_approved(client, rid, t)
    with get_session_factory()() as s:
        s.execute(update(RefactorTransformation).where(RefactorTransformation.id == t["id"]).values(status="applying"))
        s.commit()
    before = tree_state(root)
    r = client.post(rf(rid, t, "/apply"), json={"confirm": True})
    assert r.status_code == 409 and code_of(r) == "apply_in_progress" and tree_state(root) == before


# ---------------------------------------------------------------------------------------------------- duplicates
DUP_A = textwrap.dedent('''
    def total_a(items):
        total = 0
        for item in items:
            if item.price > 0:
                total += item.price * item.qty
            else:
                total -= 1
        return total
''').lstrip("\n")
DUP_B = DUP_A.replace("total_a", "total_b").replace("item", "row").replace("total", "acc")
DUP_C = DUP_A.replace("total_a", "total_c").replace("total -= 1", "total -= 2").replace("item.qty", "item.qty + 1")
DUP_D = textwrap.dedent('''
    def different(values):
        out = {}
        for key, value in values.items():
            out.setdefault(value, []).append(key)
        return {k: sorted(v) for k, v in out.items()}
''').lstrip("\n")
DUP_FILES = {"pkg/__init__.py": "", "pkg/a.py": DUP_A, "pkg/b.py": DUP_B, "pkg/c.py": DUP_C, "pkg/d.py": DUP_D,
             "pkg/gen.py": "# @generated by tool\n" + DUP_A.replace("total_a", "total_g"), "third_party/x/dup.py": DUP_A.replace("total_a", "total_n")}


def dupes(client: TestClient, rid: str, **kw: object) -> dict:
    body = {"min_lines": 5, "min_tokens": 20, **kw}
    r = client.post(rf(rid, tail="/duplicates"), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_duplicate_detection_reports_evidence_and_never_merges(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, DUP_FILES)
    before = tree_state(root)
    t = dupes(client, rid)
    assert t["status"] == "analyzed" and t["support_level"] == "proposal_only" and not t["has_diff"]
    groups = client.get(rf(rid, t, "/duplicates")).json()["groups"]
    assert groups, "expected at least one group"
    top = groups[0]
    assert top["kind"] == "renamed" and top["similarity"] == 1.0 and top["auto_merge"] is False and top["pairs"][0]["kind"] == "renamed"
    paths = {m["rel_path"] for m in top["members"]}
    assert {"pkg/a.py", "pkg/b.py"} <= paths and "third_party/x/dup.py" not in paths and "pkg/gen.py" not in paths
    m = top["members"][0]
    assert m["start_line"] == 1 and m["end_line"] >= 5 and m["excerpt"] and m["tokens"] >= 20
    assert top["strategies"][0]["id"] and top["limitations"] and top["similarity_meaning"] and "differences" in top
    for g in groups:
        assert "pkg/d.py" not in {mm["rel_path"] for mm in g["members"]}, "unrelated code must not be grouped"
    assert tree_state(root) == before
    # analysis can never be validated, approved or applied
    for tail in ("/validate", "/submit", "/approve", "/apply"):
        assert client.post(rf(rid, t, tail), json={"confirm": True, "acknowledged_warnings": []}).status_code in (409, 422), tail


def test_structural_similarity_and_behavioral_differences(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path, {"pkg/__init__.py": "", "pkg/a.py": DUP_A, "pkg/c.py": DUP_C})
    t = dupes(client, rid, threshold=0.5)
    groups = client.get(rf(rid, t, "/duplicates")).json()["groups"]
    assert len(groups) == 1 and groups[0]["kind"] == "structural" and 0.5 <= groups[0]["similarity"] < 1.0
    assert groups[0]["differences"]["behavioral"] is True and groups[0]["differences"]["per_member"]
    strict = dupes(client, rid, threshold=0.99)
    assert client.get(rf(rid, strict, "/duplicates")).json()["groups"] == []


def test_duplicate_filters_exclusions_and_stable_ranking(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(DUP_FILES)
    files["tests/test_a.py"] = DUP_A.replace("total_a", "test_a")
    rid, _ = setup(client, tmp_path, files)
    t1, t2 = dupes(client, rid), dupes(client, rid)
    g1, g2 = (client.get(rf(rid, x, "/duplicates")).json()["groups"] for x in (t1, t2))
    assert [g["id"] for g in g1] == [g["id"] for g in g2] and g1, "ids and order must be stable"
    no_tests = dupes(client, rid, exclude_tests=True)
    members = {m["rel_path"] for g in client.get(rf(rid, no_tests, "/duplicates")).json()["groups"] for m in g["members"]}
    assert "tests/test_a.py" not in members
    pathf = dupes(client, rid, exclude_paths=["pkg/b.py"])
    members = {m["rel_path"] for g in client.get(rf(rid, pathf, "/duplicates")).json()["groups"] for m in g["members"]}
    assert "pkg/b.py" not in members
    only = client.get(rf(rid, t1, "/duplicates"), params={"path": "tests/", "kind": "renamed"}).json()
    assert all(any(m["rel_path"].startswith("tests/") for m in g["members"]) and g["kind"] == "renamed" for g in only["groups"])
    big = dupes(client, rid, min_lines=40)
    assert client.get(rf(rid, big, "/duplicates")).json()["groups"] == []
    stats = client.get(rf(rid, t1, "/duplicates")).json()["stats"]
    assert stats["skipped"].get("excluded_directory", 0) >= 1 and stats["skipped"].get("generated_marker", 0) >= 1


# ---------------------------------------------------------------------------------------------------- extract function
EX_SRC = textwrap.dedent('''
    class Cart:
        def __init__(self):
            self.items = []
            self.tax = 0.2

        def summary(self, discount):
            subtotal = 0
            for it in self.items:
                subtotal += it.price * it.qty
            total = subtotal * (1 + self.tax)
            label = "x"
            return total - discount, label


    def plain(a, b):
        out = []
        for x in a:
            if x > b:
                out.append(x)
        print(len(out))
        return out


    def unsafe(items):
        for it in items:
            if it is None:
                break
            if it.skip:
                return it
            yield it


    def conditional(flag):
        if flag:
            value = 1
        result = value
        return result
''').lstrip("\n")


def extract(client: TestClient, rid: str, start: int, end: int, name: str = "helper") -> dict:
    r = client.post(rf(rid, tail="/extract-function"), json={"rel_path": "pkg/ex.py", "start_line": start, "end_line": end, "new_name": name})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def exrepo(art: Path, client: TestClient, tmp_path: Path) -> tuple[str, Path]:
    return setup(client, tmp_path, {"pkg/__init__.py": "", "pkg/ex.py": EX_SRC})


def test_extract_method_computes_parameters_and_returns(exrepo: tuple[str, Path], client: TestClient) -> None:
    rid, root = exrepo
    t = extract(client, rid, 8, 10, "compute_total")
    assert t["status"] == "generated", blockers(t)
    ex = t["plan"]["extra"]
    assert ex["parameters"] == ["subtotal"] and ex["returns"] == ["total"] and ex["function"]["kind"] == "method"
    to_approved(client, rid, t)
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).json()["status"] == "applied"
    text = (root / "pkg/ex.py").read_text()
    assert "total = self.compute_total(subtotal)" in text
    assert "def compute_total(self, subtotal):" in text and "        return total" in text
    compile(text, "ex.py", "exec")


def test_extract_function_with_side_effects_and_mutation(exrepo: tuple[str, Path], client: TestClient) -> None:
    rid, root = exrepo
    t = extract(client, rid, 17, 19, "pick")
    assert t["status"] == "generated" and t["plan"]["extra"]["parameters"] == ["a", "b", "out"] and t["plan"]["extra"]["returns"] == []
    t2 = extract(client, rid, 20, 20, "show")
    assert t2["status"] == "generated" and t2["plan"]["side_effects"], "print() must be reported as a side effect"
    assert any(f["code"] == "side_effects" for f in t2["plan"]["findings"])


@pytest.mark.parametrize(("start", "end", "code"), [
    (27, 27, "control_flow_break"), (29, 29, "control_flow_return"), (30, 30, "control_flow_yield"),
    (34, 35, "conditional_assignment"), (1, 3, "module_level_region"),
])
def test_extract_unsafe_regions_are_blockers(exrepo: tuple[str, Path], client: TestClient, start: int, end: int, code: str) -> None:
    rid, root = exrepo
    before = tree_state(root)
    t = extract(client, rid, start, end)
    assert t["status"] == "blocked" and not t["has_diff"], (code, t["plan"]["findings"])
    assert code in blockers(t) or (code == "module_level_region" and "region_not_statement_aligned" in blockers(t))
    assert t["support_level"] == "proposal_only"
    assert tree_state(root) == before


def test_extract_rejects_bad_names_and_ranges(exrepo: tuple[str, Path], client: TestClient) -> None:
    rid, _ = exrepo
    assert "invalid_name" in blockers(extract(client, rid, 8, 10, "class"))
    assert "name_conflict" in blockers(extract(client, rid, 17, 19, "plain"))
    assert "region_not_statement_aligned" in blockers(extract(client, rid, 9, 10))
    assert "invalid_range" in blockers(extract(client, rid, 10, 8))
    r = client.post(rf(rid, tail="/extract-function"), json={"rel_path": "pkg/ex.py", "start_line": 0, "end_line": 3, "new_name": "x"})
    assert r.status_code == 422


def test_extract_closure_capturing_rebound_variable_is_blocked(art: Path, client: TestClient, tmp_path: Path) -> None:
    src = "def f(n):\n    k = n\n    fn = lambda: k\n    k = n + 1\n    return fn()\n"
    rid, _ = setup(client, tmp_path, {"pkg/__init__.py": "", "pkg/ex.py": src})
    t = extract(client, rid, 3, 3, "mk")
    assert t["status"] == "blocked" and "closure_captures_rebound_variable" in blockers(t)


# ---------------------------------------------------------------------------------------------------- lifecycle
def test_lifecycle_requires_each_step_and_blocks_shortcuts(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    before = tree_state(root)
    assert code_of(client.post(rf(rid, t, "/apply"), json={"confirm": True})) == "approval_required"
    assert code_of(client.post(rf(rid, t, "/approve"), json={})) == "invalid_state_transition"
    assert code_of(client.post(rf(rid, t, "/submit"))) == "invalid_state_transition"
    client.post(rf(rid, t, "/validate"))
    assert code_of(client.post(rf(rid, t, "/apply"), json={"confirm": True})) == "approval_required"
    client.post(rf(rid, t, "/submit"))
    assert code_of(client.post(rf(rid, t, "/submit"))) == "already_submitted"
    assert code_of(client.post(rf(rid, t, "/apply"), json={"confirm": True})) == "approval_required"
    assert tree_state(root) == before, "nothing may be written before approval and confirmation"
    assert client.post(rf(rid, t, "/verifications"), json={"confirm": True}).status_code == 409
    assert code_of(client.post(rf(rid, t, "/verifications"), json={"confirm": True})) == "not_applied"


def test_reject_is_terminal_and_needs_a_reason(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    assert client.post(rf(rid, t, "/reject"), json={"reason": "no"}).status_code == 422
    r = client.post(rf(rid, t, "/reject"), json={"reason": "Not the right name"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["reject_reason"]
    assert code_of(client.post(rf(rid, t, "/reject"), json={"reason": "again please"})) == "already_rejected"
    assert client.post(rf(rid, t, "/validate")).status_code == 409
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).status_code == 409
    assert (root / "pkg/cart.py").read_text() == CART


def test_blocked_plan_cannot_proceed(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    t = rename(client, rid, new="Cart")
    assert t["status"] == "blocked" and t["plan"]["candidate_edits"] is not None
    for tail in ("/validate", "/submit", "/apply"):
        assert client.post(rf(rid, t, tail), json={"confirm": True}).status_code in (409, 422)
    assert client.post(rf(rid, t, "/close")).json()["status"] == "closed"


def test_viewer_can_read_but_not_change(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path)
    t = rename(client, rid)
    to_approved(client, rid, t)
    as_viewer()
    before = tree_state(root)
    assert client.get(rf(rid, tail="/types")).status_code == 200
    assert client.get(rf(rid)).status_code == 200
    assert client.get(rf(rid, t)).status_code == 200 and client.get(rf(rid, t, "/diff")).status_code == 200
    assert client.post(rf(rid, tail="/rename"), json={"type_id": "python.rename_symbol", "rel_path": "pkg/cart.py", "name": "add_item", "new_name": "zz"}).status_code == 403
    for tail, body in (("/validate", None), ("/submit", None), ("/approve", {}), ("/reject", {"reason": "nope nope"}), ("/apply", {"confirm": True}),
                       ("/baseline", {"confirm": True}), ("/verifications", {"confirm": True}), ("/close", None)):
        r = client.post(rf(rid, t, tail), json=body) if body is not None else client.post(rf(rid, t, tail))
        assert r.status_code == 403, (tail, r.status_code)
    assert client.post(rf(rid, tail="/duplicates"), json={}).status_code == 403
    assert tree_state(root) == before


def test_unauthorized_access_is_refused(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/refactors/types"), ("get", "/refactors"), ("post", "/refactors/rename"), ("post", "/refactors/extract-function"),
                             ("post", "/refactors/duplicates"), ("get", "/refactors/x"), ("get", "/refactors/x/plan"), ("get", "/refactors/x/diff"),
                             ("post", "/refactors/x/validate"), ("post", "/refactors/x/approve"), ("post", "/refactors/x/reject"), ("post", "/refactors/x/apply"),
                             ("post", "/refactors/x/baseline"), ("post", "/refactors/x/verifications"), ("get", "/refactors/x/verifications"), ("get", "/refactors/x/audit")):
            assert getattr(c, method)(f"{API}/repositories/abc{path}").status_code == 401, path
    reset_engine()


def test_repository_isolation_and_unknown_ids(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid1, _ = setup(client, tmp_path, name="one", dirname="r1")
    rid2, _ = setup(client, tmp_path, name="two", dirname="r2")
    t = rename(client, rid1)
    assert client.get(rf(rid2, t)).status_code == 404
    assert client.post(rf(rid2, t, "/validate")).status_code == 404
    assert client.get(rf(rid2, t, "/audit")).status_code == 404
    assert client.get(rf(rid1, "nope")).status_code == 404
    assert client.get(rf("missing-repo", tail="/types")).status_code == 404
    assert client.get(rf(rid2)).json()["total"] == 0 and client.get(rf(rid1)).json()["total"] == 1


def test_history_listing_filters_and_pagination(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    ids = [rename(client, rid, new=f"item_{i}")["id"] for i in range(3)]
    dupes(client, rid)
    page1 = client.get(rf(rid), params={"page_size": 2}).json()
    assert page1["total"] == 4 and len(page1["items"]) == 2 and page1["pages"] == 2
    assert client.get(rf(rid), params={"page": 2, "page_size": 2}).json()["items"][-1]["id"] == ids[0]
    only = client.get(rf(rid), params={"type_id": "analysis.duplicates"}).json()
    assert only["total"] == 1 and only["items"][0]["status"] == "analyzed"
    assert client.get(rf(rid), params={"status": "generated"}).json()["total"] == 3
    item = page1["items"][0]
    assert {"id", "type_id", "title", "status", "files", "blockers", "warnings", "created_at"} <= set(item)
    assert client.post(rf(rid, ids[1], "/reject"), json={"reason": "not needed now"}).status_code == 200


# ---------------------------------------------------------------------------------------------------- verification
DYN_FILES = {**FILES, "tests/test_dyn.py": 'import pkg.cart as cart\n\n\ndef test_dynamic():\n    assert getattr(cart, "add_item")("a") == ["a"]\n'}


def test_verification_records_failures_and_never_claims_equivalence(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, root = setup(client, tmp_path, DYN_FILES)
    client.post(f"{base(rid)}/tests/discover")
    t = rename(client, rid)
    assert "dynamic_reference" in warnings_of(t)
    # baseline before the change: explicit, executed, and recorded
    assert client.post(rf(rid, t, "/baseline"), json={}).status_code == 422
    t = to_approved(client, rid, t)
    b = client.post(rf(rid, t, "/baseline"), json={"confirm": True})
    assert b.status_code == 202, b.text
    run = client.get(f"{base(rid)}/tests/runs/{b.json()['execution_run_id']}").json()
    assert run["status"] == "passed"
    assert client.get(rf(rid, t)).json()["baseline_execution_run_id"] == run["id"]
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).status_code == 200
    assert code_of(client.post(rf(rid, t, "/baseline"), json={"confirm": True})) == "baseline_not_allowed"
    v = client.post(rf(rid, t, "/verifications"), json={"confirm": True})
    assert v.status_code == 202, v.text
    got = client.get(rf(rid, t, f"/verifications/{v.json()['id']}")).json()
    assert got["outcome"] == "FAILED", got
    crit = {c["id"]: c["status"] for c in got["criteria"]}
    assert crit["no_failures"] == "fail" and crit["no_new_failures"] == "fail" and crit["behavior_equivalence"] == "unavailable"
    assert got["comparison"]["new_failures"] and got["comparison"]["baseline_run_id"] == run["id"]
    assert got["run"]["tests_failed"] >= 1 and got["run"]["stdout_tail"] is not None
    final = client.get(rf(rid, t)).json()
    assert final["status"] == "verification_failed" and final["verification_summary"]["outcome"] == "FAILED"
    assert (root / "pkg/cart.py").read_text().count("def append_item") == 1, "a failed verification must not roll the change back by itself"
    assert [x["id"] for x in client.get(rf(rid, t, "/verifications")).json()] == [v.json()["id"]]


def test_verification_blocked_when_no_tests_are_linked(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/m.py": "def alpha():\n    return 1\n\n\nVALUE = alpha()\n"}
    rid, _ = setup(client, tmp_path, files)
    t = rename(client, rid, rel="pkg/m.py", name="alpha", new="beta")
    to_approved(client, rid, t)
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).status_code == 200
    v = client.post(rf(rid, t, "/verifications"), json={"confirm": True})
    assert v.status_code == 202 and v.json()["outcome"] == "BLOCKED"
    assert "discover" in v.json()["outcome_reason"].lower() or "No discovered test" in v.json()["outcome_reason"]
    assert client.get(rf(rid, t)).json()["status"] == "applied"


def test_apply_triggers_reindex(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    t = rename(client, rid)
    to_approved(client, rid, t)
    done = client.post(rf(rid, t, "/apply"), json={"confirm": True}).json()
    assert done["apply_result"]["reindex"]["scheduled"] is True
    fresh = client.get(f"{base(rid)}/freshness").json()
    assert fresh["state"] == "fresh"
    again = rename(client, rid, name="append_item", new="add_item")
    assert again["status"] == "generated" and again["plan"]["snapshot"]["complete"] is True


# ---------------------------------------------------------------------------------------------------- impact, simplify, cycles
CYCLE_FILES = {"pkg/__init__.py": "", "pkg/a.py": "from pkg.b import fb\n\n\ndef fa():\n    return fb()\n", "pkg/b.py": "from pkg.a import fa\n\n\ndef fb():\n    return 1\n\n\ndef call():\n    return fa()\n"}


def test_impact_lists_dependents_tests_cycles_and_proof(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path)
    client.post(f"{base(rid)}/tests/discover")
    t = rename(client, rid)
    imp = t["impact"]
    assert imp["index_complete"] is True and imp["snapshot_id"]
    assert "pkg/use.py" in imp["direct_dependents"] and "tests/test_cart.py" in imp["direct_dependents"]
    assert imp["cycles"]["new_cycles"] == [] and "unchanged" in imp["cycles"]["proof"]
    assert imp["affected_tests"]["discovered"] is True and "tests/test_cart.py" in {x for v in imp["affected_tests"]["associated"].values() for x in v}
    assert imp["public_entry_points"], "the exported function must be listed"
    assert "basis" in imp["affected_tests"]


def test_impact_reports_existing_cycles(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid, _ = setup(client, tmp_path, CYCLE_FILES)
    t = rename(client, rid, rel="pkg/a.py", name="fa", new="fa2")
    assert t["status"] == "generated"
    existing = t["impact"]["cycles"]["existing_cycles_involving_changed_files"]
    assert existing and set(existing[0]) == {"pkg/a.py", "pkg/b.py"}
    sug = client.post(rf(rid, tail="/cycle-suggestions"), json={}).json()
    assert sug["status"] == "analyzed" and not sug["has_diff"]
    cyc = sug["plan"]["extra"]["cycles"]
    assert len(cyc) == 1 and cyc[0]["strategies"] and cyc[0]["weakest_edge"]["imported_names"]


def test_simplify_suggestions_are_proposal_only(art: Path, client: TestClient, tmp_path: Path) -> None:
    src = ("def a(x):\n    if x > 1:\n        return True\n    else:\n        return False\n\n\n"
           "def b(x, y):\n    if x:\n        if y:\n            print(1)\n\n\n"
           "def c(x):\n    if x:\n        return 1\n    else:\n        return 2\n")
    rid, root = setup(client, tmp_path, {"pkg/__init__.py": "", "pkg/s.py": src})
    r = client.post(rf(rid, tail="/simplify"), json={"rel_path": "pkg/s.py"})
    t = r.json()
    assert r.status_code == 201 and t["status"] == "analyzed" and not t["has_diff"]
    pats = {s["pattern"] for s in t["plan"]["extra"]["suggestions"]}
    assert pats == {"return_bool_condition", "merge_nested_if", "else_after_exit"}
    assert all(s["equivalence"] and s["before"] and s["after"] for s in t["plan"]["extra"]["suggestions"])
    assert (root / "pkg/s.py").read_text() == src
    assert client.post(rf(rid, t, "/apply"), json={"confirm": True}).status_code == 409


# ---------------------------------------------------------------------------------------------------- engine units
def test_edit_engine_rejects_overlap_and_stale_text() -> None:
    from app.services.refactor.common import Edit, EditError, apply_edits, find_conflicts

    a = Edit("f.py", 1, 0, 5, "hello", "HELLO", "reference")
    b = Edit("f.py", 1, 3, 8, "lo wo", "XX", "reference")
    assert [f.code for f in find_conflicts([a, b])] == ["overlapping_edits"]
    assert find_conflicts([a, Edit("f.py", 1, 0, 5, "hello", "HELLO", "reference")]) == []
    assert apply_edits("hello world\n", [a]) == "HELLO world\n"
    with pytest.raises(EditError) as exc:
        apply_edits("goodbye world\n", [a])
    assert exc.value.code == "stale"


def test_scope_analyzer_distinguishes_same_spelled_names() -> None:
    from app.services.refactor import pyscope

    src = "x = 1\n\n\ndef f(x):\n    return x\n\n\ndef g():\n    global x\n    x = 2\n    return [x for x in range(3)]\n"
    fa = pyscope.analyze("m.py", src)
    assert fa is not None
    module_x = [s for s in fa.sites if s.name == "x" and s.resolved is fa.module_scope]
    assert {s.line for s in module_x} >= {1, 9, 10}
    assert not any(s.line == 5 for s in module_x), "the parameter shadows the module name"
    assert not any(s.line == 11 for s in module_x), "the comprehension variable shadows the module name"
