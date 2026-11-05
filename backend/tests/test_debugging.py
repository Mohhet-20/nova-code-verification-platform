"""Failure ingestion, fingerprints, localization, investigations, impact and approved verification."""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.services.debugging import ingest, localize, parsing
from tests.conftest import register_and_index
from tests.test_repository_intelligence import write

API = "/api/v1"
SECRET = "sk_live_abcdef1234567890abcdef"

CALC = textwrap.dedent('''
    def helper(x):
        if x == 0:
            raise ValueError("bad value 0")
        return 10 / x


    def percent(a, b):
        return helper(b) * a


    def double(n):
        return n + n
''').lstrip("\n")

TEST_A = textwrap.dedent('''
    from pkg.calc import double, percent


    def test_exc():
        assert percent(1, 0) == 5


    def test_assert():
        assert double(2) == 99


    def test_ok():
        assert percent(1, 2) == 5
''').lstrip("\n")

FILES = {"pkg/__init__.py": "", "pkg/calc.py": CALC, "tests/test_a.py": TEST_A}

# Real pytest output shapes (see tests/test_debugging.py::test_parser_handles_real_pytest_output).
EXC_TEXT = """def test_exc():
>       assert percent(1, 0) == 5

tests/test_a.py:5:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
pkg/calc.py:8: in percent
    return helper(b) * a
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

x = 0

    def helper(x):
        if x == 0:
>           raise ValueError("bad value 0")
E           ValueError: bad value 0

pkg/calc.py:3: ValueError
"""
ASSERT_TEXT = """def test_assert():
>       assert double(2) == 99
E       assert 4 == 99
E        +  where 4 = double(2)

tests/test_a.py:9: AssertionError
"""


def base(rid: str) -> str:
    return f"{API}/repositories/{rid}"


@pytest.fixture
def art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOVA_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    reset_settings_cache()
    return tmp_path / "artifacts"


def setup(client: TestClient, root: Path, files: dict[str, str] | None = None, name: str = "d") -> str:
    write(root, FILES if files is None else files)
    return register_and_index(client, root, name)["repo"]["id"]


def post_ingest(client: TestClient, rid: str, text: str, source: str = "pytest_text", test_id: str | None = None):  # type: ignore[no-untyped-def]
    body: dict = {"source": source, "text": text}
    if test_id:
        body["test_id"] = test_id
    return client.post(f"{base(rid)}/failures/ingest", json=body)


def ingest_one(client: TestClient, rid: str, text: str, **kw: str) -> dict:
    r = post_ingest(client, rid, text, **kw)
    assert r.status_code == 201, r.text
    return r.json()["items"][0]


def run_suite(client: TestClient, rid: str, targets_confirm: bool = True) -> dict:
    client.post(f"{base(rid)}/tests/discover")
    r = client.post(f"{base(rid)}/tests/execute", json={"confirm": targets_confirm, "coverage": False})
    assert r.status_code == 202, r.text
    return client.get(f"{base(rid)}/tests/runs/{r.json()['id']}").json()


def investigate(client: TestClient, rid: str, fid: str) -> dict:
    r = client.post(f"{base(rid)}/failures/{fid}/investigations")
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ parsing / ingestion

def test_parser_handles_real_pytest_output() -> None:
    pf = parsing.parse_pytest_text(EXC_TEXT, "ValueError: bad value 0")
    assert (pf.category, pf.exception_type) == ("exception", "ValueError")
    assert [(f.path, f.line) for f in pf.frames] == [("tests/test_a.py", 5), ("pkg/calc.py", 8), ("pkg/calc.py", 3)]
    assert pf.frames[1].function == "percent"
    pa = parsing.parse_pytest_text(ASSERT_TEXT, "assert 4 == 99\n +  where 4 = double(2)")
    assert (pa.category, pa.exception_type) == ("assertion", "AssertionError")
    tb = parsing.parse_python_traceback('Traceback (most recent call last):\n  File "/r/pkg/a.py", line 3, in f\n    x\nKeyError: \'k\'')
    assert (tb.category, tb.exception_type, tb.frames[0].function) == ("exception", "KeyError", "f")


def test_standard_assertion_failure_is_ingested(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, ASSERT_TEXT, test_id="tests/test_a.py::test_assert")
    assert f["category"] == "assertion" and f["exception_type"] == "AssertionError"
    assert f["test_id"] == "tests/test_a.py::test_assert" and f["occurrence_count"] == 1 and f["status"] == "open"


