"""Quality-gate policy: strict schema, immutable versions, and a snapshot taken by every run.

Only a workspace ``owner`` may change a policy. Each change creates a new version; runs keep the version they started with, so a later
policy edit never rewrites the meaning of a past decision.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import CvPolicy, Repository
from app.security.auth import RequestContext
from app.services import audit
from app.services.verification.errors import CvForbidden, CvInvalid

POLICY_ADMIN_ROLES = {"owner"}

# Precedence used by the gate (documented in docs/architecture/CONTINUOUS_VERIFICATION.md): FAIL > BLOCKED > INCOMPLETE > PASS.
PRECEDENCE = ("FAIL", "BLOCKED", "INCOMPLETE", "PASS")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaticRules(_Strict):
    enforce: bool = True
    max_new_findings: int = Field(0, ge=0, le=1000)


class TestRules(_Strict):
    __test__ = False
    min_tests_executed: int = Field(1, ge=1, le=100000)
    allow_preexisting_failures: bool = False
    max_skipped: int | None = Field(None, ge=0)


class CoverageRules(_Strict):
    min_line_percent: float | None = Field(None, ge=0, le=100)
    max_drop_percent: float | None = Field(None, ge=0, le=100)


class BaselineRules(_Strict):
    required: bool = False


class PolicyRules(_Strict):
    default_mode: Literal["focused", "standard", "full"] = "focused"
    static: StaticRules = Field(default_factory=StaticRules)
    tests: TestRules = Field(default_factory=TestRules)
    coverage: CoverageRules = Field(default_factory=CoverageRules)
    baseline: BaselineRules = Field(default_factory=BaselineRules)
    require_unchanged_tree: bool = True  # a working tree that changed while verifying yields INCOMPLETE


def defaults() -> dict[str, Any]:
    return PolicyRules().model_dump()


def validate_rules(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CvInvalid("invalid_policy", "The policy must be an object.")
    try:
        return PolicyRules.model_validate(raw).model_dump()
    except ValidationError as exc:
        errs = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()][:10]
        raise CvInvalid("invalid_policy", "The policy is not valid.", errs) from None


def current(db: Session, repo_id: str) -> tuple[int, dict[str, Any], CvPolicy | None]:
    row = db.scalar(select(CvPolicy).where(CvPolicy.repository_id == repo_id).order_by(CvPolicy.version.desc()).limit(1))
    if row is None:
        return 0, defaults(), None
    return row.version, validate_rules(row.rules), row


def history(db: Session, repo_id: str, limit: int = 50) -> list[CvPolicy]:
    return list(db.scalars(select(CvPolicy).where(CvPolicy.repository_id == repo_id).order_by(CvPolicy.version.desc()).limit(limit)))


def update(db: Session, ctx: RequestContext, repo: Repository, raw: Any, note: str) -> CvPolicy:
    if ctx.role not in POLICY_ADMIN_ROLES:
        raise CvForbidden("policy_admin_required", "Only a workspace owner may change the quality-gate policy.")
    rules = validate_rules(raw)
    version = (db.scalar(select(func.max(CvPolicy.version)).where(CvPolicy.repository_id == repo.id)) or 0) + 1
    row = CvPolicy(repository_id=repo.id, version=version, rules=rules, note=(note or "")[:300], created_by=ctx.user.id, created_at=utcnow())
    db.add(row)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="verification.policy_updated", entity_type="cv_policy",
                 entity_id=repo.id, details={"repository_id": repo.id, "version": version})
    db.commit()
    return row
