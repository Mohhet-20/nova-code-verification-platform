from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register_and_index

API = "/api/v1"


def test_workspace_context(client: TestClient) -> None:
    body = client.get(f"{API}/workspace").json()
    assert body["role"] == "owner" and body["auth_mode"] == "local-trust"
    assert body["multi_user_isolation"] is False and body["planner"] == "deterministic-baseline"
    assert body["limits"]["max_files"] > 0


def test_register_index_summary_flow(client: TestClient, sample_repo: Path) -> None:
    out = register_and_index(client, sample_repo)
    repo, snap = out["repo"], out["snapshot"]
    assert repo["root_path"] == str(sample_repo.resolve())
    assert snap["status"] == "completed" and snap["files_indexed"] == 10
    assert snap["skipped_counts"]["SECRET_LIKE_PATH"] == 2
    assert snap["language_counts"]["Python"] == 3
    summary = client.get(f"{API}/repositories/{repo['id']}/summary").json()
    assert summary["indexed"] and summary["top_languages"][0][0] == "Python"
    assert len(summary["largest_files"]) == 5
    detail = client.get(f"{API}/repositories/{repo['id']}").json()
    assert detail["latest_snapshot"]["id"] == snap["id"]


def test_git_metadata_recorded(client: TestClient, git_repo: Path) -> None:
    repo = register_and_index(client, git_repo)["repo"]
    assert repo["is_git"] and repo["git_branch"] == "main" and len(repo["git_head"]) == 40


def test_list_does_not_expose_full_paths(client: TestClient, sample_repo: Path) -> None:
    register_and_index(client, sample_repo)
    page = client.get(f"{API}/repositories").json()
    item = page["items"][0]
    assert item["root_label"] == "sample-repo" and "root_path" not in item
    assert str(sample_repo.parent) not in client.get(f"{API}/repositories").text
    assert item["files_indexed"] == 10 and item["latest_snapshot_status"] == "completed"


