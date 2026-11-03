"""Errors raised by the refactoring subsystem. Each has a stable machine-readable ``code``."""
from __future__ import annotations

from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.services.repair.patch import PatchError

CONFLICT_CODES = {"stale": "stale_source", "conflict": "edit_conflict"}


class RefactorConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class RefactorInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class RefactorNotFound(NotFoundError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


def from_patch_error(exc: PatchError) -> RefactorConflict | RefactorInvalid:
    if exc.code in CONFLICT_CODES:
        return RefactorConflict(CONFLICT_CODES[exc.code], exc.message, exc.details or None)
    return RefactorInvalid(exc.code, exc.message, exc.details or None)
