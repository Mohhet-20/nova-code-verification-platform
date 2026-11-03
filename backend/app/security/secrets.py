"""Names of files that commonly hold credentials. They are never indexed (not even their names are stored)."""
from __future__ import annotations

import fnmatch

_ALLOWED_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}

_SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
    ".git-credentials", ".htpasswd", "credentials", "credentials.*", "*.credentials",
    "secrets.*", "secret.*", "*.secret", "*.secrets", "service-account*.json",
    "*.tfstate", "*.tfstate.*", "*.tfvars", "*.tfvars.json", ".dockercfg", ".aws",
)


def is_secret_filename(name: str) -> bool:
    lowered = name.lower()
    if lowered in _ALLOWED_ENV_TEMPLATES:
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_PATTERNS)
