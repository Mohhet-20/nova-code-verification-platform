"""Phase 04: static analysis rules, orchestration, lifecycle, configuration, API and redaction."""
from __future__ import annotations

import json
import logging
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.analysis import catalog
from app.services.analysis.complexity import cyclomatic_complexity
from app.services.analysis.redaction import scrub
from tests.conftest import register_and_index
from tests.test_repository_intelligence import reindex, write

API = "/api/v1"
SECRET = "sk_live_abcdef1234567890abcdef"


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def setup_repo(client: TestClient, root: Path, files: dict[str, str], name: str = "a") -> str:
    write(root, files)
    return register_and_index(client, root, name)["repo"]["id"]


def run(client: TestClient, rid: str, mode: str = "incremental") -> dict:
    r = client.post(f"{base(rid)}/analysis", json={"mode": mode})
    assert r.status_code == 202, r.text
    body = client.get(f"{base(rid)}/analysis/runs/{r.json()['id']}").json()
    assert body["status"] in ("completed", "completed_with_warnings", "failed"), body
    return body


def findings(client: TestClient, rid: str, **params: object) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        body = client.get(f"{base(rid)}/findings", params={"page_size": 100, "page": page, **params}).json()
        out += body["items"]
        if page >= body["pages"]:
            return out
        page += 1


def rules_of(client: TestClient, rid: str, **params: object) -> list[str]:
    return sorted(f["rule_id"] for f in findings(client, rid, **params))


def check(client: TestClient, tmp_path: Path, code: str, **params: object) -> list[str]:
    root = tmp_path / "r"
    rid = setup_repo(client, root, {"m.py": src(code)}, "r")
    run(client, rid)
    return rules_of(client, rid, **params)


# --------------------------------------------------------------------------------------------- rules

CASES = [
    ("PY-COR-001", "x = 1\nif x == None:\n    pass\n", "x = 1\nif x is None:\n    pass\n"),
    ("PY-COR-002", "def f(a=[]):\n    return a\n", "def f(a=None):\n    return a\n"),
    ("PY-COR-003", "def f():\n    return 1\n    print('x')\n", "def f():\n    if True:\n        return 1\n    return 2\n"),
    ("PY-REL-001", "try:\n    pass\nexcept ValueError:\n    pass\n", "try:\n    pass\nexcept ValueError:\n    raise\n"),
    ("PY-REL-002", "try:\n    x = 1\nexcept Exception:\n    x = 2\n",
     "try:\n    x = 1\nexcept Exception:\n    raise\n"),
    ("PY-SEC-001", "def f(s):\n    return eval(s)\n", "def f(s):\n    import ast\n    return ast.literal_eval(s)\n"),
    ("PY-SEC-002", "import subprocess\n\ndef f(c):\n    subprocess.run(c, shell=True)\n",
     "import subprocess\n\ndef f():\n    subprocess.run(['ls', '-l'])\n"),
    ("PY-SEC-003", "password = 'hunter2hunter2'\nprint(password)\n", "password = os_env = 'changeme'\nprint(password)\n"),
    ("PY-SEC-005", "import pickle\n\ndef f(b):\n    return pickle.loads(b)\n", "import json\n\ndef f(b):\n    return json.loads(b)\n"),
    ("PY-MNT-005", "import os\nx = 1\nprint(x)\n", "import os\nprint(os.name)\n"),
    ("PY-MNT-004", "def f(a, b, c, d, e, f, g):\n    return a\n", "def f(a, b):\n    return a\n"),
]


@pytest.mark.parametrize(("rule_id", "bad", "good"), CASES, ids=[c[0] for c in CASES])
def test_rule_true_and_false_positive(client: TestClient, tmp_path: Path, rule_id: str, bad: str, good: str) -> None:
    assert rule_id in check(client, tmp_path / "bad", bad)
    assert rule_id not in check(client, tmp_path / "good", good)


