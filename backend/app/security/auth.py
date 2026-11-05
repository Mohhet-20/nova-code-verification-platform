"""Authentication and role checks.

Security boundary (documented in docs/architecture/SECURITY_MODEL.md): NOVA is a
single-user, local-first application. There is one local identity. An optional bearer token
protects the API when it is bound beyond localhost. Roles exist in the data model and are
enforced on write endpoints, but multi-user isolation is NOT implemented.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, TooManyRequestsError, UnauthorizedError
from app.db.base import utcnow
from app.db.session import get_db
from app.models import Membership, User, Workspace

LOCAL_WORKSPACE_SLUG = "local"
LOCAL_USER_HANDLE = "local-dev"
WRITE_ROLES = {"owner", "maintainer", "engineer"}


@dataclass
class RequestContext:
    user: User
    workspace: Workspace
    role: str
    auth_kind: str = "local"  # local (global token / local-trust identity) | user_token

    def require_write(self) -> None:
        if self.role not in WRITE_ROLES:
            raise ForbiddenError("Your role does not permit this action.")


def ensure_local_identity(db: Session) -> tuple[User, Workspace, Membership]:
    workspace = db.scalar(select(Workspace).where(Workspace.slug == LOCAL_WORKSPACE_SLUG))
    user = db.scalar(select(User).where(User.handle == LOCAL_USER_HANDLE))
    if workspace is None:
        workspace = Workspace(name="Local Workspace", slug=LOCAL_WORKSPACE_SLUG)
        db.add(workspace)
    if user is None:
        user = User(handle=LOCAL_USER_HANDLE, display_name="Local Developer")
        db.add(user)
    db.flush()
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace.id, Membership.user_id == user.id
        )
    )
    if membership is None:
        membership = Membership(workspace_id=workspace.id, user_id=user.id, role="owner",
                                created_at=utcnow(), updated_at=utcnow())
        db.add(membership)
    db.commit()
    return user, workspace, membership


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    scheme, _, supplied = (authorization or "").partition(" ")
    return supplied if scheme.lower() == "bearer" and supplied else None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _refuse_if_throttled(request: Request) -> None:
    throttle = getattr(request.app.state, "auth_throttle", None)
    if throttle is not None and (wait := throttle.retry_after(_client_key(request))):
        raise TooManyRequestsError("Too many failed authentication attempts. Try again later.", {"retry_after_seconds": wait})


def _record_auth_failure(request: Request) -> None:
    throttle = getattr(request.app.state, "auth_throttle", None)
    if throttle is not None:
        throttle.record_failure(_client_key(request))


def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Legacy global-token gate. A per-user token (``nova_u_...``) is validated later by ``get_context``."""
    supplied = _bearer(authorization)
    if supplied is not None:
        _refuse_if_throttled(request)
    if supplied and supplied.startswith(USER_TOKEN_PREFIX):
        return
    if not settings.api_token:
        return
    if supplied is None:
        raise UnauthorizedError("A valid bearer token is required.")
    if not hmac.compare_digest(supplied.encode(), settings.api_token.encode()):
        _record_auth_failure(request)
        raise UnauthorizedError("A valid bearer token is required.")


USER_TOKEN_PREFIX = "nova_u_"  # noqa: S105 - a public format prefix, not a secret


def get_context(request: Request, authorization: str | None = Header(default=None), _: None = Depends(require_token),
                db: Session = Depends(get_db)) -> RequestContext:
    supplied = _bearer(authorization)
    if supplied and supplied.startswith(USER_TOKEN_PREFIX):
        from app.models import UserToken

        row = db.scalar(select(UserToken).where(UserToken.token_hash == hash_token(supplied), UserToken.revoked_at.is_(None)))
        member = None
        if row is not None:
            member = db.scalar(select(Membership).where(Membership.user_id == row.user_id, Membership.workspace_id == row.workspace_id,
                                                        Membership.status == "active"))
        if row is None or member is None:
            _record_auth_failure(request)
            raise UnauthorizedError("A valid bearer token is required.")  # same answer for unknown, revoked and removed
        user, workspace = db.get(User, row.user_id), db.get(Workspace, row.workspace_id)
        assert user is not None and workspace is not None
        return RequestContext(user=user, workspace=workspace, role=member.role, auth_kind="user_token")
    user, workspace, membership = ensure_local_identity(db)
    return RequestContext(user=user, workspace=workspace, role=membership.role)
