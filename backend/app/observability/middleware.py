"""Pure-ASGI correlation/tracing/metrics middleware. Replaces the former access-log middleware."""
from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import context as ctx
from app.observability import metrics as m
from app.observability import tracing

log = logging.getLogger("nova.http")
_EXEMPT_SLO = {"/health", "/api/v1/health", "/ready", "/api/v1/ready", "/api/v1/ops/metrics"}


def _status_class(code: int) -> str:
    return f"{code // 100}xx"


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        req_id = ctx.valid_request_id(headers.get("x-request-id")) or ctx.new_request_id()
        remote = ctx.parse_traceparent(headers.get("traceparent"))
        method = scope.get("method", "GET")
        t0 = time.perf_counter()
        status = {"code": 500}
        scope["nova_request_id"] = req_id  # lets the outer 500 handler echo the id after this context is reset
        r1 = ctx.REQUEST_ID.set(req_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                raw = list(message.get("headers", []))
                raw.append((b"x-request-id", req_id.encode()))
                message["headers"] = raw
            await send(message)

        m.HTTP_IN_FLIGHT.set(m.HTTP_IN_FLIGHT.series.get((), 0) + 1)
        try:
            with tracing.span("http.request", remote_parent=remote, http_method=method) as sp:
                try:
                    await self.app(scope, receive, send_wrapper)
                except BaseException:
                    status["code"] = 500
                    raise
                finally:
                    route = self._template(scope)
                    sp.set("http.route", route)
                    sp.set("http.status_code", status["code"])
                    self._record(method, route, scope.get("path", ""), status["code"], time.perf_counter() - t0, req_id)
        finally:
            m.HTTP_IN_FLIGHT.set(max(0, m.HTTP_IN_FLIGHT.series.get((), 1) - 1))
            ctx.REQUEST_ID.reset(r1)

    @staticmethod
    def _template(scope: Scope) -> str:
        """Full route template (e.g. /api/v1/repositories/{repository_id}) so label cardinality is bounded by routes, never by ids."""
        if getattr(scope.get("route"), "path", None) is None:
            return "unmatched"
        path = scope.get("path", "")
        for name, val in (scope.get("path_params") or {}).items():
            path = path.replace(str(val), "{" + name + "}", 1)
        return path[:200]

    @staticmethod
    def _record(method: str, route: str, path: str, code: int, dur: float, req_id: str) -> None:
        try:
            sc = _status_class(code)
            m.HTTP_REQUESTS.inc(method=method, route=route, status_class=sc)
            m.HTTP_LATENCY.observe(dur, method=method, route=route)
            if path not in _EXEMPT_SLO:
                m.window_add("http", "total", 1)
                m.window_add("http", "error5xx" if code >= 500 else "ok", 1)
                m.record_latency("http_latency", dur)
            log.log(logging.ERROR if code >= 500 else logging.INFO, "http_request", extra={"event": "http_request", "method": method, "route": route, "path": path[:200], "status": code, "ms": round(dur * 1000, 2)})
        except Exception:  # noqa: BLE001, S110
            pass
