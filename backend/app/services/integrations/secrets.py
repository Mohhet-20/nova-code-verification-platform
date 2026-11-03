"""Credential protection.

* Stored credentials are AES-256-GCM envelopes bound (as associated data) to organization, owner type and owner id, so a ciphertext copied to
  another row or organization does not decrypt.
* The key comes only from ``NOVA_INTEGRATION_SECRET_KEY``. There is no default, no derived fallback and no key in the database. Without a valid
  key, storing or reading a credential raises ``SecretsUnavailable`` (fail closed); callers translate that to HTTP 503.
* Rotation: put the new key in ``NOVA_INTEGRATION_SECRET_KEY`` and the old one(s) in ``NOVA_INTEGRATION_SECRET_KEYS_PREVIOUS``; run
  ``reencrypt_all``; once it reports zero remaining old-key rows, drop the previous key.
* ``env:NOVA_CRED_*`` references resolve from the process environment (the documented development option). Only that prefix is allowed so a
  configuration cannot point an integration at unrelated environment variables.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets as _secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import IntegrationSecret

ENV_REF = re.compile(r"^env:(NOVA_CRED_[A-Z0-9_]{1,50})$")
SECRET_REF = re.compile(r"^secret:([0-9a-f]{32})$")
MAX_SECRET_LEN = 4096
_PREFIX = "v1."


class SecretsUnavailable(Exception):  # noqa: N818
    """Encryption is not configured correctly. Never carries secret material."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SecretMissing(Exception):  # noqa: N818
    """The referenced credential does not exist, was revoked, or cannot be decrypted."""


@dataclass(frozen=True)
class _Key:
    key_id: str
    raw: bytes


def _parse(value: str) -> _Key:
    try:
        raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except (binascii.Error, ValueError):
        raise SecretsUnavailable("The integration secret key is not valid base64.") from None
    if len(raw) != 32:
        raise SecretsUnavailable("The integration secret key must decode to exactly 32 bytes.")
    return _Key(hashlib.sha256(b"nova-key-id" + raw).hexdigest()[:12], raw)


def keyring(settings: Settings) -> tuple[_Key, dict[str, _Key]]:
    """Return (active key, all keys by id). Raises SecretsUnavailable when no valid active key is configured."""
    if not settings.integration_secret_key.strip():
        raise SecretsUnavailable("No integration secret key is configured (NOVA_INTEGRATION_SECRET_KEY). Stored credentials are unavailable; use env:NOVA_CRED_* references or configure a key.")
    active = _parse(settings.integration_secret_key)
    keys = {active.key_id: active}
    for old in settings.integration_secret_keys_previous:
        k = _parse(old)
        keys.setdefault(k.key_id, k)
    return active, keys


def encryption_status(settings: Settings) -> dict[str, object]:
    """Non-secret status for diagnostics."""
    try:
        active, keys = keyring(settings)
        return {"configured": True, "active_key_id": active.key_id, "previous_keys": len(keys) - 1}
    except SecretsUnavailable as exc:
        return {"configured": False, "reason": exc.reason}


def _aad(workspace_id: str, owner_type: str, owner_id: str) -> bytes:
    return f"{workspace_id}|{owner_type}|{owner_id}".encode()


def encrypt(settings: Settings, plaintext: str, workspace_id: str, owner_type: str, owner_id: str) -> tuple[str, str]:
    if not plaintext or len(plaintext) > MAX_SECRET_LEN or any(ord(c) < 32 for c in plaintext):
        raise ValueError("invalid_secret")
    active, _ = keyring(settings)
    nonce = os.urandom(12)
    ct = AESGCM(active.raw).encrypt(nonce, plaintext.encode(), _aad(workspace_id, owner_type, owner_id))
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode(), active.key_id


