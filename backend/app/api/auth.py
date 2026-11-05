"""Public-demo guest sessions.

``POST /auth/guest`` issues a per-user API token for a shared guest account with the ``engineer`` role in the local
workspace: it can explore, review and propose repairs, but not administer the organization. No password
or global token is involved. It answers 404 unless ``NOVA_GUEST_ACCESS_ENABLED`` is set, and is throttled per client
address separately from authentication failures. Pair it with ``NOVA_TEST_EXECUTION_ENABLED=false`` on any public
deployment, so no visitor can make the server run repository code.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, TooManyRequestsError
from app.db.base import utcnow
from app.db.session import get_db
from app.models import Membership, User, UserToken
from app.security.auth import USER_TOKEN_PREFIX, ensure_local_identity, hash_token

router = APIRouter(prefix="/auth", tags=["auth"])

GUEST_HANDLE = "guest"
GUEST_ROLE = "engineer"


@router.post("/guest")
def guest_session(request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)) -> dict:
    if not settings.guest_access_enabled:
        raise NotFoundError("Guest access is not available on this deployment.")
    throttle = request.app.state.guest_throttle
    client = request.client.host if request.client else "unknown"
    if wait := throttle.retry_after(client):
        raise TooManyRequestsError("Too many guest sessions from this address. Try again later.", {"retry_after_seconds": wait})
    throttle.record_failure(client)  # counts issuances; the throttle only limits how many one address can start

    _, workspace, _ = ensure_local_identity(db)
    user = db.scalar(select(User).where(User.handle == GUEST_HANDLE))
    if user is None:
        user = User(handle=GUEST_HANDLE, display_name="Guest engineer")
        db.add(user)
        db.flush()
    member = db.scalar(select(Membership).where(Membership.workspace_id == workspace.id, Membership.user_id == user.id))
    if member is None:
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=GUEST_ROLE, created_at=utcnow(), updated_at=utcnow()))
    elif member.role != GUEST_ROLE or member.status != "active":
        member.role, member.status = GUEST_ROLE, "active"

    plain = USER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.add(UserToken(workspace_id=workspace.id, user_id=user.id, token_hash=hash_token(plain), label="guest session",
                     created_by=user.id))
    db.commit()
    return {"token": plain, "role": GUEST_ROLE, "handle": GUEST_HANDLE}
