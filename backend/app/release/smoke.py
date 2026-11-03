"""Post-deploy checks against a running API using only the standard library. Output never contains the token."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

CRITICAL_AUTHENTICATED = ("/api/v1/overview", "/api/v1/repositories", "/api/v1/governance/me", "/api/v1/ops/health", "/api/v1/integrations/health")


def _get(base: str, path: str, token: str, timeout: float) -> tuple[int, Any]:
    if not base.startswith(("http://", "https://")):
        raise ValueError("base URL must be http(s)")
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"} if token else {})  # noqa: S310 - scheme validated above
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied http(s) URL
            body = r.read(1_000_000)
            return r.status, json.loads(body) if body[:1] in (b"{", b"[") else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def wait_ready(base: str, *, timeout: float = 60.0, interval: float = 1.0) -> dict[str, Any]:
    deadline, last = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        last, _ = _get(base, "/api/v1/ready", "", 3.0)
        if last == 200:
            return {"ready": True, "status": last}
        time.sleep(interval)
    return {"ready": False, "status": last}


def run(base: str, token: str = "", *, timeout: float = 10.0) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, path: str, expect: int, auth: bool, extra=None) -> None:
        status, body = _get(base, path, token if auth else "", timeout)
        ok = status == expect and (extra is None or extra(body))
        checks.append({"name": name, "path": path, "status": status, "expected": expect, "ok": bool(ok)})

    check("liveness", "/api/v1/health", 200, False)
    check("readiness", "/api/v1/ready", 200, False, lambda b: isinstance(b, dict))
    if token:
        check("auth enforced", "/api/v1/repositories", 401, False)
        for p in CRITICAL_AUTHENTICATED:
            check(f"authenticated {p.rsplit('/', 1)[-1]}", p, 200, True)
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "authenticated_checks": bool(token)}
