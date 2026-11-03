"""Errors of the security subsystem, each with a stable machine-readable ``code``."""
from __future__ import annotations

from typing import Any

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)


class SecConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class SecInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class SecNotFound(NotFoundError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class SecForbidden(ForbiddenError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class SecTooLarge(PayloadTooLargeError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code
