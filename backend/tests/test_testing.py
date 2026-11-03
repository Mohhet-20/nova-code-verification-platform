"""Phase 05: test discovery, planning, generation, validation, approval, execution, coverage and gaps."""
from __future__ import annotations

import ast
import os
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.services.testing import discovery, facts, generator, planner, validator
from tests.conftest import register_and_index
from tests.test_repository_intelligence import write

API = "/api/v1"

CALC = textwrap.dedent('''
    def percent(part: int, total: int) -> float:
        """Return part as a percentage.

        >>> percent(1, 4)
        25.0
        """
        if total == 0:
            raise ValueError("total must be non-zero")
        return part * 100 / total


    def sign(n: int) -> int:
        if n > 0:
            return 1
        if n < 0:
            return -1
        return 0


    def mystery(data, flag):
        return data if flag else None
''')

FILES = {
    "pkg/__init__.py": "",
    "pkg/calc.py": CALC,
    "pkg/unused.py": "def never_imported(x):\n    return x + 1\n",
    "tests/test_calc.py": "from pkg.calc import sign\n\n\ndef test_sign_pos():\n    assert sign(3) == 1\n",
    "tests/test_broken.py": "def test_x(:\n",
}


@pytest.fixture
def art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "artifacts"
    monkeypatch.setenv("NOVA_ARTIFACT_DIR", str(d))
    reset_settings_cache()
    return d


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


def setup(client: TestClient, root: Path, files: dict[str, str] | None = None, name: str = "t") -> str:
    write(root, FILES if files is None else files)
    return register_and_index(client, root, name)["repo"]["id"]


def generate(client: TestClient, rid: str, path: str = "pkg/calc.py", symbol: str | None = None, templates: bool = True) -> dict:
    t: dict = {"path": path}
    if symbol:
        t["symbol"] = symbol
    r = client.post(f"{base(rid)}/tests/generate", json={"targets": [t], "include_templates": templates})
    assert r.status_code == 201, r.text
    return r.json()


