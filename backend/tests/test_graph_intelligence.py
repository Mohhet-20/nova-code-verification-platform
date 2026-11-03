"""Phase 03: call extraction/resolution, graph algorithms, dependency/impact/cycle/metrics/status APIs."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.intelligence import graph
from app.services.intelligence.symbol_extractor import extract_symbols
from app.services.intelligence.types import ANALYSIS_VERSION
from tests.conftest import register_and_index
from tests.test_repository_intelligence import reindex, write

API = "/api/v1"

CHAIN = {
    "app/__init__.py": "",
    "app/a.py": "from app.b import fb\n\n\ndef fa():\n    return fb()\n",
    "app/b.py": "from app.c import fc\n\n\ndef fb():\n    return fc()\n",
    "app/c.py": ("class Base:\n    def m(self):\n        return 1\n\n\nclass Child(Base):\n"
                 "    def n(self):\n        return self.m()\n\n\ndef fc():\n    return Child().n()\n"),
    "app/dyn.py": ("import importlib\nimport missing_pkg_for_test\nfrom .nowhere import thing\n\n\n"
                   "def load(name):\n    return importlib.import_module(name)\n"),
}
CYCLE = {
    "cyc/__init__.py": "",
    "cyc/x.py": "from cyc import y\n",
    "cyc/y.py": "from cyc import z\n",
    "cyc/z.py": "from cyc import x\n",
    "cyc/solo.py": "import cyc.solo\n",
    "ok/__init__.py": "",
    "ok/one.py": "from ok import two\n",
    "ok/two.py": "VALUE = 1\n",
}


def index(client: TestClient, root: Path, files: dict[str, str], name: str = "g") -> str:
    write(root, files)
    return register_and_index(client, root, name)["repo"]["id"]


def base(repo_id: str) -> str:
    return f"{API}/repositories/{repo_id}"


def deps(client: TestClient, repo_id: str, **params: object) -> dict:
    r = client.get(f"{base(repo_id)}/dependencies", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def paths(body: dict) -> list[str]:
    return [i["path"] for i in body["items"]]


@pytest.fixture
def chain_repo(client: TestClient, tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "chain"
    return index(client, root, CHAIN), root


# ------------------------------------------------------------------ pure algorithms

def test_bfs_depth_cycles_and_determinism() -> None:
    adj = {"a": ["c", "b"], "b": ["d"], "c": ["d"], "d": ["a"]}

    def expand(fr: list[str]) -> list[graph.Edge]:
        return [(s, t, {"type": "X"}) for s in fr for t in adj.get(s, [])]

    r = graph.bfs(["a"], expand, 5, 100)
    assert {k: v.depth for k, v in r.visited.items()} == {"b": 1, "c": 1, "d": 2}
    assert r.visited["d"].parent == "b"  # deterministic tie-break: smallest discovering parent
    assert r.path_to("d", ["a"]) == ["a", "b", "d"]
    assert graph.bfs(["a"], expand, 1, 100).more_beyond_depth is True
    t = graph.bfs(["a"], expand, 5, 2)
    assert t.truncated is True and len(t.visited) == 2


def test_tarjan_and_depth_handle_cycles_and_deep_chains() -> None:
    comps = graph.find_cycles("abcde", [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("e", "e")])
    assert comps == [["a", "b", "c"], ["e"]]
    assert graph.find_cycles("abc", [("a", "b"), ("b", "c")]) == []
    assert graph.max_dependency_depth("abcd", [("a", "b"), ("b", "c"), ("c", "b"), ("c", "d")]) == 2
    n = 20_000  # iterative: must not hit the recursion limit
    names = [str(i) for i in range(n)]
    chain = [(str(i), str(i + 1)) for i in range(n - 1)]
    assert graph.find_cycles(names, chain) == []
    assert graph.max_dependency_depth(names, chain) == n - 1


def test_cycle_severity_rules() -> None:
    assert graph.cycle_severity(1, True)[0] == "info"
    assert graph.cycle_severity(2, False)[0] == "medium" and graph.cycle_severity(3, False)[0] == "medium"
    assert graph.cycle_severity(4, False)[0] == "high"


# ------------------------------------------------------------------ extraction

def test_call_extraction_and_dynamic_imports() -> None:
    src = (b"import importlib\n\n\nclass W:\n    def go(self):\n        return self.n(helper(1))\n\n"
           b"    def n(self, x):\n        return x\n\n\ndef helper(x):\n    return importlib.import_module(x)\n")
    res = extract_symbols("Python", src, "w.py")
    callees = {(c.caller_key, c.callee) for c in res.calls}
    assert any(c == "self.n" for _, c in callees) and any(c == "helper" for _, c in callees)
    assert not any(c == "importlib.import_module" for _, c in callees)  # recorded as a dynamic import instead
    dyn = [i for i in res.imports if i.kind == "dynamic"]
    assert len(dyn) == 1 and dyn[0].line == 13
    assert ANALYSIS_VERSION >= 2


def test_call_limit_is_bounded_and_reported() -> None:
    src = ("def f():\n    pass\n\n\n" + "".join("f()\n" for _ in range(5100))).encode()
    res = extract_symbols("Python", src, "many.py")
    assert len(res.calls) == 5000 and any("call limit" in d for d in res.diagnostics)


def test_syntax_error_regions_yield_no_guessed_calls() -> None:
    res = extract_symbols("Python", b"def ok():\n    helper()\n\ndef broken(:\n    nope(\n", "bad.py")
    assert res.status == "partial" and ("ok", "helper") in {(c.caller_key.split("#")[0] if c.caller_key else None, c.callee) for c in res.calls}


# ------------------------------------------------------------------ dependencies

def test_direct_and_transitive_dependencies(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    repo_id = chain_repo[0]
    one = deps(client, repo_id, path="app/a.py", depth=1)
    assert paths(one) == ["app/b.py"] and one["items"][0]["depth"] == 1
    assert one["items"][0]["relationship"] == "IMPORTS_FILE" and one["items"][0]["evidence"]
    assert one["more_beyond_depth"] is True
    three = deps(client, repo_id, path="app/a.py", depth=3)
    assert paths(three) == ["app/b.py", "app/c.py"]
    assert three["items"][1]["chain"] == ["app/a.py", "app/b.py", "app/c.py"]
    assert three["more_beyond_depth"] is False and three["truncated"] is False
    dependents = deps(client, repo_id, path="app/c.py", direction="in", depth=3)
    assert paths(dependents) == ["app/b.py", "app/a.py"]
    assert deps(client, repo_id, path="app/c.py", direction="out", depth=3)["items"] == []


def test_unresolved_and_dynamic_imports_are_listed_not_counted(client: TestClient,
                                                                chain_repo: tuple[str, Path]) -> None:
    body = deps(client, chain_repo[0], path="app/dyn.py", depth=2)
    assert body["items"] == []
    by_status = {u["status"] for u in body["unresolved"]}
    assert "unresolved" in by_status and "unsupported" in by_status
    dyn = [u for u in body["unresolved"] if u["kind"] == "dynamic"]
    assert len(dyn) == 1 and dyn[0]["status"] == "unsupported"
    assert body["unresolved_total"] >= 3


def test_dependency_pagination_and_validation(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    repo_id = chain_repo[0]
    page2 = deps(client, repo_id, path="app/a.py", depth=3, page=2, page_size=1)
    assert paths(page2) == ["app/c.py"] and page2["total"] == 2 and page2["pages"] == 2
    b = base(repo_id)
    for params in ({"path": "app/a.py", "depth": 0}, {"path": "app/a.py", "depth": 11},
                   {"path": "app/a.py", "direction": "sideways"}, {"path": "app/a.py", "page_size": 5000},
                   {"path": "app/a.py", "page": 0}):
        assert client.get(f"{b}/dependencies", params=params).status_code == 422
    assert client.get(f"{b}/dependencies").status_code == 400  # neither file_id nor path
    assert client.get(f"{b}/dependencies", params={"path": "app/a.py", "file_id": "x"}).status_code == 400
    assert client.get(f"{b}/dependencies", params={"path": "app/missing.py"}).status_code == 404
    assert client.get(f"{b}/dependencies", params={"file_id": "nope"}).status_code == 404


@pytest.mark.parametrize("bad", ["../secret.py", "app/../../x", "/etc/passwd", "C:\\Windows\\x", "a\x00b",
                                 "..\\x.py"])
def test_path_escape_is_rejected(client: TestClient, chain_repo: tuple[str, Path], bad: str) -> None:
    for url in ("dependencies", "dependencies/impact"):
        r = client.get(f"{base(chain_repo[0])}/{url}", params={"path": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_path", (url, bad, r.text)


# ------------------------------------------------------------------ impact

def impact(client: TestClient, repo_id: str, **params: object) -> dict:
    r = client.get(f"{base(repo_id)}/dependencies/impact", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_file_impact_multi_level(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    body = impact(client, chain_repo[0], path="app/c.py")
    assert [(i["path"], i["depth"]) for i in body["items"]] == [("app/b.py", 1), ("app/a.py", 2)]
    assert body["summary"] == {"direct": 1, "transitive": 1, "total": 2, "files_affected": 2,
                               "max_depth_reached": 2}
    assert "imports app/c.py directly" in body["items"][0]["explanation"]
    assert "2 steps" in body["items"][1]["explanation"]
    assert body["items"][1]["chain"] == ["app/a.py", "app/b.py", "app/c.py"]
    assert body["disclaimer"].startswith("Static analysis estimate") and body["cycles"] == []
    shallow = impact(client, chain_repo[0], path="app/c.py", max_depth=1)
    assert shallow["total"] == 1 and shallow["more_beyond_depth"] is True


def test_symbol_impact_follows_calls_and_inheritance(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    repo_id = chain_repo[0]
    syms = client.get(f"{base(repo_id)}/symbols", params={"q": "fc", "page_size": 50}).json()["items"]
    fc = next(s for s in syms if s["name"] == "fc")
    body = impact(client, repo_id, symbol_id=fc["stable_id"])
    names = [(i["name"].rsplit(".", 1)[-1], i["depth"], i["relationship"]) for i in body["items"]]
    assert ("fb", 1, "CALLS") in names and ("fa", 2, "CALLS") in names
    assert body["target_kind"] == "symbol" and body["items"][0]["evidence"] == "inferred"
    base_cls = next(s for s in client.get(f"{base(repo_id)}/symbols", params={"q": "Base"}).json()["items"]
                    if s["name"] == "Base")
    ext = impact(client, repo_id, symbol_id=base_cls["stable_id"])
    assert any(i["name"].endswith("Child") and i["relationship"] == "EXTENDS_SYMBOL" for i in ext["items"])
    assert client.get(f"{base(repo_id)}/dependencies/impact", params={"symbol_id": "0" * 32}).status_code == 404
    assert client.get(f"{base(repo_id)}/dependencies/impact",
                      params={"symbol_id": fc["stable_id"], "path": "app/a.py"}).status_code == 400


def test_symbol_detail(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    repo_id = chain_repo[0]
    items = client.get(f"{base(repo_id)}/symbols", params={"q": "Child", "page_size": 50}).json()["items"]
    child = next(s for s in items if s["name"] == "Child" and s["kind"] == "class")
    d = client.get(f"{base(repo_id)}/symbols/{child['stable_id']}").json()
    assert d["name"] == "Child" and [c["name"] for c in d["children"]] == ["n"]
    assert [b["name"] for b in d["base_links"]] == ["Base"] and d["base_links"][0]["status"] == "resolved"
    fc = next(s for s in client.get(f"{base(repo_id)}/symbols", params={"q": "fc"}).json()["items"]
              if s["name"] == "fc")
    fcd = client.get(f"{base(repo_id)}/symbols/{fc['stable_id']}").json()
    assert [c["name"] for c in fcd["callers"]] == ["fb"] and fcd["caller_total"] == 1
    assert client.get(f"{base(repo_id)}/symbols/{'f' * 32}").status_code == 404
    assert client.get(f"{base(repo_id)}/symbols/search", params={"q": "fc"}).status_code == 200  # no route clash


# ------------------------------------------------------------------ cycles

def test_cycles_detected_with_severity(client: TestClient, tmp_path: Path) -> None:
    repo_id = index(client, tmp_path / "cyc", CYCLE)
    body = client.get(f"{base(repo_id)}/dependencies/cycles").json()
    sizes = sorted(c["size"] for c in body["items"])
    assert sizes == [1, 3] and body["total"] == 2
    three = next(c for c in body["items"] if c["size"] == 3)
    assert three["severity"] == "medium" and three["kind"] == "structural_dependency_cycle"
    assert {f["path"] for f in three["files"]} == {"cyc/x.py", "cyc/y.py", "cyc/z.py"}
    assert len(three["edges"]) == 3
    assert next(c for c in body["items"] if c["size"] == 1)["severity"] == "info"
    assert "not proof of a runtime failure" in body["note"] and set(body["severity_rules"]) == {"info", "medium", "high"}
    only = client.get(f"{base(repo_id)}/dependencies/cycles", params={"severity": "medium"}).json()
    assert only["total"] == 1
    assert client.get(f"{base(repo_id)}/dependencies/cycles", params={"severity": "bad"}).status_code == 422
    imp = impact(client, repo_id, path="cyc/x.py")
    assert imp["cycles"] and imp["cycles"][0]["size"] == 3  # traversal terminated and warned
    assert {i["path"] for i in imp["items"]} == {"cyc/y.py", "cyc/z.py"}


def test_no_cycles(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    body = client.get(f"{base(chain_repo[0])}/dependencies/cycles").json()
    assert body["items"] == [] and body["total"] == 0 and body["pages"] == 1


# ------------------------------------------------------------------ incremental behaviour

def edge_set(client: TestClient, repo_id: str, path: str) -> list[str]:
    return paths(deps(client, repo_id, path=path, depth=5))


def test_add_modify_delete_and_idempotent_reindex(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    repo_id, root = chain_repo
    before = (edge_set(client, repo_id, "app/a.py"), client.get(f"{base(repo_id)}/metrics").json())
    again = reindex(client, repo_id)  # unchanged tree
    assert again["files_new"] == 0 and again["files_changed"] == 0
    after = client.get(f"{base(repo_id)}/metrics").json()
    for k in ("files", "symbols", "edges_by_type", "imports_by_status", "calls_by_status", "cycles"):
        assert before[1][k] == after[k], k
    assert edge_set(client, repo_id, "app/a.py") == before[0]
    assert after["index"]["generation"] == before[1]["index"]["generation"] + 1

    write(root, {"app/e.py": "def fe():\n    return 1\n"})  # add
    write(root, {"app/a.py": "from app.e import fe\n\n\ndef fa():\n    return fe()\n"})  # modify
    snap = reindex(client, repo_id)
    assert snap["files_new"] == 1 and snap["files_changed"] == 1
    assert edge_set(client, repo_id, "app/a.py") == ["app/e.py"]
    assert paths(impact(client, repo_id, path="app/c.py")) == ["app/b.py"]  # a.py no longer reaches c.py

    (root / "app" / "e.py").unlink()  # delete
    reindex(client, repo_id)
    body = deps(client, repo_id, path="app/a.py", depth=3)
    assert body["items"] == [] and any(u["status"] == "unresolved" for u in body["unresolved"])


def test_failed_run_reports_and_keeps_previous_generation(client: TestClient, chain_repo: tuple[str, Path],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    repo_id = chain_repo[0]
    from app.services.intelligence import pipeline

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(pipeline._Run, "resolve_all", boom)
    snap = reindex(client, repo_id)
    assert snap["status"] == "failed"
    status = client.get(f"{base(repo_id)}/index/status").json()
    assert status["state"] == "failed" and status["serving"]["status"] == "completed"
    assert status["last_failure"] and "previous generation" in status["message"]
    assert status["serving"]["generation"] == 1
    assert paths(deps(client, repo_id, path="app/a.py", depth=1)) == ["app/b.py"]  # old index still answers
    monkeypatch.undo()
    assert reindex(client, repo_id)["status"] in ("completed", "completed_with_warnings")  # recoverable


def test_rebuild_endpoint_and_conflict(client: TestClient, chain_repo: tuple[str, Path],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo_id = chain_repo[0]
    from app.services.intelligence import pipeline

    calls: list[str] = []
    real = pipeline.extract_symbols
    monkeypatch.setattr(pipeline, "extract_symbols", lambda lang, data, rel, *a: (calls.append(rel), real(lang, data, rel, *a))[1])
    assert reindex(client, repo_id)["status"] == "completed" and calls == []  # incremental: all reused
    r = client.post(f"{base(repo_id)}/index/rebuild")
    assert r.status_code == 202 and r.json()["mode"] == "full"
    snap = client.get(f"{base(repo_id)}/snapshots/{r.json()['id']}").json()
    assert snap["status"] in ("completed", "completed_with_warnings")
    assert snap["generation"] == 3 and len(calls) == 5  # every source file was parsed again
    assert client.post(f"{base('nope')}/index/rebuild").status_code == 404


def test_index_status_states(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "st"
    write(root, {"a.py": "x = 1\n"})
    repo_id = client.post(f"{API}/repositories", json={"name": "s", "root_path": str(root)}).json()["id"]
    st = client.get(f"{base(repo_id)}/index/status").json()
    assert st["state"] == "never_indexed" and st["serving"] is None and st["latest"] is None
    client.post(f"{base(repo_id)}/index")
    st = client.get(f"{base(repo_id)}/index/status", params={"include_pending": True}).json()
    assert st["state"] == "completed" and st["serving"]["generation"] == 1
    assert st["files_awaiting_indexing"] == 0 and st["analysis_version"] == ANALYSIS_VERSION
    time.sleep(0.01)
    write(root, {"b.py": "y = 2\n", "a.py": "x = 22\n"})
    st = client.get(f"{base(repo_id)}/index/status", params={"include_pending": True}).json()
    assert st["files_awaiting_indexing"] == 2 and st["pending"]["state"] == "stale"


# ------------------------------------------------------------------ metrics / empty / auth

def test_metrics_values_and_definitions(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    m = client.get(f"{base(chain_repo[0])}/metrics").json()
    assert m["indexed"] is True and m["files"] == 5 and m["cycles"] == 0
    assert m["edges_by_type"]["IMPORTS_FILE"] == 2 and m["edges_by_type"]["CALLS"] >= 4
    assert m["edges_by_type"]["EXTENDS_SYMBOL"] == 1
    assert m["calls_by_status"]["resolved"] >= 4 and m["imports_by_status"]["unsupported"] == 1
    assert m["max_dependency_depth"] == 2 and m["parse_failures"] == 0 and m["symbols_by_kind"]["class"] == 2
    assert set(m["definitions"]) >= {"files", "symbols", "cycles", "max_dependency_depth", "edges_by_type"}
    assert m["files_awaiting_indexing"] is None


def test_empty_repository(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    repo_id = register_and_index(client, root, "empty")["repo"]["id"]
    m = client.get(f"{base(repo_id)}/metrics").json()
    assert m["files"] == 0 and m["symbols"] == 0 and m["cycles"] == 0 and m["max_dependency_depth"] == 0
    assert client.get(f"{base(repo_id)}/dependencies/cycles").json()["items"] == []
    assert client.get(f"{base(repo_id)}/dependencies", params={"path": "x.py"}).status_code == 404


def test_never_indexed_endpoints(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "never"
    write(root, {"a.py": "x = 1\n"})
    repo_id = client.post(f"{API}/repositories", json={"name": "n", "root_path": str(root)}).json()["id"]
    m = client.get(f"{base(repo_id)}/metrics").json()
    assert m["indexed"] is False and m["definitions"]
    assert client.get(f"{base(repo_id)}/dependencies/cycles").json()["items"] == []
    assert client.get(f"{base(repo_id)}/dependencies", params={"path": "a.py"}).status_code == 404


def test_graph_endpoints_require_token(migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    root = tmp_path / "t"
    write(root, {"a.py": "x = 1\n"})
    with TestClient(create_app()) as c:
        auth = {"Authorization": "Bearer " + "t" * 32}
        repo_id = c.post(f"{API}/repositories", json={"name": "t", "root_path": str(root)}, headers=auth).json()["id"]
        b = base(repo_id)
        for url in ("/dependencies?path=a.py", "/dependencies/impact?path=a.py", "/dependencies/cycles",
                    "/symbols/" + "a" * 32, "/index/status", "/metrics"):
            assert c.get(b + url).status_code == 401, url
        assert c.post(b + "/index/rebuild").status_code == 401
        assert c.get(b + "/index/status", headers=auth).status_code == 200
        assert c.get(b + "/metrics", headers=auth).status_code == 200
        assert c.post(b + "/index/rebuild", headers=auth).status_code == 202


def test_other_workspace_cannot_read_graph(client: TestClient, chain_repo: tuple[str, Path]) -> None:
    b = base("0" * 32)
    for url in ("/dependencies?path=a.py", "/dependencies/cycles", "/index/status", "/metrics"):
        assert client.get(b + url).status_code == 404


# ------------------------------------------------------------------ larger graphs

def test_large_chain_is_bounded_and_fast(client: TestClient, tmp_path: Path) -> None:
    n = 150
    files = {"big/__init__.py": ""}
    for i in range(n):
        files[f"big/m{i}.py"] = f"from big import m{i + 1}\n" if i < n - 1 else "X = 1\n"
    repo_id = index(client, tmp_path / "big", files, "big")
    t = time.perf_counter()
    deep = deps(client, repo_id, path="big/m0.py", depth=10, page_size=200)
    got = set(paths(deep))
    assert {f"big/m{i}.py" for i in range(1, 11)} <= got and "big/m11.py" not in got
    assert max(i["depth"] for i in deep["items"]) == 10 and deep["more_beyond_depth"] is True
    inc = impact(client, repo_id, path=f"big/m{n - 1}.py", max_depth=10)
    assert inc["summary"]["max_depth_reached"] == 10 and inc["more_beyond_depth"] is True
    m = client.get(f"{base(repo_id)}/metrics").json()
    assert m["max_dependency_depth"] == n - 1 and m["cycles"] == 0
    assert time.perf_counter() - t < 10


def test_traversal_node_cap_marks_truncated(client: TestClient, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.intelligence import graph_queries

    files = {"hub/__init__.py": "", "hub/core.py": "X = 1\n"}
    for i in range(30):
        files[f"hub/u{i}.py"] = "from hub import core\n"
    repo_id = index(client, tmp_path / "hub", files, "hub")
    monkeypatch.setattr(graph_queries, "MAX_TRAVERSAL_NODES", 10)
    body = impact(client, repo_id, path="hub/core.py")
    assert body["truncated"] is True and body["total"] == 10


def test_single_row_graph_load_matches_row_by_row_and_respects_the_cap(client: TestClient, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    # file_edges loads the whole graph in ONE SQLite row (see graph_queries: the per-row GIL hand-off collapsed throughput under
    # concurrency). It must return exactly what the row-by-row query returns, and the edge cap must still apply.
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models import IndexSnapshot
    from app.services.intelligence import graph_queries as gq

    repo_id = index(client, tmp_path / "cyc", CYCLE)
    with get_session_factory()() as db:
        sid = db.scalar(select(IndexSnapshot.id).where(IndexSnapshot.repository_id == repo_id, IndexSnapshot.status.like("completed%")))
        nodes, edges, capped = gq.file_edges(db, sid)
        monkeypatch.setattr(gq, "_single_row", lambda _db: False)
        r_nodes, r_edges, r_capped = gq.file_edges(db, sid)
        assert sorted(nodes) == sorted(r_nodes) and sorted(edges) == sorted(r_edges) and capped is r_capped is False
        assert len(edges) >= 4 and all(len(a) == 32 and len(b) == 32 for a, b in edges)
        monkeypatch.setattr(gq, "_single_row", lambda _db: True)
        monkeypatch.setattr(gq, "MAX_EDGE_ROWS", 2)
        _, few, capped = gq.file_edges(db, sid)
        assert capped is True and len(few) == 2
