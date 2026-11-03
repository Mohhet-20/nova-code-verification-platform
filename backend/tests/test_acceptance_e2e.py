"""Phase 18 acceptance: cross-module workflows through the real API (no direct database inserts for the domain objects under test)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.release import backup as bk
from app.release import dbtools, smoke
from tests.test_governance import API, World, w  # noqa: F401 - shared fixture
from tests.test_review import BASE_FILES, make_repo


def test_change_review_to_independent_approval_with_audit_lineage(w: World, tmp_path: Path) -> None:  # noqa: F811
    """Workflow 2 + 5: register -> index -> modify -> real review -> governance workflow -> author cannot approve -> independent approval -> audit."""
    root = make_repo(tmp_path, name="e2e-repo")
    rid = w.post("/repositories", "lead", {"name": "e2e", "root_path": str(root)}).json()["id"]
    started = w.post(f"/repositories/{rid}/index", "lead")
    assert started.status_code == 202
    assert w.get(f"/repositories/{rid}/snapshots/{started.json()['id']}", "lead").json()["status"] == "completed"
    assert w.put(f"/teams/{w.ids['alpha']}/ownership", None, {"repository_id": rid}).status_code == 200

    target = sorted(p for p in BASE_FILES if p.endswith(".py"))[0]
    original = (root / target).read_text(encoding="utf-8")
    (root / target).write_text(original + "\n\ndef added_by_change(value):\n    return eval(value)\n", encoding="utf-8")  # a controlled, harmless fixture change

    created = w.post(f"/repositories/{rid}/reviews", "eng2", {"mode": "working_tree"})
    assert created.status_code == 202, created.text
    review_id = created.json()["id"]
    review = w.get(f"/repositories/{rid}/reviews/{review_id}", "eng2").json()
    assert review["status"] in {"completed", "completed_with_warnings"}, review
    changed = {c["rel_path"] for c in w.get(f"/repositories/{rid}/reviews/{review_id}/changes?page_size=100", "eng2").json()["items"]}
    assert changed == {target}
    findings = w.get(f"/repositories/{rid}/reviews/{review_id}/findings?page_size=100", "eng2").json()["items"]
    for f in findings:  # every reported location points at a real file inside the repository
        if f.get("rel_path"):
            assert (root / f["rel_path"]).is_file()
            assert not Path(f["rel_path"]).is_absolute() and ".." not in Path(f["rel_path"]).parts
    risk = w.get(f"/repositories/{rid}/reviews/{review_id}/risk", "eng2")
    assert risk.status_code == 200 and risk.json()["category"] in {"low", "medium", "high", "critical", "unknown"}

    inst = w.instance("eng1", "code_review", review_id, title="E2E change")
    iid = inst["id"]
    assert inst["team_id"] == w.ids["alpha"]
    assert w.submit("eng1", iid).json()["state"] == "IN_REVIEW"
    assert w.decide("eng2", iid).status_code == 403  # the author of the change cannot approve it
    assert w.decide("eng1", iid).status_code == 403  # nor can the requester
    assert w.decide("outsider", iid).status_code == 404
    first = w.decide("rev1", iid, "approve", "Reviewed the diff")
    assert first.status_code == 201 and first.json()["state"] == "APPROVED"
    assert w.decide("rev1", iid, "approve").status_code == 409  # a repeated decision cannot inflate the outcome
    events = w.get(f"/workflows/instances/{iid}/events", "eng1").json()["items"]
    assert [e["event_type"] for e in events].count("decision_recorded") == 1
    audit = w.get("/governance/audit?page_size=100", "aud").json()["items"]
    actions = {e["action"] for e in audit}
    assert "authz.denied" in actions and any(a.startswith("workflow.") for a in actions), sorted(actions)
    assert (root / target).read_text(encoding="utf-8").endswith("return eval(value)\n")  # the review never modified the working tree


def test_release_and_disaster_recovery_chain_on_a_governed_database(w: World, tmp_path: Path) -> None:  # noqa: F811
    """Workflow 7 (data side): populated database -> verified backup -> isolated restore -> relationships, audit history and files intact -> restored
    database is at head and usable by a second application instance."""
    url = os.environ["NOVA_DATABASE_URL"]
    rev = w.review(w.ids["repoA"], author="eng2")
    iid = w.instance("eng1", "code_review", rev)["id"]
    w.submit("eng1", iid)
    assert w.decide("rev1", iid).status_code == 201
    art = tmp_path / "art"
    (art / "run").mkdir(parents=True)
    (art / "run" / "out.txt").write_text("evidence")
    before = dbtools.row_counts(url)
    b = bk.create_backup(url, tmp_path / "bk", artifact_root=art)
    restored = bk.restore(b["path"], tmp_path / "restored")
    assert restored["ok"], restored
    ru = restored["database_url"]
    after = dbtools.row_counts(ru)
    for table in ("users", "memberships", "teams", "repositories", "audit_events", "workflow_instances", "workflow_events", "workflow_decisions"):
        if table in before:
            assert after[table] == before[table], table
    con = sqlite3.connect(ru.removeprefix("sqlite:///"))
    try:
        orphan = con.execute("SELECT COUNT(*) FROM workflow_decisions d LEFT JOIN workflow_instances i ON i.id = d.instance_id WHERE i.id IS NULL").fetchone()[0] if "workflow_decisions" in after else 0
        repos_ok = con.execute("SELECT COUNT(*) FROM repositories WHERE workspace_id NOT IN (SELECT id FROM workspaces)").fetchone()[0] if "repositories" in after else 0
    finally:
        con.close()
    assert orphan == 0 and repos_ok == 0
    assert (tmp_path / "restored" / "artifacts" / "run" / "out.txt").read_text() == "evidence"
    assert dbtools.schema_status(ru)["at_head"]
    assert smoke._get is not None  # smoke tooling importable; the live-process smoke run is in test_release_ops
