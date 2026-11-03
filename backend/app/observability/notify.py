"""Alert notifications: signed generic webhook and Slack-compatible webhook. Opt-in, SSRF-guarded, bounded retries, recorded attempts.

Email is NOT implemented (reported as such). Secrets are read from environment variables named in settings and never stored or logged.
Delivery failures are recorded and never propagate.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import OpsDelivery, OpsIncident
from app.observability import metrics as m
from app.observability.redact import clean_str

log = logging.getLogger("nova.notify")
BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class Blocked(Exception):  # noqa: N818
    """Destination refused by the SSRF guard."""


class Transport(Protocol):
    def post(self, url: str, host: str, port: int, ip: str, path: str, body: bytes, headers: dict[str, str], timeout: float) -> int: ...


Resolver = Callable[[str], list[str]]


def default_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def _bad_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified or str(a) in ("169.254.169.254", "100.100.100.200")


def validate_destination(url: str, allow_private: bool, resolver: Resolver | None = None) -> tuple[str, int, str, str]:
    """Return (host, port, ip, path). Raises Blocked for non-https (unless private allowed), credentials in URL, or any blocked address."""
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise Blocked("invalid_url")
    if p.scheme == "http" and not allow_private:
        raise Blocked("https_required")
    if p.username or p.password:
        raise Blocked("credentials_in_url")
    host = p.hostname
    if host.lower() in BLOCKED_HOSTS and not allow_private:
        raise Blocked("metadata_host")
    try:
        ips = [host] if _is_ip(host) else (resolver or default_resolver)(host)
    except OSError as exc:
        raise Blocked("unresolvable") from exc
    if not ips:
        raise Blocked("unresolvable")
    if not allow_private and any(_bad_ip(i) for i in ips):
        raise Blocked("blocked_address")
    port = p.port or (443 if p.scheme == "https" else 80)
    return host, port, ips[0], (p.path or "/") + (f"?{p.query}" if p.query else "")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HttpTransport:
    """Connects to the already-validated IP (no second DNS lookup, so no rebinding) while keeping the hostname for TLS/SNI and Host."""

    def post(self, url, host, port, ip, path, body, headers, timeout):  # type: ignore[no-untyped-def]
        raw = socket.create_connection((ip, port), timeout=timeout)
        if url.startswith("https"):
            sock: Any = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            sock, conn = raw, http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        try:
            conn.request("POST", path, body=body, headers={**headers, "Host": host})
            return conn.getresponse().status
        finally:
            conn.close()


def _secret(env_name: str | None) -> str | None:
    return os.environ.get(env_name) if env_name else None


def status(settings: Settings) -> dict[str, Any]:
    webhook = bool(settings.ops_webhook_url)
    slack = bool(_secret(settings.ops_slack_webhook_env))
    return {"enabled": webhook or slack, "channels": [
        {"channel": "webhook", "enabled": webhook, "signed": bool(_secret(settings.ops_webhook_secret_env)) if webhook else None},
        {"channel": "slack", "enabled": slack},
        {"channel": "email", "enabled": False, "implemented": False, "note": "Email delivery is not implemented."},
    ], "private_destinations_allowed": settings.ops_notify_allow_private}


def payload_for(inc: OpsIncident) -> dict[str, Any]:
    return {"incident_id": inc.id, "rule_id": inc.rule_id, "title": inc.title, "severity": inc.severity, "component": inc.component, "status": inc.status,
            "summary": clean_str(inc.summary, 300), "occurrences": inc.occurrences, "first_seen_at": inc.first_seen_at.isoformat(), "last_seen_at": inc.last_seen_at.isoformat()}


def _deliver(db: Session, settings: Settings, inc: OpsIncident, channel: str, url: str, secret: str | None, transport: Transport, resolver: Resolver | None, sleep: Callable[[float], None]) -> dict[str, Any]:
    ikey = f"{channel}:{hashlib.sha256(f'{inc.id}:{inc.status}:{inc.occurrences}'.encode()).hexdigest()[:40]}"
    done = db.scalar(select(OpsDelivery).where(OpsDelivery.workspace_id == inc.workspace_id, OpsDelivery.idempotency_key == ikey, OpsDelivery.status == "delivered"))
    host_label = (urlsplit(url).hostname or "invalid")[:120]
    if done is not None:
        return {"channel": channel, "status": "skipped", "reason": "already_delivered", "idempotency_key": ikey}
    data = payload_for(inc)
    body = json.dumps(data if channel == "webhook" else {"text": f"[{inc.severity.upper()}] {inc.title} — {data['summary']} (incident {inc.id})"}, sort_keys=True).encode()
    attempts = 0
    last: dict[str, Any] = {}
    for n in range(1, settings.ops_notify_max_attempts + 1):
        attempts = n
        rec = OpsDelivery(workspace_id=inc.workspace_id, incident_id=inc.id, channel=channel, destination=host_label, idempotency_key=ikey, attempt=n, status="failed")
        try:
            host, port, ip, path = validate_destination(url, settings.ops_notify_allow_private, resolver)
            headers = {"Content-Type": "application/json", "X-Nova-Idempotency-Key": ikey}
            if channel == "webhook" and secret:
                ts = str(int(time.time()))
                headers["X-Nova-Timestamp"] = ts
                headers["X-Nova-Signature"] = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
            code = transport.post(url, host, port, ip, path, body, headers, settings.ops_notify_timeout_seconds)
            rec.http_status = code
            if 200 <= code < 300:
                rec.status = "delivered"
            elif 400 <= code < 500 and code != 429:
                rec.error_class = "client_error"
                n = settings.ops_notify_max_attempts  # permanent: do not retry
            else:
                rec.error_class = "server_error"
        except Blocked as exc:
            rec.status, rec.error_class = "blocked", str(exc)[:60]
            n = settings.ops_notify_max_attempts
        except Exception as exc:  # noqa: BLE001
            rec.error_class = type(exc).__name__[:60]
        db.add(rec)
        db.flush()
        m.NOTIFICATIONS.inc(channel=channel, outcome=rec.status)
        last = {"channel": channel, "status": rec.status, "attempts": attempts, "http_status": rec.http_status, "error_class": rec.error_class, "idempotency_key": ikey}
        if rec.status in ("delivered", "blocked") or n >= settings.ops_notify_max_attempts:
            break
        sleep(min(0.25 * 2 ** (n - 1), 4.0))
    return last


def dispatch(db: Session, settings: Settings, inc: OpsIncident, *, transport: Transport | None = None, resolver: Resolver | None = None, sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Send to every configured channel. Never raises. Returns one result per channel (empty list = notifications disabled)."""
    out: list[dict[str, Any]] = []
    tp = transport or HttpTransport()
    jobs = []
    if settings.ops_webhook_url:
        jobs.append(("webhook", settings.ops_webhook_url, _secret(settings.ops_webhook_secret_env)))
    if (slack := _secret(settings.ops_slack_webhook_env)):
        jobs.append(("slack", slack, None))
    for channel, url, secret in jobs:
        try:
            out.append(_deliver(db, settings, inc, channel, url, secret, tp, resolver, sleep))
        except Exception as exc:  # noqa: BLE001
            log.warning("notify_failed", extra={"event": "notify_failed", "channel": channel, "error_class": type(exc).__name__})
            out.append({"channel": channel, "status": "failed", "error_class": type(exc).__name__})
    return out
