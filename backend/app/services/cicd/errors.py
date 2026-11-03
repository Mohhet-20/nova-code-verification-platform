"""Errors of the CI/CD subsystem, each with a stable machine-readable ``code``."""
from __future__ import annotations

from typing import Any

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    UnauthorizedError,
    ValidationFailedError,
)


class _Coded:
    code: str = "error"


class CiConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CiInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CiNotFound(NotFoundError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CiForbidden(ForbiddenError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CiUnauthorized(UnauthorizedError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CiTooLarge(PayloadTooLargeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderError(Exception):
    """A failure talking to an external provider. ``retryable`` is a classification, never a promise that retrying will help."""

    def __init__(self, error_class: str, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.retryable = retryable
        self.status = status
