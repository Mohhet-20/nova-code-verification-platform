from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register_and_index

API = "/api/v1"


def task_payload(repo_id: str, **over) -> dict:
    base = {"repository_id": repo_id, "title": "Harden login session handling",
            "problem_statement": "Sessions expire unexpectedly in the auth module.",
            "objective": "Make session expiry configurable.",
            "scope": ["src/auth only"], "acceptance_criteria": ["Expiry is configurable", "Tests added"]}
    base.update(over)
    return base


def test_create_task_generates_structured_plan(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    r = client.post(f"{API}/tasks", json=task_payload(repo_id))
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["status"] == "planned" and task["repository_name"] == "sample"
    plan = task["plan"]
    assert plan["generator"] == "deterministic-baseline" and plan["revision"] == 1
    assert plan["edited_by_user"] is False and plan["snapshot_id"]
    content = plan["content"]
    assert any(p.startswith("src/auth/") for p in content["relevant_files"])
    assert "tests/test_login.py" in content["relevant_files"]
    assert {c["suggested_command"] for c in content["expected_checks"]} >= {"python -m pytest", "npm test"}
    assert all(c["executed"] is False for c in content["expected_checks"])
    assert [s["kind"] for s in content["steps"]][-1] == "review"
    assert "deterministic" in content["summary"].lower()


def test_create_task_for_unindexed_repo_warns(client: TestClient, sample_repo: Path) -> None:
    repo = client.post(f"{API}/repositories", json={"name": "n", "root_path": str(sample_repo)}).json()
    plan = client.post(f"{API}/tasks", json=task_payload(repo["id"])).json()["plan"]
    assert plan["snapshot_id"] is None and plan["content"]["confidence"] == "low"
    assert plan["content"]["warnings"]


@pytest.mark.parametrize("override", [
    {"title": "ab"}, {"title": " " * 5}, {"problem_statement": "short"}, {"objective": "x"},
    {"acceptance_criteria": []}, {"acceptance_criteria": ["   "]}, {"title": "t" * 201},
    {"scope": ["s"] * 31}, {"repository_id": ""},
])
def test_task_validation(client: TestClient, sample_repo: Path, override: dict) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    r = client.post(f"{API}/tasks", json=task_payload(repo_id, **override))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_task_for_unknown_repository(client: TestClient) -> None:
    r = client.post(f"{API}/tasks", json=task_payload("missing"))
    assert r.status_code == 404


def test_list_get_and_pagination(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    ids = [client.post(f"{API}/tasks", json=task_payload(repo_id, title=f"Task number {i}")).json()["id"]
           for i in range(5)]
    page = client.get(f"{API}/tasks", params={"page_size": 2, "page": 3}).json()
    assert page["total"] == 5 and page["pages"] == 3 and len(page["items"]) == 1
    assert "plan" not in page["items"][0]
    listed = client.get(f"{API}/tasks", params={"page_size": 100}).json()["items"]
    assert {t["id"] for t in listed} == set(ids)
    assert client.get(f"{API}/tasks/{ids[0]}").json()["plan"]["content"]["steps"]
    assert client.get(f"{API}/tasks", params={"repository_id": "other"}).json()["total"] == 0
    assert client.get(f"{API}/tasks/missing").status_code == 404


def test_plan_is_editable_and_regenerable(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    task = client.post(f"{API}/tasks", json=task_payload(repo_id)).json()
    content = task["plan"]["content"]
    content["steps"][0]["title"] = "My edited first step"
    edited = client.put(f"{API}/tasks/{task['id']}/plan", json={"content": content})
    assert edited.status_code == 200
    p = edited.json()["plan"]
    assert p["edited_by_user"] and p["revision"] == 2
    assert p["content"]["steps"][0]["title"] == "My edited first step"
    assert client.get(f"{API}/tasks/{task['id']}").json()["plan"]["content"]["steps"][0]["title"] == \
        "My edited first step"
    regen = client.post(f"{API}/tasks/{task['id']}/plan/regenerate").json()["plan"]
    assert regen["revision"] == 3 and not regen["edited_by_user"]
    assert regen["content"]["steps"][0]["title"] != "My edited first step"


def test_invalid_plan_edit_rejected(client: TestClient, sample_repo: Path) -> None:
    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    task = client.post(f"{API}/tasks", json=task_payload(repo_id)).json()
    bad = copy.deepcopy(task["plan"]["content"])
    bad["steps"] = []
    assert client.put(f"{API}/tasks/{task['id']}/plan", json={"content": bad}).status_code == 422
    bad2 = copy.deepcopy(task["plan"]["content"])
    bad2["steps"] = [{"id": "s1", "kind": "execute", "title": "t", "description": ""}]
    assert client.put(f"{API}/tasks/{task['id']}/plan", json={"content": bad2}).status_code == 422
    assert client.put(f"{API}/tasks/missing/plan", json={"content": task["plan"]["content"]}).status_code == 404


def test_planning_never_modifies_repository(client: TestClient, sample_repo: Path) -> None:
    def snapshot() -> dict[str, tuple[int, float]]:
        return {str(p.relative_to(sample_repo)): (p.stat().st_size, p.stat().st_mtime)
                for p in sample_repo.rglob("*") if p.is_file()}

    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    before = snapshot()
    client.post(f"{API}/tasks", json=task_payload(repo_id))
    assert snapshot() == before


def test_unknown_ai_provider_is_rejected(client: TestClient, sample_repo: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache

    repo_id = register_and_index(client, sample_repo)["repo"]["id"]
    monkeypatch.setenv("NOVA_AI_PROVIDER", "openai")
    reset_settings_cache()
    r = client.post(f"{API}/tasks", json=task_payload(repo_id))
    assert r.status_code == 400 and "not implemented" in r.json()["error"]["message"]