def approve_all(client: TestClient, rid: str, gen: dict) -> dict:
    assert client.post(f"{base(rid)}/tests/generations/{gen['id']}/validate").status_code == 200
    r = client.post(f"{base(rid)}/tests/generations/{gen['id']}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def execute(client: TestClient, rid: str, gen_id: str, **body: object) -> dict:
    r = client.post(f"{base(rid)}/tests/generations/{gen_id}/execute", json=body)
    assert r.status_code == 202, r.text
    return client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()


def staged_file(art: Path, rid: str, gen: dict) -> Path:
    files = list((art / rid / "generated" / gen["id"]).glob("*.py"))
    assert len(files) == 1
    return files[0]


# ------------------------------------------------------------------------------------------------ discovery

def test_discovery_finds_tests_associates_sources_and_records_errors(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    d = client.post(f"{base(rid)}/tests/discover")
    assert d.status_code == 201
    body = d.json()
    assert body["files_found"] == 2 and body["files_unparsable"] == 1 and body["tests_found"] == 1
    assert any("test_broken.py" in e for e in body["errors"])
    files = {f["rel_path"]: f for f in client.get(f"{base(rid)}/tests/files").json()["items"]}
    assert files["tests/test_broken.py"]["parse_status"] == "syntax_error"
    ok = files["tests/test_calc.py"]
    assert ok["tests"][0]["id"] == "tests/test_calc.py::test_sign_pos"
    assert any(a["path"] == "pkg/calc.py" and a["via"] == "import" and a["confidence"] == "high" for a in ok["associated_sources"])
    assert "pkg/unused.py" not in {a["path"] for a in ok["associated_sources"]}
    assert "not mean" in client.get(f"{base(rid)}/tests").json()["note"] or "executed" in client.get(f"{base(rid)}/tests").json()["note"]


def test_discovery_does_not_execute_or_import_repo_code(art: Path, client: TestClient, tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    files = dict(FILES)
    files["tests/test_boom.py"] = f"open({str(marker)!r}, 'w').write('x')\n\ndef test_a():\n    assert True\n"
    files["pkg/side.py"] = f"open({str(marker)!r}, 'w').write('x')\n"
    rid = setup(client, tmp_path / "r", files)
    client.post(f"{base(rid)}/tests/discover")
    client.post(f"{base(rid)}/tests/plan", json={"targets": [{"path": "pkg/side.py"}]})
    assert not marker.exists()


def test_framework_config_read_statically_and_unittest_collected(tmp_path: Path) -> None:
    write(tmp_path, {"pytest.ini": "[pytest]\npython_files = check_*.py\n",
                     "check_a.py": "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n",
                     "tests/test_b.py": "class TestX:\n    def test_y(self):\n        pass\n    def helper(self):\n        pass\n"})
    cfg = discovery.read_pytest_config(tmp_path)
    assert "check_*.py" in str(cfg)
    result = discovery.discover(tmp_path, {"check_a.py": "", "tests/test_b.py": ""}, max_files=100, max_bytes=100000)
    names = sorted(t["id"] for f in result[1] for t in f.tests)
    assert names == ["check_a.py::T::test_a"]  # test_b.py does not match the configured python_files pattern


# ------------------------------------------------------------------------------------------------ planning

def test_plan_has_required_fields_and_no_invented_outputs(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    r = client.post(f"{base(rid)}/tests/plan", json={"targets": [{"path": "pkg/calc.py"}]})
    assert r.status_code == 200
    cases = [c for t in r.json()["targets"] for c in t["cases"]]
    required = {"target_symbol", "purpose", "preconditions", "input_strategy", "expected", "category", "rationale",
                "confidence", "assumptions", "review_required"}
    assert cases and all(required <= set(c) for c in cases)
    by_name = {c["test_name"]: c for c in cases}
    assert by_name["test_percent_example"]["runnable"] and "25.0" in by_name["test_percent_example"]["expected"]
    mystery = [c for c in cases if c["target_symbol"] == "mystery"]
    assert mystery and all(c["review_required"] and not c["runnable"] for c in mystery)
    assert client.get(f"{base(rid)}/tests/generations").json()["total"] == 0  # planning wrote nothing


def test_generated_candidate_content_is_valid_safe_and_non_trivial(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid)
    cand = gen["candidates"][0]
    tree = ast.parse(cand["source"])
    assert "NOVA-GENERATED" in cand["source"]
    assert "from pkg.calc import" in cand["source"]
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "test_percent_raises" in names
    assert "assert True" not in cand["source"] and "assert 1 == 1" not in cand["source"]
    assert "pytest.mark.skip" in cand["source"]  # review templates are skipped, not faked
    assert "mystery" in cand["source"] and "NotImplementedError" in cand["source"]
    # staged outside the repository, nothing written into it
    assert staged_file(art, rid, gen).is_file()
    assert not list((tmp_path / "r").rglob("*_nova.py"))


def test_generate_is_idempotent(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    a = generate(client, rid)
    b = client.post(f"{base(rid)}/tests/generate", json={"targets": [{"path": "pkg/calc.py"}]})
    assert b.status_code == 201 and b.json()["id"] == a["id"] and b.json()["created"] is False
    assert client.get(f"{base(rid)}/tests/generations").json()["total"] == 1


def test_generation_never_executes_automatically(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid)
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0
    assert all(c["status"] == "generated" for c in gen["candidates"])


# ------------------------------------------------------------------------------------------------ validation / approval

def test_target_validation_rejections(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    for path, code in (("../etc/passwd", 404), ("pkg/missing.py", 404), ("tests/test_calc.py", 422)):
        r = client.post(f"{base(rid)}/tests/generate", json={"targets": [{"path": path}]})
        assert r.status_code in (400, 404, 422), (path, r.status_code)
        assert r.status_code == code or path.startswith(".."), (path, r.status_code)
    assert client.post(f"{base(rid)}/tests/generate", json={"targets": []}).status_code == 422


@pytest.mark.parametrize("mutation, check", [
    ("def broken(:\n", "syntax"),
    ("import subprocess\n", "imports"),
    ("import os\nos.system('echo hi')\n", "imports"),
    ("\n\ndef test_x():\n    eval('1')\n", "prohibited_patterns"),
    ("\n\ndef test_y():\n    open('/etc/passwd').read()\n", "prohibited_patterns"),
])
def test_validator_rejects_tampered_or_dangerous_files(art: Path, client: TestClient, tmp_path: Path, mutation: str, check: str) -> None:
    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid)
    f = staged_file(art, rid, gen)
    f.write_text(f.read_text() + mutation, encoding="utf-8")
    r = client.post(f"{base(rid)}/tests/generations/{gen['id']}/validate").json()
    cand = r["candidates"][0]
    assert cand["status"] == "rejected" and cand["rejection_reasons"]
    failed = {c["check"] for c in cand["validation"] if not c["passed"]}
    assert "integrity" in failed or check in failed
    assert client.post(f"{base(rid)}/tests/generations/{gen['id']}/approve").status_code in (409, 422)
    assert client.post(f"{base(rid)}/tests/generations/{gen['id']}/execute", json={}).status_code == 409


def test_validator_detects_oversize_duplicates_and_changed_target() -> None:
    good = "from pkg.calc import sign\n\n\ndef test_a():\n    assert sign(1) == 1\n"
    import hashlib

    sha = hashlib.sha256(good.encode()).hexdigest()
    kw = dict(max_bytes=100000, expected_module="pkg.calc", recorded_sha=sha, target_sha_recorded="a",
              target_sha_now="a", other_hashes=set(), path_ok=True)
    checks = validator.validate(good.encode(), **{**kw, "other_hashes": {sha}})
    assert {c.check: c.passed for c in checks}["duplicate"] is False
    checks = validator.validate(good.encode(), **{**kw, "max_bytes": 10})
    assert {c.check: c.passed for c in checks}["size"] is False
    checks = validator.validate(good.encode(), **{**kw, "target_sha_now": "b"})
    assert {c.check: c.passed for c in checks}["target_unchanged"] is False
    checks = validator.validate(good.encode(), **{**kw, "path_ok": False, "path_detail": "outside"})
    assert {c.check: c.passed for c in checks}["output_path"] is False
    assert validator.failed(checks)


def test_production_files_never_modified_by_generation_or_validation(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    gen = generate(client, rid)
    approve_all(client, rid, gen)
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before
    assert set(root.rglob("*.py")) == set(before)


def test_approval_required_and_changed_file_blocks_execution(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid)
    assert client.post(f"{base(rid)}/tests/generations/{gen['id']}/approve").status_code in (409, 422)  # not validated
    assert client.post(f"{base(rid)}/tests/generations/{gen['id']}/execute", json={}).status_code == 409
    approve_all(client, rid, gen)
    f = staged_file(art, rid, gen)
    f.write_text(f.read_text() + "\n# edited after approval\n", encoding="utf-8")
    r = client.post(f"{base(rid)}/tests/generations/{gen['id']}/execute", json={})
    assert r.status_code == 409 and "approved" in r.text
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0


def test_read_only_role_cannot_mutate(art: Path, client: TestClient, tmp_path: Path) -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership

    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid)
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()
    for method, path, body in (("post", "/tests/discover", None), ("post", "/tests/generate", {"targets": [{"path": "pkg/calc.py"}]}),
                               ("post", f"/tests/generations/{gen['id']}/validate", None),
                               ("post", f"/tests/generations/{gen['id']}/approve", None),
                               ("post", f"/tests/generations/{gen['id']}/execute", {}),
                               ("post", "/tests/execute", {"confirm": True})):
        r = getattr(client, method)(base(rid) + path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)
    assert client.get(f"{base(rid)}/tests/generations/{gen['id']}").status_code == 200  # reading stays allowed


# ------------------------------------------------------------------------------------------------ export

def test_export_never_overwrites_user_tests(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    gen = approve_all(client, rid, generate(client, rid))
    cid = gen["candidates"][0]["id"]
    url = f"{base(rid)}/tests/generations/{gen['id']}/candidates/{cid}/export"
    assert client.post(url, json={"destination": "tests/test_calc.py", "overwrite": True}).status_code in (409, 422)
    assert (root / "tests/test_calc.py").read_text().startswith("from pkg.calc import sign")
    for bad in ("../outside.py", "/abs/x.py", "tests/x.txt"):
        assert client.post(url, json={"destination": bad}).status_code in (400, 422), bad
    ok = client.post(url, json={"destination": "tests/test_calc_generated.py"})
    assert ok.status_code == 200 and ok.json()["exported_path"] == "tests/test_calc_generated.py"
    assert "NOVA-GENERATED" in (root / "tests/test_calc_generated.py").read_text()
    assert client.post(url, json={"destination": "tests/test_calc_generated.py"}).status_code == 409
    assert client.post(url, json={"destination": "tests/test_calc_generated.py", "overwrite": True}).status_code == 200


# ------------------------------------------------------------------------------------------------ execution

def test_approved_generated_tests_pass_with_measured_coverage(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid, symbol=None, templates=False)
    approve_all(client, rid, gen)
    run = execute(client, rid, gen["id"])
    assert run["status"] == "passed" and run["exit_code"] == 0
    assert run["tests_collected"] == run["tests_passed"] and run["tests_passed"] >= 3 and run["tests_failed"] == 0
    assert run["coverage_status"] == "measured" and run["coverage_branch"] is True
    assert 0 < run["line_percent"] <= 100 and run["branches_total"] > 0
    env = run["environment_profile"]
    assert env["sandbox"] is False and env["network_isolated"] is False and env["mechanism"] == "restricted-subprocess"
    assert "PYTHONPATH" in env["environment_keys"] and "API_KEY" not in env["environment_keys"]
    assert run["tool_versions"]["pytest"] and run["tool_versions"]["coverage.py"]
    assert run["source_hashes"]["pkg/calc.py"]
    cov = client.get(f"{base(rid)}/coverage").json()
    assert cov["state"] == "measured" and "generated tests only" in cov["basis"]
    files = client.get(f"{base(rid)}/coverage/files").json()["items"]
    assert [f["rel_path"] for f in files] == ["pkg/calc.py"]
    assert files[0]["covered"] <= files[0]["statements"]


def test_execution_not_started_twice_and_run_history(art: Path, client: TestClient, tmp_path: Path) -> None:
    from app.db.session import get_session_factory
    from app.models import TestExecutionRun

    rid = setup(client, tmp_path / "r")
    gen = generate(client, rid, templates=False)
    approve_all(client, rid, gen)
    with get_session_factory()() as s:
        s.add(TestExecutionRun(repository_id=rid, kind="existing", status="running"))
        s.commit()
    r = client.post(f"{base(rid)}/tests/generations/{gen['id']}/execute", json={})
    assert r.status_code == 409 and "in progress" in r.text
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 1


def test_failing_existing_suite_is_reported_as_failed(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    files["tests/test_calc.py"] = ("from pkg.calc import sign\n\n\ndef test_ok():\n    assert sign(3) == 1\n\n\n"
                                   "def test_bad():\n    assert sign(3) == 99\n")
    rid = setup(client, tmp_path / "r", files)
    assert client.post(f"{base(rid)}/tests/execute", json={"confirm": True}).status_code == 409  # needs discovery first
    client.post(f"{base(rid)}/tests/discover")
    assert client.post(f"{base(rid)}/tests/execute", json={"confirm": False}).status_code == 422
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["status"] == "failed" and run["exit_code"] == 1
    assert (run["tests_passed"], run["tests_failed"]) == (1, 1)
    assert run["failures"] and run["failures"][0]["name"].endswith("test_bad")
    assert isinstance(run["failures"][0].get("traceback", ""), str)
    assert "basis" in client.get(f"{base(rid)}/coverage").json() and "repository's own" in client.get(f"{base(rid)}/coverage").json()["basis"]


def test_timeout_is_enforced_and_recorded(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_TEST_TIMEOUT_SECONDS", "2")
    reset_settings_cache()
    files = dict(FILES)
    files["tests/test_calc.py"] = "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
    rid = setup(client, tmp_path / "r", files)
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": False})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["status"] == "timed_out" and run["duration_ms"] < 20000
    assert run["coverage_status"] == "not_requested"


def test_output_is_bounded_and_secrets_are_redacted(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_TEST_MAX_OUTPUT_BYTES", "2000")
    reset_settings_cache()
    secret = "sk_live_abcdef1234567890abcdef"
    files = dict(FILES)
    files["tests/test_calc.py"] = (f"def test_noisy():\n    print('x' * 500000)\n    print('{secret}')\n    assert False\n")
    rid = setup(client, tmp_path / "r", files)
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": False})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["output_truncated"] is True
    assert len(run["stdout_tail"]) <= 4000 and len(run["stderr_tail"]) <= 4000
    assert secret not in run["stdout_tail"] + run["stderr_tail"] + str(run["failures"])


def test_environment_is_minimal_and_secrets_not_inherited(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_SUPER_SECRET_TOKEN", "leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me-too")
    files = dict(FILES)
    files["tests/test_calc.py"] = ("import os\n\n\ndef test_env():\n    assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
                                   "    assert 'NOVA_SUPER_SECRET_TOKEN' not in os.environ\n")
    rid = setup(client, tmp_path / "r", files)
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": False})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["status"] == "passed", run
    assert run["environment_profile"]["environment_inherited"] is False


def test_unusable_interpreter_is_reported_not_crashed(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_TEST_PYTHON", str(tmp_path / "no-such-python"))
    reset_settings_cache()
    rid = setup(client, tmp_path / "r")
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["status"] == "error" and run["reason"]
    assert run["tests_passed"] is None  # nothing is claimed
    assert run["coverage_status"] != "measured" and run["line_percent"] is None


def test_elevated_privileges_are_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.services.testing import runner

    monkeypatch.setattr(runner, "is_elevated", lambda: True)
    spec = runner.RunSpec("existing", tmp_path, tmp_path / "run", tmp_path, tmp_path, [], [tmp_path], False)
    res = runner.execute(spec, Settings())
    assert res.status == "rejected"
    assert any(w in (res.reason or "").lower() for w in ("administrator", "root"))


def test_interrupted_runs_are_recovered(art: Path, client: TestClient, tmp_path: Path) -> None:
    from app.db.session import get_session_factory
    from app.models import TestExecutionRun
    from app.services.testing.execution import recover_interrupted_executions

    rid = setup(client, tmp_path / "r")
    with get_session_factory()() as s:
        s.add(TestExecutionRun(repository_id=rid, kind="existing", status="running"))
        s.commit()
        assert recover_interrupted_executions(s) == 1
    run = client.get(f"{base(rid)}/tests/runs").json()["items"][0]
    assert run["status"] == "error" and "Interrupted" in run["reason"]


# ------------------------------------------------------------------------------------------------ coverage and gaps

def test_no_data_is_distinct_from_zero_percent(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    files["tests/test_calc.py"] = "def test_unrelated():\n    assert 1 + 1 == 2\n"
    rid = setup(client, tmp_path / "r", files)
    never = client.get(f"{base(rid)}/coverage").json()
    assert never["state"] == "never_measured" and never["run"] is None
    assert client.get(f"{base(rid)}/coverage/files").json()["items"] == []
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": False})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["coverage_status"] == "not_requested" and run["line_percent"] is None
    assert client.get(f"{base(rid)}/coverage").json()["state"] == "never_measured"
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": True, "coverage": True})
    run = client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()
    assert run["coverage_status"] == "measured"
    zero = {f["rel_path"]: f for f in client.get(f"{base(rid)}/coverage/files").json()["items"]}
    assert zero["pkg/calc.py"]["covered"] == 0 and zero["pkg/calc.py"]["line_percent"] == 0.0


def test_gaps_separate_static_recommendations_from_measured(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    client.post(f"{base(rid)}/tests/discover")
    g = client.get(f"{base(rid)}/coverage/gaps").json()
    assert g["coverage_run_id"] is None and g["items"]
    assert all(i["evidence"] == "static" and i["measured"] is None for i in g["items"])
    assert all("Static recommendation" in i["limitations"] for i in g["items"])
    by = {(i["rel_path"], i["symbol"]): i for i in g["items"]}
    assert by[("pkg/unused.py", "never_imported")]["has_associated_tests"] is False
    assert by[("pkg/calc.py", "sign")]["has_associated_tests"] is True  # associated via an import, not a file name
    client.post(f"{base(rid)}/tests/execute", json={"confirm": True})
    g2 = client.get(f"{base(rid)}/coverage/gaps").json()
    sign = next(i for i in g2["items"] if i["symbol"] == "sign")
    assert sign["evidence"] == "measured+static" and sign["measured"]["missing_lines"]
    assert all(i["symbol"] != "__init__" for i in g2["items"])
    assert client.get(f"{base(rid)}/coverage/gaps", params={"path": "unused"}).json()["total"] == 1


def test_gap_analysis_ignores_file_naming(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    files["tests/test_unused.py"] = "def test_nothing_related():\n    assert True\n"  # named for unused.py but imports nothing
    rid = setup(client, tmp_path / "r", files)
    client.post(f"{base(rid)}/tests/discover")
    items = client.get(f"{base(rid)}/coverage/gaps").json()["items"]
    unused = next(i for i in items if i["rel_path"] == "pkg/unused.py")
    assert unused["measured"] is None and unused["confidence"] == "low"


def test_planner_boundary_and_guard_facts() -> None:
    tree = facts.parse_source(CALC)
    fs = {f.name: f for f in facts.analyze_module(tree)}
    assert fs["percent"].guards and fs["percent"].examples
    assert fs["sign"].boundary_compares and not fs["sign"].guards
    assert planner.GENERATOR_VERSION
    assert generator.build  # imported API surface


# ------------------------------------------------------------------------------------------------ isolation / API hygiene

def test_cross_repository_isolation_and_unknown_ids(art: Path, client: TestClient, tmp_path: Path) -> None:
    r1 = setup(client, tmp_path / "a", name="a")
    r2 = setup(client, tmp_path / "b", name="b")
    gen = approve_all(client, r1, generate(client, r1))
    run = execute(client, r1, gen["id"])
    assert client.get(f"{base(r2)}/tests/generations/{gen['id']}").status_code == 404
    assert client.post(f"{base(r2)}/tests/generations/{gen['id']}/execute", json={}).status_code == 404
    assert client.get(f"{base(r2)}/tests/runs/{run['id']}").status_code == 404
    assert client.get(f"{base(r2)}/coverage").json()["state"] == "never_measured"
    assert client.get(f"{base(r2)}/coverage/files", params={"run_id": run["id"]}).status_code == 404
    assert client.get(f"{base('nope')}/tests").status_code == 404
    assert client.post(f"{base('nope')}/tests/discover").status_code == 404


def test_artifacts_are_scoped_per_repo_and_run(art: Path, client: TestClient, tmp_path: Path) -> None:
    r1 = setup(client, tmp_path / "a", name="a")
    r2 = setup(client, tmp_path / "b", name="b")
    g1, g2 = generate(client, r1), generate(client, r2)
    assert (art / r1 / "generated" / g1["id"]).is_dir() and (art / r2 / "generated" / g2["id"]).is_dir()
    assert not (art / r1 / "generated" / g2["id"]).exists()


def test_api_never_exposes_absolute_paths_or_environment(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    client.post(f"{base(rid)}/tests/discover")
    gen = approve_all(client, rid, generate(client, rid))
    run = execute(client, rid, gen["id"])
    client.post(f"{base(rid)}/tests/execute", json={"confirm": True})
    blobs = [client.get(base(rid) + p).text for p in (
        "/tests", "/tests/files", f"/tests/generations/{gen['id']}", "/tests/generations", "/tests/runs",
        f"/tests/runs/{run['id']}", "/coverage", "/coverage/files", "/coverage/gaps")]
    joined = "\n".join(blobs)
    for needle in (str(tmp_path), str(art), os.path.expanduser("~"), "/sessions/"):
        assert needle not in joined, needle


def test_authorization_required_when_token_configured(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/tests"), ("get", "/tests/files"), ("post", "/tests/discover"), ("post", "/tests/plan"),
                             ("post", "/tests/generate"), ("get", "/tests/generations"), ("get", "/tests/runs"),
                             ("post", "/tests/execute"), ("get", "/coverage"), ("get", "/coverage/files"), ("get", "/coverage/gaps")):
            assert getattr(c, method)(f"{API}/repositories/abc{path}").status_code == 401, path
    reset_engine()
    reset_settings_cache()