def decrypt(settings: Settings, row: IntegrationSecret) -> str:
    _, keys = keyring(settings)
    key = keys.get(row.key_id)
    if key is None or not row.ciphertext.startswith(_PREFIX):
        raise SecretMissing("The credential was encrypted with a key that is no longer configured.")
    try:
        blob = base64.urlsafe_b64decode(row.ciphertext[len(_PREFIX):])
        return AESGCM(key.raw).decrypt(blob[:12], blob[12:], _aad(row.workspace_id, row.owner_type, row.owner_id)).decode()
    except (InvalidTag, ValueError, binascii.Error):
        raise SecretMissing("The credential could not be decrypted.") from None


def store(db: Session, settings: Settings, *, workspace_id: str, owner_type: str, owner_id: str, plaintext: str, actor: str | None) -> IntegrationSecret:
    """Create a new credential row and revoke the previous active one for the same owner (history is kept; plaintext never is)."""
    ciphertext, key_id = encrypt(settings, plaintext, workspace_id, owner_type, owner_id)
    prev = list(db.scalars(select(IntegrationSecret).where(IntegrationSecret.owner_type == owner_type, IntegrationSecret.owner_id == owner_id, IntegrationSecret.revoked_at.is_(None))))
    version = max([p.version for p in prev] or [0]) + 1
    for p in prev:
        p.revoked_at = utcnow()
    row = IntegrationSecret(workspace_id=workspace_id, owner_type=owner_type, owner_id=owner_id, ciphertext=ciphertext, key_id=key_id, version=version, created_by=actor)
    db.add(row)
    db.flush()
    return row


def revoke_owner(db: Session, owner_type: str, owner_id: str) -> int:
    n = 0
    for p in db.scalars(select(IntegrationSecret).where(IntegrationSecret.owner_type == owner_type, IntegrationSecret.owner_id == owner_id, IntegrationSecret.revoked_at.is_(None))):
        p.revoked_at = utcnow()
        n += 1
    return n


def resolve(db: Session, settings: Settings, workspace_id: str, ref: str | None) -> str:
    """Resolve a credential reference to its plaintext at the execution boundary. Raises SecretMissing / SecretsUnavailable."""
    if not ref:
        raise SecretMissing("No credential is configured.")
    m = ENV_REF.match(ref)
    if m:
        value = os.environ.get(m.group(1), "")
        if not value:
            raise SecretMissing("The referenced environment credential is not set.")
        return value
    m = SECRET_REF.match(ref)
    if m:
        row = db.get(IntegrationSecret, m.group(1))
        if row is None or row.workspace_id != workspace_id or row.revoked_at is not None:
            raise SecretMissing("The credential does not exist or was revoked.")
        return decrypt(settings, row)
    raise SecretMissing("Unsupported credential reference.")


def valid_ref(ref: str) -> bool:
    return bool(ENV_REF.match(ref) or SECRET_REF.match(ref))


def reencrypt_all(db: Session, settings: Settings) -> dict[str, int]:
    """Re-encrypt every active credential with the active key. Idempotent; reports how many rows still use another key (should be 0)."""
    active, _ = keyring(settings)
    done = remaining = 0
    for row in db.scalars(select(IntegrationSecret).where(IntegrationSecret.revoked_at.is_(None))):
        if row.key_id == active.key_id:
            continue
        try:
            plaintext = decrypt(settings, row)
        except SecretMissing:
            remaining += 1
            continue
        row.ciphertext, row.key_id = encrypt(settings, plaintext, row.workspace_id, row.owner_type, row.owner_id)
        done += 1
    db.flush()
    return {"reencrypted": done, "undecryptable": remaining}


def new_webhook_secret() -> str:
    return "whsec_" + base64.urlsafe_b64encode(_secrets.token_bytes(32)).decode().rstrip("=")


if __name__ == "__main__":  # python -m app.services.integrations.secrets generate-key
    import sys

    if sys.argv[1:] == ["generate-key"]:
        print(base64.urlsafe_b64encode(os.urandom(32)).decode())
    else:
        print("usage: python -m app.services.integrations.secrets generate-key")
