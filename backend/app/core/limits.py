"""Request-size limit (pure ASGI, so the body is never buffered beyond the limit). Applies to every route; webhook endpoints keep their own, stricter limits."""
from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._reject(send)
        seen, exceeded = 0, False

        async def counted() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.request", "body": b"", "more_body": False}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:  # stop reading; whatever the app answers is replaced by 413 below
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def guarded_send(msg: Message) -> None:
            if exceeded:
                if msg["type"] == "http.response.start":
                    await self._reject(send)
                return  # swallow the application's own (parse-error) response
            await send(msg)

        await self.app(scope, counted, guarded_send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps({"error": {"code": "payload_too_large", "message": f"Request body exceeds {self.max_bytes} bytes."}}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
