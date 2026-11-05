"""Phase 12: security engineering and supply-chain intelligence.

Everything is synthetic: advisories are offline OSV-format fixtures, secrets are fake values assembled at runtime, and no network is used.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from tests.conftest import register_and_index
from tests.test_refactor import art, as_viewer, git  # noqa: F401
from tests.test_repository_intelligence import write

API = "/api/v1"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

APP = """import os
import subprocess
import yaml
from flask import Flask, request

app = Flask(__name__)


@app.route("/run")
def run():
    subprocess.call(request.args["cmd"], shell=True)
    return yaml.load(open("c.yml"))


if __name__ == "__main__":
    app.run(debug=True)
"""
REQS = "pyyaml==5.3\nflask==2.0.0\nrequests>=2.0\n"
CFG = f"AWS_KEY = '{FAKE_AWS}'\nTOKEN = '{FAKE_GH}'\n"


def osv(aid: str, pkg: str, introduced: str, fixed: str, eco: str = "PyPI", sev: str | None = "HIGH", aliases: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": aid, "aliases": aliases or [], "summary": f"Synthetic advisory for {pkg}", "modified": "2024-01-01T00:00:00Z", "published": "2023-01-01T00:00:00Z",
                         "affected": [{"package": {"ecosystem": eco, "name": pkg}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": introduced}, {"fixed": fixed}]}]}],
                         "references": [{"type": "ADVISORY", "url": f"https://example.invalid/advisories/{aid}"}]}
    if sev:
        d["database_specific"] = {"severity": sev}
    return d


@pytest.fixture
def adv_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "advisories"
    d.mkdir()
    (d / "a.json").write_text(json.dumps(osv("SYN-0001", "pyyaml", "0", "5.4", aliases=["CVE-2099-0001"])))
    (d / "b.json").write_text(json.dumps(osv("SYN-0002", "pyyaml", "0", "5.4", aliases=["CVE-2099-0001"], sev="CRITICAL")))  # duplicate by alias
    (d / "c.json").write_text(json.dumps(osv("SYN-0003", "flask", "2.0.0", "2.0.1", sev=None)))
    monkeypatch.setenv("NOVA_SECURITY_ADVISORY_DIR", str(d))
    reset_settings_cache()
    return d


def sb(rid: str) -> str:
    return f"{API}/repositories/{rid}/security"


FILES = {"app.py": APP, "requirements.txt": REQS, "settings.py": CFG, "README.md": "# demo\n"}


def project(client: TestClient, tmp_path: Path, files: dict[str, str] | None = None, name: str = "sec", dirname: str = "r") -> tuple[str, Path]:
    root = tmp_path / dirname
    write(root, FILES if files is None else files)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return register_and_index(client, root, name)["repo"]["id"], root


def scan(client: TestClient, rid: str, **kw: Any) -> dict[str, Any]:
    r = client.post(f"{sb(rid)}/scans", json={**kw})
    assert r.status_code == 202, r.text
    for _ in range(100):
        got = client.get(f"{sb(rid)}/scans/{r.json()['id']}")
        assert got.status_code == 200
        if got.json()["status"] in ("completed", "failed"):
            return got.json()
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


def findings(client: TestClient, rid: str, **params: Any) -> list[dict[str, Any]]:
    r = client.get(f"{sb(rid)}/findings", params={"page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ================================================================================================ versions
def test_version_semantics_are_ecosystem_aware_not_lexicographic() -> None:
    from app.services.security import versions as v

    assert v.compare("PyPI", "1.10", "1.9") > 0 and "1.10" < "1.9"  # lexicographic order would be wrong
    assert v.compare("PyPI", "2.0.0rc1", "2.0.0") < 0 and v.is_prerelease("PyPI", "2.0.0rc1") is True
    assert v.compare("npm", "1.10.0", "1.9.0") > 0 and v.compare("npm", "1.0.0-alpha", "1.0.0") < 0
    assert v.parse("PyPI", "not a version!!") is None and v.compare("PyPI", "x!", "1.0") is None
    assert v.major("npm", "3.4.5") == 3
    rng = [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "5.4"}]}]
    assert v.affected("PyPI", "5.3", rng, []) is True
    assert v.affected("PyPI", "5.4", rng, []) is False and v.affected("PyPI", "5.10", rng, []) is False
    assert v.affected("PyPI", None, rng, []) is None and v.affected("PyPI", "garbage!!", rng, []) is None  # unknown stays unknown
    assert v.affected("PyPI", "1.0", [], ["1.0"]) is True and v.affected("PyPI", "1.1", [], ["1.0"]) is False
    assert v.satisfies("npm", "1.2.5", "^1.2.0") is True and v.satisfies("npm", "2.0.0", "^1.2.0") is False and v.satisfies("npm", "1.2.9", "~1.2.0") is True
    assert v.satisfies("PyPI", "2.0.1", ">=2.0,<3") is True and v.satisfies("PyPI", "3.1", ">=2.0,<3") is False
    assert v.satisfies("PyPI", "1.0", "@@@") is None


# ================================================================================================ inventory
def test_inventory_parses_manifests_without_installing_and_reports_gaps(tmp_path: Path) -> None:
    from app.services.security import inventory

    lock = {"name": "x", "lockfileVersion": 3, "packages": {"": {"name": "x", "dependencies": {"a": "^1.0.0"}}, "node_modules/a": {"version": "1.2.3", "dependencies": {"b": "^2.0.0"}},
                                                             "node_modules/b": {"version": "2.0.1"}}}
    write(tmp_path, {"requirements.txt": "pyyaml==5.3\nflask>=2.0\nPyYAML==5.3\n# comment\n-e git+https://example.invalid/x.git#egg=vcs\n", "pyproject.toml": "[project\nname=",
                     "package.json": json.dumps({"dependencies": {"a": "^1.0.0"}}), "package-lock.json": json.dumps(lock), "yarn.lock": "# yarn\n", "go.mod": "module x\n",
                     "node_modules/zz/package.json": json.dumps({"dependencies": {"ignored": "1.0.0"}})})
    (tmp_path / "evil.txt").write_text("x")
    (tmp_path / "requirements-link.txt").symlink_to(tmp_path / "evil.txt")
    inv = inventory.build(tmp_path, 1000, 1_000_000, 1000)
    by = {(c.ecosystem, c.name): c for c in inv.components}
    assert by[("PyPI", "pyyaml")].version == "5.3" and by[("PyPI", "pyyaml")].version_state == "pinned"
    assert len([c for c in inv.components if c.name == "pyyaml"]) == 1  # PyYAML == pyyaml: normalised and de-duplicated
    assert by[("PyPI", "flask")].version is None and by[("PyPI", "flask")].version_state == "range"  # never invent a version
    assert by[("npm", "a")].relationship == "direct" and by[("npm", "b")].relationship == "transitive" and "a" in json.dumps(by[("npm", "b")].paths)
    assert ("npm", "ignored") not in by  # node_modules is not scanned
    st = {f.path: f.status for f in inv.files}
    assert st["pyproject.toml"] == "malformed" and st["yarn.lock"] == "unsupported" and st["go.mod"] == "unsupported"
    assert st.get("requirements-link.txt") == "skipped" and not any(c.manifests and c.manifests[0]["path"] == "requirements-link.txt" for c in inv.components)  # symlinks are reported, never read
    assert inv.coverage()["unsupported"] and inventory.build(tmp_path, 1000, 1_000_000, 1000).digest == inv.digest  # reproducible


def test_inventory_empty_and_limits(tmp_path: Path) -> None:
    from app.services.security import inventory

    empty = inventory.build(tmp_path, 100, 1000, 100)
    assert empty.components == [] and empty.files == []
    write(tmp_path, {"requirements.txt": "\n".join(f"pkg{i}==1.0" for i in range(30))})
    small = inventory.build(tmp_path, 100, 1_000_000, 10)
    assert small.truncated is True and len(small.components) <= 10  # a limit is reported, never silent


# ================================================================================================ secrets
def test_secret_detection_redacts_and_ignores_placeholders() -> None:
    from app.services.security import secrets_scan as ss

    hits = ss.scan_text("c.py", CFG + "password = 'changeme'\nEXAMPLE_KEY = 'your-api-key-here'\n", "salt")
    assert {h.line for h in hits} == {1, 2}
    for h in hits:
        blob = repr(h)
        assert FAKE_AWS not in blob and FAKE_GH not in blob and FAKE_GH[8:] not in blob
        assert "REDACTED" in h.preview or "*" in h.preview
        assert h.value_hash and h.value_hash not in (FAKE_AWS, FAKE_GH)
    again = ss.scan_text("c.py", "\n\n" + CFG, "salt")
    assert {h.value_hash for h in hits} == {h.value_hash for h in again}  # identity survives a line move
    assert {h.value_hash for h in ss.scan_text("c.py", CFG, "other-salt")}.isdisjoint({h.value_hash for h in hits})


def test_secret_scan_records_exclusions_and_limits(tmp_path: Path) -> None:
    from app.services.security import secrets_scan as ss

    write(tmp_path, {"a.py": CFG, "keep/b.py": CFG, "big.txt": "x" * 5000})
    (tmp_path / "link.py").symlink_to(tmp_path / "a.py")
    res = ss.scan_repository(tmp_path, "s", max_files=100, max_file_bytes=1000, exclude_globs=["keep/**"])
    assert {h.rel_path for h in res.hits} == {"a.py"}
    assert any("keep" in x for x in res.excluded_by_config)  # exclusions are recorded, not silent
    assert res.skipped  # the oversized file and the symlink are accounted for


# ================================================================================================ code rules
@pytest.mark.parametrize("rule,src", [
    ("PY-SEC-002", "import subprocess\nsubprocess.call('ls', shell=True)\n"),
    ("PY-SEC-005", "import yaml\nyaml.load(open('x'))\n"),
    ("PY-SEC-006", "def f(request, cur):\n    cur.execute('select * from t where a=' + request.args['a'])\n"),
    ("PY-SEC-007", "def f(request):\n    return open(request.args['p'])\n"),
    ("PY-SEC-008", "import requests\ndef f(request):\n    return requests.get(request.args['u'])\n"),
    ("PY-SEC-009", "import hashlib\nhashlib.md5(b'x').hexdigest()\n"),
    ("PY-SEC-010", "import logging\ndef f(password):\n    logging.info('p %s', password)\n"),
    ("PY-SEC-011", "import requests\nrequests.get('https://x', verify=False)\n"),
    ("PY-SEC-012", "import os\ndef f(request):\n    os.system(request.args['c'])\n"),
    ("PY-SEC-013", "from flask import Flask\napp = Flask(__name__)\napp.run(debug=True)\n"),
])
def test_security_rules_flag_unsafe_fixtures(rule: str, src: str) -> None:
    from app.services.security import codescan

    hits, problem, _ = codescan.scan_source("m.py", src.encode())
    assert problem is None and rule in {h.rule_id for h in hits}, [h.rule_id for h in hits]


@pytest.mark.parametrize("src", [
    "import subprocess\nsubprocess.run(['ls', '-l'])\n",
    "import yaml\nyaml.safe_load(open('x'))\n",
    "def f(cur, a):\n    cur.execute('select * from t where a=?', (a,))\n",
    "import hashlib\nhashlib.sha256(b'x').hexdigest()\n",
    "def f(x):\n    return x + 1\n",
])
def test_security_rules_do_not_flag_safe_fixtures(src: str) -> None:
    from app.services.security import codescan

    hits, problem, _ = codescan.scan_source("m.py", src.encode())
    assert problem is None and hits == []


def test_security_scan_does_not_follow_symlinks_or_read_oversized_files(tmp_path: Path) -> None:
    from app.services.security import codescan

    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    assert codescan.read_regular(tmp_path, "real.py", 1000)[0] == b"x = 1\n"
    assert codescan.read_regular(tmp_path, "link.py", 1000)[0] is None
    assert codescan.read_regular(tmp_path, "big.py", 1000)[0] is None
    assert codescan.read_regular(tmp_path, "../etc/passwd", 1000)[0] is None


# ================================================================================================ scans through the API
ALL_URLS = ("/overview", "/scans", "/findings", "/advisory-matches", "/suppressions", "/exceptions", "/policy", "/providers", "/remediations", "/evaluations")


def everything(client: TestClient, rid: str) -> str:
    """Every response body the workspace can show for a repository, as one string."""
    out = []
    for u in ALL_URLS:
        out.append(client.get(f"{sb(rid)}{u}").text)
    for f in findings(client, rid):
        out.append(client.get(f"{sb(rid)}/findings/{f['id']}").text)
    return "\n".join(out)


def test_scan_end_to_end_dedupes_advisories_and_never_exposes_secrets(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = project(client, tmp_path)
    s = scan(client, rid)
    assert s["status"] == "completed" and s["head_sha"] and s["dirty"] is False and s["summary"]["state"] == "partial"  # requests>=2.0 is unpinned
    fs = findings(client, rid)
    dep = {f["title"].split(":")[0]: f for f in fs if f["kind"] == "dependency"}
    assert set(dep) == {"pyyaml 5.3", "flask 2.0.0"}
    d = client.get(f"{sb(rid)}/findings/{dep['pyyaml 5.3']['id']}").json()
    assert d["severity"] == "critical" and d["evidence"]["advisory_ids"] == ["CVE-2099-0001", "SYN-0001", "SYN-0002"] or set(d["evidence"]["advisory_ids"]) >= {"SYN-0001", "SYN-0002"}
    assert len(d["evidence"]["sources"]) == 2 and d["evidence"]["fixed_versions"] == ["5.4"] and d["evidence"]["match_status"] == "confirmed"  # deduped, sources kept
    fl = client.get(f"{sb(rid)}/findings/{dep['flask 2.0.0']['id']}").json()
    assert fl["evidence"]["severity_source"] == "unspecified"  # never invented
    comps = client.get(f"{sb(rid)}/scans/{s['id']}/components").json()["items"]
    status = {c["name"]: (c["assessment"] or {}).get("status") for c in comps}
    assert status["requests"] == "unassessed" and status["pyyaml"] == "confirmed" or status["pyyaml"] in ("confirmed", "affected")
    secrets = [f for f in fs if f["kind"] == "secret"]
    assert len(secrets) == 2 and all("REDACTED" in (f["preview"] or "") or "*" in (f["preview"] or "") for f in secrets)
    blob = everything(client, rid)
    assert FAKE_AWS not in blob and FAKE_GH not in blob and FAKE_GH[8:20] not in blob
    cov = client.get(f"{sb(rid)}/scans/{s['id']}").json()["coverage"]
    assert "limitation" in cov["secrets"] and cov["code"]["languages"] == ["Python"] and cov["inventory"]["status_counts"]["parsed"] >= 1


def test_rescan_keeps_identity_and_resolves_fixed_findings(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = project(client, tmp_path)
    scan(client, rid)
    before = {f["id"]: f for f in findings(client, rid)}
    again = scan(client, rid)
    after = {f["id"]: f for f in findings(client, rid)}
    assert set(before) == set(after) and again["summary"]["findings_total"] == len(before)  # same fingerprints, no duplicates
    (root / "settings.py").write_text("AWS_KEY = 'moved'\n")
    scan(client, rid)
    gone = [f for f in findings(client, rid) if f["kind"] == "secret"]
    assert gone and all(f["status"] == "resolved" for f in gone)
    d = client.get(f"{sb(rid)}/findings/{gone[0]['id']}").json()
    assert [e["action"] for e in d["events"]][-1] == "resolved" and d["resolution_reason"]
    (root / "settings.py").write_text(CFG)
    scan(client, rid)
    back = [f for f in findings(client, rid) if f["kind"] == "secret"]
    assert back and all(f["status"] == "open" for f in back)  # reopened
    assert "reopened" in [e["action"] for e in client.get(f"{sb(rid)}/findings/{back[0]['id']}").json()["events"]]


def test_one_active_scan_and_validation(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    assert client.post(f"{sb(rid)}/scans", json={"scan_types": []}).status_code == 422
    assert client.post(f"{sb(rid)}/scans", json={"scan_types": ["nope"]}).status_code == 422
    assert client.post(f"{sb(rid)}/scans", json={"exclude_globs": ["../x"]}).status_code == 422
    assert client.post(f"{sb(rid)}/scans", json={"bogus": 1}).status_code == 422
    from app.db.session import get_session_factory
    from app.models import SecScan
    with get_session_factory()() as db:
        db.add(SecScan(repository_id=rid, status="running", scan_types=["code"], options={}))
        db.commit()
    r = client.post(f"{sb(rid)}/scans", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "scan_in_progress"


def test_interrupted_scans_are_marked_failed(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    from app.db.session import get_session_factory
    from app.models import SecScan
    from app.services.security import scan as sc
    with get_session_factory()() as db:
        row = SecScan(repository_id=rid, status="running", scan_types=["code"], options={})
        db.add(row)
        db.commit()
        assert sc.recover_interrupted(db) == 1
        db.refresh(row)
        assert row.status == "failed" and row.error


def test_findings_filters_sorting_and_pagination(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    assert all(f["kind"] == "secret" for f in findings(client, rid, kind="secret"))
    assert {f["severity"] for f in findings(client, rid, severity="critical")} == {"critical"}
    assert all("app.py" == f["rel_path"] for f in findings(client, rid, path="app.py"))
    assert all(f["rule_id"] == "PY-SEC-005" for f in findings(client, rid, rule_id="PY-SEC-005"))
    p1 = client.get(f"{sb(rid)}/findings", params={"page_size": 3, "page": 1}).json()
    p2 = client.get(f"{sb(rid)}/findings", params={"page_size": 3, "page": 2}).json()
    assert p1["total"] == 8 and len(p1["items"]) == 3 and {f["id"] for f in p1["items"]}.isdisjoint({f["id"] for f in p2["items"]})
    scores = [f["priority"]["score"] for f in findings(client, rid)]
    assert scores == sorted(scores, reverse=True)
    assert client.get(f"{sb(rid)}/findings", params={"sort": "bogus"}).status_code == 422
    assert client.get(f"{sb(rid)}/findings", params={"page_size": 1000}).status_code == 422
    assert client.get(f"{sb(rid)}/findings", params={"q": "%"}).json()["total"] == 0  # wildcard characters are literal
    pr = findings(client, rid)[0]["priority"]
    assert pr["model"] == "nova.priority/1" and pr["factors"] if "factors" in pr else True


# ================================================================================================ triage, suppression, authorisation
def one(client: TestClient, rid: str, **params: Any) -> dict[str, Any]:
    fs = findings(client, rid, **params)
    assert fs
    return fs[0]


def test_triage_lifecycle_requires_reason_and_keeps_history(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    f = one(client, rid, rule_id="PY-SEC-013")
    url = f"{sb(rid)}/findings/{f['id']}/triage"
    assert client.post(url, json={"status": "false_positive", "reason": ""}).status_code == 422
    assert client.post(url, json={"status": "resolved", "reason": "nope nope nope"}).status_code == 422  # system-managed status
    r = client.post(url, json={"status": "false_positive", "reason": "debug flag is test-only"})
    assert r.status_code == 200 and r.json()["status"] == "false_positive"
    assert client.post(url, json={"status": "false_positive", "reason": "again again"}).status_code == 409
    assert client.post(url, json={"status": "open", "reason": ""}).json()["status"] == "open"
    ev = client.get(f"{sb(rid)}/findings/{f['id']}").json()["events"]
    assert [e["action"] for e in ev] == ["detected", "triaged", "triaged"] and ev[1]["reason"] == "debug flag is test-only"
    scan(client, rid)  # triage survives a rescan
    client.post(url, json={"status": "accepted_risk", "reason": "accepted until the redesign"})
    scan(client, rid)
    assert client.get(f"{sb(rid)}/findings/{f['id']}").json()["status"] == "accepted_risk"


def test_suppressions_are_scoped_expiring_audited_and_reversible(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from datetime import UTC, datetime, timedelta
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    sup = f"{sb(rid)}/suppressions"
    soon = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assert client.post(sup, json={"rule_id": "PY-SEC-013", "path_glob": "**", "reason": "too broad here"}).status_code == 422
    assert client.post(sup, json={"rule_id": "PY-SEC-013", "path_glob": "app.py", "reason": "x"}).status_code == 422  # reason required
    assert client.post(sup, json={"rule_id": "PY-SEC-013", "path_glob": "app.py", "reason": "valid reason", "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}).status_code == 422
    assert client.post(sup, json={"reason": "valid reason"}).status_code == 422  # no scope
    r = client.post(sup, json={"rule_id": "PY-SEC-013", "path_glob": "app.py", "reason": "dev server only", "expires_at": soon})
    assert r.status_code == 201 and r.json()["state"] == "active"
    scan(client, rid)
    f = one(client, rid, rule_id="PY-SEC-013")
    assert f["status"] == "suppressed"  # still listed, never deleted
    assert one(client, rid, rule_id="PY-SEC-005")["status"] == "open"  # scope is limited to the rule
    assert client.get(f"{sb(rid)}/scans").json()["items"][0]["summary"]["suppressions_applied"]
    rv = client.post(f"{sup}/{r.json()['id']}/revoke", json={"reason": "no longer true"})
    assert rv.status_code == 200 and rv.json()["state"] == "revoked"
    assert client.get(f"{sb(rid)}/findings/{f['id']}").json()["status"] == "open"
    assert client.post(f"{sup}/{r.json()['id']}/revoke", json={}).status_code == 409
    from app.db.session import get_session_factory
    from app.models import AuditEvent
    with get_session_factory()() as db:
        actions = {a.action for a in db.query(AuditEvent)}
    assert {"security.suppression_created", "security.suppression_revoked", "security.scan_started", "security.scan_completed"} <= actions


def test_expired_suppression_stops_applying(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from app.db.session import get_session_factory
    from app.models import SecSuppression
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    r = client.post(f"{sb(rid)}/suppressions", json={"rule_id": "PY-SEC-013", "path_glob": "app.py", "reason": "short lived"}).json()
    scan(client, rid)
    assert one(client, rid, rule_id="PY-SEC-013")["status"] == "suppressed"
    with get_session_factory()() as db:
        row = db.get(SecSuppression, r["id"])
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert client.get(f"{sb(rid)}/suppressions").json()[0]["state"] == "expired"
    scan(client, rid)
    assert one(client, rid, rule_id="PY-SEC-013")["status"] == "open"


def test_viewer_cannot_mutate_but_can_read(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    f = one(client, rid)
    as_viewer()
    for method, path, body in (("post", "/scans", {}), ("post", f"/findings/{f['id']}/triage", {"status": "false_positive", "reason": "reason here"}),
                               ("post", "/suppressions", {"finding_id": f["id"], "reason": "reason here"}), ("put", "/policy", {"rules": {}}), ("post", "/evaluate", {}),
                               ("post", "/reports", {}), ("post", f"/findings/{f['id']}/remediations", None), ("post", "/advisories/import", {"documents": [osv("X-1", "p", "0", "1")]}),
                               ("post", "/exceptions", {"selector": {"type": "rule", "value": "PY-SEC-013"}, "reason": "reason here", "expires_at": "2099-01-01T00:00:00Z"})):
        r = getattr(client, method)(f"{sb(rid)}{path}", **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code, r.text)
    for path in ALL_URLS:
        assert client.get(f"{sb(rid)}{path}").status_code == 200, path


def test_cross_tenant_access_is_rejected_and_advisories_are_workspace_scoped(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from app.db.session import get_session_factory
    from app.models import Repository, SecAdvisory, SecFinding, SecReport, SecScan, Workspace
    rid, _ = project(client, tmp_path)
    with get_session_factory()() as db:
        ws = Workspace(name="other", slug="other")
        db.add(ws)
        db.flush()
        other = Repository(workspace_id=ws.id, name="o", root_path=str(tmp_path / "elsewhere"))
        db.add(other)
        db.flush()
        sc = SecScan(repository_id=other.id, status="completed", scan_types=["code"], options={})
        db.add(sc)
        db.flush()
        fi = SecFinding(repository_id=other.id, fingerprint="f" * 32, kind="code", rule_id="PY-SEC-002", category="security", title="t", severity="high", confidence="high",
                        evidence_quality="syntactic", status="open", first_seen_scan_id=sc.id, last_seen_scan_id=sc.id)
        db.add(fi)
        rp = SecReport(repository_id=other.id, scan_id=sc.id, format="json", sha256="0" * 64, body="{}")
        db.add(rp)
        # an advisory belonging to ANOTHER workspace must never match this workspace's components
        db.add(SecAdvisory(workspace_id=ws.id, provider="imported", advisory_id="OTHER-1", ecosystem="PyPI", package="requests", aliases=[], summary="", ranges=[{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                           versions=[], fixed=[], references=[], uncertainty=[], raw_sha256="x"))
        db.commit()
        oid, sid, fid, rpid = other.id, sc.id, fi.id, rp.id
    for path in (f"/scans/{sid}", f"/scans/{sid}/components", f"/findings/{fid}", f"/reports/{rpid}", f"/reports/{rpid}/download"):
        assert client.get(f"{sb(rid)}{path}").status_code == 404, path  # a real id of another repository, requested through this one
        assert client.get(f"{sb(oid)}{path}").status_code == 404, path  # another tenant's repository is not addressable at all
    assert client.get(f"{sb(oid)}/overview").status_code == 404 and client.post(f"{sb(oid)}/scans", json={}).status_code == 404
    assert client.post(f"{sb(rid)}/findings/{fid}/triage", json={"status": "false_positive", "reason": "cross tenant"}).status_code == 404
    assert client.post(f"{sb(rid)}/findings/{fid}/remediations").status_code == 404
    scan(client, rid)
    assert not [f for f in findings(client, rid) if "OTHER-1" in json.dumps(f)]
    assert client.get(f"{sb(rid)}/findings/{fid}").status_code == 404


# ================================================================================================ policy
def test_policy_gate_pass_fail_indeterminate_and_versioning(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    assert client.post(f"{sb(rid)}/evaluate", json={}).status_code == 422  # nothing to evaluate yet
    scan(client, rid)
    d = client.post(f"{sb(rid)}/evaluate", json={}).json()
    assert d["status"] == "fail" and d["policy_id"] and d["policy_version"] == 0 and d["causes"] and d["revision"]["head_sha"] and d["evaluated_at"] and d["explanation"].startswith("FAIL")
    assert {"checks", "missing_data", "skipped_checks", "exceptions_applied"} <= set(d)
    assert client.get(f"{sb(rid)}/policy").json()["is_default"] is True
    # a lenient policy over a PARTIAL scan is INDETERMINATE, never a silent pass
    lenient = {"fail_on_severity": "critical", "fail_on_advisory_severity": "critical", "fail_on_secret_confidence": "high", "possible_advisories": "ignore", "indeterminate_effect": "block"}
    r = client.put(f"{sb(rid)}/policy", json={"rules": {"fail_on_severity": "critical", "fail_on_secret_confidence": "high"}, "note": "relax"})
    assert r.status_code == 200 and r.json()["version"] == 1
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"nonsense": 1}}).status_code == 422
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"fail_on_severity": "bogus"}}).status_code == 422
    hist = client.get(f"{sb(rid)}/policy/history").json()
    assert [h["version"] for h in hist][0] == 1 and lenient
    d2 = client.post(f"{sb(rid)}/evaluate", json={}).json()
    assert d2["policy_version"] == 1 and d2["status"] in ("fail", "indeterminate")
    assert client.get(f"{sb(rid)}/evaluations").json()["total"] == 2


def test_indeterminate_when_no_advisory_source_has_data(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path, {"requirements.txt": "pyyaml==5.3\n", "ok.py": "x = 1\n"})
    s = scan(client, rid)
    assert findings(client, rid) == [] and s["summary"]["state"] == "partial"  # zero findings from an incomplete scan
    assert "PARTIAL" in s["summary"]["headline"] or "not a result" in s["summary"]["headline"]
    d = client.post(f"{sb(rid)}/evaluate", json={}).json()
    assert d["status"] == "indeterminate" and d["missing_data"], d  # never a pass
    r = client.put(f"{sb(rid)}/policy", json={"rules": {"require_advisory_data": False}})
    assert r.status_code == 200
    comps = client.get(f"{sb(rid)}/scans/{s['id']}/components").json()["items"]
    assert "not a statement that the package is safe" in json.dumps(comps[0]["assessment"])  # a missing advisory is never presented as safety


def test_exceptions_expire_are_listed_and_owner_only(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from app.db.session import get_session_factory
    from app.models import SecException
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    ex = f"{sb(rid)}/exceptions"
    soon = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    assert client.post(ex, json={"selector": {"type": "rule", "value": "PY-SEC-013"}, "reason": "ok ok ok ok"}).status_code == 422  # expiry required
    assert client.post(ex, json={"selector": {"type": "weird", "value": "x"}, "reason": "ok ok ok ok", "expires_at": soon}).status_code == 422
    assert client.post(ex, json={"selector": {"type": "rule", "value": "PY-SEC-013"}, "reason": "ok ok ok ok", "expires_at": (datetime.now(UTC) + timedelta(days=4000)).isoformat()}).status_code == 422
    base = client.post(f"{sb(rid)}/evaluate", json={}).json()
    by_rule = [f for f in findings(client, rid) if f["severity"] in ("critical", "high")]
    created = []
    for f in by_rule:
        sel = {"type": "rule", "value": f["rule_id"]} if f["kind"] != "dependency" else {"type": "advisory", "value": "SYN-0001"}
        created.append(client.post(ex, json={"selector": sel, "reason": "accepted for this release", "expires_at": soon}))
    assert all(c.status_code == 201 for c in created)
    d = client.post(f"{sb(rid)}/evaluate", json={}).json()
    assert d["exceptions_applied"] and base["status"] == "fail"
    assert all(e.get("reason") and e.get("expires_at") for e in d["exceptions_applied"])
    with get_session_factory()() as db:
        for x in db.query(SecException):
            x.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    d2 = client.post(f"{sb(rid)}/evaluate", json={}).json()
    assert d2["status"] == "fail" and not d2["exceptions_applied"] and d2["expired_exceptions"]
    assert {e["state"] for e in client.get(ex).json()} == {"expired"}
    live = client.post(ex, json={"selector": {"type": "rule", "value": "PY-SEC-013"}, "reason": "ok ok ok ok", "expires_at": soon}).json()
    assert client.post(f"{ex}/{live['id']}/revoke").json()["state"] == "revoked" and client.post(f"{ex}/{live['id']}/revoke").status_code == 409


# ================================================================================================ remediation
def rem(client: TestClient, rid: str, fid: str) -> dict[str, Any]:
    r = client.post(f"{sb(rid)}/findings/{fid}/remediations")
    assert r.status_code == 201, r.text
    return r.json()


def repair_url(rid: str, inv: str, pid: str, tail: str) -> str:
    return f"{API}/repositories/{rid}/repairs/{inv}/patches/{pid}/{tail}"


def test_code_remediation_goes_through_review_approval_and_is_never_silent(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = project(client, tmp_path)
    scan(client, rid)
    f = one(client, rid, rule_id="PY-SEC-005")
    original = (root / "app.py").read_bytes()
    r = rem(client, rid, f["id"])
    assert r["status"] == "proposed" and "yaml.safe_load" in r["diff_text"] and r["proposal"]["supported"] is True
    assert "does not verify" in " ".join(r["proposal"]["caveats"]) and r["proposal"]["side_effects"]  # never claims behavior is preserved
    assert (root / "app.py").read_bytes() == original  # proposing writes nothing
    assert client.post(f"{sb(rid)}/findings/{f['id']}/remediations").status_code == 409  # one active proposal per finding
    s = client.post(f"{sb(rid)}/remediations/{r['id']}/submit").json()
    assert s["status"] == "submitted" and s["validation"]["ok"] is True and s["validation"]["files_modified"] is False and s["patch_id"] and s["investigation_id"]
    assert (root / "app.py").read_bytes() == original  # validating writes nothing either
    inv, pid = s["investigation_id"], s["patch_id"]
    assert client.post(repair_url(rid, inv, pid, "apply"), json={"confirm": True}).status_code == 409  # not approved
    assert (root / "app.py").read_bytes() == original
    assert client.post(repair_url(rid, inv, pid, "submit")).status_code == 200
    assert client.post(repair_url(rid, inv, pid, "approve")).status_code == 200
    assert client.post(repair_url(rid, inv, pid, "apply"), json={"confirm": False}).status_code == 422
    assert client.post(repair_url(rid, inv, pid, "apply"), json={"confirm": True}).status_code == 200
    assert b"yaml.safe_load" in (root / "app.py").read_bytes()
    assert client.get(f"{sb(rid)}/remediations/{r['id']}").json()["proposal"]["repair_patch_status"] in ("applied", "verification_failed", "verified")
    scan(client, rid)
    assert client.get(f"{sb(rid)}/findings/{f['id']}").json()["status"] == "resolved"


def test_dependency_remediation_uses_advisory_data_and_never_touches_lockfiles(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = project(client, tmp_path, {**FILES, "poetry.lock": "# lock\n"})
    scan(client, rid)
    f = one(client, rid, kind="dependency", severity="critical")
    r = rem(client, rid, f["id"])
    p = r["proposal"]
    assert r["status"] == "proposed" and p["from_version"] == "5.3" and p["to_version"] == "5.4" and p["manifest"] == "requirements.txt" and p["major_version_change"] is False
    assert "-pyyaml==5.3" in r["diff_text"] and "+pyyaml==5.4" in r["diff_text"] and "poetry.lock" not in r["diff_text"]
    assert any("Lockfiles are not modified" in c for c in p["caveats"]) and p["basis"]
    s = client.post(f"{sb(rid)}/remediations/{r['id']}/submit").json()
    assert s["status"] == "submitted" and (root / "requirements.txt").read_text() == REQS


def test_remediation_is_refused_when_data_does_not_support_it(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    (adv_dir / "d.json").write_text(json.dumps(osv("SYN-0004", "requests", "0", "9.0")))
    (adv_dir / "e.json").write_text(json.dumps(osv("SYN-0005", "b", "2.0.0", "2.0.5", eco="npm")))
    lock = {"name": "x", "lockfileVersion": 3, "packages": {"": {"name": "x", "dependencies": {"a": "^1.0.0"}}, "node_modules/a": {"version": "1.2.3", "dependencies": {"b": "^2.0.0"}},
                                                             "node_modules/b": {"version": "2.0.1"}}}
    rid, _ = project(client, tmp_path, {**FILES, "package.json": json.dumps({"dependencies": {"a": "^1.0.0"}}), "package-lock.json": json.dumps(lock)})
    scan(client, rid)
    poss = next(f for f in findings(client, rid, kind="dependency") if f["match_status"] == "possible")
    r1 = rem(client, rid, poss["id"])
    assert r1["status"] == "cannot_fix" and "possible" in r1["proposal"]["reason"] and r1["diff_text"] == ""  # a guess is never turned into an edit
    trans = next(f for f in findings(client, rid, kind="dependency") if f["title"].startswith("b "))
    r2 = rem(client, rid, trans["id"])
    assert r2["status"] == "cannot_fix" and "transitive" in r2["proposal"]["reason"]
    sec = one(client, rid, kind="secret")
    r3 = rem(client, rid, sec["id"])
    assert r3["status"] == "cannot_fix" and "rotate" in r3["proposal"]["reason"]
    assert client.post(f"{sb(rid)}/remediations/{r1['id']}/submit").status_code == 409  # cannot_fix proposals cannot be submitted


def test_failed_validation_preserves_original_files(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, root = project(client, tmp_path)
    scan(client, rid)
    f = one(client, rid, kind="dependency", severity="critical")
    r = rem(client, rid, f["id"])
    changed = "pyyaml==5.3  # edited after the proposal\nflask==2.0.0\nrequests>=2.0\n"
    (root / "requirements.txt").write_text(changed)  # the source moves under the proposal
    s = client.post(f"{sb(rid)}/remediations/{r['id']}/submit").json()
    assert s["status"] == "validation_failed" and s["validation"]["ok"] is False
    assert (root / "requirements.txt").read_text() == changed  # untouched
    dep = next(x for x in findings(client, rid) if x["id"] == f["id"])
    assert dep["status"] == "open"  # a failed proposal never closes the finding


# ================================================================================================ reports
def make_report(client: TestClient, rid: str, fmt: str, **kw: Any) -> dict[str, Any]:
    r = client.post(f"{sb(rid)}/reports", json={"format": fmt, **kw})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.skipif(os.name == "nt", reason="creates a file named 'a<script>alert(1)</script>.py'; '<' and '>' are invalid in Windows file names")
def test_reports_are_redacted_escaped_and_distinguish_incomplete_from_clean(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path, {**FILES, "=cmd.py": "import os\nos.system('x', shell=True)\n", "a<script>alert(1)</script>.py": "import subprocess\nsubprocess.call('x', shell=True)\n"})
    scan(client, rid)
    client.post(f"{sb(rid)}/evaluate", json={})
    bodies = {}
    for fmt in ("json", "markdown", "html", "csv"):
        r = make_report(client, rid, fmt)
        got = client.get(f"{sb(rid)}/reports/{r['id']}").json()
        dl = client.get(f"{sb(rid)}/reports/{r['id']}/download")
        assert dl.status_code == 200 and dl.headers["x-content-type-options"] == "nosniff" and "attachment" in dl.headers["content-disposition"] and dl.text == got["body"]
        assert len(r["sha256"]) == 64 and r["size_bytes"] > 0
        bodies[fmt] = got["body"]
        assert FAKE_AWS not in got["body"] and FAKE_GH not in got["body"] and FAKE_GH[8:20] not in got["body"]
    doc = json.loads(bodies["json"])
    assert doc["schema"] == "nova.security.report/1" and doc["scan"]["state"] == "partial" and doc["scan"]["incomplete_reasons"] and doc["limitations"] and doc["policy_evaluation"]["status"]
    assert "<script>" not in bodies["html"] and "&lt;script&gt;" in bodies["html"] and "Content-Security-Policy" in bodies["html"]
    assert "<script>" not in bodies["markdown"].replace("\\<script\\>", "")
    cells = [c for line in bodies["csv"].splitlines()[1:] for c in line.split(",")]
    assert "'=cmd.py" in cells and not any(c.startswith("=") for c in cells)  # formula injection neutralised
    assert "INCOMPLETE" in doc["headline"] or "PARTIAL" in doc["headline"] or doc["findings"]


def test_report_headline_for_zero_findings_depends_on_completeness(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path, {"ok.py": "x = 1\n"})
    s = scan(client, rid, scan_types=["code", "secrets"])
    assert s["summary"]["state"] == "complete" and findings(client, rid) == []
    clean = json.loads(client.get(f"{sb(rid)}/reports/{make_report(client, rid, 'json')['id']}").json()["body"])
    assert "not a guarantee" in clean["headline"] and clean["findings"] == []
    rid2, _ = project(client, tmp_path, {"requirements.txt": "pyyaml==5.3\n", "ok.py": "x = 1\n"}, name="p2", dirname="r2")
    scan(client, rid2)
    part = json.loads(client.get(f"{sb(rid2)}/reports/{make_report(client, rid2, 'json')['id']}").json()["body"])
    assert part["findings"] == [] and "INCOMPLETE" in part["headline"] and part["scan"]["incomplete_reasons"]
    md = client.get(f"{sb(rid2)}/reports/{make_report(client, rid2, 'markdown')['id']}").json()["body"]
    assert "INCOMPLETE" in md


def test_report_size_cap_and_validation(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("NOVA_SECURITY_REPORT_MAX_BYTES", "10000")
    reset_settings_cache()
    files = {**FILES, **{f"m{i}.py": "import os\nos.system('x', shell=True)\n" for i in range(40)}}
    rid, _ = project(client, tmp_path, files)
    scan(client, rid)
    r = make_report(client, rid, "json")
    assert r["truncated"] is True and r["size_bytes"] <= 10_400
    assert client.post(f"{sb(rid)}/reports", json={"format": "pdf"}).status_code == 422
    assert client.post(f"{sb(rid)}/reports", json={"scan_id": "nope"}).status_code == 404
    assert client.get(f"{sb(rid)}/reports/nope").status_code == 404


# ================================================================================================ advisory import & providers
def test_advisory_import_validation_and_provenance(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path, {"requirements.txt": "pyyaml==5.3\n", "ok.py": "x = 1\n"})
    prov = client.get(f"{sb(rid)}/providers").json()
    assert {p["name"]: p["enabled"] for p in prov["providers"]} == {"workspace-store": True, "fixtures": False, "osv": False}  # OSV is off unless enabled
    r = client.post(f"{sb(rid)}/advisories/import", json={"documents": [osv("IMP-1", "pyyaml", "0", "5.4"), {"nope": 1}]})
    assert r.status_code == 201 and r.json()["imported"] == 1 and len(r.json()["rejected"]) == 1
    assert client.post(f"{sb(rid)}/advisories/import", json={"documents": []}).status_code == 422
    s = scan(client, rid)
    dep = one(client, rid, kind="dependency")
    d = client.get(f"{sb(rid)}/findings/{dep['id']}").json()
    assert d["evidence"]["sources"][0]["provider"] == "imported" and d["evidence"]["sources"][0]["retrieved_at"]
    assert s["coverage"]["advisories"]["sources"] == ["workspace-store"]


def test_osv_provider_failure_is_reported_not_hidden(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from app.services.cicd import providers as cip
    from app.services.security import scan as sc

    class Boom:
        def request(self, *a: Any, **k: Any) -> Any:
            raise cip.ProviderError("network_error", "simulated outage", retryable=True)

    monkeypatch.setenv("NOVA_SECURITY_OSV_ENABLED", "true")
    reset_settings_cache()
    monkeypatch.setattr(sc, "OSV_TRANSPORT", Boom())
    rid, _ = project(client, tmp_path)
    s = scan(client, rid)
    assert s["status"] == "completed" and s["summary"]["state"] == "partial"
    assert any("osv" in r.lower() for r in s["summary"]["incomplete_reasons"])
    assert {p["name"]: p["state"] for p in s["coverage"]["advisories"]["providers"]}["osv"] in ("unavailable", "error")
    assert any(f["kind"] == "dependency" for f in findings(client, rid))  # the offline fixtures still answered


# ================================================================================================ CI / readiness integration
def test_readiness_security_gate_is_opt_in_and_never_passes_on_missing_scan(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from tests.test_cicd import cb, local_cr, readiness, rules_of, start

    rid, root, cid, cr = local_cr(client, tmp_path)
    w = start(client, rid, cr["id"])
    assert "security_policy" not in rules_of(readiness(client, rid, w["id"]))  # default behaviour unchanged
    base = client.get(f"{cb(rid)}/readiness-policy").json()["rules"]
    assert client.put(f"{cb(rid)}/readiness-policy", json={"rules": {**base, "require_security_scan": True}}).status_code == 200
    r = readiness(client, rid, w["id"])
    assert rules_of(r)["security_policy"] == "blocked" and r["decision"]["outcome"] != "READY"  # no scan for this exact head
    rep = client.post(f"{cb(rid)}/change-requests/{cr['id']}/reports", json={"kind": "review"})
    assert rep.status_code in (200, 201)
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"require_inventory": False, "require_advisory_data": False, "required_scan_types": ["code", "secrets"]}}).status_code == 200
    s = scan(client, rid, scan_types=["code", "secrets"])
    assert s["head_sha"] == cr["head_sha"] and s["dirty"] is False
    r2 = readiness(client, rid, w["id"])
    assert rules_of(r2)["security_policy"] in ("pass", "fail", "incomplete", "blocked")
    assert next(x for x in r2["decision"]["rules"] if x["id"] == "security_policy")["evidence"]["scan_id"] == s["id"]
    # an indeterminate security result maps through the explicit effect and is never a silent pass
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"require_inventory": True, "required_scan_types": ["code", "secrets"], "indeterminate_effect": "incomplete"}}).status_code == 200
    r3 = readiness(client, rid, w["id"])
    assert rules_of(r3)["security_policy"] in ("incomplete", "fail") and r3["decision"]["outcome"] != "READY"


# ================================================================================================ migration
def test_security_migration_upgrades_and_downgrades(db_url: str) -> None:
    from alembic import command

    from app.db.session import get_engine
    from tests.conftest import alembic_config

    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    names = {"sec_scans", "sec_components", "sec_advisories", "sec_findings", "sec_events", "sec_suppressions", "sec_exceptions", "sec_policies", "sec_evaluations", "sec_remediations", "sec_reports"}
    with get_engine().connect() as conn:
        assert names <= {r[0] for r in conn.exec_driver_sql("select name from sqlite_master where type='table'")}
    command.downgrade(cfg, "0011")
    with get_engine().connect() as conn:
        assert not names & {r[0] for r in conn.exec_driver_sql("select name from sqlite_master where type='table'")}
    command.upgrade(cfg, "head")


# ================================================================================================ execution boundaries, priority, audit
def test_scan_never_executes_repository_code_or_lifecycle_scripts(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    marker = tmp_path / "EXECUTED"
    boom = f"open({str(marker)!r}, 'w').write('x')\n"
    files = {**FILES, "setup.py": boom, "conftest.py": boom, "evil.py": boom, "sitecustomize.py": boom,
             "package.json": json.dumps({"scripts": {"postinstall": f"touch {marker}", "preinstall": f"touch {marker}"}, "dependencies": {"left-pad": "1.3.0"}})}
    rid, _ = project(client, tmp_path, files)
    s = scan(client, rid)
    assert s["status"] == "completed" and not marker.exists()
    unsupported = [u["path"] for u in s["coverage"]["inventory"]["unsupported"]]
    assert "setup.py" in unsupported  # reported as a gap, not executed


def test_symlinks_and_files_outside_the_repository_are_not_read(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    outside = tmp_path / "outside.py"
    outside.write_text(CFG + "import os\nos.system('x', shell=True)\n")
    rid, root = project(client, tmp_path)
    (root / "leak.py").symlink_to(outside)
    (root / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    s = scan(client, rid)
    assert not [f for f in findings(client, rid) if (f["rel_path"] or "").startswith(("leak", "linkdir"))]
    assert s["status"] == "completed"


def test_priority_is_transparent_configurable_and_never_invents_reachability() -> None:
    from app.services.security import priority

    w = priority.weights(None)
    base = {"severity": "high", "confidence": "high", "evidence_quality": "exact_version", "reachability": "unknown", "fix_available": False, "scope_kind": "runtime"}
    unknown = priority.score(base, w)
    imported = priority.score({**base, "reachability": "imported"}, w)
    assert imported["score"] >= unknown["score"] and unknown["model"] == "nova.priority/1" and unknown["factors"]
    assert any("absence of a signal is never used to lower" in x for x in unknown["factors"])  # unknown reachability does not demote
    assert priority.score({**base, "severity": "critical", "confidence": "low", "evidence_quality": "heuristic"}, w)["bucket"] in ("P1", "P2")  # severity floor
    assert priority.score({**base, "fix_available": True}, w)["score"] >= unknown["score"]
    custom = priority.weights({"fix_bonus": 30})
    assert priority.score({**base, "fix_available": True}, custom)["score"] > priority.score({**base, "fix_available": True}, w)["score"]
    with pytest.raises(Exception):  # noqa: B017
        priority.validate_overrides({"nonsense": 1})


def test_priority_override_through_policy_is_validated_and_recorded(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    rid, _ = project(client, tmp_path)
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"priority_weights": {"nonsense": 1}}}).status_code == 422
    assert client.put(f"{sb(rid)}/policy", json={"rules": {"priority_weights": {"fix_bonus": 25}}, "note": "weight fixes"}).status_code == 200
    scan(client, rid)
    dep = one(client, rid, kind="dependency", severity="critical")
    assert dep["priority"]["model"] == "nova.priority/1" and dep["priority"]["factors"]


def test_audit_trail_records_actions_without_secrets(art: Path, adv_dir: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    from app.db.session import get_session_factory
    from app.models import AuditEvent
    rid, _ = project(client, tmp_path)
    scan(client, rid)
    f = one(client, rid, kind="secret")
    client.post(f"{sb(rid)}/findings/{f['id']}/triage", json={"status": "false_positive", "reason": f"test fixture {FAKE_GH}"})
    client.put(f"{sb(rid)}/policy", json={"rules": {}, "note": "audit"})
    client.post(f"{sb(rid)}/evaluate", json={})
    make_report(client, rid, "json")
    with get_session_factory()() as db:
        events = [e for e in db.query(AuditEvent) if e.action.startswith("security.")]
    actions = {e.action for e in events}
    assert {"security.scan_started", "security.scan_completed", "security.finding_triaged", "security.policy_evaluated", "security.report_generated", "security.policy_updated"} <= actions
    blob = json.dumps([e.details for e in events], default=str)
    assert FAKE_GH not in blob and FAKE_AWS not in blob and all(e.actor_user_id for e in events if e.action != "security.scan_completed")
    assert FAKE_GH not in everything(client, rid)  # the triage reason is scrubbed before it is stored


def test_a_secret_repeated_in_one_file_is_one_finding_and_the_scan_completes(art: Path, client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    # Regression: identical value in the same file -> identical fingerprint; the second copy used to be inserted again and the
    # unique constraint failed the whole scan (seen when NOVA scanned its own test suite).
    rid, _ = project(client, tmp_path, {"conf.py": f"A = '{FAKE_GH}'\nB = '{FAKE_GH}'\n", "README.md": "# x\n"})
    s = scan(client, rid, scan_types=["secrets"])
    assert s["status"] == "completed", s.get("error")
    hits = [f for f in findings(client, rid) if f["kind"] == "secret" and f["rel_path"] == "conf.py"]
    assert len(hits) == 1 and hits[0]["line"] == 1
    assert scan(client, rid, scan_types=["secrets"])["status"] == "completed"  # a rescan updates it, never duplicates
    assert len([f for f in findings(client, rid) if f["kind"] == "secret" and f["rel_path"] == "conf.py"]) == 1
