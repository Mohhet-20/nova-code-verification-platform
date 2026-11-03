"""API errors with stable, specific codes for the repair workflow."""
from __future__ import annotations

from typing import Any

from app.core.errors import ConflictError, ValidationFailedError
from app.services.repair.patch import PatchError


class RepairConflict(ConflictError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


class RepairInvalid(ValidationFailedError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message, details)
        self.code = code


CONFLICT_CODES = {"stale": "stale_source", "conflict": "patch_conflict"}


def from_patch_error(exc: PatchError) -> RepairConflict | RepairInvalid:
    if exc.code in CONFLICT_CODES:
        return RepairConflict(CONFLICT_CODES[exc.code], exc.message, exc.details or None)
    return RepairInvalid(exc.code, exc.message, exc.details or None)
