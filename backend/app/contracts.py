"""API contract governance: reduce the generated OpenAPI document to a stable summary and detect breaking changes against a checked-in baseline.

Breaking (fails the check): a removed path or method; a removed success status code; a newly *required* parameter or request property; a removed
or retyped request/response property; a parameter whose type changed. Additive changes (new paths, new optional fields) are allowed, after which
the baseline is regenerated deliberately: ``python -m app.contracts write``. Deprecation convention: mark the operation ``deprecated=True`` and
keep it working for at least one minor release before removal; the baseline records the flag so a removal of a non-deprecated operation is flagged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BASELINE = Path(__file__).resolve().parent.parent / "contracts" / "openapi.baseline.json"
METHODS = ("get", "post", "put", "patch", "delete")


def _resolve(doc: dict[str, Any], schema: Any, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 6:
        return {}
    if "$ref" in schema:
        node: Any = doc
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return _resolve(doc, node, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return {"union": sorted({json.dumps(_type(doc, s), sort_keys=True) for s in schema[key]})}
    return schema


def _type(doc: dict[str, Any], schema: Any) -> Any:
    s = _resolve(doc, schema)
    if "union" in s:
        return s
    if s.get("type") == "array":
        return {"array": _type(doc, s.get("items", {}))}
    return s.get("type") or ("object" if "properties" in s else "any")


def _props(doc: dict[str, Any], schema: Any) -> dict[str, Any]:
    s = _resolve(doc, schema)
    if s.get("type") == "array":
        s = _resolve(doc, s.get("items", {}))
    req = set(s.get("required", []))
    return {name: {"type": _type(doc, p), "required": name in req} for name, p in (s.get("properties") or {}).items()}


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, item in sorted(doc.get("paths", {}).items()):
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            params = {f"{p['in']}:{p['name']}": {"required": bool(p.get("required")), "type": _type(doc, p.get("schema", {}))} for p in op.get("parameters", [])}
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            responses = {}
            for code, r in op.get("responses", {}).items():
                sch = r.get("content", {}).get("application/json", {}).get("schema")
                responses[code] = _props(doc, sch) if sch else {}
            out[f"{method.upper()} {path}"] = {"deprecated": bool(op.get("deprecated")), "parameters": params, "request": _props(doc, body) if body else {}, "responses": responses}
    return out


def diff(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for op, b in baseline.items():
        c = current.get(op)
        if c is None:
            problems.append(f"removed operation: {op}" + ("" if b.get("deprecated") else " (not deprecated first)"))
            continue
        for name, bp in b["parameters"].items():
            cp = c["parameters"].get(name)
            if cp is None:
                problems.append(f"{op}: parameter removed: {name}")
            elif cp["type"] != bp["type"]:
                problems.append(f"{op}: parameter type changed: {name}")
        for name, cp in c["parameters"].items():
            if cp["required"] and name not in b["parameters"]:
                problems.append(f"{op}: new required parameter: {name}")
        for name, cp in c["request"].items():
            if cp["required"] and name not in b["request"]:
                problems.append(f"{op}: new required request property: {name}")
        for name, bp in b["request"].items():
            cp = c["request"].get(name)
            if cp is None:
                problems.append(f"{op}: request property removed: {name}")
            elif cp["type"] != bp["type"]:
                problems.append(f"{op}: request property type changed: {name}")
        for code, bprops in b["responses"].items():
            if code.startswith("2") and code not in c["responses"]:
                problems.append(f"{op}: success response {code} removed")
                continue
            for name, bp in bprops.items():
                cp = c["responses"].get(code, {}).get(name)
                if cp is None:
                    problems.append(f"{op}: response {code} property removed: {name}")
                elif cp["type"] != bp["type"]:
                    problems.append(f"{op}: response {code} property type changed: {name}")
    return problems


def current_summary() -> dict[str, Any]:
    from app.main import create_app
    return summarize(create_app().openapi())


def main(argv: list[str]) -> int:
    if argv[:1] == ["write"]:
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(current_summary(), indent=1, sort_keys=True) + "\n")
        print(f"baseline written: {BASELINE}")
        return 0
    problems = diff(json.loads(BASELINE.read_text()), current_summary())
    for p in problems:
        print("BREAKING:", p)
    print("contract OK" if not problems else f"{len(problems)} breaking change(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
