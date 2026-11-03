"""End-to-end smoke test against a RUNNING NOVA deployment (standard library only; never prints the token).

Read-only checks (always): liveness, readiness, security headers, request-id echo, auth enforcement (missing / wrong token -> 401),
authenticated reads, malformed and oversized requests rejected, unknown resource -> 404 envelope.
Write workflow (only with --write and --repo-path): register a repository (path as the SERVER sees it, under NOVA_REGISTRATION_BASE_DIRS),
index it, wait for completion, query symbols, run static analysis, read findings, create a task and read it back.
The write workflow leaves that repository and task in the database: run it against a fresh deployment or staging, not a production tenant.
Optional --web-url checks that the web container serves the SPA, its fallback route, hashed assets, and proxies /api.

Usage:
  NOVA_API_TOKEN=... python scripts/deployment_smoke.py --url http://127.0.0.1:8000 [--write --repo-path /srv/repos/sample] [--web-url http://127.0.0.1:8080]
Exit code 0 only when every executed check passes. Output is JSON lines plus a summary.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class Smoke:
    def __init__(self, base: str, token: str, timeout: float) -> None:
        if not base.startswith(("http://", "https://")):
            raise SystemExit("--url must be http(s)")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.results: list[dict[str, Any]] = []

    def call(self, method: str, path: str, body: Any = None, *, auth: bool = True, token: str | None = None, raw: bytes | None = None,
             headers: dict[str, str] | None = None, base: str | None = None) -> tuple[int, dict[str, str], Any]:
        h = dict(headers or {})
        tok = self.token if token is None else token
        if auth and tok:
            h["Authorization"] = f"Bearer {tok}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request((base or self.base) + path, data=data, method=method, headers=h)  # noqa: S310 - scheme checked
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                payload = r.read(2_000_000)
                return r.status, {k.lower(): v for k, v in r.headers.items()}, _json(payload)
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, _json(exc.read(200_000))
        except (urllib.error.URLError, OSError) as exc:
            return 0, {}, {"transport_error": type(exc).__name__}

    def declared_oversize(self, path: str, size: int) -> int:
        """Declare a body larger than the server limit and send none of it: the server must answer 413 without reading the body.
        (Streaming the whole body with urllib fails on the client side once the server has answered and closed the connection.)"""
        u = urllib.parse.urlsplit(self.base + path)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port, timeout=self.timeout)
        try:
            conn.putrequest("POST", u.path)
            for k, v in (("Content-Type", "application/json"), ("Content-Length", str(size))) + ((("Authorization", f"Bearer {self.token}"),) if self.token else ()):
                conn.putheader(k, v)
            conn.endheaders()
            return conn.getresponse().status
        except OSError:
            return 0
        finally:
            conn.close()

    def check(self, name: str, ok: bool, **detail: Any) -> bool:
        entry = {"check": name, "ok": bool(ok), **detail}
        self.results.append(entry)
        print(json.dumps(entry, default=str), flush=True)
        return bool(ok)


def _json(b: bytes) -> Any:
    try:
        return json.loads(b) if b[:1] in (b"{", b"[") else b.decode("utf-8", "replace")[:20000]
    except ValueError:
        return None


def read_only(s: Smoke) -> None:
    st, hd, body = s.call("GET", "/api/v1/health", auth=False)
    s.check("liveness", st == 200 and isinstance(body, dict) and body.get("status") == "ok", status=st)
    st, _, body = s.call("GET", "/api/v1/ready", auth=False)
    s.check("readiness", st == 200 and body.get("status") == "ready", status=st, checks=(body or {}).get("checks") if isinstance(body, dict) else None)
    s.check("security headers", hd.get("x-content-type-options") == "nosniff" and hd.get("cache-control") == "no-store",
            nosniff=hd.get("x-content-type-options"), cache_control=hd.get("cache-control"))
    rid = "smoke-" + uuid.uuid4().hex[:12]
    st, hd, _ = s.call("GET", "/api/v1/health", auth=False, headers={"X-Request-ID": rid})
    s.check("request id echoed", hd.get("x-request-id") == rid, status=st)
    # Through the web proxy these paths return the SPA (200 HTML), so look for the documents themselves rather than a status code.
    st, _, docs = s.call("GET", "/docs", auth=False)
    st2, _, spec = s.call("GET", "/openapi.json", auth=False)
    exposed = (st == 200 and isinstance(docs, str) and "swagger" in docs.lower()) or (st2 == 200 and isinstance(spec, dict) and "openapi" in spec)
    s.check("interactive docs not exposed (production)", not exposed, docs_status=st, openapi_status=st2)
    if not s.token:
        s.check("token supplied", False, reason="set NOVA_API_TOKEN (or --token-env) to run authenticated checks")
        return
    st, hd, body = s.call("GET", "/api/v1/repositories", auth=False)
    s.check("missing token -> 401", st == 401 and body.get("error", {}).get("code") == "unauthorized", status=st)
    wrong = "x" * 40  # deliberately invalid; counts once toward the per-client auth throttle
    st, _, _ = s.call("GET", "/api/v1/repositories", token=wrong)
    s.check("wrong token -> 401", st == 401, status=st)
    for path in ("/api/v1/workspace", "/api/v1/overview", "/api/v1/repositories", "/api/v1/tasks", "/api/v1/governance/me",
                 "/api/v1/ops/health", "/api/v1/integrations/health"):
        st, _, _ = s.call("GET", path)
        s.check(f"authenticated GET {path}", st == 200, status=st)
    st, _, body = s.call("GET", "/api/v1/repositories/doesnotexist")
    s.check("unknown resource -> 404 envelope", st == 404 and isinstance(body, dict) and "error" in body, status=st)
    st, _, body = s.call("POST", "/api/v1/tasks", raw=b"{not json")
    s.check("malformed JSON -> 422", st == 422 and body.get("error", {}).get("code") == "validation_error", status=st)
    canary = "zz-smoke-canary-" + uuid.uuid4().hex[:6]
    st, _, body = s.call("POST", "/api/v1/tasks", {"title": canary, "repository_id": canary})
    s.check("invalid body -> 422 without echoing submitted values", st == 422 and canary not in json.dumps(body), status=st)
    st = s.declared_oversize("/api/v1/tasks", 11 * 1024 * 1024)  # above the API's 8 MiB and the web proxy's 10 MiB limits
    s.check("oversized body -> 413 (rejected on declared length, before upload)", st == 413, status=st)


def wait_for(s: Smoke, path: str, done: set[str], timeout: float) -> tuple[str, Any]:
    deadline, body = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        st, _, body = s.call("GET", path)
        status = (body.get("status") or body.get("state")) if isinstance(body, dict) else None
        if st == 200 and status in done:
            return status, body
        if st == 200 and status == "failed":
            return "failed", body
        time.sleep(1.0)
    return "timeout", body


def write_workflow(s: Smoke, repo_path: str, timeout: float) -> None:
    name = "smoke-" + uuid.uuid4().hex[:8]
    st, _, body = s.call("POST", "/api/v1/repositories", {"name": name, "root_path": repo_path, "description": "deployment smoke test"})
    if st == 409 and isinstance(body, dict) and (body.get("error", {}).get("details") or {}).get("repository_id"):
        repo_id = body["error"]["details"]["repository_id"]
        s.check("register repository (already registered: reused)", True, status=st)
    else:
        if not s.check("register repository", st == 201, status=st, error=(body or {}).get("error") if isinstance(body, dict) else body):
            return
        repo_id = body["id"]
    st, _, snap = s.call("POST", f"/api/v1/repositories/{repo_id}/index", {"mode": "full"})
    if not s.check("start indexing (202)", st == 202, status=st, error=(snap or {}).get("error") if isinstance(snap, dict) else snap):
        return
    status, snap_body = wait_for(s, f"/api/v1/repositories/{repo_id}/snapshots/{snap['id']}", {"completed", "completed_with_warnings"}, timeout)
    if not s.check("indexing completes", status in ("completed", "completed_with_warnings"), status=status,
                   files_indexed=(snap_body or {}).get("files_indexed") if isinstance(snap_body, dict) else None):
        return
    st, _, syms = s.call("GET", f"/api/v1/repositories/{repo_id}/symbols?page_size=5")
    s.check("symbols query (database read of indexed data)", st == 200 and isinstance(syms, dict) and syms.get("total", 0) > 0,
            status=st, total=(syms or {}).get("total") if isinstance(syms, dict) else None)
    st, _, run = s.call("POST", f"/api/v1/repositories/{repo_id}/analysis", {"mode": "full"})
    if s.check("start static analysis (202)", st == 202, status=st):
        status, run_body = wait_for(s, f"/api/v1/repositories/{repo_id}/analysis/runs/{run['id']}", {"completed", "completed_with_warnings"}, timeout)
        s.check("analysis completes", status in ("completed", "completed_with_warnings"), status=status,
                findings_open=(run_body or {}).get("findings_open") if isinstance(run_body, dict) else None)
        st, _, fnd = s.call("GET", f"/api/v1/repositories/{repo_id}/findings?page_size=5")
        s.check("findings readable", st == 200 and isinstance(fnd, dict) and "items" in fnd, status=st,
                total=(fnd or {}).get("total") if isinstance(fnd, dict) else None)
    # Code review reads the working tree through the git CLI: a 409 "Git is not available" here means the image lacks git.
    st, _, rv = s.call("POST", f"/api/v1/repositories/{repo_id}/reviews", {"mode": "working_tree"})
    if s.check("start code review (git) (202)", st == 202, status=st, error=(rv or {}).get("error") if isinstance(rv, dict) else rv):
        status, rbody = wait_for(s, f"/api/v1/repositories/{repo_id}/reviews/{rv['id']}", {"completed", "completed_with_warnings"}, timeout)
        s.check("code review completes", status in ("completed", "completed_with_warnings"), status=status,
                error=(rbody or {}).get("error_message") if isinstance(rbody, dict) else None)
    # Running repository tests: either a real terminal result, or an explicit refusal when NOVA_TEST_EXECUTION_ENABLED=false. Never a silent pass.
    st, _, _ = s.call("POST", f"/api/v1/repositories/{repo_id}/tests/discover", {})
    if s.check("test discovery (201)", st == 201, status=st):
        st, _, run = s.call("POST", f"/api/v1/repositories/{repo_id}/tests/execute", {"confirm": True, "coverage": False})
        if s.check("start test execution (202)", st == 202, status=st):
            deadline, item = time.monotonic() + timeout, {}
            while time.monotonic() < deadline:
                _, _, pg = s.call("GET", f"/api/v1/repositories/{repo_id}/tests/runs?page_size=5")
                item = next((i for i in (pg or {}).get("items", []) if i.get("id") == run["id"]), {}) if isinstance(pg, dict) else {}
                if item.get("status") not in (None, "pending", "running"):
                    break
                time.sleep(1.0)
            status, reason = item.get("status"), item.get("reason") or ""
            disabled = status == "rejected" and "NOVA_TEST_EXECUTION_ENABLED" in reason
            ran = status in ("passed", "failed") and item.get("tests_collected") is not None
            s.check("test execution result is explicit" + (" (disabled by configuration)" if disabled else ""), disabled or ran,
                    status=status, reason=reason[:160], tests_collected=item.get("tests_collected"), tests_passed=item.get("tests_passed"))
    task = {"repository_id": repo_id, "title": "Smoke test task", "problem_statement": "Deployment smoke test creates and reads a task.",
            "objective": "Verify database writes", "acceptance_criteria": ["The task can be read back"]}
    st, _, created = s.call("POST", "/api/v1/tasks", task)
    if s.check("create task (database write)", st == 201, status=st):
        st, _, got = s.call("GET", f"/api/v1/tasks/{created['id']}")
        s.check("read task back", st == 200 and got.get("title") == task["title"], status=st)


def web_checks(s: Smoke, web: str) -> None:
    web = web.rstrip("/")
    st, hd, body = s.call("GET", "/", auth=False, base=web)
    html = body if isinstance(body, str) else ""
    s.check("web: index served with CSP", st == 200 and '<div id="root">' in html and "content-security-policy" in hd, status=st)
    st, _, _ = s.call("GET", "/repositories/some/deep/link", auth=False, base=web)
    s.check("web: SPA fallback for client routes", st == 200, status=st)
    st, _, _ = s.call("GET", "/assets/definitely-missing.js", auth=False, base=web)
    s.check("web: missing asset is 404 (not index.html)", st == 404, status=st)
    st, _, raw = s.call("GET", "/", auth=False, base=web)
    m = re.search(r'src="(/assets/[^"]+\.js)"', raw if isinstance(raw, str) else "")
    if s.check("web: index references a hashed bundle", bool(m)):
        st, hd, _ = s.call("GET", m.group(1), auth=False, base=web)
        s.check("web: hashed asset cached long-term", st == 200 and "max-age=31536000" in hd.get("cache-control", ""), status=st)
    st, _, body = s.call("GET", "/api/v1/health", auth=False, base=web)
    s.check("web: /api proxied to the API", st == 200 and isinstance(body, dict) and body.get("status") == "ok", status=st)
    if s.token:
        st, _, _ = s.call("GET", "/api/v1/overview", base=web)
        s.check("web: authenticated request through the proxy", st == 200, status=st)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="API base URL, e.g. http://127.0.0.1:8000 (or the web URL: /api is proxied)")
    p.add_argument("--token-env", default="NOVA_API_TOKEN")
    p.add_argument("--wait", type=float, default=60.0, help="seconds to wait for readiness first")
    p.add_argument("--write", action="store_true", help="run the write workflow (creates data)")
    p.add_argument("--repo-path", help="repository path as seen by the server (required with --write)")
    p.add_argument("--web-url", help="web container URL to check static serving and the /api proxy")
    p.add_argument("--job-timeout", type=float, default=180.0)
    a = p.parse_args(argv)
    s = Smoke(a.url, os.environ.get(a.token_env, ""), timeout=30.0)
    deadline = time.monotonic() + a.wait
    while time.monotonic() < deadline and s.call("GET", "/api/v1/ready", auth=False)[0] != 200:
        time.sleep(1.0)
    read_only(s)
    if a.write:
        if not a.repo_path:
            s.check("write workflow", False, reason="--repo-path is required with --write")
        else:
            write_workflow(s, a.repo_path, a.job_timeout)
    if a.web_url:
        web_checks(s, a.web_url)
    failed = [r["check"] for r in s.results if not r["ok"]]
    print(json.dumps({"summary": {"checks": len(s.results), "passed": len(s.results) - len(failed), "failed": failed}}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
