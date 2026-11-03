from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register_and_index
from tests.test_repository_intelligence import FILES, files_by_path, reindex, write

API = "/api/v1"

SEARCH_FILES = {
    "a.py": "class Order:\n    pass\n\n\nclass OrderItem:\n    pass\n\n\ndef reorder():\n    pass\n",
    "b.py": "def Order():\n    pass\n\n\ndef parse_order_line():\n    pass\n",
    "secrets_here.py": 'API_TOKEN = "super-secret-token-value-123"\n\n\ndef connect():\n    return "hunter2-password"\n',
}


@pytest.fixture
def search_repo(client: TestClient, tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "search"
    write(root, SEARCH_FILES)
    return register_and_index(client, root)["repo"]["id"], root


def search(client: TestClient, repo_id: str, q: str, **params: object) -> dict:
    r = client.get(f"{API}/repositories/{repo_id}/symbols/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_ranking_and_explanations(client: TestClient, search_repo: tuple[str, Path]) -> None:
    body = search(client, search_repo[0], "order")
    matched = [(h["name"], h["matched_on"]) for h in body["items"]]
    assert [m for _, m in matched[:2]] == ["exact_name", "exact_name"]  # class Order and def Order
    order = [m for _, m in matched]
    assert order.index("name_prefix") < order.index("name_substring")
    assert body["ambiguous"] is True  # two distinct symbols are named exactly "Order"
    assert body["index"]["generation"] == 1 and body["index"]["status"] == "completed"
    hit = body["items"][0]
    assert hit["file_path"] in ("a.py", "b.py") and hit["start_line"] >= 1 and hit["parser"] == "tree-sitter-python"


def test_search_filters_and_not_ambiguous(client: TestClient, search_repo: tuple[str, Path]) -> None:
    repo_id = search_repo[0]
    only_classes = search(client, repo_id, "order", kind="class")
    assert {h["kind"] for h in only_classes["items"]} == {"class"}
    assert search(client, repo_id, "reorder")["ambiguous"] is False
    assert search(client, repo_id, "zzz-no-match")["total"] == 0


def test_search_treats_wildcards_literally(client: TestClient, search_repo: tuple[str, Path]) -> None:
    assert search(client, search_repo[0], "%")["total"] == 0
    assert search(client, search_repo[0], "o_d")["total"] == 0


def test_invalid_input_is_rejected(client: TestClient, search_repo: tuple[str, Path]) -> None:
    base = f"{API}/repositories/{search_repo[0]}"
    assert client.get(f"{base}/symbols/search").status_code == 422
    assert client.get(f"{base}/symbols/search", params={"q": ""}).status_code == 422
    assert client.get(f"{base}/symbols/search", params={"q": "x" * 500}).status_code == 422
    assert client.get(f"{base}/symbols", params={"page_size": 5000}).status_code == 422
    assert client.get(f"{base}/files", params={"analysis_status": "bogus"}).status_code == 422
    assert client.get(f"{base}/snapshots/nope/diagnostics").status_code == 404
    assert client.get(f"{base}/files/nope").status_code == 404


def test_symbol_listing_pagination_and_filters(client: TestClient, search_repo: tuple[str, Path]) -> None:
    base = f"{API}/repositories/{search_repo[0]}/symbols"
    p1 = client.get(base, params={"page_size": 3}).json()
    assert p1["total"] >= 6 and len(p1["items"]) == 3 and p1["pages"] >= 2
    p2 = client.get(base, params={"page_size": 3, "page": 2}).json()
    assert {s["id"] for s in p1["items"]}.isdisjoint({s["id"] for s in p2["items"]})
    funcs = client.get(base, params={"kind": "function"}).json()["items"]
    assert funcs and all(s["kind"] == "function" for s in funcs)


def test_file_detail_and_filters(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root, {**FILES, "bad.py": "def broken(:\n    pass\n"})
    repo_id = register_and_index(client, root)["repo"]["id"]
    files = files_by_path(client, repo_id)
    detail = client.get(f"{API}/repositories/{repo_id}/files/{files['pkg/service.py']['id']}").json()
    assert detail["analysis_status"] == "parsed" and detail["import_count"] == 5 and detail["dependency_count"] > 0
    bad = client.get(f"{API}/repositories/{repo_id}/files", params={"analysis_status": "partial"}).json()
    assert [f["rel_path"] for f in bad["items"]] == ["bad.py"]
    bad_detail = client.get(f"{API}/repositories/{repo_id}/files/{bad['items'][0]['id']}").json()
    assert bad_detail["parse_diagnostics"]
    assert "sha256" in bad_detail and "content" not in bad_detail


def test_old_generation_file_ids_are_not_served(client: TestClient, search_repo: tuple[str, Path]) -> None:
    repo_id = search_repo[0]
    old = files_by_path(client, repo_id)["a.py"]["id"]
    reindex(client, repo_id)
    assert client.get(f"{API}/repositories/{repo_id}/files/{old}").status_code == 404


def test_diagnostics_report_reasons_without_leaking_secret_names(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "d"
    write(root, {"ok.py": "x = 1\n", "big.txt": "a" * 3_000_000})
    (root / ".env").write_text("TOKEN=abc")
    (root / "img.png").write_bytes(b"\x89PNG\x00")
    (root / "latin.py").write_bytes("x = 'caf\xe9'\n".encode("latin-1"))
    out = register_and_index(client, root)
    repo_id, snap = out["repo"]["id"], out["snapshot"]
    body = client.get(f"{API}/repositories/{repo_id}/snapshots/{snap['id']}/diagnostics").json()
    codes = {d["code"] for d in body["items"]}
    assert {"SECRET_LIKE_PATH", "BINARY_FILE", "FILE_TOO_LARGE", "UNSUPPORTED_ENCODING"} <= codes
    secret = next(d for d in body["items"] if d["code"] == "SECRET_LIKE_PATH")
    assert secret["rel_path"] is None
    assert ".env" not in json.dumps(body)
    only = client.get(f"{API}/repositories/{repo_id}/snapshots/{snap['id']}/diagnostics",
                      params={"code": "BINARY_FILE"}).json()
    assert [d["rel_path"] for d in only["items"]] == ["img.png"]


def test_freshness_states(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "f"
    write(root, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    created = client.post(f"{API}/repositories", json={"name": "f", "root_path": str(root)}).json()
    repo_id = created["id"]
    assert client.get(f"{API}/repositories/{repo_id}/freshness").json()["state"] == "never_indexed"
    reindex(client, repo_id)
    fresh = client.get(f"{API}/repositories/{repo_id}/freshness").json()
    assert fresh["state"] == "fresh" and fresh["changed"] == fresh["new"] == fresh["removed"] == 0
    (root / "a.py").write_text("x = 100000\n")
    os.utime(root / "a.py", (time.time() + 5, time.time() + 5))
    (root / "c.py").write_text("z = 3\n")
    (root / "b.py").unlink()
    stale = client.get(f"{API}/repositories/{repo_id}/freshness").json()
    assert stale["state"] == "stale" and (stale["changed"], stale["new"], stale["removed"]) == (1, 1, 1)
    assert set(stale["sample_paths"]) == {"a.py", "c.py", "b.py"}
    reindex(client, repo_id)
    assert client.get(f"{API}/repositories/{repo_id}/freshness").json()["state"] == "fresh"


def test_empty_repository_and_unindexed_queries(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    repo_id = client.post(f"{API}/repositories", json={"name": "e", "root_path": str(root)}).json()["id"]
    base = f"{API}/repositories/{repo_id}"
    for url in ("/symbols", "/files"):
        assert client.get(base + url).json()["total"] == 0
    assert search(client, repo_id, "x")["items"] == []
    snap = reindex(client, repo_id)
    assert snap["status"] == "completed" and snap["files_indexed"] == 0
    assert client.get(f"{base}/summary").json()["indexed"] is True


def test_capabilities_are_honest(client: TestClient) -> None:
    caps = client.get(f"{API}/capabilities").json()
    by = {c["language"]: c for c in caps["languages"]}
    assert {"Python", "JavaScript", "JSX", "TypeScript", "TSX"} <= set(by)
    assert by["Python"]["status"] == "structural" and by["Python"]["semantic_supported"] is False
    assert "CALLS" in by["Python"]["relationships"]  # name-based, inferred; still no type resolution
    assert "CALLS" not in caps["unsupported_relationships"] and "type resolution" in caps["unsupported_relationships"]
    assert caps["analysis_version"] >= 1 and caps["limits"]["max_files"] > 0


def test_summary_reports_symbol_kinds_and_state(client: TestClient, search_repo: tuple[str, Path]) -> None:
    s = client.get(f"{API}/repositories/{search_repo[0]}/summary").json()
    assert s["index_state"] == "ready" and dict(s["symbol_kinds"])["class"] == 2
    assert s["snapshot"]["analysis_counts"]["parsed"] == 3


def test_no_source_or_secret_values_in_any_response(client: TestClient, search_repo: tuple[str, Path]) -> None:
    repo_id = search_repo[0]
    base = f"{API}/repositories/{repo_id}"
    blobs = [client.get(f"{base}/symbols", params={"page_size": 200}).text, client.get(f"{base}/files").text,
             client.get(f"{base}/summary").text, search(client, repo_id, "connect") and
             client.get(f"{base}/symbols/search", params={"q": "token"}).text]
    files = files_by_path(client, repo_id)
    for f in files.values():
        for part in ("", "/imports", "/dependencies", "/dependents"):
            blobs.append(client.get(f"{base}/files/{f['id']}{part}").text)
    joined = "\n".join(blobs)
    assert "super-secret-token-value-123" not in joined and "hunter2-password" not in joined


def test_intelligence_endpoints_require_token_when_configured(migrated_url: str, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    root = tmp_path / "t"
    write(root, {"a.py": "x = 1\n"})
    with TestClient(create_app()) as c:
        auth = {"Authorization": "Bearer " + "t" * 32}
        repo_id = c.post(f"{API}/repositories", json={"name": "t", "root_path": str(root)}, headers=auth).json()["id"]
        for url in ("/symbols", "/symbols/search?q=x", "/freshness", "/snapshots"):
            assert c.get(f"{API}/repositories/{repo_id}{url}").status_code == 401
            assert c.get(f"{API}/repositories/{repo_id}{url}", headers=auth).status_code == 200
        assert c.get(f"{API}/capabilities").status_code == 401
        assert c.get(f"{API}/capabilities", headers=auth).status_code == 200
