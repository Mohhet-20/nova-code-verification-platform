"""Errors of the operations subsystem, each with a stable machine-readable ``code``."""
from __future__ import annotations

from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError


class OpsNotFound(NotFoundError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class OpsConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class OpsInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code