def test_duplicate_registration_conflicts(client: TestClient, sample_repo: Path) -> None:
    register_and_index(client, sample_repo)
    r = client.post(f"{API}/repositories", json={"name": "again", "root_path": str(sample_repo)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize("payload,status", [
    ({"name": "x", "root_path": "/definitely/not/here"}, 422),
    ({"name": "x", "root_path": "relative/dir"}, 422),
    ({"name": "", "root_path": "/tmp"}, 422),
    ({"name": "x"}, 422),
    ({"name": "x", "root_path": "/"}, 422),
])
def test_invalid_registration(client: TestClient, payload: dict, status: int) -> None:
    r = client.post(f"{API}/repositories", json=payload)
    assert r.status_code == status
    assert "error" in r.json()


def test_file_root_rejected(client: TestClient, sample_repo: Path) -> None:
    r = client.post(f"{API}/repositories",
                    json={"name": "f", "root_path": str(sample_repo / "README.md")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_repository"


def test_registration_base_dirs_enforced(migrated_url: str, sample_repo: Path, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    allowed = tmp_path / "only-here"
    allowed.mkdir()
    monkeypatch.setenv("NOVA_REGISTRATION_BASE_DIRS", str(allowed))
    reset_settings_cache()
    with TestClient(create_app()) as c:
        r = c.post(f"{API}/repositories", json={"name": "x", "root_path": str(sample_repo)})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_path"
        assert c.get(f"{API}/workspace").json()["registration_restricted"] is True


def test_file_inventory_pagination_and_filters(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    base = f"{API}/repositories/{repo_id}/files"
    p1 = client.get(base, params={"page": 1, "page_size": 4}).json()
    p3 = client.get(base, params={"page": 3, "page_size": 4}).json()
    assert p1["total"] == 10 and p1["pages"] == 3 and len(p1["items"]) == 4 and len(p3["items"]) == 2
    paths = [f["rel_path"] for f in p1["items"]]
    assert paths == sorted(paths)
    py = client.get(base, params={"language": "Python"}).json()
    assert py["total"] == 3
    q = client.get(base, params={"q": "AUTH"}).json()
    assert {f["rel_path"] for f in q["items"]} == {"src/auth/login.py", "src/auth/session.py"}
    assert client.get(base, params={"q": "%"}).json()["total"] == 0  # wildcard is escaped
    assert client.get(base, params={"page_size": 500}).status_code == 422
    assert client.get(base, params={"page": 0}).status_code == 422


def test_files_endpoint_exposes_metadata_only(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    text = client.get(f"{API}/repositories/{repo_id}/files").text
    assert "def login" not in text and "content" not in text and "sha256" not in text


def test_files_empty_before_indexing(client: TestClient, sample_repo: Path) -> None:
    repo = client.post(f"{API}/repositories", json={"name": "n", "root_path": str(sample_repo)}).json()
    assert client.get(f"{API}/repositories/{repo['id']}/files").json()["total"] == 0
    assert client.get(f"{API}/repositories/{repo['id']}/summary").json()["indexed"] is False


def test_reindex_replaces_old_file_rows(client: TestClient, sample_repo: Path) -> None:
    out = register_and_index(client, sample_repo)
    repo_id = out["repo"]["id"]
    (sample_repo / "src" / "new_module.py").write_text("x = 1\n")
    s2 = client.post(f"{API}/repositories/{repo_id}/index").json()
    done = client.get(f"{API}/repositories/{repo_id}/snapshots/{s2['id']}").json()
    assert done["files_indexed"] == 11
    assert client.get(f"{API}/repositories/{repo_id}/files").json()["total"] == 11


def test_interrupted_indexing_is_recovered_on_startup(migrated_url: str, sample_repo: Path) -> None:
    from app.db.session import get_session_factory
    from app.main import create_app
    from app.models import IndexSnapshot

    with TestClient(create_app()) as c:
        repo_id = c.post(f"{API}/repositories", json={"name": "n", "root_path": str(sample_repo)}).json()["id"]
        with get_session_factory()() as s:
            s.add(IndexSnapshot(id="stale", repository_id=repo_id, status="running"))
            s.commit()
    with TestClient(create_app()) as c:  # simulated restart
        snap = c.get(f"{API}/repositories/{repo_id}/snapshots/stale").json()
        assert snap["status"] == "failed" and "Interrupted" in snap["error_message"]
        assert c.post(f"{API}/repositories/{repo_id}/index").status_code == 202


def test_concurrent_index_conflict(client: TestClient, sample_repo: Path, migrated_url: str) -> None:
    from app.db.session import get_session_factory
    from app.models import IndexSnapshot

    repo_id = client.post(f"{API}/repositories",
                          json={"name": "n", "root_path": str(sample_repo)}).json()["id"]
    with get_session_factory()() as s:
        s.add(IndexSnapshot(repository_id=repo_id, status="running"))
        s.commit()
    r = client.post(f"{API}/repositories/{repo_id}/index")
    assert r.status_code == 409


def test_index_failure_is_recorded(client: TestClient, sample_repo: Path) -> None:
    import shutil

    repo_id = client.post(f"{API}/repositories",
                          json={"name": "n", "root_path": str(sample_repo)}).json()["id"]
    shutil.rmtree(sample_repo)
    snap = client.post(f"{API}/repositories/{repo_id}/index").json()
    done = client.get(f"{API}/repositories/{repo_id}/snapshots/{snap['id']}").json()
    assert done["status"] == "failed" and "does not exist" in done["error_message"]


def test_truncation_reported(migrated_url: str, sample_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("NOVA_INDEX_MAX_FILES", "3")
    reset_settings_cache()
    with TestClient(create_app()) as c:
        snap = register_and_index(c, sample_repo)["snapshot"]
    assert snap["truncated"] and snap["files_indexed"] == 3 and snap["status"] == "completed_with_warnings"
    assert snap["limits"]["max_files"] == 3


def test_not_found_resources(client: TestClient) -> None:
    for url in (f"{API}/repositories/nope", f"{API}/repositories/nope/summary",
                f"{API}/repositories/nope/files", f"{API}/repositories/nope/snapshots/x"):
        r = client.get(url)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.post(f"{API}/repositories/nope/index").status_code == 404


def test_cross_repository_snapshot_not_readable(client: TestClient, sample_repo: Path,
                                                tmp_path: Path) -> None:
    a = register_and_index(client, sample_repo)
    other = tmp_path / "other"
    other.mkdir()
    b = client.post(f"{API}/repositories", json={"name": "b", "root_path": str(other)}).json()
    r = client.get(f"{API}/repositories/{b['id']}/snapshots/{a['snapshot']['id']}")
    assert r.status_code == 404


def test_client_supplied_paths_cannot_read_files(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    for probe in ("../../etc/passwd", "/etc/passwd", "src/auth/login.py"):
        r = client.get(f"{API}/repositories/{repo_id}/files/{probe}")
        assert r.status_code in (404, 405)


def test_overview_counts_are_real(client: TestClient, sample_repo: Path) -> None:
    empty = client.get(f"{API}/overview").json()
    assert empty["repositories"] == 0 and empty["tasks_total"] == 0 and empty["recent_events"] == []
    register_and_index(client, sample_repo)
    ov = client.get(f"{API}/overview").json()
    assert ov["repositories"] == 1 and ov["indexed_repositories"] == 1 and ov["indexed_files"] == 10
    actions = [e["action"] for e in ov["recent_events"]]
    assert "index.completed" in actions and "repository.registered" in actions
