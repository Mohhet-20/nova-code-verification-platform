"""Outbound HTTP for integrations, behind the SSRF guard.

Every request is validated at connection time (scheme, credentials in URL, metadata hosts, loopback/private/link-local/reserved addresses,
DNS resolution of ALL returned addresses) and then connects to the validated IP so a second lookup cannot rebind. Redirects are never
followed (a 3xx is reported as ``redirect_blocked``), responses are size-capped and every call has a timeout. An optional host allowlist
narrows destinations further. Limitation: this guards application-layer destinations only; network egress policy (firewall/proxy) is still
the deployment's responsibility.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.observability.notify import Blocked, Resolver, validate_destination


@dataclass
class HttpResult:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


class HttpError(Exception):
    """Transport-level failure with a safe category (never contains URL credentials or response bodies)."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class HttpClient(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, allowed_hosts: list[str] | None = None) -> HttpResult: ...


class SafeHttpClient:
    def __init__(self, *, allow_private: bool = False, timeout: float = 8.0, max_bytes: int = 262_144, resolver: Resolver | None = None) -> None:
        self.allow_private, self.timeout, self.max_bytes, self.resolver = allow_private, timeout, max_bytes, resolver

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, allowed_hosts: list[str] | None = None) -> HttpResult:
        try:
            host, port, ip, path = validate_destination(url, self.allow_private, self.resolver)
        except Blocked as exc:
            raise HttpError(f"blocked_{exc}") from None
        if allowed_hosts is not None and host.lower() not in {h.lower() for h in allowed_hosts}:
            raise HttpError("host_not_allowed")
        try:
            raw = socket.create_connection((ip, port), timeout=self.timeout)
            if url.startswith("https"):
                sock: Any = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=self.timeout)
            else:
                sock, conn = raw, http.client.HTTPConnection(host, port, timeout=self.timeout)
            conn.sock = sock
            try:
                conn.request(method, path, body=body, headers={**(headers or {}), "Host": host})
                resp = conn.getresponse()
                data = resp.read(self.max_bytes + 1)
                hdrs = {k.lower(): v for k, v in resp.getheaders() if k.lower() in ("content-type", "retry-after", "x-ratelimit-remaining")}
                if 300 <= resp.status < 400:
                    raise HttpError("redirect_blocked")
                return HttpResult(resp.status, data[: self.max_bytes], hdrs, len(data) > self.max_bytes)
            finally:
                conn.close()
        except HttpError:
            raise
        except ssl.SSLError:
            raise HttpError("tls_error") from None
        except TimeoutError:
            raise HttpError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise HttpError("connection_failed") from None