def test_exception_in_repository_code_maps_frames_and_symbols(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    d = client.get(f"{base(rid)}/failures/{f['id']}").json()
    frames = d["frames"]
    assert [x["rel_path"] for x in frames] == ["tests/test_a.py", "pkg/calc.py", "pkg/calc.py"]
    assert [x["is_test"] for x in frames] == [True, False, False]
    assert all(x["scope"] == "repository" for x in frames)
    assert frames[1]["function"] == "percent" and frames[2]["function"] == "pkg.calc.helper"  # trace name / index name
    assert frames[2]["function_source"] == "index"
    assert d["revision"] is None and d["revision_known"] is False  # never invented
    assert f["top_location"] == "pkg/calc.py:3"


def test_frame_symbols_are_real_indexed_symbols(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT)
    frames = client.get(f"{base(rid)}/failures/{f['id']}").json()["frames"]
    sym = client.get(f"{base(rid)}/symbols/{frames[2]['symbol_stable_id']}")
    assert sym.status_code == 200 and sym.json()["qualified_name"] == "pkg.calc.helper"


def test_failure_without_repository_frames(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    text = ('Traceback (most recent call last):\n  File "/usr/lib/python3.12/json/decoder.py", line 338, in decode\n    x\n'
            '  File "/venv/lib/python3.12/site-packages/lib/core.py", line 9, in run\n    y\nValueError: nope')
    f = ingest_one(client, rid, text, source="python_traceback")
    d = client.get(f"{base(rid)}/failures/{f['id']}").json()
    assert {x["scope"] for x in d["frames"]} == {"runtime", "dependency"}
    assert d["frames"][1]["display_path"].startswith("site-packages/")
    assert any("No stack frame maps" in w for w in d["warnings"])
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert loc["state"] == "insufficient_evidence" and loc["candidates"] == []
    assert loc["missing_evidence"] and "heuristic" in loc["interpretation"] or "ranking" in loc["interpretation"]


def test_malformed_stack_trace_is_kept_without_inventing_structure(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, "this is not a traceback ::: ??? \x00\x1b[31mred\x1b[0m", source="python_traceback")
    d = client.get(f"{base(rid)}/failures/{f['id']}").json()
    assert d["category"] == "unknown" and d["frames"] == [] and d["exception_type"] is None
    assert "\x1b" not in d["message"] and "\x00" not in d["message"]
    assert client.post(f"{base(rid)}/failures/ingest", json={"source": "pytest_text", "text": "   "}).status_code == 422


def test_oversized_report_is_rejected(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_DEBUG_MAX_REPORT_BYTES", "2000")
    reset_settings_cache()
    rid = setup(client, tmp_path / "r")
    r = post_ingest(client, rid, "E   ValueError: x\n" * 500)
    assert r.status_code == 413 and "at most 2000 bytes" in r.text
    assert client.get(f"{base(rid)}/failures").json()["total"] == 0


def test_path_escape_is_never_mapped_or_read(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    text = (f'Traceback (most recent call last):\n  File "../outside.py", line 1, in f\n    x\n  File "{outside}", line 1, in g\n    y\n'
            '  File "../../../etc/passwd", line 1, in h\n    z\nValueError: boom')
    f = ingest_one(client, rid, text, source="python_traceback")
    d = client.get(f"{base(rid)}/failures/{f['id']}").json()
    assert all(x["rel_path"] is None and x["scope"] in ("unknown", "runtime") for x in d["frames"])
    assert all(".." not in x["display_path"] and str(tmp_path) not in x["display_path"] for x in d["frames"])
    assert any("outside the repository" in w for w in d["warnings"])
    # a repository-relative path that merely contains '..' segments but stays inside is normalised, not trusted blindly
    r = parsing.resolve_frame_path("pkg/../pkg/calc.py", {"pkg/calc.py"}, None)
    assert r.rel_path == "pkg/calc.py"
    assert parsing.resolve_frame_path("../pkg/calc.py", {"pkg/calc.py"}, None).rel_path is None


def test_credentials_and_paths_are_redacted_everywhere(art: Path, client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rid = setup(client, tmp_path / "r")
    caplog.set_level(logging.DEBUG)
    text = EXC_TEXT.replace("bad value 0", f"bad value password={SECRET} at {tmp_path}/secret/data.txt")
    f = ingest_one(client, rid, text)
    blobs = [client.get(f"{base(rid)}/failures/{f['id']}").text, client.get(f"{base(rid)}/failures").text,
             client.get(f"{base(rid)}/failures/{f['id']}/localization").text]
    inv = investigate(client, rid, f["id"])
    blobs += [client.get(f"{base(rid)}/investigations/{inv['id']}").text, client.get(f"{base(rid)}/failures/{f['id']}/related").text]
    joined = "\n".join(blobs) + caplog.text
    assert SECRET not in joined and str(tmp_path) not in joined
    assert "[REDACTED]" in joined
    notes = client.patch(f"{base(rid)}/investigations/{inv['id']}", json={"notes": f"token={SECRET} keep going"}).json()["notes"]
    assert SECRET not in notes


def test_junit_xml_ingest_and_xml_hardening(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    xml = ('<testsuites><testsuite><testcase classname="tests.test_a" name="test_exc"><failure message="ValueError: bad value 0">'
           + EXC_TEXT.replace("&", "&amp;").replace("<", "&lt;") + '</failure></testcase><testcase classname="tests.test_a" name="test_ok"/>'
           '<testcase classname="" name="tests.test_a"><error message="collection failure">E   SyntaxError: bad</error></testcase>'
           '</testsuite></testsuites>')
    r = post_ingest(client, rid, xml, source="junit_xml")
    assert r.status_code == 201 and r.json()["created"] == 2
    cats = {i["category"] for i in r.json()["items"]}
    assert cats == {"exception", "collection_error"}
    assert {i["test_id"] for i in r.json()["items"]} == {"tests/test_a.py::test_exc", "tests/test_a.py"}
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="t"><failure message="&a;"/></testcase></testsuite>'
    assert post_ingest(client, rid, bomb, source="junit_xml").status_code == 422
    assert post_ingest(client, rid, "<testsuite></testsuite>", source="junit_xml").status_code == 422
    assert post_ingest(client, rid, "not xml at all", source="junit_xml").status_code == 422


# ------------------------------------------------------------------------------------------------ fingerprints

def test_equivalent_failures_deduplicate_and_recurrence_is_tracked(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    a = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    shifted = EXC_TEXT.replace("pkg/calc.py:3:", "pkg/calc.py:4:")  # line moved within the same function (symbol resolves identically)
    r = post_ingest(client, rid, shifted, test_id="tests/test_a.py::test_exc")
    assert r.status_code == 201 and r.json()["created"] == 0 and r.json()["recurred"] == 1
    b = r.json()["items"][0]
    assert b["id"] == a["id"] and b["occurrence_count"] == 2
    assert b["first_seen_at"] == a["first_seen_at"] and b["last_seen_at"] >= a["last_seen_at"]
    # same message shape with different literal numbers
    c = post_ingest(client, rid, EXC_TEXT.replace("bad value 0", "bad value 7"), test_id="tests/test_a.py::test_exc").json()["items"][0]
    assert c["id"] == a["id"] and c["occurrence_count"] == 3
    assert client.get(f"{base(rid)}/failures").json()["total"] == 1
    assert len(client.get(f"{base(rid)}/failures/{a['id']}").json()["occurrences"]) == 3


def test_distinct_failures_stay_separate(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    a = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    other_test = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_other")  # same message and frames, different test
    other_type = ingest_one(client, rid, EXC_TEXT.replace("ValueError", "KeyError"), test_id="tests/test_a.py::test_exc")
    other_frames = ingest_one(client, rid, EXC_TEXT.replace("pkg/calc.py:8: in percent", "pkg/__init__.py:8: in percent"),
                              test_id="tests/test_a.py::test_exc")
    assert len({a["id"], other_test["id"], other_type["id"], other_frames["id"]}) == 4


def test_fingerprint_function_properties() -> None:
    fp = ingest.compute_fingerprint
    frames = [("pkg/a.py", "f")]
    base_fp = fp("exception", "ValueError", "bad value 1", "t.py::t", frames)
    assert base_fp == fp("exception", "ValueError", "bad value 99", "t.py::t", frames)
    assert base_fp == fp("exception", "ValueError", "bad value 1", "t.py::t", frames)
    assert base_fp != fp("exception", "KeyError", "bad value 1", "t.py::t", frames)
    assert base_fp != fp("exception", "ValueError", "other words 1", "t.py::t", frames)
    assert base_fp != fp("exception", "ValueError", "bad value 1", "t.py::u", frames)
    assert ingest.message_structure("x=0x1f 'abc' 12.5") == "x=H S N"


def test_failures_are_never_merged_across_repositories(art: Path, client: TestClient, tmp_path: Path) -> None:
    r1 = setup(client, tmp_path / "a", name="a")
    r2 = setup(client, tmp_path / "b", name="b")
    a = ingest_one(client, r1, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    b = ingest_one(client, r2, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    assert a["id"] != b["id"] and a["occurrence_count"] == b["occurrence_count"] == 1
    assert client.get(f"{base(r2)}/failures/{a['id']}").status_code == 404
    assert client.get(f"{base(r1)}/failures").json()["total"] == 1


# ------------------------------------------------------------------------------------------------ localization

def test_single_candidate_ranking_and_explanation(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r", {"pkg/__init__.py": "", "pkg/only.py": "def boom(x):\n    raise RuntimeError('no')\n"})
    f = ingest_one(client, rid, "pkg/only.py:2: RuntimeError\nE   RuntimeError: no\n")
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert loc["state"] == "ranked" and len(loc["candidates"]) == 1
    c = loc["candidates"][0]
    assert (c["role"], c["rel_path"], c["symbol"]) == ("exception_origin", "pkg/only.py", "pkg.only.boom")
    assert c["score"] == localize.DEFAULT_WEIGHTS["exception_origin"] and c["confidence"] == "medium"  # one evidence type only: never "high"
    assert "score 5" in c["explanation"] and c["evidence"][0]["type"] == "exception_origin"
    assert "not probabilities" in loc["interpretation"]


def test_multiple_candidates_roles_and_documented_weights(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    got = [(c["rank"], c["symbol"], c["role"]) for c in loc["candidates"]]
    assert got == [(1, "pkg.calc.helper", "exception_origin"), (2, "pkg.calc.percent", "caller_context")]
    top, second = loc["candidates"]
    w = localize.DEFAULT_WEIGHTS
    assert top["score"] == pytest.approx(w["exception_origin"] + w["test_imports_file"])  # origin + test imports the file
    assert second["score"] == pytest.approx(w["caller_context"] + w["test_imports_file"])
    assert top["confidence"] == "high" and second["confidence"] == "low"  # 2.75 < 3 and not a strong role
    assert loc["weights"] == w and {r["role"] for r in loc["frame_roles"]} == {"exception_origin", "caller_context", "failing_test"}


def test_assertion_failure_uses_call_graph_not_stack(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, ASSERT_TEXT, test_id="tests/test_a.py::test_assert")
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    top = loc["candidates"][0]
    assert top["symbol"] == "pkg.calc.double" and top["role"] == "called_by_test"
    assert {e["type"] for e in top["evidence"]} >= {"called_on_failing_line", "test_imports_file"}
    assert top["score"] >= localize.DEFAULT_WEIGHTS["called_on_failing_line"]


def test_insufficient_evidence_is_explicit(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r", {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    f = ingest_one(client, rid, "ZeroDivisionError: division by zero")
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert loc["state"] == "insufficient_evidence" and loc["candidates"] == []
    assert any("No stack frame" in m for m in loc["missing_evidence"])
    inv = investigate(client, rid, f["id"])
    assert inv["content"]["localization_state"] == "insufficient_evidence" and inv["content"]["candidates"] == []


def test_ranking_is_deterministic_and_deduplicates_evidence(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    a = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    b = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert a == b
    for c in a["candidates"]:
        ids = [e["id"] for e in c["evidence"]]
        assert len(ids) == len(set(ids))
        assert c["score"] == pytest.approx(sum(e["weight"] for e in c["evidence"]) + sum(e["weight"] for e in c["contradicting"]))


def test_findings_enrich_ranking_without_double_counting(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/ev.py": "def run(code):\n    return eval(code)\n"}
    rid = setup(client, tmp_path / "r", files)
    assert client.post(f"{base(rid)}/analysis").status_code == 202
    assert client.get(f"{base(rid)}/findings", params={"path": "pkg/ev.py"}).json()["total"] >= 1
    f = ingest_one(client, rid, "pkg/ev.py:2: in run\n    return eval(code)\npkg/ev.py:1: NameError\nE   NameError: name 'x' is not defined\n")
    c = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()["candidates"][0]
    kinds = [e["type"] for e in c["evidence"] if e["type"].startswith("finding")]
    assert kinds and len(kinds) == len(set(e["id"] for e in c["evidence"] if e["type"].startswith("finding")))
    d = client.get(f"{base(rid)}/failures/{f['id']}").json()
    assert any(x["rel_path"] == "pkg/ev.py" for x in d["related_findings"])
    inv = investigate(client, rid, f["id"])
    assert any(x["rel_path"] == "pkg/ev.py" for x in inv["content"]["related_findings"])


def test_configurable_weights_and_invalid_override(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    monkeypatch.setenv("NOVA_DEBUG_WEIGHTS_JSON", '{"exception_origin": 9.0}')
    reset_settings_cache()
    top = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()["candidates"][0]
    assert top["score"] == pytest.approx(9.0 + localize.DEFAULT_WEIGHTS["test_imports_file"])
    monkeypatch.setenv("NOVA_DEBUG_WEIGHTS_JSON", '{"made_up": 1}')
    reset_settings_cache()
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert loc["weights"] == localize.DEFAULT_WEIGHTS and any("invalid" in n for n in loc["notes"])


# ------------------------------------------------------------------------------------------------ dependency impact

def test_missing_dependency_data_is_reported_honestly(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r", {"solo.py": "def f():\n    raise ValueError('x')\n"})
    f = ingest_one(client, rid, "solo.py:2: ValueError\nE   ValueError: x\n")
    loc = client.get(f"{base(rid)}/failures/{f['id']}/localization").json()
    assert any("dependency graph is empty" in m for m in loc["missing_evidence"])
    inv = investigate(client, rid, f["id"])
    imp = inv["content"]["impact"]
    assert imp["graph_available"] is False and imp["dependents"] == [] and any("unknown, not absent" in x for x in imp["limitations"])


def test_cyclic_dependencies_terminate_and_respect_depth(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n\ndef fa():\n    raise ValueError('a')\n",
             "pkg/b.py": "from pkg import a\n\ndef fb():\n    return a.fa()\n",
             "pkg/c.py": "from pkg import b\n", "pkg/d.py": "from pkg import c\n", "pkg/e.py": "from pkg import d\n"}
    rid = setup(client, tmp_path / "r", files)
    f = ingest_one(client, rid, "pkg/a.py:4: ValueError\nE   ValueError: a\n")
    inv = investigate(client, rid, f["id"])
    imp = inv["content"]["impact"]
    paths = [d["rel_path"] for d in imp["dependents"]]
    assert len(paths) == len(set(paths)) and "pkg/a.py" not in paths  # the cycle a <-> b does not loop or list the seed
    assert imp["max_depth"] == 2 and max(d["depth"] for d in imp["dependents"]) <= 2
    assert "pkg/e.py" not in paths and any("beyond depth 2" in x for x in imp["limitations"])
    deep = client.get(f"{base(rid)}/failures/{f['id']}/related").json()["impact"]
    assert deep["max_depth"] == 2


# ------------------------------------------------------------------------------------------------ investigations

def test_investigation_separates_facts_from_hypotheses(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    inv = investigate(client, rid, f["id"])
    c = inv["content"]
    assert all(o["kind"] == "fact" for o in c["observations"])
    assert c["hypotheses"] and all(h["kind"] == "hypothesis" and h["status"] == "unverified" for h in c["hypotheses"])
    origin = next(h for h in c["hypotheses"] if h["id"] == "H-origin")
    assert origin["supporting"] and origin["contradicting"]  # both sides are present
    assert c["missing_information"] and c["verification_steps"] and c["next_tests"] and c["limitations"]
    assert any("No AI provider was used" in x for x in c["limitations"])
    assert "proven" not in str(c).lower() or "not proof" in str(c).lower()
    assert client.post(f"{base(rid)}/failures/{f['id']}/investigations").status_code == 409  # one open investigation per failure
    got = client.get(f"{base(rid)}/failures/{f['id']}").json()
    assert got["status"] == "investigating" and got["investigation_id"] == inv["id"]


def test_investigation_patch_workflow(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT)
    inv = investigate(client, rid, f["id"])
    url = f"{base(rid)}/investigations/{inv['id']}"
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"status": "bogus"}).status_code == 422
    assert client.patch(url, json={"status": "closed"}).status_code == 409  # needs the user's own resolution
    ok = client.patch(url, json={"status": "closed", "resolution": "not_a_bug", "notes": "intended validation"})
    assert ok.status_code == 200 and ok.json()["status"] == "closed" and ok.json()["resolution"] == "not_a_bug"
    assert client.get(f"{base(rid)}/failures/{f['id']}").json()["status"] == "resolved"
    listing = client.get(f"{base(rid)}/investigations", params={"status": "closed"}).json()
    assert listing["total"] == 1 and listing["items"][0]["failure_summary"]
    assert client.post(f"{base(rid)}/failures/{f['id']}/investigations").status_code == 201  # a closed one does not block a new one


# ------------------------------------------------------------------------------------------------ verification

def test_verification_requires_confirmation_and_discovered_tests(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    inv = investigate(client, rid, f["id"])
    url = f"{base(rid)}/investigations/{inv['id']}/verification"
    assert client.post(url, json={}).status_code == 409  # discovery has not run
    client.post(f"{base(rid)}/tests/discover")
    pv = client.post(url, json={})
    assert pv.status_code == 200 and pv.json()["executes"] is False and pv.json()["targets"] == ["tests/test_a.py::test_exc"]
    assert "not a sandbox" in pv.json()["execution"]
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0  # previewing ran nothing
    for bad in (["tests/test_missing.py"], ["../../etc/passwd"], ["tests/test_a.py::test_nope; rm -rf /"]):
        assert client.post(url, json={"confirm": True, "test_ids": bad}).status_code == 422, bad
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0


def test_verification_persists_then_not_reproduced_after_fix(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    run = run_suite(client, rid)
    assert run["status"] == "failed"
    ing = client.post(f"{base(rid)}/failures/ingest", json={"source": "execution_run", "execution_run_id": run["id"]}).json()
    assert ing["created"] == 2
    f = next(i for i in ing["items"] if i["category"] == "exception")
    inv = investigate(client, rid, f["id"])
    url = f"{base(rid)}/investigations/{inv['id']}/verification"
    v1 = client.post(url, json={"confirm": True})
    assert v1.status_code == 202 and v1.json()["outcome"] in ("PENDING", "PERSISTS")
    got = client.get(f"{base(rid)}/investigations/{inv['id']}").json()["verifications"][0]
    assert got["outcome"] == "PERSISTS"
    assert got["original_run"]["id"] == run["id"] and got["verification_run"]["id"] != run["id"]
    assert got["verification_run"]["failures"] and got["verification_run"]["tests_failed"] == 1
    assert client.get(f"{base(rid)}/failures/{f['id']}").json()["occurrence_count"] == 2  # linked to the new execution
    # the user fixes the code; the test passes; the result is "not reproduced", never "fixed"
    (root / "pkg/calc.py").write_text(CALC.replace('raise ValueError("bad value 0")', "return 5"), encoding="utf-8")
    v2 = client.post(url, json={"confirm": True})
    assert v2.status_code == 202
    outcomes = [v["outcome"] for v in client.get(f"{url}s").json()["items"]]
    assert outcomes[0] == "NOT_REPRODUCED_IN_VERIFICATION_RUN" and outcomes[1] == "PERSISTS"
    latest = client.get(f"{url}s").json()["items"][0]
    assert "not proof" in latest["meaning"] and latest["verification_run"]["tests_passed"] == 1
    assert "FIXED" not in str(client.get(f"{url}s").json()).upper().replace("NOT_REPRODUCED", "")


def test_changed_failure_is_not_called_not_reproduced(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    run = run_suite(client, rid)
    ing = client.post(f"{base(rid)}/failures/ingest", json={"source": "execution_run", "execution_run_id": run["id"]}).json()
    f = next(i for i in ing["items"] if i["category"] == "exception")
    inv = investigate(client, rid, f["id"])
    (root / "pkg/calc.py").write_text(CALC.replace('raise ValueError("bad value 0")', "return 0"), encoding="utf-8")  # wrong "fix"
    assert client.post(f"{base(rid)}/investigations/{inv['id']}/verification", json={"confirm": True}).status_code == 202
    v = client.get(f"{base(rid)}/investigations/{inv['id']}/verifications").json()["items"][0]
    assert v["outcome"] == "CHANGED_FAILURE"


def test_verification_that_cannot_be_evaluated(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    inv = investigate(client, rid, f["id"])
    client.post(f"{base(rid)}/tests/discover")
    monkeypatch.setenv("NOVA_TEST_PYTHON", str(tmp_path / "no-such-python"))
    reset_settings_cache()
    assert client.post(f"{base(rid)}/investigations/{inv['id']}/verification", json={"confirm": True}).status_code == 202
    v = client.get(f"{base(rid)}/investigations/{inv['id']}/verifications").json()["items"][0]
    assert v["outcome"] == "INCONCLUSIVE" and v["reason"]
    assert v["verification_run"]["status"] in ("error", "rejected")


def test_verification_is_never_started_without_confirmation(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT, test_id="tests/test_a.py::test_exc")
    inv = investigate(client, rid, f["id"])
    client.post(f"{base(rid)}/tests/discover")
    for body in ({}, {"confirm": False}, {"confirm": False, "test_ids": ["tests/test_a.py"]}):
        assert client.post(f"{base(rid)}/investigations/{inv['id']}/verification", json=body).status_code == 200
    assert client.get(f"{base(rid)}/tests/runs").json()["total"] == 0
    assert client.get(f"{base(rid)}/investigations/{inv['id']}").json()["verifications"] == []


# ------------------------------------------------------------------------------------------------ API hygiene

def test_authorization_and_roles(migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import reset_engine
    from app.main import create_app

    monkeypatch.setenv("NOVA_API_TOKEN", "t" * 32)
    reset_settings_cache()
    reset_engine()
    with TestClient(create_app()) as c:
        for method, path in (("get", "/failures"), ("post", "/failures/ingest"), ("get", "/failures/x"), ("get", "/failures/x/localization"),
                             ("post", "/failures/x/investigations"), ("get", "/investigations"), ("get", "/investigations/x"),
                             ("patch", "/investigations/x"), ("get", "/failures/x/related"), ("post", "/investigations/x/verification")):
            assert getattr(c, method)(f"{API}/repositories/abc{path}").status_code == 401, path
    reset_engine()
    reset_settings_cache()


def test_read_only_role_cannot_mutate(art: Path, client: TestClient, tmp_path: Path) -> None:
    from sqlalchemy import update

    from app.db.session import get_session_factory
    from app.models import Membership

    rid = setup(client, tmp_path / "r")
    f = ingest_one(client, rid, EXC_TEXT)
    inv = investigate(client, rid, f["id"])
    with get_session_factory()() as s:
        s.execute(update(Membership).values(role="viewer"))
        s.commit()
    assert post_ingest(client, rid, EXC_TEXT).status_code == 403
    assert client.post(f"{base(rid)}/failures/{f['id']}/investigations").status_code == 403
    assert client.patch(f"{base(rid)}/investigations/{inv['id']}", json={"notes": "x"}).status_code == 403
    assert client.post(f"{base(rid)}/investigations/{inv['id']}/verification", json={"confirm": True}).status_code == 403
    assert client.get(f"{base(rid)}/failures/{f['id']}").status_code == 200


def test_cross_repository_isolation_for_investigations(art: Path, client: TestClient, tmp_path: Path) -> None:
    r1 = setup(client, tmp_path / "a", name="a")
    r2 = setup(client, tmp_path / "b", name="b")
    f = ingest_one(client, r1, EXC_TEXT)
    inv = investigate(client, r1, f["id"])
    for path in (f"/failures/{f['id']}", f"/failures/{f['id']}/localization", f"/failures/{f['id']}/related",
                 f"/investigations/{inv['id']}", f"/investigations/{inv['id']}/verifications"):
        assert client.get(base(r2) + path).status_code == 404, path
    assert client.post(f"{base(r2)}/failures/{f['id']}/investigations").status_code == 404
    assert client.patch(f"{base(r2)}/investigations/{inv['id']}", json={"notes": "x"}).status_code == 404
    assert client.post(f"{base(r2)}/investigations/{inv['id']}/verification", json={}).status_code == 404
    assert client.get(f"{base(r2)}/investigations").json()["total"] == 0
    assert client.get(f"{base('nope')}/failures").status_code == 404
    other = client.post(f"{base(r2)}/failures/ingest", json={"source": "execution_run", "execution_run_id": "0" * 32})
    assert other.status_code == 404


def test_pagination_filters_and_invalid_inputs(art: Path, client: TestClient, tmp_path: Path) -> None:
    rid = setup(client, tmp_path / "r")
    for n in range(5):
        ingest_one(client, rid, EXC_TEXT.replace("ValueError", f"Err{n}Error"), test_id=f"tests/test_a.py::test_{n}")
    ingest_one(client, rid, ASSERT_TEXT, test_id="tests/test_a.py::test_assert")
    page = client.get(f"{base(rid)}/failures", params={"page_size": 4}).json()
    assert page["total"] == 6 and len(page["items"]) == 4 and page["pages"] == 2
    assert client.get(f"{base(rid)}/failures", params={"page": 2, "page_size": 4}).json()["items"].__len__() == 2
    assert client.get(f"{base(rid)}/failures", params={"category": "assertion"}).json()["total"] == 1
    assert client.get(f"{base(rid)}/failures", params={"q": "Err3"}).json()["total"] == 1
    assert client.get(f"{base(rid)}/failures", params={"path": "pkg/calc"}).json()["total"] == 5
    assert client.get(f"{base(rid)}/failures", params={"q": "%"}).json()["total"] == 0  # wildcard characters are literal
    asc = [i["id"] for i in client.get(f"{base(rid)}/failures", params={"sort": "first_seen", "order": "asc", "page_size": 100}).json()["items"]]
    desc = [i["id"] for i in client.get(f"{base(rid)}/failures", params={"sort": "first_seen", "order": "desc", "page_size": 100}).json()["items"]]
    assert asc == desc[::-1]
    for params in ({"sort": "password"}, {"order": "sideways"}, {"category": "weird"}, {"status": "nope"}, {"page": 0}, {"page_size": 1000}, {"q": "x" * 300}):
        assert client.get(f"{base(rid)}/failures", params=params).status_code == 422, params
    assert client.get(f"{base(rid)}/investigations", params={"status": "weird"}).status_code == 422
    assert client.post(f"{base(rid)}/failures/ingest", json={"source": "carrier_pigeon", "text": "x"}).status_code == 422
    assert client.post(f"{base(rid)}/failures/ingest", json={"source": "execution_run"}).status_code == 422


def test_ingest_from_execution_run_requires_failures(art: Path, client: TestClient, tmp_path: Path) -> None:
    files = dict(FILES)
    files["tests/test_a.py"] = "def test_fine():\n    assert True\n"
    rid = setup(client, tmp_path / "r", files)
    run = run_suite(client, rid)
    assert run["status"] == "passed"
    r = client.post(f"{base(rid)}/failures/ingest", json={"source": "execution_run", "execution_run_id": run["id"]})
    assert r.status_code == 409 and "no failures" in r.text


def test_api_responses_contain_no_absolute_paths(art: Path, client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "r"
    rid = setup(client, root)
    text = EXC_TEXT.replace("pkg/calc.py:8:", f"{root}/pkg/calc.py:8:")  # absolute path inside the registered root maps to a relative one
    f = ingest_one(client, rid, text, test_id="tests/test_a.py::test_exc")
    inv = investigate(client, rid, f["id"])
    client.post(f"{base(rid)}/tests/discover")
    client.post(f"{base(rid)}/investigations/{inv['id']}/verification", json={"confirm": True})
    blobs = [client.get(base(rid) + p).text for p in (
        "/failures", f"/failures/{f['id']}", f"/failures/{f['id']}/localization", f"/failures/{f['id']}/related",
        "/investigations", f"/investigations/{inv['id']}", f"/investigations/{inv['id']}/verifications")]
    joined = "\n".join(blobs)
    for needle in (str(tmp_path), str(art), "/sessions/"):
        assert needle not in joined, needle
    frames = client.get(f"{base(rid)}/failures/{f['id']}").json()["frames"]
    assert frames[1]["rel_path"] == "pkg/calc.py"
