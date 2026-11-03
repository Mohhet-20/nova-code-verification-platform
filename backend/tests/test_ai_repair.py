# ruff: noqa: F811  (pytest fixtures imported from test_repair are redefined as parameters by design)
"""AI-assisted repair proposals (NOVA_AI_PROVIDER=anthropic): Claude proposes, NOVA verifies. The Anthropic client is faked - no network."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, reset_settings_cache
from app.release import prodcheck
from app.services.ai import claude
from tests.test_release_ops import prod
from tests.test_repair import CART, FIXED_CART, art, base, new_repair, rurl, setup, tree_state  # noqa: F401 - `art` is a fixture

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
EDITS = [
    {"path": "pkg/cart.py", "find": "def add_item(item, items=[]):", "replace": "def add_item(item, items=None):", "reason": "no shared default"},
    {"path": "pkg/cart.py", "find": "    items.append(item)", "replace": "    if items is None:\n        items = []\n    items.append(item)",
     "reason": "fresh list per call"},
]


class FakeClient:
    """Mimics the parts of anthropic.Anthropic used by app.services.ai.claude and records every request."""

    def __init__(self, answer: dict[str, Any] | None = None, *, stop_reason: str = "end_turn", error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer, self.stop_reason, self.error = answer, stop_reason, error
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        text = json.dumps(self.answer) if self.answer is not None else ""
        return SimpleNamespace(
            model=kwargs["model"], stop_reason=self.stop_reason, _request_id="req_test123",
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            content=[SimpleNamespace(type="text", text=text)] if text else [],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300, iterations=None))


def answer(edits: list[dict[str, Any]] | None = None, **over: Any) -> dict[str, Any]:
    return {"root_cause": "pkg/cart.py:1 uses a mutable default list shared between calls.", "explanation": "Default to None.",
            "confidence": "high", "edits": EDITS if edits is None else edits, "cannot_fix_reason": "", **over}


@pytest.fixture
def ai_on(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:  # after db_url, which clears NOVA_AI_PROVIDER
    monkeypatch.setenv("NOVA_AI_PROVIDER", "anthropic")
    reset_settings_cache()


def use(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> FakeClient:
    monkeypatch.setattr(claude, "client_factory", lambda settings: fake)
    return fake


def repo_with_secret(client: TestClient, tmp_path: Path) -> tuple[str, Path, dict]:
    files = {"pkg/__init__.py": "", "pkg/cart.py": CART + f'\nGITHUB = "{SECRET}"\n', "pkg/other.py": "x = 1\n"}
    rid, root = setup(client, tmp_path, files)
    repair = new_repair(client, rid, hint_files=["pkg/cart.py"])
    assert client.post(rurl(rid, repair, "/evidence")).status_code == 200
    return rid, root, repair


def test_disabled_by_default_and_nothing_is_sent(art: Path, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeClient(answer()))
    rid, _, repair = repo_with_secret(client, tmp_path)
    r = client.post(rurl(rid, repair, "/patches"), json={"source": "ai_assisted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_disabled"
    assert fake.calls == []


def test_claude_proposes_nova_validates_human_approves_and_applies(art: Path, ai_on: None, client: TestClient, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeClient(answer()))
    rid, root, repair = repo_with_secret(client, tmp_path)
    before = tree_state(root)
    r = client.post(rurl(rid, repair, "/patches"), json={"source": "ai_assisted"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["source"] == "ai_assisted" and p["status"] == "draft" and [f["path"] for f in p["files"]] == ["pkg/cart.py"]
    assert "AI-assisted proposal (claude-opus-5-5, confidence high; request req_test123)" in p["summary"] and "Not trusted" in p["summary"]
    assert tree_state(root) == before  # a proposal never touches the working tree

    # the request: structured output, effort, server-side refusal fallback, and NO secret in what left the server
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema" and call["output_config"]["effort"] == "high"
    sent = json.dumps(call)
    assert SECRET not in sent and "[REDACTED]" in sent and "pkg/other.py" not in sent  # only the relevant files are sent

    # from here it is the ordinary patch pipeline
    v = client.post(rurl(rid, repair, f"/patches/{p['id']}/validate"))
    assert v.status_code == 200 and v.json()["status"] == "validated", v.text
    assert client.post(rurl(rid, repair, f"/patches/{p['id']}/submit")).status_code == 200
    assert client.post(rurl(rid, repair, f"/patches/{p['id']}/approve")).status_code == 200
    a = client.post(rurl(rid, repair, f"/patches/{p['id']}/apply"), json={"confirm": True})
    assert a.status_code == 200, a.text
    assert (root / "pkg/cart.py").read_text() == FIXED_CART + f'\nGITHUB = "{SECRET}"\n'  # the secret line itself is untouched


@pytest.mark.parametrize("edits,needle", [
    ([{**EDITS[0], "path": "pkg/other.py"}], "was not one of the files provided"),
    ([{**EDITS[0], "path": "../outside.py"}], "was not one of the files provided"),
    ([{"path": "pkg/cart.py", "find": "items", "replace": "things", "reason": "x"}], "occurs"),
    ([{"path": "pkg/cart.py", "find": "def nonexistent():", "replace": "", "reason": "x"}], "occurs 0 times"),
    ([{"path": "pkg/cart.py", "find": 'GITHUB = "[REDACTED]"', "replace": 'GITHUB = ""', "reason": "x"}], "redacted"),
    ([EDITS[0], {"path": "pkg/other.py", "find": "x = 1", "replace": "x = 2", "reason": "x"}], "was not one of the files provided"),
])
def test_unsafe_or_ambiguous_edits_reject_the_whole_proposal(art: Path, ai_on: None, client: TestClient, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch, edits: list, needle: str) -> None:
    use(monkeypatch, FakeClient(answer(edits)))
    rid, root, repair = repo_with_secret(client, tmp_path)
    before = tree_state(root)
    r = client.post(rurl(rid, repair, "/patches"), json={"source": "ai_assisted"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_edit_rejected", r.text
    assert any(needle in x for x in r.json()["error"]["details"]["problems"])
    assert client.get(rurl(rid, repair, "/patches")).json() == [] and tree_state(root) == before  # all or nothing


def test_no_fix_refusal_and_provider_errors_are_explicit(art: Path, ai_on: None, client: TestClient, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx2

    rid, _, repair = repo_with_secret(client, tmp_path)
    url = rurl(rid, repair, "/patches")
    use(monkeypatch, FakeClient(answer([], cannot_fix_reason="The failing input is not in the evidence.")))
    r = client.post(url, json={"source": "ai_assisted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_no_fix" and "failing input" in r.json()["error"]["message"]

    use(monkeypatch, FakeClient(answer(), stop_reason="refusal"))
    r = client.post(url, json={"source": "ai_assisted"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ai_declined" and r.json()["error"]["details"]["category"] == "cyber"

    use(monkeypatch, FakeClient(answer(), stop_reason="max_tokens"))
    assert client.post(url, json={"source": "ai_assisted"}).json()["error"]["code"] == "ai_unavailable"

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    auth = anthropic.AuthenticationError("invalid x-api-key sk-ant-SHOULD-NOT-LEAK", response=httpx2.Response(401, request=req), body=None)
    use(monkeypatch, FakeClient(error=auth))
    r = client.post(url, json={"source": "ai_assisted"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable" and "SHOULD-NOT-LEAK" not in r.text

    limited = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=req), body=None)
    use(monkeypatch, FakeClient(error=limited))
    assert client.post(url, json={"source": "ai_assisted"}).status_code == 429

    use(monkeypatch, FakeClient(error=anthropic.APIConnectionError(request=req)))
    assert client.post(url, json={"source": "ai_assisted"}).status_code == 503
    assert client.get(url).json() == []


def test_models_without_server_side_fallback_use_the_plain_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeClient({"ok": True}))
    out = claude.complete_json(Settings(ai_provider="anthropic", ai_model="claude-haiku-5-5", ai_effort="bogus"), system="s", user="u",
                               schema={"type": "object"})
    assert out.data == {"ok": True} and "fallbacks" not in fake.calls[0] and "betas" not in fake.calls[0]
    assert fake.calls[0]["output_config"]["effort"] == "high"  # an invalid effort setting falls back to a valid one
    assert out.provenance["model"] == "claude-haiku-5-5" and out.provenance["fallback_used"] is False


def test_production_contract_and_planner_with_ai_enabled(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "ai_egress" in {f.code for f in prodcheck.check(prod(ai_provider="anthropic")) if f.severity == "warning"}
    assert "ai_provider_unknown" in {f.code for f in prodcheck.check(prod(ai_provider="openai")) if f.severity == "error"}
    from app.services.providers import DeterministicPlanProvider, get_plan_provider

    monkeypatch.setenv("NOVA_AI_PROVIDER", "anthropic")
    reset_settings_cache()
    assert isinstance(get_plan_provider(), DeterministicPlanProvider)  # planning stays deterministic


def test_workspace_reports_the_provider_for_the_ui(art: Path, ai_on: None, client: TestClient) -> None:
    assert client.get(f"{base('x').rsplit('/repositories', 1)[0]}/workspace").json()["ai_provider"] == "anthropic"


def test_migration_0017_keeps_patches_and_refuses_a_destructive_downgrade(art: Path, ai_on: None, client: TestClient, tmp_path: Path,
                                                                            migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from alembic import command

    from app.db.session import reset_engine
    from tests.conftest import alembic_config
    from tests.test_repair import manual, mdiff

    use(monkeypatch, FakeClient(answer()))
    rid, _, repair = repo_with_secret(client, tmp_path)
    assert manual(client, rid, repair, mdiff("pkg/cart.py", CART + f'\nGITHUB = "{SECRET}"\n', FIXED_CART + f'\nGITHUB = "{SECRET}"\n')).status_code == 201
    assert client.post(rurl(rid, repair, "/patches"), json={"source": "ai_assisted"}).status_code == 201
    reset_engine()
    db = migrated_url.removeprefix("sqlite:///")
    rows = lambda: sorted(r[0] for r in sqlite3.connect(db).execute("select source from repair_patches"))  # noqa: E731
    assert rows() == ["ai_assisted", "manual"]
    with pytest.raises(RuntimeError, match="AI-assisted patch"):  # would violate the old constraint: refuse, never delete
        command.downgrade(alembic_config(migrated_url), "0016")
    assert rows() == ["ai_assisted", "manual"]
    con = sqlite3.connect(db)
    con.execute("delete from repair_patches where source = 'ai_assisted'")
    con.commit()
    con.close()
    command.downgrade(alembic_config(migrated_url), "0016")  # with only old-style rows the cycle is lossless
    command.upgrade(alembic_config(migrated_url), "head")
    assert rows() == ["manual"]
