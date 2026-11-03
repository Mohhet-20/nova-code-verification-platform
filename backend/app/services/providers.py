"""Plan-provider interface. Task plans are always produced by the deterministic baseline; NOVA_AI_PROVIDER=anthropic enables AI-assisted
repair proposals only (app/services/repair/ai.py) and does not change planning."""
from __future__ import annotations

from typing import Protocol

from app.core.config import get_settings
from app.core.errors import NovaError
from app.schemas.tasks import PlanContent
from app.services import planner


class PlanProvider(Protocol):
    name: str
    version: str

    def generate(self, inp: planner.PlannerInput) -> PlanContent: ...


class DeterministicPlanProvider:
    name = planner.GENERATOR
    version = planner.GENERATOR_VERSION

    def generate(self, inp: planner.PlannerInput) -> PlanContent:
        return planner.build_plan(inp)


def get_plan_provider() -> PlanProvider:
    configured = get_settings().ai_provider.lower()
    if configured in ("", "none", "anthropic"):
        return DeterministicPlanProvider()
    raise NovaError(
        f"AI provider '{configured}' is not implemented. Set NOVA_AI_PROVIDER to none or anthropic.", None
    )
