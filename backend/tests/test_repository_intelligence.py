"""Index consistency, incremental behaviour, relationships and failure handling."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models import IndexedFile, IndexSnapshot, Relationship, Symbol
from app.services.intelligence import parser_registry
from tests.conftest import register_and_index

API = "/api/v1"

FILES = {
    "pyproject.toml": "[project]\nname='demo'\ndependencies=['requests>=2']\n",
    "pkg/__init__.py": "",
    "pkg/models.py": "class Base:\n    pass\n\n\nclass User(Base):\n    def name(self):\n        return 'x'\n",
    "pkg/service.py": ("import os\nimport requests\nimport missingmod\nfrom .models import User\n"
                       "from pkg import models\n\n\ndef make():\n    return User()\n"),
    "web/package.json": '{"dependencies": {"react": "1.0.0"}}\n',
    "web/src/b.ts": "export class B2 {}\nexport const b = 1;\n",
    "web/src/a.ts": ('import {b, B2} from "./b";\nimport React from "react";\nimport x from "nope-pkg";\n'
                     "export class A extends B2 {}\n"),
    "notes.md": "# notes\n",
}


@pytest.fixture(autouse=True)
def _parsers() -> None:
    parser_registry.reset_for_tests()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


@pytest.fixture
def intel_repo(tmp_path: Path) -> Path:
    root = tmp_path / "intel"
    write(root, FILES)
    return root


def reindex(client: TestClient, repo_id: str, mode: str | None = None) -> dict:
    r = client.post(f"{API}/repositories/{repo_id}/index", json={"mode": mode} if mode else None)
    assert r.status_code == 202, r.text
    return client.get(f"{API}/repositories/{repo_id}/snapshots/{r.json()['id']}").json()


def files_by_path(client: TestClient, repo_id: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    page = 1
    while True:
        body = client.get(f"{API}/repositories/{repo_id}/files", params={"page": page, "page_size": 200}).json()
        out.update({f["rel_path"]: f for f in body["items"]})
        if page >= body["pages"]:
            return out
        page += 1


def all_symbols(client: TestClient, repo_id: str) -> list[dict]:
    return client.get(f"{API}/repositories/{repo_id}/symbols", params={"page_size": 200}).json()["items"]


def deps(client: TestClient, repo_id: str, file_id: str, kind: str = "dependencies") -> list[dict]:
    return client.get(f"{API}/repositories/{repo_id}/files/{file_id}/{kind}").json()["items"]


def row_counts() -> dict[str, int]:
    with get_session_factory()() as db:
        return {"files": db.scalar(select(func.count()).select_from(IndexedFile)) or 0,
                "symbols": db.scalar(select(func.count()).select_from(Symbol)) or 0,
                "rels": db.scalar(select(func.count()).select_from(Relationship)) or 0}


def test_first_run_statuses_and_counts(client: TestClient, intel_repo: Path) -> None:
    out = register_and_index(client, intel_repo)
    snap, repo_id = out["snapshot"], out["repo"]["id"]
    assert snap["status"] == "completed" and snap["generation"] == 1
    assert snap["files_new"] == snap["files_indexed"] == len(FILES) and snap["files_unchanged"] == 0
    assert snap["symbols_count"] > 0 and snap["analysis_counts"]["parsed"] == 5
    files = files_by_path(client, repo_id)
    assert files["pkg/models.py"]["analysis_status"] == "parsed"
    assert files["notes.md"]["analysis_status"] == "unsupported"
    assert files["web/src/a.ts"]["language"] == "TypeScript"


def test_import_resolution_kinds(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    files = files_by_path(client, repo_id)
    imps = client.get(f"{API}/repositories/{repo_id}/files/{files['pkg/service.py']['id']}/imports").json()["items"]
    by_spec = {(i["specifier"], i["names"][0]["name"]): i for i in imps}
    assert by_spec[("os", "os")]["resolution_status"] == "external"
    assert by_spec[("requests", "requests")]["resolution_status"] == "external"
    assert by_spec[("missingmod", "missingmod")]["resolution_status"] == "unresolved"
    models = next(i for i in imps if i["specifier"] == "models" and i["level"] == 1)
    assert models["resolution_status"] == "resolved" and models["resolved_path"] == "pkg/models.py"
    ts = client.get(f"{API}/repositories/{repo_id}/files/{files['web/src/a.ts']['id']}/imports").json()["items"]
    status = {i["specifier"]: i["resolution_status"] for i in ts}
    assert status == {"./b": "resolved", "react": "external", "nope-pkg": "unresolved"}


def test_dependencies_dependents_and_extends(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    files = files_by_path(client, repo_id)
    dependents = deps(client, repo_id, files["pkg/models.py"]["id"], "dependents")
    assert [d["path"] for d in dependents if d["type"] == "IMPORTS_FILE"] == ["pkg/service.py"]
    own = deps(client, repo_id, files["pkg/models.py"]["id"])
    ext = [d for d in own if d["type"] == "EXTENDS_SYMBOL"]
    assert len(ext) == 1 and ext[0]["evidence"] == "inferred" and ext[0]["target_text"] == "Base"
    ts_ext = [d for d in deps(client, repo_id, files["web/src/a.ts"]["id"]) if d["type"] == "EXTENDS_SYMBOL"]
    assert ts_ext and ts_ext[0]["path"] == "web/src/b.ts"
    pkgs = {d["target_text"] for d in deps(client, repo_id, files["pkg/service.py"]["id"])
            if d["type"] == "DEPENDS_ON_PACKAGE"}
    assert {"requests", "os"} <= pkgs


def test_repeat_run_is_idempotent_and_reuses_unchanged_files(client: TestClient, intel_repo: Path) -> None:
    out = register_and_index(client, intel_repo)
    repo_id = out["repo"]["id"]
    first_syms = {(s["stable_id"], s["file_path"], s["start_line"]) for s in all_symbols(client, repo_id)}
    before = row_counts()
    second = reindex(client, repo_id)
    assert second["generation"] == 2 and second["files_unchanged"] == len(FILES)
    assert second["files_new"] == second["files_changed"] == 0 and second["files_removed"] == 0
    assert {(s["stable_id"], s["file_path"], s["start_line"]) for s in all_symbols(client, repo_id)} == first_syms
    assert row_counts() == before  # previous generation removed, nothing duplicated
    assert second["relationships_count"] == out["snapshot"]["relationships_count"]


def test_full_mode_reparses_everything_with_same_result(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    before = {s["stable_id"] for s in all_symbols(client, repo_id)}
    snap = reindex(client, repo_id, "full")
    assert snap["mode"] == "full" and snap["files_unchanged"] == len(FILES)
    assert {s["stable_id"] for s in all_symbols(client, repo_id)} == before


def test_change_add_remove_are_reconciled(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    (intel_repo / "pkg/models.py").write_text("class Base:\n    pass\n\n\nclass Admin(Base):\n    pass\n")
    write(intel_repo, {"pkg/extra.py": "def extra():\n    return 1\n"})
    (intel_repo / "notes.md").unlink()
    snap = reindex(client, repo_id)
    assert (snap["files_new"], snap["files_changed"], snap["files_removed"]) == (1, 1, 1)
    assert snap["files_unchanged"] == len(FILES) - 2
    names = {s["name"] for s in all_symbols(client, repo_id)}
    assert "Admin" in names and "extra" in names and "User" not in names and "make" in names
    files = files_by_path(client, repo_id)
    assert "notes.md" not in files and files["pkg/extra.py"]["change_status"] == "new"
    assert files["pkg/models.py"]["change_status"] == "changed"


def test_relationships_reconciled_when_target_removed(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    (intel_repo / "pkg/models.py").unlink()
    reindex(client, repo_id)
    files = files_by_path(client, repo_id)
    imps = client.get(f"{API}/repositories/{repo_id}/files/{files['pkg/service.py']['id']}/imports").json()["items"]
    assert next(i for i in imps if i["specifier"] == "models")["resolution_status"] == "unresolved"
    targets = {d["path"] for d in deps(client, repo_id, files["pkg/service.py"]["id"]) if d["type"] == "IMPORTS_FILE"}
    assert "pkg/models.py" not in targets  # the edge to the removed file is gone


def test_stable_symbol_ids_survive_unrelated_edits(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    ids = {(s["qualified_name"]): s["stable_id"] for s in all_symbols(client, repo_id) if s["file_path"] == "pkg/service.py"}
    (intel_repo / "pkg/models.py").write_text("# comment\n" + FILES["pkg/models.py"])
    reindex(client, repo_id)
    again = {s["qualified_name"]: s["stable_id"] for s in all_symbols(client, repo_id) if s["file_path"] == "pkg/service.py"}
    assert ids == again and ids


def test_failed_run_keeps_previous_index(client: TestClient, intel_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    good_syms = {s["stable_id"] for s in all_symbols(client, repo_id)}
    before = row_counts()
    write(intel_repo, {"pkg/new.py": "def brand_new(): pass\n"})
    import app.services.intelligence.pipeline as pipeline

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after staging")

    monkeypatch.setattr(pipeline.git, "inspect_repository", boom)
    snap = reindex(client, repo_id)
    assert snap["status"] == "failed" and "RuntimeError" in snap["error_message"]
    assert {s["stable_id"] for s in all_symbols(client, repo_id)} == good_syms
    assert row_counts() == before  # staged rows were removed, old generation untouched
    summary = client.get(f"{API}/repositories/{repo_id}/summary").json()
    assert summary["indexed"] and summary["snapshot"]["status"] == "completed"
    monkeypatch.undo()
    recovered = reindex(client, repo_id)
    assert recovered["status"] == "completed" and recovered["files_new"] == 1


def test_concurrent_index_request_conflicts(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    with get_session_factory()() as db:
        db.add(IndexSnapshot(repository_id=repo_id, status="running"))
        db.commit()
    r = client.post(f"{API}/repositories/{repo_id}/index")
    assert r.status_code == 409
    assert client.get(f"{API}/repositories/{repo_id}/freshness").json()["state"] == "running"


def test_parser_unavailable_then_recovers(client: TestClient, intel_repo: Path) -> None:
    parser_registry.disable_parser_for_tests("Python")
    out = register_and_index(client, intel_repo)
    repo_id = out["repo"]["id"]
    assert out["snapshot"]["status"] == "completed_with_warnings"
    assert out["snapshot"]["analysis_counts"]["unavailable"] == 3
    files = files_by_path(client, repo_id)
    assert files["pkg/models.py"]["analysis_status"] == "unavailable" and files["pkg/models.py"]["symbol_count"] == 0
    parser_registry.reset_for_tests()
    snap = reindex(client, repo_id)
    assert snap["status"] == "completed" and snap["analysis_counts"].get("unavailable", 0) == 0
    assert files_by_path(client, repo_id)["pkg/models.py"]["analysis_status"] == "parsed"


def test_syntax_error_file_makes_run_completed_with_warnings(client: TestClient, intel_repo: Path) -> None:
    write(intel_repo, {"pkg/bad.py": "def broken(:\n    pass\n"})
    snap = register_and_index(client, intel_repo)["snapshot"]
    assert snap["status"] == "completed_with_warnings" and snap["analysis_counts"]["partial"] == 1
    assert any("syntax" in w for w in snap["warnings"])


def test_time_budget_truncation(client: TestClient, intel_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    import app.services.intelligence.pipeline as pipeline

    real = pipeline._limits
    monkeypatch.setattr(pipeline, "_limits", lambda s: dataclasses.replace(real(s), time_budget_seconds=0.0))
    snap = register_and_index(client, intel_repo)["snapshot"]
    assert snap["truncated"] and "time budget" in snap["truncation_reason"]
    assert snap["status"] == "completed_with_warnings" and snap["files_removed"] == 0


def test_truncated_rerun_does_not_claim_removals(client: TestClient, intel_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    with TestClient(create_app()) as c:
        repo_id = register_and_index(c, intel_repo)["repo"]["id"]
        monkeypatch.setenv("NOVA_INDEX_MAX_FILES", "2")
        reset_settings_cache()
    with TestClient(create_app()) as c2:
        snap = reindex(c2, repo_id)
    assert snap["truncated"] and snap["files_removed"] is None


def test_repository_isolation(client: TestClient, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, {"m.py": "class OnlyInA:\n    pass\n"})
    write(b, {"m.py": "class OnlyInB:\n    pass\n"})
    ra = register_and_index(client, a, "a")["repo"]["id"]
    rb = register_and_index(client, b, "b")["repo"]["id"]
    names_a = {s["name"] for s in all_symbols(client, ra)}
    assert names_a == {"OnlyInA"} and {s["name"] for s in all_symbols(client, rb)} == {"OnlyInB"}
    hits = client.get(f"{API}/repositories/{rb}/symbols/search", params={"q": "OnlyInA"}).json()
    assert hits["total"] == 0
    file_a = files_by_path(client, ra)["m.py"]["id"]
    assert client.get(f"{API}/repositories/{rb}/files/{file_a}").status_code == 404
    reindex(client, ra)
    assert {s["name"] for s in all_symbols(client, rb)} == {"OnlyInB"}  # b untouched by a's re-index


def test_snapshot_history_is_pruned(client: TestClient, intel_repo: Path) -> None:
    repo_id = register_and_index(client, intel_repo)["repo"]["id"]
    for _ in range(12):
        reindex(client, repo_id)
    body = client.get(f"{API}/repositories/{repo_id}/snapshots", params={"page_size": 100}).json()
    assert body["total"] == 10 and body["items"][0]["generation"] == 13


def test_repo_code_is_never_executed(client: TestClient, tmp_path: Path) -> None:
    root, marker = tmp_path / "evil", tmp_path / "executed.txt"
    write(root, {
        "setup.py": f"open({str(marker)!r}, 'w').write('x')\n",
        "conftest.py": f"open({str(marker)!r}, 'w').write('x')\n",
        "package.json": '{"scripts": {"postinstall": "touch executed"}}\n',
        "x.py": f"import os\nos.system('touch {marker}')\n",
    })
    register_and_index(client, root)
    assert not marker.exists()
