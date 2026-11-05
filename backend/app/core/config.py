"""Application settings, read from environment variables prefixed with ``NOVA_``."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

APP_VERSION = "0.19.0"
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOVA_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'data' / 'nova.db').as_posix()}"
    log_level: str = "INFO"
    log_json: bool = True

    # Optional shared secret. When set, every /api/v1 route except health requires
    # ``Authorization: Bearer <token>``. When empty, the API trusts the local machine.
    api_token: str = ""

    # Comma separated origins allowed by CORS (the Vite dev server by default).
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Optional comma separated allow-list of directories under which repositories may be
    # registered. Empty means any existing directory on this machine may be registered.
    registration_base_dirs: Annotated[list[str], NoDecode] = []

    # Indexing limits
    index_max_concurrent_runs: int = Field(4, ge=1)  # admission limit across all repositories (per-repo is already exclusive)
    index_max_files: int = Field(20_000, ge=1)
    index_max_depth: int = Field(12, ge=1)
    index_max_file_bytes: int = Field(1_000_000, ge=1)
    index_max_total_bytes: int = Field(200_000_000, ge=1)

    # Workload limits added
    index_time_budget_seconds: float = Field(300.0, gt=0)
    index_max_symbols_per_file: int = Field(5_000, ge=1)
    index_max_diagnostics: int = Field(500, ge=0)
    # Extra gitignore-style exclusion patterns, configured ONLY here (never read from repository files).
    index_extra_excludes: Annotated[list[str], NoDecode] = []

    # Static analysis limits. Repository configuration can never raise these.
    analysis_max_files: int = Field(5_000, ge=1)
    analysis_max_file_bytes: int = Field(500_000, ge=1024)
    analysis_time_budget_seconds: float = Field(120.0, gt=0)
    analysis_max_findings_per_file: int = Field(200, ge=1)
    analysis_max_concurrent_runs: int = Field(2, ge=1)

    # Test engineering
    artifact_dir: str = ""  # empty = <backend>/data/artifacts. Generated tests and run artifacts live here, never in repositories.
    test_python: str = ""  # interpreter used to run tests; empty = the interpreter running NOVA
    test_max_candidate_bytes: int = Field(60_000, ge=1000)
    test_max_candidates_per_run: int = Field(40, ge=1)
    test_max_files_discovered: int = Field(5_000, ge=1)
    test_max_file_bytes: int = Field(500_000, ge=1024)
    test_timeout_seconds: float = Field(120.0, gt=0)
    test_max_output_bytes: int = Field(100_000, ge=1024)
    test_memory_limit_mb: int = Field(1024, ge=64)  # RLIMIT_AS on POSIX; not enforced on Windows
    test_max_concurrent_runs: int = Field(1, ge=1)
    # Kill switch for every feature that runs repository code (approved test runs, debugging/repair/verification reruns). The runner is a
    # restricted subprocess under the API's own user, NOT a sandbox: disable it where registered repositories are not trusted.
    test_execution_enabled: bool = True
    # Public demo: POST /auth/guest issues an engineer token for a shared guest account. Off by default; pair with
    # test_execution_enabled=false so visitors cannot make the server run repository code.
    guest_access_enabled: bool = False
    # Debugging
    debug_max_report_bytes: int = 65_536
    debug_max_frames: int = 60
    debug_max_candidates: int = 10
    debug_impact_depth: int = 2
    debug_weights_json: str = ""  # optional JSON object overriding individual localization weights

    # Code review
    review_max_files: int = Field(200, ge=1)  # changed files analysed per review; the rest are counted and reported
    review_max_diff_bytes: int = Field(2_000_000, ge=1024)  # total patch text read from git per review
    review_max_file_bytes: int = Field(500_000, ge=1024)  # largest file read for static analysis / symbol mapping
    review_time_budget_seconds: float = Field(90.0, gt=0)
    review_impact_depth: int = Field(2, ge=1, le=4)
    review_max_findings: int = Field(100, ge=1)
    review_max_findings_per_file: int = Field(10, ge=1)
    review_min_confidence: str = "medium"  # heuristic findings below this are counted, not shown (critical/high are never dropped)
    review_max_recommendations: int = Field(30, ge=1)
    # Repair and patch workflow
    repair_max_files: int = Field(10, ge=1, le=50)  # files one patch may touch
    repair_max_patch_bytes: int = Field(200_000, ge=1024)  # size of one diff
    repair_max_file_bytes: int = Field(500_000, ge=1024)  # largest file a patch may modify
    repair_output_chars: int = Field(4000, ge=200)  # verification output returned by the API
    repair_min_root_cause_score: float = Field(2.0, gt=0)  # below this the root-cause ranking is "insufficient evidence"
    repair_allowed_suffixes: str = ".py,.md,.txt,.toml,.cfg,.ini,.json,.yaml,.yml,.js,.jsx,.ts,.tsx,.html,.css,.rst"

    # Refactoring and code transformation
    refactor_max_files: int = Field(60, ge=1, le=500)  # files one transformation may change
    refactor_max_edits: int = Field(3000, ge=1)  # individual edits in one plan
    refactor_max_scan_files: int = Field(5000, ge=1)  # files read while resolving references or looking for duplicates
    refactor_dup_min_lines: int = Field(6, ge=2)  # smallest function/sequence considered for duplicate detection
    refactor_dup_min_tokens: int = Field(40, ge=5)
    refactor_dup_threshold: float = Field(0.85, gt=0, le=1)  # default similarity for near-duplicates
    refactor_dup_max_units: int = Field(4000, ge=10)  # units compared; more are skipped and reported
    refactor_dup_exclude: str = "node_modules,vendor,dist,build,.venv,venv,site-packages,__pycache__,.tox,third_party,.git"
    refactor_output_chars: int = Field(4000, ge=200)
    # Continuous verification
    cv_max_changed_files: int = Field(2000, ge=1)  # files one change set may contain
    cv_max_file_bytes: int = Field(1_000_000, ge=1000)  # per file read for syntax / static checks and fingerprints
    cv_history_limit: int = Field(200, ge=1)  # runs returned by one history query
    cv_flaky_lookback: int = Field(10, ge=2, le=50)  # earlier runs inspected for repeated evidence
    cv_output_chars: int = Field(4000, ge=200)

    # CI/CD integration
    ci_webhook_max_bytes: int = Field(1_000_000, ge=1024)  # request body limit for provider webhooks
    ci_http_timeout_seconds: float = Field(10.0, gt=0)
    ci_http_max_response_bytes: int = Field(1_000_000, ge=1024)
    ci_github_api_base: str = "https://api.github.com"
    ci_status_max_attempts: int = Field(5, ge=1, le=20)
    ci_retry_base_seconds: int = Field(30, ge=1)
    ci_max_changed_files: int = Field(500, ge=1)
    ci_report_max_bytes: int = Field(400_000, ge=10_000)

    # Security engineering. Scans only read files; nothing from a scanned repository is executed or installed.
    security_max_files: int = Field(5000, ge=1)
    security_max_file_bytes: int = Field(1_000_000, ge=1_000)
    security_max_manifest_bytes: int = Field(5_000_000, ge=1_000)
    security_max_components: int = Field(20_000, ge=1)
    security_max_findings: int = Field(2000, ge=1)
    security_time_limit_seconds: float = Field(120.0, gt=0)
    security_advisory_dir: str | None = None  # directory of OSV-format JSON files for offline operation
    security_osv_enabled: bool = False  # live lookups are OFF unless explicitly enabled
    security_osv_base: str = "https://api.osv.dev"
    security_http_timeout_seconds: float = Field(10.0, gt=0)
    security_http_max_response_bytes: int = Field(2_000_000, ge=1_000)
    security_report_max_bytes: int = Field(2_000_000, ge=10_000)
    # --- observability ---
    ops_log_ring_size: int = Field(2000, ge=100, le=50_000)
    ops_log_field_max: int = Field(1000, ge=100, le=10_000)
    ops_trace_sample_ratio: float = Field(1.0, ge=0.0, le=1.0)
    ops_trace_ring_size: int = Field(2000, ge=100, le=50_000)
    ops_metrics_public: bool = False  # /ops/metrics requires auth unless explicitly made public
    ops_retention_days: int = Field(90, ge=1)  # operational events/evaluations/deliveries only; audit events are never deleted
    ops_health_cache_seconds: float = Field(10.0, ge=0)
    ops_probe_timeout_seconds: float = Field(2.0, gt=0)
    ops_periodic_evaluation: bool = False  # off: no continuous monitoring is claimed unless enabled
    ops_evaluation_interval_seconds: float = Field(60.0, ge=5)
    ops_notify_timeout_seconds: float = Field(5.0, gt=0)
    ops_notify_max_attempts: int = Field(3, ge=1, le=6)
    ops_notify_allow_private: bool = False  # SSRF guard: private/loopback/link-local/metadata blocked unless true
    ops_webhook_url: str | None = None
    ops_webhook_secret_env: str | None = None  # NAME of the env var holding the signing secret
    ops_slack_webhook_env: str | None = None  # NAME of the env var holding the Slack webhook URL


    # Release engineering. In production the API refuses to start against a schema that is not at the migration head
    # (migrations are a separate release step, see docs/operations/DEPLOYMENT_GUIDE.md). Set true to enforce the same check elsewhere.
    require_current_schema: bool = False
    max_request_body_bytes: int = Field(8_388_608, ge=65_536)  # global cap on request bodies (413 beyond it); webhook endpoints are stricter
    docs_enabled: bool = True  # OpenAPI/Swagger UI; forced off in production
    # Release 0.19: per-client throttling of INVALID bearer tokens (missing tokens are not counted). Per process, in memory. 0 disables.
    auth_failure_limit: int = Field(20, ge=0)
    auth_failure_window_seconds: int = Field(300, ge=10)
    # Enterprise integrations. Stored credentials are encrypted with a key supplied ONLY through the environment; with no key, stored
    # credentials cannot be created or read (fail closed). Credential references of the form ``env:NOVA_CRED_*`` need no key.
    integration_secret_key: str = ""  # urlsafe-base64, 32 random bytes (python -m app.services.integrations.secrets generate-key)
    integration_secret_keys_previous: Annotated[list[str], NoDecode] = []  # older keys, decrypt-only, kept until credentials are re-encrypted
    integration_allow_private_destinations: bool = False  # SSRF guard exception for local testing only; never enable in production
    integration_inbound_max_bytes: int = Field(262_144, ge=1024)
    integration_replay_window_seconds: int = Field(300, ge=30)
    integration_http_timeout_seconds: float = Field(8.0, gt=0, le=30)
    integration_http_max_response_bytes: int = Field(262_144, ge=1024)
    webhook_max_attempts: int = Field(6, ge=1, le=12)
    webhook_backoff_base_seconds: float = Field(30.0, ge=0)
    webhook_backoff_max_seconds: float = Field(3600.0, ge=0)
    webhook_disable_after_failures: int = Field(20, ge=1)
    webhook_lease_seconds: int = Field(120, ge=10)
    integration_worker_enabled: bool = False  # off by default: no background delivery is claimed unless enabled
    integration_worker_interval_seconds: float = Field(5.0, ge=1)
    integration_event_retention_days: int = Field(30, ge=1)

    # Git subprocess limits
    git_timeout_seconds: float = Field(10.0, gt=0)
    git_max_output_bytes: int = Field(1_000_000, ge=1024)

    # "none" (default): everything is deterministic and no code leaves the server. "anthropic": opt-in AI-assisted repair proposals from Claude.
    # Credentials come from the Anthropic SDK's own sources (ANTHROPIC_API_KEY, ...), never from NOVA settings. A proposal is only a draft patch:
    # it goes through the same validation, human approval, apply and test verification as any other patch.
    ai_provider: str = "none"
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "high"  # output_config.effort: low | medium | high | xhigh | max
    ai_timeout_seconds: float = Field(180.0, gt=0, le=900)
    ai_max_context_bytes: int = Field(120_000, ge=4_000, le=1_000_000)  # repository text sent per request (secrets scrubbed first)
    ai_max_edits: int = Field(20, ge=1, le=100)

    @field_validator("cors_origins", "registration_base_dirs", "index_extra_excludes", "integration_secret_keys_previous", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