def test_every_catalog_rule_has_metadata() -> None:
    ids = [r.spec.id for a in catalog.ANALYZERS for r in a.rules]
    assert len(ids) == len(set(ids)) >= 19
    for a in catalog.ANALYZERS:
        for r in a.rules:
            s = r.spec
            assert s.title and s.description and s.remediation and s.limitations and s.version
            assert s.severity in ("critical", "high", "medium", "low", "info")
            assert s.category in ("correctness", "maintainability", "reliability", "security", "complexity")
            assert s.support in ("implemented", "partial")


def test_long_function_nesting_and_complexity_with_thresholds(client: TestClient, tmp_path: Path) -> None:
    body = "def big(x):\n" + "".join(f"    if x == {i}:\n        x += 1\n" for i in range(12)) + "    return x\n"
    nest = "def deep(x):\n    for a in x:\n        for b in a:\n            for c in b:\n                for d in c:\n" \
           "                    for e in d:\n                        print(e)\n"
    root = tmp_path / "t"
    rid = setup_repo(client, root, {"big.py": body, "deep.py": nest})
    run(client, rid)
    got = rules_of(client, rid)
    assert "PY-CPX-001" in got and "PY-MNT-003" in got and "PY-MNT-001" not in got
    cpx = next(f for f in findings(client, rid, rule_id="PY-CPX-001"))
    assert cpx["metric_value"] == 13
    write(root, {".nova/analysis.json": json.dumps({"version": 1, "rules": {
        "PY-MNT-001": {"options": {"max_lines": 10}}, "PY-CPX-001": {"options": {"max_complexity": 20}}}})})
    run(client, rid)
    got = rules_of(client, rid, status=["new", "existing", "reopened"])
    assert "PY-MNT-001" in got and "PY-CPX-001" not in got


def test_cyclomatic_complexity_definition() -> None:
    import ast

    def cc(code: str) -> int:
        return cyclomatic_complexity(ast.parse(textwrap.dedent(code)).body[0])  # type: ignore[arg-type]

    assert cc("def f():\n    return 1\n") == 1
    assert cc("def f(a, b):\n    if a and b:\n        return 1\n    return 2\n") == 3
    assert cc("def f(xs):\n    for x in xs:\n        if x:\n            continue\n    return [y for y in xs if y]\n") >= 4
    assert cc("def f():\n    try:\n        pass\n    except A:\n        pass\n    except B:\n        pass\n") == 3


# --------------------------------------------------------------------------------------------- evidence & redaction

