"""Errors raised by the continuous-verification subsystem, each with a stable machine-readable ``code``."""
from __future__ import annotations

from typing import Any

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError


class CvConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CvInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CvNotFound(NotFoundError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class CvForbidden(ForbiddenError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code
