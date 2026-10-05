"""Costs and budget (APP_SPEC §6.10)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from duoskin.models.common import PartId, Strict, UtcDatetime


class CostUnit(Strict):
    name: Literal["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
                  "image_input_tokens", "image_output_tokens", "images", "credits", "api_units", "seconds"]
    qty: float
    unit_price_usd: float


class CostEntry(Strict):
    id: str = ""                        # filled by the ledger when empty
    ts: UtcDatetime
    project_id: str | None = None
    step_id: str | None = None
    part_id: PartId | None = None
    attempt: int = 0
    provider: Literal["anthropic", "openai", "recraft", "tripo", "gemini", "fal", "mock"]
    model: str = ""
    operation: str                      # "messages.stream:L3", "images.edit:I2", "multiview_to_model", ...
    units: list[CostUnit] = Field(default_factory=list)
    usd: float
    credits: float | None = None        # Tripo credits (1 credit ~ $0.01 [third-party])
    basis: Literal["estimate", "usage", "credits", "orphan"] = "usage"
    state: Literal["reserved", "committed", "released", "orphan"] = "committed"
    batch: bool = False                 # Anthropic batch discount applied
    price_table: str = ""               # "prices.json@2026-09-29"
    request_id: str | None = None
    remote_task_id: str | None = None
    balance_before: float | None = None
    balance_after: float | None = None  # Tripo


class Estimate(Strict):
    """What a paid step expects to cost, before it runs (``handler.estimate`` -> USD)."""

    usd: float = Field(ge=0)
    credits: float | None = None
    provider: Literal["anthropic", "openai", "recraft", "tripo", "gemini", "fal", "mock"] = "mock"
    operation: str = "step"
    price_table: str = ""


class Reservation(Strict):
    id: str                             # the ledger row id
    project_id: str | None
    step_id: str | None
    attempt: int
    usd: float


class Budget(Strict):
    project_id: str | None
    cap_usd: float
    spent_usd: float
    reserved_usd: float
    ask_above_usd: float
    tripo_available_credits: float | None = None    # balance - frozen (conservative until FM-T4)

    @property
    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd - self.reserved_usd
