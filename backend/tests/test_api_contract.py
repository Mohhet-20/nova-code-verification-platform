"""API contract governance: a checked-in baseline, a breaking-change detector, and secret-free schemas."""
from __future__ import annotations

import json

from app import contracts


def test_current_api_has_no_breaking_changes_against_baseline() -> None:
    baseline = json.loads(contracts.BASELINE.read_text())
    assert contracts.diff(baseline, contracts.current_summary()) == [], "Breaking API change: fix it, deprecate first, or regenerate the baseline deliberately (python -m app.contracts write)."


def test_detector_flags_each_breaking_kind() -> None:
    base = {"POST /x": {"deprecated": False, "parameters": {"query:a": {"required": False, "type": "string"}}, "request": {"n": {"type": "integer", "required": False}},
                       "responses": {"200": {"id": {"type": "string", "required": True}}}}}
    assert contracts.diff(base, base) == []
    removed = contracts.diff(base, {})
    assert removed and "not deprecated first" in removed[0]
    changed = json.loads(json.dumps(base))
    op = changed["POST /x"]
    op["parameters"]["query:b"] = {"required": True, "type": "string"}
    op["request"]["n"]["type"] = "string"
    op["request"]["m"] = {"type": "string", "required": True}
    del op["responses"]["200"]["id"]
    msgs = " | ".join(contracts.diff(base, changed))
    assert "new required parameter" in msgs and "request property type changed" in msgs and "new required request property" in msgs and "response 200 property removed" in msgs
    additive = json.loads(json.dumps(base))
    additive["POST /x"]["request"]["opt"] = {"type": "string", "required": False}
    additive["POST /x"]["responses"]["200"]["extra"] = {"type": "string", "required": False}
    assert contracts.diff(base, additive) == []  # additive optional changes are allowed


def test_integration_schemas_expose_no_secret_fields() -> None:
    summary = contracts.current_summary()
    for op, spec in summary.items():
        if "/integrations/" not in op:
            continue
        for code, props in spec["responses"].items():
            for name in props:
                assert name not in ("credential", "credential_ref_value", "ciphertext", "token", "password"), (op, name)
