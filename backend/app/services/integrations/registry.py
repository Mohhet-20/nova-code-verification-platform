"""Typed integration adapter interface, capability declarations and the trusted built-in adapter registry.

Adapters are compiled-in classes. Nothing is imported from a database row, path or request parameter. An adapter that does not
support an operation raises ``Unsupported`` (never silently succeeds).
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.services.cicd import providers as ci_providers
from app.services.cicd.errors import CiInvalid, CiUnauthorized
from app.services.integrations.netguard import HttpClient, HttpError

CAPABILITIES = ("validate_config", "test_connection", "inbound_events", "ci_evidence", "publish_status", "issue_tracker", "outbound_action")


class Unsupported(Exception):  # noqa: N818
    def __init__(self, operation: str) -> None:
        super().__init__(f"unsupported_operation:{operation}")
        self.operation = operation


class ConfigInvalid(Exception):  # noqa: N818
    def __init__(self, problems: list[str]) -> None:
        super().__init__("config_invalid")
        self.problems = problems


class AdapterError(Exception):
    """Provider-side failure with a safe category and retryability."""

    def __init__(self, category: str, retryable: bool = False) -> None:
        super().__init__(category)
        self.category, self.retryable = category, retryable


class InboundAuthError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class InvalidInbound(Exception):  # noqa: N818
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass
class InboundEvent:
    """Provider-neutral normalized inbound event (a minimal summary, never the raw payload)."""
    delivery_id: str
    event_type: str
    action: str = ""
    external_repo: str = ""
    external_ref: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    ci_run: dict[str, Any] | None = None
    ignored: bool = False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoMapping(_Strict):
    external: str = Field(max_length=201)
    repository_id: str = Field(min_length=1, max_length=32)

    @field_validator("external")
    @classmethod
    def _repo(cls, v: str) -> str:
        if not ci_providers.EXTERNAL_REPO.match(v):
            raise ValueError("invalid repository name")
        return v


class GitHubConfig(_Strict):
    api_base: str = Field(default="https://api.github.com", max_length=200)
    repositories: list[RepoMapping] = Field(min_length=1, max_length=50)
    publish_status: bool = False

    @field_validator("api_base")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://") or "@" in v or "?" in v or "#" in v:
            raise ValueError("api_base must be a plain https URL")
        return v.rstrip("/")


class IssueConfig(_Strict):
    api_base: str = Field(default="https://api.github.com", max_length=200)
    project: str = Field(max_length=201)
    allow_create: bool = False

    @field_validator("project")
    @classmethod
    def _proj(cls, v: str) -> str:
        if not ci_providers.EXTERNAL_REPO.match(v):
            raise ValueError("invalid project name")
        return v

    @field_validator("api_base")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://") or "@" in v or "?" in v or "#" in v:
            raise ValueError("api_base must be a plain https URL")
        return v.rstrip("/")


class LocalConfig(_Strict):
    repositories: list[RepoMapping] = Field(default_factory=list, max_length=50)


class Adapter:
    type_id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    category: ClassVar[str] = ""
    description: ClassVar[str] = ""
    capabilities: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[BaseModel]] = _Strict
    requires_credential: ClassVar[bool] = True
    requires_webhook_secret: ClassVar[bool] = False
    permissions: ClassVar[tuple[str, ...]] = ()  # permissions requested of the external system (read-only unless writes are needed)
    trusted: ClassVar[bool] = True

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.config_model.model_validate(config).model_dump()
        except ValidationError as exc:
            raise ConfigInvalid([f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()][:10]) from None

    def test_connection(self, config: dict[str, Any], token: str | None, http: HttpClient) -> dict[str, Any]:
        raise Unsupported("test_connection")

    def verify_inbound(self, secret: str | None, headers: dict[str, str], body: bytes) -> None:
        raise Unsupported("inbound_events")

    def normalize_inbound(self, headers: dict[str, str], payload: Any) -> InboundEvent:
        raise Unsupported("inbound_events")

    def publish_status(self, config: dict[str, Any], token: str | None, http: HttpClient, **kw: Any) -> dict[str, Any]:
        raise Unsupported("publish_status")

    def search_issues(self, config: dict[str, Any], token: str | None, http: HttpClient, query: str) -> list[dict[str, Any]]:
        raise Unsupported("search_issues")

    def get_issue(self, config: dict[str, Any], token: str | None, http: HttpClient, external_id: str) -> dict[str, Any]:
        raise Unsupported("get_issue")

    def create_issue(self, config: dict[str, Any], token: str | None, http: HttpClient, title: str, body: str) -> dict[str, Any]:
        raise Unsupported("create_issue")

    def manifest(self) -> dict[str, Any]:
        return {"type": self.type_id, "title": self.title, "category": self.category, "description": self.description, "capabilities": list(self.capabilities),
                "permissions": list(self.permissions), "requires_credential": self.requires_credential, "requires_webhook_secret": self.requires_webhook_secret,
                "config_schema": self.config_model.model_json_schema()}


def _gh_call(http: HttpClient, method: str, api_base: str, path: str, token: str | None, body: Any = None, allowed: list[str] | None = None) -> tuple[Any, int, dict[str, str]]:
    if not token:
        raise AdapterError("credential_missing")
    hdrs = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nova-integrations", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    try:
        r = http.request(method, api_base + path, headers=hdrs, body=data, allowed_hosts=allowed)
    except HttpError as exc:
        raise AdapterError(exc.category, retryable=exc.category in ("timeout", "connection_failed")) from None
    if r.status in (401, 403, 404, 422, 429) or r.status >= 500 or not 200 <= r.status < 300:
        cat, retry = ci_providers.classify_http(r.status, r.headers)
        raise AdapterError(cat, retry)
    if r.truncated:
        raise AdapterError("response_too_large")
    try:
        return (json.loads(r.body.decode("utf-8", "replace")) if r.body else None), r.status, r.headers
    except ValueError:
        raise AdapterError("malformed_response") from None


def _host(api_base: str) -> list[str]:
    return [api_base.split("//", 1)[1].split("/", 1)[0].split(":")[0]]


_GH = ci_providers.GitHubProvider("https://api.github.com")


class GitHubAdapter(Adapter):
    type_id = "github"
    title = "GitHub"
    category = "version_control"
    description = "Receive pull request and check-run events, record CI evidence, and optionally publish a NOVA commit status."
    capabilities = ("validate_config", "test_connection", "inbound_events", "ci_evidence", "publish_status")
    config_model = GitHubConfig
    requires_webhook_secret = True
    permissions = ("metadata:read", "pull_requests:read", "checks:read", "statuses:write")

    def test_connection(self, config: dict[str, Any], token: str | None, http: HttpClient) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        perms: set[str] = set()
        for m in config["repositories"]:
            data, _, _ = _gh_call(http, "GET", config["api_base"], f"/repos/{m['external']}", token, allowed=_host(config["api_base"]))
            if not isinstance(data, dict) or str(data.get("full_name", "")).lower() != m["external"].lower():
                raise AdapterError("repository_mismatch")
            p = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
            perms |= {k for k, v in p.items() if v}
            out.append({"repository": m["external"], "private": bool(data.get("private"))})
        return {"repositories": out, "granted": sorted(perms)[:10]}

    def verify_inbound(self, secret: str | None, headers: dict[str, str], body: bytes) -> None:
        try:
            _GH.verify_webhook(secret, headers, body)
        except CiUnauthorized as exc:
            raise InboundAuthError(exc.code if hasattr(exc, "code") else "signature_invalid") from None

    def normalize_inbound(self, headers: dict[str, str], payload: Any) -> InboundEvent:
        delivery = headers.get("x-github-delivery", "")
        event = headers.get("x-github-event", "")
        if not ci_providers.DELIVERY.match(delivery) or not event.replace("_", "").isalpha() or len(event) > 40:
            raise InvalidInbound("headers_invalid")
        if event in ("check_run", "workflow_run"):
            return self._ci(delivery, event, payload)
        try:
            n = _GH.normalize_webhook(headers, payload)
        except CiInvalid:
            raise InvalidInbound("payload_invalid") from None
        if n.kind == "ping":
            return InboundEvent(delivery, "ping", external_repo=n.repository, summary={}, ignored=False)
        if n.kind == "ignored":
            return InboundEvent(delivery, event, external_repo=n.repository, ignored=True)
        cr = n.change_request
        assert cr is not None
        return InboundEvent(delivery, "pull_request", n.action, n.repository, cr.external_id,
                            {"number": cr.external_id, "head_sha": cr.head_sha, "state": cr.state, "title": cr.title[:200]})

    def _ci(self, delivery: str, event: str, payload: Any) -> InboundEvent:
        if not isinstance(payload, dict):
            raise InvalidInbound("payload_invalid")
        repo = payload.get("repository")
        full = repo.get("full_name") if isinstance(repo, dict) else None
        run = payload.get(event)
        if not isinstance(full, str) or not ci_providers.EXTERNAL_REPO.match(full) or not isinstance(run, dict):
            raise InvalidInbound("payload_invalid")
        sha = run.get("head_sha")
        rid = run.get("id")
        if not (isinstance(sha, str) and ci_providers.SHA.match(sha)) or not isinstance(rid, int) or isinstance(rid, bool):
            raise InvalidInbound("payload_invalid")
        status, concl = run.get("status"), run.get("conclusion")
        if status != "completed":
            st = "pending"
        else:
            st = {"success": "success", "failure": "failed", "timed_out": "failed", "cancelled": "cancelled"}.get(str(concl), "unknown")
        url = run.get("html_url")
        return InboundEvent(delivery, event, str(payload.get("action", ""))[:40], full, str(rid),
                            {"head_sha": sha, "status": st},
                            ci_run={"source": "github", "external_run_id": str(rid), "name": str(run.get("name", ""))[:120], "head_sha": sha, "status": st,
                                    "url": url if isinstance(url, str) and url.startswith("https://") else None,
                                    "pr_number": None, "started_at": run.get("started_at") or run.get("run_started_at"), "completed_at": run.get("completed_at")})

    def publish_status(self, config: dict[str, Any], token: str | None, http: HttpClient, **kw: Any) -> dict[str, Any]:
        if not config.get("publish_status"):
            raise Unsupported("publish_status_disabled")
        repo, sha, state = kw["external_repo"], kw["sha"], kw["state"]
        if not ci_providers.SHA.match(sha) or state not in ("success", "failure", "error", "pending") or not ci_providers.CONTEXT.match(kw.get("context", "nova/verification")):
            raise AdapterError("rejected")
        if repo.lower() not in {m["external"].lower() for m in config["repositories"]}:
            raise AdapterError("repository_not_mapped")
        data, status, _ = _gh_call(http, "POST", config["api_base"], f"/repos/{repo}/statuses/{sha}", token,
                                   {"state": state, "description": str(kw.get("description", ""))[:140], "context": kw.get("context", "nova/verification")}, _host(config["api_base"]))
        return {"http_status": status, "provider_id": str(data.get("id"))[:40] if isinstance(data, dict) else None}


class GitHubIssuesAdapter(Adapter):
    type_id = "github-issues"
    title = "GitHub Issues"
    category = "issue_tracker"
    description = "Search, retrieve and link GitHub issues. Creating an issue is a separate explicit, authorized action."
    capabilities = ("validate_config", "test_connection", "issue_tracker")
    config_model = IssueConfig
    permissions = ("metadata:read", "issues:read", "issues:write")

    def test_connection(self, config: dict[str, Any], token: str | None, http: HttpClient) -> dict[str, Any]:
        data, _, _ = _gh_call(http, "GET", config["api_base"], f"/repos/{config['project']}", token, allowed=_host(config["api_base"]))
        if not isinstance(data, dict) or str(data.get("full_name", "")).lower() != config["project"].lower():
            raise AdapterError("repository_mismatch")
        return {"project": config["project"], "issues_enabled": bool(data.get("has_issues", True))}

    @staticmethod
    def _issue(d: Any) -> dict[str, Any]:
        if not isinstance(d, dict) or not isinstance(d.get("number"), int) or "pull_request" in d:
            raise AdapterError("malformed_response")
        url = d.get("html_url")
        return {"external_id": str(d["number"]), "key": f"#{d['number']}", "title": str(d.get("title", ""))[:200], "state": str(d.get("state", ""))[:20],
                "url": url if isinstance(url, str) and url.startswith("https://") else ""}

    def search_issues(self, config: dict[str, Any], token: str | None, http: HttpClient, query: str) -> list[dict[str, Any]]:
        from urllib.parse import quote
        q = quote(f"repo:{config['project']} is:issue {query[:100]}")
        data, _, _ = _gh_call(http, "GET", config["api_base"], f"/search/issues?q={q}&per_page=10", token, allowed=_host(config["api_base"]))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise AdapterError("malformed_response")
        return [self._issue(i) for i in items[:10] if isinstance(i, dict) and "pull_request" not in i]

    def get_issue(self, config: dict[str, Any], token: str | None, http: HttpClient, external_id: str) -> dict[str, Any]:
        if not external_id.isdigit():
            raise AdapterError("rejected")
        data, _, _ = _gh_call(http, "GET", config["api_base"], f"/repos/{config['project']}/issues/{external_id}", token, allowed=_host(config["api_base"]))
        return self._issue(data)

    def create_issue(self, config: dict[str, Any], token: str | None, http: HttpClient, title: str, body: str) -> dict[str, Any]:
        if not config.get("allow_create"):
            raise Unsupported("create_issue_disabled")
        data, _, _ = _gh_call(http, "POST", config["api_base"], f"/repos/{config['project']}/issues", token, {"title": title[:200], "body": body[:4000]}, _host(config["api_base"]))
        return self._issue(data)


class LocalTestAdapter(Adapter):
    """Clearly labelled local/test adapter: no network, accepts HMAC-signed JSON events in NOVA's own simple format. Not a production provider."""
    type_id = "local-test"
    title = "Local test adapter (no network)"
    category = "testing"
    description = "For local development and tests only. Accepts signed JSON events and records CI evidence without contacting any external system."
    capabilities = ("validate_config", "test_connection", "inbound_events", "ci_evidence")
    config_model = LocalConfig
    requires_credential = False
    requires_webhook_secret = True

    def test_connection(self, config: dict[str, Any], token: str | None, http: HttpClient) -> dict[str, Any]:
        return {"mode": "local", "network": False}

    def verify_inbound(self, secret: str | None, headers: dict[str, str], body: bytes) -> None:
        import hashlib
        if not secret:
            raise InboundAuthError("webhook_secret_missing")
        sig = headers.get("x-nova-signature", "")
        exp = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, exp):
            raise InboundAuthError("signature_invalid")

    def normalize_inbound(self, headers: dict[str, str], payload: Any) -> InboundEvent:
        delivery = headers.get("x-nova-delivery", "")
        if not ci_providers.DELIVERY.match(delivery) or not isinstance(payload, dict):
            raise InvalidInbound("payload_invalid")
        repo, sha, rid = payload.get("repository"), payload.get("head_sha"), payload.get("run_id")
        st = payload.get("status")
        if not (isinstance(repo, str) and ci_providers.EXTERNAL_REPO.match(repo) and isinstance(sha, str) and ci_providers.SHA.match(sha) and isinstance(rid, str) and 0 < len(rid) <= 80
                and st in ("pending", "success", "failed", "cancelled", "unknown")):
            raise InvalidInbound("payload_invalid")
        return InboundEvent(delivery, "ci_run", "", repo, rid, {"head_sha": sha, "status": st},
                            ci_run={"source": "local-test", "external_run_id": rid, "name": str(payload.get("name", ""))[:120], "head_sha": sha, "status": st,
                                    "url": None, "pr_number": None, "started_at": None, "completed_at": None})


ADAPTERS: dict[str, Adapter] = {a.type_id: a for a in (GitHubAdapter(), GitHubIssuesAdapter(), LocalTestAdapter())}


def get_adapter(type_id: str) -> Adapter:
    a = ADAPTERS.get(type_id)
    if a is None:
        raise Unsupported("unknown_integration_type")
    return a
