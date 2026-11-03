"""Baseline security headers for every /api response (pure ASGI). A header an endpoint already set (e.g. a report's own CSP) is kept."""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    # JSON from this API is never meant to be rendered as a document; reports that are downloaded set their own, stricter policy.
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    # Responses carry repository content and audit data: never store them in shared or browser caches.
    (b"cache-control", b"no-store"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):  # /docs (development only) needs scripts and styles
            return await self.app(scope, receive, send)

        async def send_with_headers(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in API_HEADERS if k not in present]
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, send_with_headers)