def test_finding_has_actionable_fields_and_no_secret(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "s"
    rid = setup_repo(client, root, {"cfg.py": f'API_KEY = "{SECRET}"\nDB_PASSWORD = "correct-horse-battery"\n'})
    run(client, rid)
    items = findings(client, rid, rule_id="PY-SEC-003")
    assert items
    blob = json.dumps(items)
    assert SECRET not in blob and "correct-horse-battery" not in blob
    f = items[0]
    for key in ("explanation", "evidence", "remediation", "limitations", "confidence", "rel_path", "fingerprint", "rule_version"):
        assert f[key] not in (None, "")
    assert not f["rel_path"].startswith("/") and ":" not in f["rel_path"]
    detail = client.get(f"{base(rid)}/findings/{f['id']}")
    assert detail.status_code == 200 and SECRET not in detail.text
    assert SECRET not in client.get(f"{base(rid)}/analysis/summary").text
    assert SECRET not in client.get(f"{base(rid)}/analysis/status").text


def test_secrets_not_in_logs(client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "l"
    rid = setup_repo(client, root, {"cfg.py": f'TOKEN = "{SECRET}"\n', "bad.py": f'x = "{SECRET}" +\n'})
    run(client, rid)
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(client.get(f"{base(rid)}/analysis/runs").json())


def test_scrub_redacts_known_shapes() -> None:
    assert SECRET not in scrub(f"key is {SECRET}")
    assert "hunter2hunter2" not in scrub('password = "hunter2hunter2"')


# --------------------------------------------------------------------------------------------- failures

def test_parse_failure_is_recorded_and_other_files_still_analysed(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "p"
    rid = setup_repo(client, root, {"broken.py": "def f(:\n", "ok.py": "import os\n"})
    r = run(client, rid)
    assert r["status"] == "completed_with_warnings" and r["files_parse_failed"] == 1
    assert "PY-MNT-005" in rules_of(client, rid)
    assert all(f["rel_path"] != "broken.py" for f in findings(client, rid))


def test_one_analyzer_failing_does_not_stop_others(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "f"
    rid = setup_repo(client, root, {"m.py": "import os\nx = 1\nif x == None:\n    pass\n"})
    target = next(r for a in catalog.ANALYZERS if a.id == "py-maintainability" for r in a.rules)

    def boom(ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom with /secret/path")
        yield  # pragma: no cover

    monkeypatch.setattr(target, "check", boom)
    r = run(client, rid)
    assert r["status"] == "completed_with_warnings" and r["analyzer_errors"]
    assert "PY-COR-001" in rules_of(client, rid)
    assert "/secret/path" not in json.dumps(r) and "Traceback" not in json.dumps(r)


# --------------------------------------------------------------------------------------------- config

def test_invalid_config_is_rejected_with_details(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "c"
    rid = setup_repo(client, root, {"m.py": "x = 1\n", ".nova/analysis.json": json.dumps(
        {"version": 1, "rules": {"PY-NOPE-1": {"enabled": False}, "PY-MNT-001": {"options": {"max_lines": 1}}}})})
    r = client.post(f"{base(rid)}/analysis")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_analysis_config"
    cfg = client.get(f"{base(rid)}/analysis/config").json()
    assert cfg["state"]["valid"] is False and len(cfg["state"]["errors"]) >= 2
    assert client.get(f"{base(rid)}/analysis/status").json()["config"]["valid"] is False
    assert findings(client, rid) == []


def test_malformed_json_config_rejected(client: TestClient, tmp_path: Path) -> None:
    rid = setup_repo(client, tmp_path / "j", {"m.py": "x = 1\n", ".nova/analysis.json": "{not json"})
    assert client.post(f"{base(rid)}/analysis").status_code == 422


def test_disabled_rule_severity_override_and_exclusions(client: TestClient, tmp_path: Path) -> None:
    files = {
        "a.py": "import os\nx = 1\nif x == None:\n    pass\n",
        "gen/out.py": "import os\n",
        "api_pb2.py": "import os\n",
        "auto.py": "# Code generated by tool. DO NOT EDIT.\nimport os\n",
        ".nova/analysis.json": json.dumps({"version": 1, "exclude": ["gen/**"], "rules": {
            "PY-MNT-005": {"enabled": False}, "PY-COR-001": {"severity": "high"}}}),
    }
    root = tmp_path / "e"
    rid = setup_repo(client, root, files)
    r = run(client, rid)
    got = findings(client, rid)
    assert [f["rule_id"] for f in got] == ["PY-COR-001"] and got[0]["severity"] == "high"
    assert r["files_excluded"] >= 3 and r["rules_disabled"] == 1
    note = " ".join(client.get(f"{base(rid)}/analysis/summary").json()["notes"])
    assert "excluded" in note and "disabled" in note


def test_language_specific_rule_config_and_generated_files_can_be_included(client: TestClient, tmp_path: Path) -> None:
    files = {"api_pb2.py": "import os\n", ".nova/analysis.json": json.dumps({
        "version": 1, "exclude_generated": False, "languages": {"python": {"rules": {"PY-MNT-005": {"severity": "medium"}}}}})}
    rid = setup_repo(client, tmp_path / "lg", files)
    run(client, rid)
    got = findings(client, rid)
    assert got and got[0]["rel_path"] == "api_pb2.py" and got[0]["severity"] == "medium"


def test_config_cannot_change_server_limits(client: TestClient, tmp_path: Path) -> None:
    rid = setup_repo(client, tmp_path / "lim", {"m.py": "x=1\n", ".nova/analysis.json": json.dumps(
        {"version": 1, "max_files": 10 ** 9})})
    assert client.post(f"{base(rid)}/analysis").status_code == 422


def test_rules_endpoint_lists_catalog_and_effective_config(client: TestClient, tmp_path: Path) -> None:
    rid = setup_repo(client, tmp_path / "ru", {"m.py": "x=1\n", ".nova/analysis.json": json.dumps(
        {"version": 1, "rules": {"PY-MNT-001": {"enabled": False, "options": {"max_lines": 33}}}})})
    body = client.get(f"{base(rid)}/analysis/rules").json()
    by_id = {r["id"]: r for r in body["rules"]}
    assert len(by_id) >= 19 and by_id["PY-MNT-001"]["enabled"] is False
    assert by_id["PY-MNT-001"]["options"][0]["effective"] == 33 and by_id["PY-MNT-001"]["options"][0]["default"] == 80
    assert any(lang["status"] == "unsupported" for lang in body["languages"])
    cfg = client.get(f"{base(rid)}/analysis/config").json()
    assert cfg["disabled_rules"] == ["PY-MNT-001"] and cfg["editable_via_api"] is False


# --------------------------------------------------------------------------------------------- lifecycle

BAD = "import os\nx = 1\nif x == None:\n    pass\n"


def test_fingerprints_stable_and_repeat_run_is_idempotent(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "i"
    rid = setup_repo(client, root, {"m.py": BAD})
    run(client, rid)
    first = {f["fingerprint"]: f["id"] for f in findings(client, rid)}
    assert len(first) == 2
    r2 = run(client, rid, "full")
    second = {f["fingerprint"]: f["id"] for f in findings(client, rid)}
    assert first == second and r2["findings_new"] == 0
    assert {f["status"] for f in findings(client, rid)} == {"existing"}


def test_fingerprint_survives_line_shift(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "sh"
    rid = setup_repo(client, root, {"m.py": BAD})
    run(client, rid)
    before = {f["fingerprint"]: f["start_line"] for f in findings(client, rid)}
    write(root, {"m.py": "# a comment\n# another\n" + BAD})
    reindex(client, rid)
    run(client, rid)
    after = {f["fingerprint"]: f["start_line"] for f in findings(client, rid)}
    assert set(before) == set(after) and all(after[k] == before[k] + 2 for k in before)
    assert len(findings(client, rid)) == 2


def test_resolved_when_fixed_and_reopened_when_back(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "lc"
    rid = setup_repo(client, root, {"m.py": BAD})
    run(client, rid)
    write(root, {"m.py": "x = 1\nif x is None:\n    pass\n"})
    reindex(client, rid)
    r = run(client, rid)
    assert r["findings_resolved"] == 2 and r["findings_open"] == 0
    assert {f["status"] for f in findings(client, rid)} == {"resolved"}
    assert {f["resolution_reason"] for f in findings(client, rid)} == {"fixed"}
    write(root, {"m.py": BAD})
    reindex(client, rid)
    r = run(client, rid)
    items = findings(client, rid)
    assert r["findings_reopened"] == 2 and len(items) == 2
    assert {f["status"] for f in items} == {"reopened"} and all(f["reopened_count"] == 1 for f in items)


def test_not_resolved_when_file_fails_to_parse_or_analyzer_fails(client: TestClient, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "nr"
    rid = setup_repo(client, root, {"m.py": BAD, "ok.py": "y = 2\n"})
    run(client, rid)
    write(root, {"m.py": "def f(:\n"})
    reindex(client, rid)
    r = run(client, rid)
    assert r["status"] == "completed_with_warnings" and r["files_parse_failed"] == 1
    assert {f["status"] for f in findings(client, rid)} <= {"new", "existing"}
    assert len(findings(client, rid, status=["resolved"])) == 0
    # analyzer failure on a now-valid but clean file
    write(root, {"m.py": "x = 1\n"})
    reindex(client, rid)
    target = next(r_ for a in catalog.ANALYZERS if a.id == "py-correctness" for r_ in a.rules if r_.spec.id == "PY-COR-001")
    orig = target.check

    def boom(ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("x")
        yield  # pragma: no cover

    monkeypatch.setattr(target, "check", boom)
    run(client, rid)
    still = [f for f in findings(client, rid, rule_id="PY-COR-001")]
    assert still and all(f["status"] != "resolved" for f in still)
    monkeypatch.setattr(target, "check", orig)
    run(client, rid)
    assert all(f["status"] == "resolved" for f in findings(client, rid, rule_id="PY-COR-001"))


def test_removed_file_and_disabled_rule_are_resolved_with_distinct_reasons(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "rm"
    rid = setup_repo(client, root, {"a.py": BAD, "b.py": "import os\n"})
    run(client, rid)
    (root / "b.py").unlink()
    write(root, {".nova/analysis.json": json.dumps({"version": 1, "rules": {"PY-COR-001": {"enabled": False}}})})
    reindex(client, rid)
    run(client, rid)
    reasons = {(f["rule_id"], f["rel_path"]): f["resolution_reason"] for f in findings(client, rid, status=["resolved"])}
    assert reasons[("PY-COR-001", "a.py")] == "rule_disabled"
    assert reasons[("PY-MNT-005", "b.py")] == "file_removed"
    s = client.get(f"{base(rid)}/analysis/summary").json()
    assert s["resolved_by_reason"].get("fixed", 0) == 0 and s["open"] == 1


def test_incremental_run_skips_unchanged_files(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "inc"
    rid = setup_repo(client, root, {"a.py": BAD, "b.py": "x = 1\n"})
    r1 = run(client, rid)
    assert r1["files_analyzed"] == 2
    r2 = run(client, rid)
    assert r2["files_analyzed"] == 0 and r2["files_unchanged"] == 2
    write(root, {"b.py": "import os\n"})
    reindex(client, rid)
    r3 = run(client, rid)
    assert r3["files_analyzed"] == 1 and r3["files_unchanged"] == 1
    assert len(findings(client, rid)) == 3
    r4 = run(client, rid, "full")
    assert r4["files_analyzed"] == 2


def test_suppression_workflow(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "sup"
    rid = setup_repo(client, root, {"m.py": BAD})
    run(client, rid)
    f = findings(client, rid, rule_id="PY-COR-001")[0]
    url = f"{base(rid)}/findings/{f['id']}"
    assert client.patch(url, json={"status": "suppressed"}).status_code == 422
    assert client.patch(url, json={"status": "suppressed", "reason": "no"}).status_code == 422
    assert client.patch(url, json={"status": "resolved", "reason": "nope nope"}).status_code == 422
    assert client.patch(url, json={"status": "existing"}).status_code == 409
    ok = client.patch(url, json={"status": "suppressed", "reason": "Intentional comparison in legacy code"})
    assert ok.status_code == 200 and ok.json()["status"] == "suppressed" and ok.json()["suppression_reason"]
    run(client, rid, "full")
    assert client.get(url).json()["status"] == "suppressed"
    assert client.get(f"{base(rid)}/analysis/summary").json()["by_status"]["suppressed"] == 1
    back = client.patch(url, json={"status": "existing"})
    assert back.status_code == 200 and back.json()["status"] == "existing" and back.json()["suppression_reason"] is None
    write(root, {"m.py": "x = 1\n"})
    reindex(client, rid)
    run(client, rid)
    assert client.patch(url, json={"status": "suppressed", "reason": "too late to suppress"}).status_code == 409


# --------------------------------------------------------------------------------------------- API

def test_status_lifecycle_and_empty_repository(client: TestClient, tmp_path: Path) -> None:
    rid = setup_repo(client, tmp_path / "em", {"README.md": "# hi\n"})
    st = client.get(f"{base(rid)}/analysis/status").json()
    assert st["state"] == "never_analyzed" and st["latest"] is None
    r = run(client, rid)
    assert r["status"] == "completed" and r["files_eligible"] == 0 and r["findings_open"] == 0
    s = client.get(f"{base(rid)}/analysis/summary").json()
    assert s["analyzed"] and s["open"] == 0 and "score" not in s
    assert client.get(f"{base(rid)}/findings").json()["items"] == []


def test_requires_index_first(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "ni"
    write(root, {"m.py": "x=1\n"})
    rid = client.post(f"{API}/repositories", json={"name": "ni", "root_path": str(root)}).json()["id"]
    assert client.post(f"{base(rid)}/analysis").status_code == 409


def test_filters_sorting_pagination_and_validation(client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/a.py": BAD + "def f(a=[]):\n    return eval(a)\n", "pkg/b.py": "import os\n", "top.py": "import sys\n"}
    rid = setup_repo(client, tmp_path / "fl", files)
    run(client, rid)
    allf = findings(client, rid)
    assert len(allf) >= 6
    assert {f["rule_id"] for f in findings(client, rid, rule_id="PY-MNT-005")} == {"PY-MNT-005"}
    assert all(f["category"] == "security" for f in findings(client, rid, category="security"))
    assert all(f["severity"] in ("medium", "high") for f in findings(client, rid, severity=["medium", "high"]))
    assert {f["rel_path"] for f in findings(client, rid, path="pkg/")} == {"pkg/a.py", "pkg/b.py"}
    assert all("100%" not in f["rel_path"] for f in findings(client, rid, path="%"))
    assert findings(client, rid, path="%") == []
    assert all(f["language"] == "Python" for f in findings(client, rid, language="Python"))
    assert findings(client, rid, language="Rust") == []
    assert findings(client, rid, q="unused import")
    sev = [f["severity"] for f in findings(client, rid, sort="severity", order="asc")]
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    assert [rank[s] for s in sev] == sorted(rank[s] for s in sev)
    p1 = client.get(f"{base(rid)}/findings", params={"page_size": 3, "page": 1}).json()
    p2 = client.get(f"{base(rid)}/findings", params={"page_size": 3, "page": 2}).json()
    assert len(p1["items"]) == 3 and p1["total"] == len(allf) and p1["pages"] == -(-len(allf) // 3)
    assert not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}
    for bad in ({"sort": "password"}, {"order": "sideways"}, {"page_size": 1000}, {"page": 0}, {"severity": "urgent"},
                {"status": "open"}, {"category": "style"}):
        assert client.get(f"{base(rid)}/findings", params=bad).status_code == 422, bad


def test_paths_are_relative_and_no_stack_traces(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "pa"
    rid = setup_repo(client, root, {"m.py": BAD})
    run(client, rid)
    text = json.dumps([client.get(f"{base(rid)}/findings").json(), client.get(f"{base(rid)}/analysis/status").json(),
                       client.get(f"{base(rid)}/analysis/summary").json()])
    assert str(tmp_path) not in text and "Traceback" not in text


def test_cross_repository_access_is_not_found(client: TestClient, tmp_path: Path) -> None:
    r1 = setup_repo(client, tmp_path / "x1", {"m.py": BAD}, "x1")
    r2 = setup_repo(client, tmp_path / "x2", {"m.py": "x = 1\n"}, "x2")
    run(client, r1)
    fid = findings(client, r1)[0]["id"]
    assert client.get(f"{base(r2)}/findings/{fid}").status_code == 404
    assert client.patch(f"{base(r2)}/findings/{fid}", json={"status": "suppressed", "reason": "cross repo!"}).status_code == 404
    run_id = client.get(f"{base(r1)}/analysis/runs").json()["items"][0]["id"]
    assert client.get(f"{base(r2)}/analysis/runs/{run_id}").status_code == 404
    assert findings(client, r2) == []
    assert client.get(f"{base('nonexistent')}/findings").status_code == 404
    assert client.post(f"{base('nonexistent')}/analysis").status_code == 404


def test_authorization_required_when_token_configured(migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/analysis/status"), ("get", "/findings"), ("post", "/analysis"),
                             ("get", "/analysis/rules"), ("get", "/analysis/config"), ("get", "/analysis/summary"),
                             ("get", "/analysis/runs"), ("patch", "/findings/abc")):
            r = getattr(c, method)(f"{API}/repositories/abc{path}")
            assert r.status_code == 401, (method, path, r.status_code)
    reset_engine()
    reset_settings_cache()


def test_run_history_and_concurrency_guard(client: TestClient, tmp_path: Path) -> None:
    from app.db.session import get_session_factory
    from app.models import AnalysisRun

    rid = setup_repo(client, tmp_path / "cc", {"m.py": BAD})
    run(client, rid)
    run(client, rid)
    assert client.get(f"{base(rid)}/analysis/runs", params={"page_size": 1}).json()["total"] == 2
    with get_session_factory()() as s:
        s.add(AnalysisRun(repository_id=rid, status="running", mode="full"))
        s.commit()
    r = client.post(f"{base(rid)}/analysis")
    assert r.status_code == 409
    assert client.get(f"{base(rid)}/analysis/status").json()["state"] == "running"


def test_interrupted_run_recovered_at_startup(migrated_url: str, tmp_path: Path) -> None:
    from app.db.session import get_session_factory, reset_engine
    from app.models import AnalysisRun, Repository, Workspace
    from app.services.analysis.orchestrator import recover_interrupted_analysis

    reset_engine()
    with get_session_factory()() as s:
        ws = Workspace(name="w", slug="w")
        s.add(ws)
        s.flush()
        repo = Repository(workspace_id=ws.id, name="r", root_path=str(tmp_path))
        s.add(repo)
        s.flush()
        s.add(AnalysisRun(repository_id=repo.id, status="running", mode="full"))
        s.commit()
        assert recover_interrupted_analysis(s) == 1
        assert s.query(AnalysisRun).one().status == "failed"
    reset_engine()


def test_file_limit_is_reported_not_silent(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache

    monkeypatch.setenv("NOVA_ANALYSIS_MAX_FILES", "2")
    reset_settings_cache()
    rid = setup_repo(client, tmp_path / "big", {f"f{i}.py": "import os\n" for i in range(5)})
    r = run(client, rid)
    assert r["status"] == "completed_with_warnings" and r["files_analyzed"] == 2 and r["files_not_reached"] == 3
    assert any("limit" in w.lower() for w in r["warnings"])
    assert len(findings(client, rid)) == 2


def test_oversized_file_is_skipped_with_warning(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import reset_settings_cache

    monkeypatch.setenv("NOVA_ANALYSIS_MAX_FILE_BYTES", "2048")
    reset_settings_cache()
    rid = setup_repo(client, tmp_path / "ov", {"big.py": "import os\n" + "# pad\n" * 1000, "ok.py": "import sys\n"})
    r = run(client, rid)
    assert r["files_skipped"] == 1 and r["status"] == "completed_with_warnings"
    assert {f["rel_path"] for f in findings(client, rid)} == {"ok.py"}


def test_repository_code_is_never_executed(client: TestClient, tmp_path: Path) -> None:
    marker = tmp_path / "pwned.txt"
    code = f"open({str(marker)!r}, 'w').write('x')\nimport os\n"
    rid = setup_repo(client, tmp_path / "ex", {"evil.py": code, "conftest.py": code, "setup.py": code})
    run(client, rid)
    assert not marker.exists()


def test_run_fails_when_nothing_could_be_analysed(client: TestClient, tmp_path: Path) -> None:
    rid = setup_repo(client, tmp_path / "allbad", {"m.py": "def f(:\n"})
    r = run(client, rid)
    assert r["status"] == "failed" and r["error_message"]
    assert client.get(f"{base(rid)}/analysis/status").json()["state"] == "failed"
