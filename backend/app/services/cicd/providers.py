"""Provider-neutral adapter contract plus the GitHub and local-Git adapters.

The core (workflows, readiness, reports) never imports provider specifics. A provider only knows how to: verify and normalize webhooks,
validate a connection, list changed files, and report a status. All network access goes through an injected ``Transport`` so tests
mock exactly the external boundary. Tokens are read from environment variables *named* by the connection; they are never stored.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.services.analysis.redaction import scrub
from app.services.cicd.errors import CiInvalid, CiUnauthorized, ProviderError

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
EXTERNAL_REPO = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]{1,100}/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CONTEXT = re.compile(r"^nova/[A-Za-z0-9_.-]{1,50}$")
DELIVERY = re.compile(r"^[A-Za-z0-9-]{8,80}$")
REF = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
PROVIDERS = ("local", "github")


@dataclass
class NormalizedChangeRequest:
    external_id: str
    source_branch: str
    target_branch: str
    head_sha: str
    base_sha: str | None
    title: str
    description: str
    author: str
    state: str  # open | closed | merged
    updated_at: datetime | None = None


@dataclass
class NormalizedEvent:
    kind: str  # ping | change_request | ignored
    action: str = ""
    repository: str = ""
    change_request: NormalizedChangeRequest | None = None
    note: str = ""


@dataclass
class TransportResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, max_bytes: int) -> TransportResponse: ...


class HttpTransport:
    """The only code that touches the network. HTTPS to a configured API base only; bounded time and size."""

    def __init__(self, allowed_base: str) -> None:
        self.allowed_base = allowed_base.rstrip("/")

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, max_bytes: int) -> TransportResponse:
        if not url.startswith(self.allowed_base + "/") or not url.lower().startswith("https://"):
            raise ProviderError("blocked_url", "The request URL is outside the configured provider API.", retryable=False)
        req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 - scheme and host checked above
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ProviderError("response_too_large", "The provider response exceeded the size limit.", retryable=False)
                return TransportResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, data)
        except urllib.error.HTTPError as exc:
            data = exc.read(max_bytes + 1)[:max_bytes]
            return TransportResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, data)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ProviderError("network_error", f"The provider could not be reached ({type(exc).__name__}).", retryable=True) from None


def classify_http(status: int, headers: dict[str, str]) -> tuple[str, bool]:
    """(error class, retryable) for a non-2xx provider response. Conservative: unknown errors are permanent."""
    if status == 401:
        return "auth_failed", False
    if status == 403:
        if headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers:
            return "rate_limited", True
        return "forbidden", False
    if status == 429:
        return "rate_limited", True
    if status in (404, 410):
        return "not_found", False
    if status in (400, 422):
        return "rejected", False
    if status >= 500:
        return "provider_unavailable", True
    return "unexpected_status", False


def resolve_secret(env_name: str) -> str | None:
    if not env_name or not ENV_NAME.match(env_name):
        return None
    value = os.environ.get(env_name)
    return value or None


class Provider:
    name = "base"
    supports_webhooks = False
    supports_status_reporting = False
    supports_file_listing = False

    def verify_webhook(self, secret: str | None, headers: dict[str, str], body: bytes) -> None:
        raise CiInvalid("webhooks_unsupported", f"The {self.name} provider does not accept webhooks.")

    def normalize_webhook(self, headers: dict[str, str], payload: Any) -> NormalizedEvent:
        raise CiInvalid("webhooks_unsupported", f"The {self.name} provider does not accept webhooks.")

    def validate_connection(self, external_repo: str, token: str | None, transport: Transport, timeout: float, max_bytes: int) -> dict[str, Any]:
        raise NotImplementedError

    def list_changed_files(self, external_repo: str, number: str, token: str | None, transport: Transport, timeout: float, max_bytes: int,
                           limit: int) -> tuple[list[dict[str, str]], bool]:
        raise ProviderError("unsupported", "This provider cannot list changed files.", retryable=False)

    def report_status(self, external_repo: str, sha: str, state: str, description: str, context: str, token: str | None, transport: Transport,
                      timeout: float, max_bytes: int, api_base: str) -> dict[str, Any]:
        raise ProviderError("unsupported", "This provider cannot receive status reports.", retryable=False)


class LocalProvider(Provider):
    """Local Git branches. No network, no webhooks, no external status reporting."""

    name = "local"

    def validate_connection(self, external_repo: str, token: str | None, transport: Transport, timeout: float, max_bytes: int) -> dict[str, Any]:
        return {"mode": "local-git"}


def _text(v: Any, limit: int) -> str:
    return scrub(v, limit) if isinstance(v, str) else ""


class GitHubProvider(Provider):
    name = "github"
    supports_webhooks = True
    supports_status_reporting = True
    supports_file_listing = True

    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")

    # -- webhook authentication: X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(secret, raw body)
    def verify_webhook(self, secret: str | None, headers: dict[str, str], body: bytes) -> None:
        if not secret:
            raise CiUnauthorized("webhook_secret_missing", "The webhook secret is not configured, so deliveries cannot be authenticated.")
        sig = headers.get("x-hub-signature-256", "")
        if not sig.startswith("sha256=") or len(sig) != 71:
            raise CiUnauthorized("signature_invalid", "The webhook signature is missing or malformed.")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise CiUnauthorized("signature_invalid", "The webhook signature does not match.")

    def normalize_webhook(self, headers: dict[str, str], payload: Any) -> NormalizedEvent:
        event = headers.get("x-github-event", "")
        if not isinstance(payload, dict):
            raise CiInvalid("payload_invalid", "The webhook payload must be a JSON object.")
        repo = payload.get("repository")
        full = repo.get("full_name") if isinstance(repo, dict) else None
        if not isinstance(full, str) or not EXTERNAL_REPO.match(full):
            raise CiInvalid("payload_invalid", "The webhook payload does not identify a repository.")
        if event == "ping":
            return NormalizedEvent("ping", "", full)
        if event != "pull_request":
            return NormalizedEvent("ignored", "", full, note=f"Event '{event[:40]}' is not handled.")
        action = payload.get("action")
        pr = payload.get("pull_request")
        if not isinstance(action, str) or not isinstance(pr, dict):
            raise CiInvalid("payload_invalid", "The pull_request payload is malformed.")
        head, base = pr.get("head"), pr.get("base")
        num = pr.get("number")
        if not isinstance(head, dict) or not isinstance(base, dict) or not isinstance(num, int) or isinstance(num, bool) or num < 1:
            raise CiInvalid("payload_invalid", "The pull_request payload is malformed.")
        hs, bs = head.get("sha"), base.get("sha")
        hr, br = head.get("ref"), base.get("ref")
        if not (isinstance(hs, str) and SHA.match(hs) and isinstance(bs, str) and SHA.match(bs) and isinstance(hr, str) and REF.match(hr) and isinstance(br, str) and REF.match(br)):
            raise CiInvalid("payload_invalid", "The pull_request revisions or branches are invalid.")
        user = pr.get("user")
        merged = pr.get("merged") is True
        state = "merged" if merged else "closed" if pr.get("state") == "closed" else "open"
        updated: datetime | None = None
        raw_up = pr.get("updated_at")
        if isinstance(raw_up, str):
            try:
                updated = datetime.fromisoformat(raw_up.replace("Z", "+00:00"))
            except ValueError:
                updated = None
        ncr = NormalizedChangeRequest(str(num), _text(hr, 255), _text(br, 255), hs, bs, _text(pr.get("title"), 300), _text(pr.get("body"), 4000),
                                      _text(user.get("login") if isinstance(user, dict) else "", 120), state, updated)
        return NormalizedEvent("change_request", action[:40], full, ncr)

    def _headers(self, token: str | None) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nova-verification"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _call(self, transport: Transport, method: str, path: str, token: str | None, body: Any, timeout: float, max_bytes: int) -> tuple[Any, TransportResponse]:
        data = json.dumps(body).encode() if body is not None else None
        hdrs = self._headers(token)
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        resp = transport.request(method, f"{self.api_base}{path}", hdrs, data, timeout, max_bytes)
        if not 200 <= resp.status < 300:
            cls, retry = classify_http(resp.status, resp.headers)
            raise ProviderError(cls, f"The provider answered HTTP {resp.status}.", retryable=retry, status=resp.status)
        try:
            return (json.loads(resp.body.decode("utf-8", "replace")) if resp.body else None), resp
        except ValueError:
            raise ProviderError("malformed_response", "The provider returned a response that is not valid JSON.", retryable=False, status=resp.status) from None

    def validate_connection(self, external_repo: str, token: str | None, transport: Transport, timeout: float, max_bytes: int) -> dict[str, Any]:
        if not token:
            raise ProviderError("token_missing", "No API token is available in the configured environment variable.", retryable=False)
        data, resp = self._call(transport, "GET", f"/repos/{external_repo}", token, None, timeout, max_bytes)
        full = data.get("full_name") if isinstance(data, dict) else None
        if not isinstance(full, str) or full.lower() != external_repo.lower():
            raise ProviderError("repository_mismatch", "The provider returned a different repository than the one configured.", retryable=False, status=resp.status)
        perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        return {"full_name": full, "default_branch": _text(data.get("default_branch"), 255), "private": bool(data.get("private")),
                "permissions": {k: bool(v) for k, v in list(perms.items())[:10]}}

    def list_changed_files(self, external_repo: str, number: str, token: str | None, transport: Transport, timeout: float, max_bytes: int,
                           limit: int) -> tuple[list[dict[str, str]], bool]:
        if not token:
            raise ProviderError("token_missing", "No API token is available in the configured environment variable.", retryable=False)
        if not number.isdigit():
            raise ProviderError("rejected", "Invalid change request number.", retryable=False)
        out: list[dict[str, str]] = []
        page = 1
        while len(out) < limit and page <= 30:
            data, _ = self._call(transport, "GET", f"/repos/{external_repo}/pulls/{number}/files?per_page=100&page={page}", token, None, timeout, max_bytes)
            if not isinstance(data, list):
                raise ProviderError("malformed_response", "The file list is not an array.", retryable=False)
            for f in data:
                if isinstance(f, dict) and isinstance(f.get("filename"), str):
                    out.append({"path": _text(f["filename"], 1024), "status": _text(f.get("status"), 20) or "modified"})
            if len(data) < 100:
                return out[:limit], False
            page += 1
        return out[:limit], True

    def report_status(self, external_repo: str, sha: str, state: str, description: str, context: str, token: str | None, transport: Transport,
                      timeout: float, max_bytes: int, api_base: str) -> dict[str, Any]:
        if not token:
            raise ProviderError("token_missing", "No API token is available in the configured environment variable.", retryable=False)
        if not SHA.match(sha) or state not in ("success", "failure", "error", "pending") or not CONTEXT.match(context):
            raise ProviderError("rejected", "The status report is not valid.", retryable=False)
        data, resp = self._call(transport, "POST", f"/repos/{external_repo}/statuses/{sha}", token,
                                {"state": state, "description": description[:140], "context": context}, timeout, max_bytes)
        return {"http_status": resp.status, "provider_id": str(data.get("id"))[:40] if isinstance(data, dict) else None,
                "rate_limit_remaining": resp.headers.get("x-ratelimit-remaining")}


def get_provider(name: str, api_base: str) -> Provider:
    if name == "github":
        return GitHubProvider(api_base)
    if name == "local":
        return LocalProvider()
    raise CiInvalid("unsupported_provider", f"Provider '{name[:30]}' is not supported. Supported: {', '.join(PROVIDERS)}.")
