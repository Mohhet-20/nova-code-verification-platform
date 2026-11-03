"""Startup configuration contract. ``check`` returns findings (never secret values); ``enforce_startup`` refuses to boot a production process
that violates a mandatory rule or runs against a schema that is not at the migration head."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url

from app.core.config import Settings

ENVIRONMENTS = ("development", "test", "production")
PLACEHOLDERS = ("replace", "changeme", "change-me", "change_me", "example", "placeholder", "secret", "password", "token", "test", "demo", "your", "xxx", "todo", "default", "admin", "nova",
                "generate", "random", "<", ">")  # the last four catch template text such as "<generate: 48+ random characters>"
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class Finding:
    severity: str  # error | warning
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


class ConfigurationError(RuntimeError):
    """Raised at startup. The message names settings and rules, never values."""


def is_production(settings: Settings) -> bool:
    return settings.environment == "production"


def weak_secret(value: str) -> str | None:
    """Why a secret is unacceptable, or None. Never echoes the value."""
    v = value.strip()
    if len(v) < MIN_TOKEN_LENGTH:
        return f"shorter than {MIN_TOKEN_LENGTH} characters"
    low = v.lower()
    if any(p in low for p in PLACEHOLDERS):
        return "looks like a placeholder"
    if len(set(v)) < 8:
        return "has too little variety"
    return None


def check(settings: Settings) -> list[Finding]:
    f: list[Finding] = []
    prod = is_production(settings)
    sev = "error" if prod else "warning"
    if settings.environment not in ENVIRONMENTS:
        f.append(Finding("error", "environment_unknown", f"NOVA_ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)}."))
    if prod:
        if not settings.api_token:
            f.append(Finding("error", "auth_disabled", "NOVA_API_TOKEN is required in production; without it the API trusts every caller."))
        elif (why := weak_secret(settings.api_token)):
            f.append(Finding("error", "api_token_weak", f"NOVA_API_TOKEN is {why}."))
        if settings.integration_allow_private_destinations:
            f.append(Finding("error", "ssrf_guard_relaxed", "NOVA_INTEGRATION_ALLOW_PRIVATE_DESTINATIONS must be false in production."))
        if "*" in settings.cors_origins:
            f.append(Finding("error", "cors_wildcard", "NOVA_CORS_ORIGINS must not contain '*' in production."))
        if any("localhost" in o or "127.0.0.1" in o for o in settings.cors_origins):
            f.append(Finding("warning", "cors_local_origin", "NOVA_CORS_ORIGINS still lists a localhost origin."))
        if settings.ops_metrics_public:
            f.append(Finding("error", "metrics_public", "NOVA_OPS_METRICS_PUBLIC exposes metrics without authentication."))
        if not settings.log_json:
            f.append(Finding("warning", "log_not_json", "NOVA_LOG_JSON=false; structured logs are recommended in production."))
        if not settings.artifact_dir:
            f.append(Finding("warning", "artifact_dir_default", "NOVA_ARTIFACT_DIR is unset; artifacts default to a directory inside the code tree."))
        if not settings.registration_base_dirs:
            f.append(Finding("warning", "registration_unrestricted", "NOVA_REGISTRATION_BASE_DIRS is empty; any readable directory may be registered."))
        if settings.test_execution_enabled:
            f.append(Finding("warning", "test_execution_unsandboxed", "NOVA_TEST_EXECUTION_ENABLED=true: approved repository test code runs as the API's own user "
                             "(restricted subprocess, not a sandbox) and can read and write the database and artifacts. Register trusted repositories only, "
                             "or set it to false."))
        if settings.auth_failure_limit == 0:
            f.append(Finding("warning", "auth_throttle_disabled", "NOVA_AUTH_FAILURE_LIMIT=0 disables throttling of invalid bearer tokens."))
    if settings.integration_secret_key:
        if (why := weak_secret_key(settings.integration_secret_key)):
            f.append(Finding("error", "integration_key_invalid", f"NOVA_INTEGRATION_SECRET_KEY is {why}."))
    elif prod:
        f.append(Finding("warning", "integration_key_missing", "NOVA_INTEGRATION_SECRET_KEY is unset: stored integration credentials and webhook secrets are unavailable (env: references still work)."))
    provider = settings.ai_provider.lower()
    if provider not in ("", "none", "anthropic"):
        f.append(Finding("error", "ai_provider_unknown", "NOVA_AI_PROVIDER must be 'none' or 'anthropic'."))
    elif provider == "anthropic":
        f.append(Finding("warning", "ai_egress", "NOVA_AI_PROVIDER=anthropic: when a user requests an AI-assisted repair proposal, the investigation "
                         "and its relevant files (secret patterns scrubbed) are sent to Anthropic. Proposals are drafts that still need validation, "
                         "approval and test verification."))
    backend = make_url(settings.database_url).get_backend_name()
    if backend == "sqlite" and prod:
        f.append(Finding("warning", "sqlite_in_production", "SQLite is a single-node database: one writer, no replication; back it up with `python -m app.release backup`."))
    elif backend != "sqlite":
        f.append(Finding(sev if prod else "warning", "database_backend_unverified", f"The {backend} backend has not been verified against NOVA's migrations and queries; backup/restore tooling supports SQLite only."))
    return f


def weak_secret_key(value: str) -> str | None:
    import base64
    import binascii
    try:
        raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except (binascii.Error, ValueError):
        return "not valid urlsafe base64"
    return None if len(raw) == 32 else "not 32 bytes after decoding"


def enforce_startup(settings: Settings) -> None:
    """Raise ConfigurationError for a production process that must not start. Development/test only log (the caller does that)."""
    if not is_production(settings):
        if settings.require_current_schema:
            _require_head(settings)
        return
    errors = [x for x in check(settings) if x.severity == "error"]
    if errors:
        raise ConfigurationError("Invalid production configuration: " + "; ".join(f"[{e.code}] {e.message}" for e in errors))
    _require_head(settings)


def _require_head(settings: Settings) -> None:
    from app.release.dbtools import schema_status

    st = schema_status(settings.database_url)
    if not st["at_head"]:
        raise ConfigurationError(f"Database schema is {st['state']} (current={st['current']}, head={st['head']}). Run `python -m app.release migrate` as a release step before starting the API.")
