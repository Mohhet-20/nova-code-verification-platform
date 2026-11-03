"""Extension ecosystem (trusted built-ins only).

An extension is a *declared* capability bundle backed by compiled-in code that ships with NOVA. This module never imports, evals, or
executes anything named by a database row, upload, path or request parameter. ``validate_manifest`` is a pure schema check (a dry run)
so a future manifest can be reviewed; registering a non-built-in extension is rejected. Loading third-party code is explicitly DEFERRED
(it needs sandboxing, signing and review that are out of scope). Extensions cannot write audit events, approvals or policy results:
they expose adapter operations only, and those return data to NOVA services, which alone decide.
"""
from __future__ import annotations

import re
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import APP_VERSION
from app.core.errors import NotFoundError, ValidationFailedError
from app.models import ExtensionActivation
from app.services.governance.common import emit
from app.services.integrations.registry import ADAPTERS, CAPABILITIES

ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
CATEGORIES = ("version_control", "issue_tracker", "notification", "testing")
PERMISSIONS = ("read:events", "read:evidence", "write:external_status", "write:external_issue", "send:webhook")


class Manifest(BaseModel):
    """Strict manifest. There is deliberately NO field for a module path, entry point, URL or command."""
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(min_length=1, max_length=80)
    version: str
    category: str
    nova_compatibility: str = Field(max_length=60)
    capabilities: list[str] = Field(max_length=20)
    permissions: list[str] = Field(default_factory=list, max_length=10)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    provenance: str = Field(max_length=40)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("invalid id")
        return v

    @field_validator("version")
    @classmethod
    def _ver(cls, v: str) -> str:
        try:
            Version(v)
        except InvalidVersion:
            raise ValueError("invalid version") from None
        return v

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError("unknown category")
        return v

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in CAPABILITIES]
        if bad:
            raise ValueError("unknown capability")
        return v

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        if any(p not in PERMISSIONS for p in v):
            raise ValueError("unknown permission")
        return v

    @field_validator("nova_compatibility")
    @classmethod
    def _compat(cls, v: str) -> str:
        try:
            SpecifierSet(v)
        except InvalidSpecifier:
            raise ValueError("invalid compatibility range") from None
        return v


def _builtin_manifests() -> dict[str, Manifest]:
    perm_map = {"statuses:write": "write:external_status", "issues:write": "write:external_issue"}
    out: dict[str, Manifest] = {}
    for a in ADAPTERS.values():
        perms = sorted({perm_map.get(p, "read:events" if a.type_id != "github-issues" else "read:events") for p in a.permissions})
        out[a.type_id] = Manifest(id=a.type_id, name=a.title, version=APP_VERSION, category=a.category, nova_compatibility=f">={APP_VERSION}", capabilities=list(a.capabilities),
                                  permissions=perms, config_schema=a.config_model.model_json_schema(), provenance="built-in")
    out["webhook-notifier"] = Manifest(id="webhook-notifier", name="Signed outbound webhooks", version=APP_VERSION, category="notification", nova_compatibility=f">={APP_VERSION}",
                                       capabilities=["outbound_action"], permissions=["send:webhook"], provenance="built-in")
    return out


BUILTIN = _builtin_manifests()


def compatible(m: Manifest) -> bool:
    return Version(APP_VERSION) in SpecifierSet(m.nova_compatibility, prereleases=True)


def validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """Dry-run validation: pure data checks, nothing is imported or executed."""
    try:
        m = Manifest.model_validate(raw)
    except ValidationError as exc:
        return {"valid": False, "problems": [f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()][:15], "installable": False}
    return {"valid": True, "problems": [], "compatible": compatible(m), "installable": False,
            "note": "Valid manifest. Only built-in extensions can be activated; loading external code is not supported in this release."}


def get_builtin(ext_id: str) -> Manifest:
    m = BUILTIN.get(ext_id)
    if m is None:
        raise NotFoundError("Extension is not in the trusted built-in registry.")
    return m


def list_extensions(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    acts = {a.extension_id: a for a in db.scalars(select(ExtensionActivation).where(ExtensionActivation.workspace_id == workspace_id))}
    return [{**m.model_dump(), "compatible": compatible(m), "status": "enabled" if acts.get(i) and acts[i].enabled else "available", "trusted": True} for i, m in sorted(BUILTIN.items())]


def set_enabled(db: Session, workspace_id: str, actor: str, ext_id: str, enabled: bool) -> dict[str, Any]:
    m = get_builtin(ext_id)
    if enabled and not compatible(m):
        raise ValidationFailedError("The extension is not compatible with this NOVA version.")
    act = db.scalar(select(ExtensionActivation).where(ExtensionActivation.workspace_id == workspace_id, ExtensionActivation.extension_id == ext_id))
    if act is None:
        act = ExtensionActivation(workspace_id=workspace_id, extension_id=ext_id)
        db.add(act)
    act.enabled, act.activated_by = enabled, actor
    db.flush()
    emit(db, workspace_id=workspace_id, actor=actor, action="extension.enabled" if enabled else "extension.disabled", entity_type="extension", entity_id=ext_id[:32])
    return {"id": ext_id, "status": "enabled" if enabled else "available"}


def is_enabled(db: Session, workspace_id: str, ext_id: str) -> bool:
    a = db.scalar(select(ExtensionActivation).where(ExtensionActivation.workspace_id == workspace_id, ExtensionActivation.extension_id == ext_id))
    return bool(a and a.enabled)
