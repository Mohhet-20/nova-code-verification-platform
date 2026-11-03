"""Domain errors and the consistent JSON error envelope."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("nova.errors")


class NovaError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(NovaError):
    status_code = 404
    code = "not_found"


class ConflictError(NovaError):
    status_code = 409
    code = "conflict"


class ForbiddenError(NovaError):
    status_code = 403
    code = "forbidden"


class UnauthorizedError(NovaError):
    status_code = 401
    code = "unauthorized"


class TooManyRequestsError(NovaError):
    status_code = 429
    code = "too_many_requests"


class ValidationFailedError(NovaError):
    status_code = 422
    code = "validation_error"


class PayloadTooLargeError(NovaError):
    status_code = 413
    code = "payload_too_large"


class UnsafePathError(NovaError):
    status_code = 400
    code = "unsafe_path"


class InvalidRepositoryError(NovaError):
    status_code = 422
    code = "invalid_repository"


def error_body(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NovaError)
    async def _nova(_: Request, exc: NovaError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        if exc.status_code == 429 and isinstance(exc.details, dict) and "retry_after_seconds" in exc.details:
            headers = {"Retry-After": str(exc.details["retry_after_seconds"])}
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details), status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field locations and messages are returned; submitted values are not echoed back.
        details = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body("validation_error", "Request validation failed.", details), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(OperationalError)
    @app.exception_handler(DatabaseError)
    async def _database(request: Request, exc: Exception) -> JSONResponse:
        # Missing/locked/corrupt storage is a temporary-unavailability condition, not an application bug: 503 lets clients and deploy automation
        # distinguish it. Constraint violations (IntegrityError) are not matched here and keep their own handling.
        if isinstance(exc, IntegrityError):
            return await _unhandled(request, exc)
        logger.error("database_unavailable", extra={"event": "database_unavailable", "path": request.url.path, "error_type": type(exc).__name__})
        headers = {"Retry-After": "5"}
        if rid := request.scope.get("nova_request_id"):
            headers["X-Request-ID"] = rid
        return JSONResponse(error_body("database_unavailable", "The database is unavailable or not ready. Try again shortly."), status_code=503, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the exception type and route only; never request bodies or file contents.
        logger.error(
            "unhandled_error", exc_info=exc,
            extra={"event": "unhandled_error", "path": request.url.path, "error_type": type(exc).__name__},
        )
        headers = {"X-Request-ID": rid} if (rid := request.scope.get("nova_request_id")) else None
        return JSONResponse(
            error_body("internal_error", "An internal error occurred."), status_code=500, headers=headers
        )
