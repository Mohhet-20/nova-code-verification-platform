"""Claude (Anthropic) client for structured, single-request tasks.

Credentials are resolved by the Anthropic SDK itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile); NOVA never stores
or logs them. Every call asks for JSON that matches a schema (structured outputs), checks the stop reason before reading content, and returns
provenance (model actually used, request id, token usage) so the result can be audited. Errors are mapped to NOVA errors without echoing
provider messages.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.errors import NovaError, TooManyRequestsError

# Models that accept the server-side `fallbacks: "default"` refusal fallback on the Claude API.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AIUnavailableError(NovaError):
    status_code = 503
    code = "ai_unavailable"


class AIDeclinedError(NovaError):
    status_code = 422
    code = "ai_declined"


@dataclass
class StructuredResult:
    data: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)


def _default_client_factory(settings: Settings) -> Any:
    import anthropic  # imported lazily: the dependency is only needed when the provider is enabled

    return anthropic.Anthropic(timeout=settings.ai_timeout_seconds, max_retries=2)


# Replaced in tests; production always uses the real SDK client.
client_factory: Callable[[Settings], Any] = _default_client_factory


def _fallback_used(usage: Any) -> bool:
    return any(getattr(entry, "type", None) == "fallback_message" for entry in (getattr(usage, "iterations", None) or []))


def complete_json(settings: Settings, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 16000) -> StructuredResult:
    """One structured request. Raises AIUnavailableError / AIDeclinedError / TooManyRequestsError; never returns unvalidated text."""
    import anthropic

    effort = settings.ai_effort if settings.ai_effort in EFFORTS else "high"
    kwargs: dict[str, Any] = {
        "model": settings.ai_model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }
    if settings.ai_model in FALLBACK_MODELS:  # a policy decline is re-run server-side on Anthropic's recommended fallback model
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    try:
        client = client_factory(settings)
        response = client.beta.messages.create(**kwargs) if "betas" in kwargs else client.messages.create(**kwargs)
    except anthropic.RateLimitError as exc:
        raise TooManyRequestsError("Claude is rate limiting requests; try again shortly.") from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AIUnavailableError("Claude rejected the configured credentials (set ANTHROPIC_API_KEY on the server).") from exc
    except anthropic.APITimeoutError as exc:
        raise AIUnavailableError("Claude did not answer within NOVA_AI_TIMEOUT_SECONDS.") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailableError("Claude could not be reached from this server.") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailableError(f"Claude returned HTTP {exc.status_code}; nothing was changed.") from exc
    except anthropic.AnthropicError as exc:  # e.g. no credentials configured at all
        raise AIUnavailableError(f"Claude is not configured on this server ({type(exc).__name__}).") from exc

    usage = getattr(response, "usage", None)
    provenance = {
        "provider": "anthropic", "requested_model": settings.ai_model, "model": getattr(response, "model", settings.ai_model),
        "request_id": getattr(response, "_request_id", None), "stop_reason": response.stop_reason, "effort": effort,
        "input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None),
        "fallback_used": _fallback_used(usage),
    }
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise AIDeclinedError("Claude declined this request; no proposal was produced.",
                              {"category": getattr(details, "category", None), "provenance": provenance})
    if response.stop_reason == "max_tokens":
        raise AIUnavailableError("Claude's answer was cut off at the token limit; no partial proposal is used.")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise AIUnavailableError("Claude returned no answer text.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIUnavailableError("Claude's answer was not valid JSON; nothing was changed.") from exc
    if not isinstance(data, dict):
        raise AIUnavailableError("Claude's answer did not match the expected structure.")
    return StructuredResult(data=data, provenance=provenance)
