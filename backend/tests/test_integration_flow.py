"""End-to-end API flow on a synthetic repository (the Phase 01 acceptance scenario)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

API = "/api/v1"


def test_register_index_summarize_task_plan(client: TestClient, git_repo: Path) -> None:
    assert client.get(f"{API}/ready").json()["status"] == "ready"
    repo = client.post(f"{API}/repositories", json={
        "name": "synthetic", "root_path": str(git_repo), "description": "fixture"}).json()
    snap = client.post(f"{API}/repositories/{repo['id']}/index").json()
    snap = client.get(f"{API}/repositories/{repo['id']}/snapshots/{snap['id']}").json()
    assert snap["status"] == "completed" and snap["git_branch"] == "main"

    summary = client.get(f"{API}/repositories/{repo['id']}/summary").json()
    assert summary["snapshot"]["files_indexed"] == 10
    assert not any("secret" in p or p.endswith(".pem") for p, _ in summary["largest_files"])

    task = client.post(f"{API}/tasks", json={
        "repository_id": repo["id"], "title": "Rework login validation",
        "problem_statement": "Login accepts any password in src/auth/login.py.",
        "objective": "Validate passwords before granting a session.",
        "scope": ["src/auth"], "acceptance_criteria": ["Wrong password is rejected"]}).json()
    plan = task["plan"]["content"]
    assert plan["relevant_files"][0] == "src/auth/login.py"
    assert plan["confidence"] in {"medium", "high"}

    events = client.get(f"{API}/overview").json()["recent_events"]
    assert {"task.created", "index.completed", "repository.registered"} <= {e["action"] for e in events}
    assert "super-secret-value" not in client.get(f"{API}/overview").text
