"""Prices, cost records and pre-call estimates (APP_SPEC 6.10 and 8.8, bible section 20.1).

Every adapter returns a ``cost`` dict shaped like ``duoskin.models.cost.CostEntry`` without the fields the ledger
owns (``id``, ``ts``, ``project_id``, ``step_id``, ``part_id``, ``attempt``, ``state``). ``estimate_*`` functions
give the engine its pre-call ``Estimate`` (as ``PriceEstimate``; ``as_dict()`` validates as ``models.cost.Estimate``).

The default price table is in this file. ``duoskin/data/prices.json`` (dated) may override any numeric leaf of
``DEFAULT_PRICES`` by dotted path; unknown keys are ignored. Every estimate and cost names its price table.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PRICE_TABLE_DATE = "2026-09-29"
PRICE_TABLE = f"prices.json@{PRICE_TABLE_DATE}"

# [ESTIMATE] / [UNVERIFIED] values are conservative guesses; the ledger replaces them with usage as soon as the
# provider reports it.
DEFAULT_PRICES: dict[str, Any] = {
    "claude": {   # USD per million tokens
        "opus": {"input": 5.0, "output": 25.0},
        "sonnet": {"input": 2.0, "output": 10.0},
        "cache_read_mult": 0.1, "cache_write_5m_mult": 1.25, "cache_write_1h_mult": 2.0, "batch_mult": 0.5,
    },
    "openai_image": {   # USD per million tokens (Flare and Sunburst share the rates)
        "text_in": 5.0, "image_in": 8.0, "image_out": 30.0,
        # output tokens per image by quality at 1024x1024; they do not scale with pixels [third-party]
        "out_tokens": {"low": 196, "medium": 439, "high": 1756, "xhigh": 3122, "max": 7024},
        "ref_image_tokens": 1200,    # [DERIVED] about $0.01 per reference image
    },
    "recraft": {   # USD per image / per request
        "recraftv4_1_vector": 0.08, "recraftv4_1_utility_vector": 0.08, "recraftv4_styles_vector": 0.05,
        "recraftv4_1_pro_vector": 0.30, "recraftv4_1_utility_pro_vector": 0.30, "recraftv4_styles_pro_vector": 0.30,
        "style_create": 0.005, "vectorize": 0.01, "remove_background": 0.01,
    },
    "tripo": {   # credits; 1 credit = $0.01 [third-party]
        "usd_per_credit": 0.01,
        "image_to_multiview": 10, "edit_multiview": 5, "multiview_to_model": 110, "image_to_model": 110,
        "text_to_model": 110, "convert": 10, "import_model": 0,
        "mesh_segment": 40, "mesh_complete": 30, "retopology": 20, "texture_model": 60,   # [UNVERIFIED]
    },
    "gemini": {   # USD per million tokens; the first row ends 2026-12-31
        "judge_until": "2026-12-31",
        "judge": {"input": 0.75, "output": 3.75}, "judge_after": {"input": 1.50, "output": 7.50},
        "image": 0.067,
    },
}


def _merge_numeric(base: dict[str, Any], over: dict[str, Any]) -> None:
    for k, v in over.items():
        if k in base:
            if isinstance(base[k], dict) and isinstance(v, dict):
                _merge_numeric(base[k], v)
            elif isinstance(base[k], (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool):
                base[k] = v


def load_prices(path: Path | None = None) -> dict[str, Any]:
    """``DEFAULT_PRICES`` with numeric overrides from ``duoskin/data/prices.json`` when that file exists."""
    import copy
    prices = copy.deepcopy(DEFAULT_PRICES)
    p = path or Path(__file__).resolve().parent.parent / "data" / "prices.json"
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _merge_numeric(prices, data)
    except (OSError, ValueError):
        pass
    return prices


_PRICES_CACHE: dict[str, Any] | None = None


def prices() -> dict[str, Any]:
    global _PRICES_CACHE
    if _PRICES_CACHE is None:
        _PRICES_CACHE = load_prices()
    return _PRICES_CACHE


def set_prices(table: dict[str, Any] | None) -> None:
    """Replace (or with ``None`` reload) the active table; used by tests and by a settings reload."""
    global _PRICES_CACHE
    _PRICES_CACHE = table


# --------------------------------------------------------------------------------------------------------------
# Cost records and estimates
# --------------------------------------------------------------------------------------------------------------

def _unit(name: str, qty: float, unit_price_usd: float) -> dict[str, Any]:
    return {"name": name, "qty": float(qty), "unit_price_usd": float(unit_price_usd)}


def cost_record(provider: str, model: str, operation: str, *, units: list[dict[str, Any]], usd: float,
                credits: float | None = None, basis: str = "usage", request_id: str | None = None,
                remote_task_id: str | None = None, batch: bool = False) -> dict[str, Any]:
    """A ``CostEntry``-like dict. ``usd`` is rounded to 1e-6."""
    return {
        "provider": provider, "model": model, "operation": operation, "units": units,
        "usd": round(float(usd), 6), "credits": credits, "basis": basis, "batch": batch,
        "price_table": PRICE_TABLE, "request_id": request_id, "remote_task_id": remote_task_id,
    }


@dataclass(frozen=True)
class PriceEstimate:
    """A pre-call estimate; ``as_dict()`` validates as ``duoskin.models.cost.Estimate``."""

    usd: float
    provider: str
    operation: str
    credits: float | None = None
    price_table: str = PRICE_TABLE
    units: tuple[dict[str, Any], ...] = field(default=(), compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"usd": round(self.usd, 6), "credits": self.credits, "provider": self.provider,
                "operation": self.operation, "price_table": self.price_table}


# --------------------------------------------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------------------------------------------

def _claude_rates(model: str) -> dict[str, float]:
    t = prices()["claude"]
    return t["sonnet"] if "sonnet" in model.lower() or "haiku" in model.lower() else t["opus"]


def claude_cost(model: str, usage: dict[str, int] | None, *, operation: str, request_id: str | None = None,
                batch: bool = False, cache_ttl: str = "5m") -> dict[str, Any]:
    """Cost of one Claude call from ``usage`` (keys: ``input``, ``output``, ``cache_read_input_tokens``,
    ``cache_creation_input_tokens``, optional ``cache_creation_5m`` / ``cache_creation_1h``). The served model
    prices the call (a fallback may have answered)."""
    t = prices()["claude"]
    r = _claude_rates(model)
    u = usage or {}
    inp, out = int(u.get("input", 0) or 0), int(u.get("output", 0) or 0)
    cr = int(u.get("cache_read_input_tokens", 0) or 0)
    cw = int(u.get("cache_creation_input_tokens", 0) or 0)
    w5 = int(u.get("cache_creation_5m", 0) or 0)
    w1 = int(u.get("cache_creation_1h", 0) or 0)
    if cw and not (w5 or w1):
        w5, w1 = (cw, 0) if cache_ttl != "1h" else (0, cw)
    per = 1e-6
    p_in, p_out = r["input"] * per, r["output"] * per
    units = [
        _unit("input_tokens", inp, p_in), _unit("output_tokens", out, p_out),
        _unit("cache_read_tokens", cr, p_in * t["cache_read_mult"]),
        _unit("cache_write_5m_tokens", w5, p_in * t["cache_write_5m_mult"]),
        _unit("cache_write_1h_tokens", w1, p_in * t["cache_write_1h_mult"]),
    ]
    usd = sum(x["qty"] * x["unit_price_usd"] for x in units)
    if batch:
        usd *= t["batch_mult"]
        for x in units:
            x["unit_price_usd"] *= t["batch_mult"]
    return cost_record("anthropic", model, operation, units=[x for x in units if x["qty"]], usd=usd,
                       basis="usage", request_id=request_id, batch=batch)


def estimate_claude(model: str, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0,
                    operation: str = "messages.stream", batch: bool = False) -> PriceEstimate:
    """Worst-case-ish estimate: ``output_tokens`` is the expected output, not ``max_tokens``."""
    r = _claude_rates(model)
    t = prices()["claude"]
    usd = ((input_tokens - cached_input_tokens) * r["input"] + cached_input_tokens * r["input"] * t["cache_read_mult"]
           + output_tokens * r["output"]) * 1e-6
    if batch:
        usd *= t["batch_mult"]
    return PriceEstimate(usd=usd, provider="anthropic", operation=operation)


# --------------------------------------------------------------------------------------------------------------
# OpenAI images
# --------------------------------------------------------------------------------------------------------------

def openai_image_cost(model: str, usage: dict[str, Any] | None, *, operation: str, quality: str, n: int,
                      n_input_images: int = 0, prompt_chars: int = 1200,
                      request_id: str | None = None) -> dict[str, Any]:
    """From ``usage`` when the response carried it (basis ``usage``), else an estimate (basis ``estimate``)."""
    t = prices()["openai_image"]
    per = 1e-6
    if usage:
        det = usage.get("input_tokens_details") or {}
        img_in = int(det.get("image_tokens", 0) or 0)
        total_in = int(usage.get("input_tokens", 0) or 0)
        text_in = int(det.get("text_tokens", max(0, total_in - img_in)) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        units = [_unit("input_tokens", text_in, t["text_in"] * per), _unit("image_input_tokens", img_in, t["image_in"] * per),
                 _unit("image_output_tokens", out, t["image_out"] * per)]
        usd = sum(u["qty"] * u["unit_price_usd"] for u in units)
        return cost_record("openai", model, operation, units=[u for u in units if u["qty"]], usd=usd,
                           basis="usage", request_id=request_id)
    est = estimate_openai_image(quality=quality, n=n, n_input_images=n_input_images, prompt_chars=prompt_chars,
                                operation=operation)
    return cost_record("openai", model, operation, units=list(est.units), usd=est.usd, basis="estimate",
                       request_id=request_id)


def estimate_openai_image(*, quality: str, n: int = 1, n_input_images: int = 0, prompt_chars: int = 1200,
                          operation: str = "images.generate") -> PriceEstimate:
    t = prices()["openai_image"]
    per = 1e-6
    out_tok = t["out_tokens"].get(quality, t["out_tokens"]["high"]) * n
    img_in = t["ref_image_tokens"] * n_input_images * n      # [UNVERIFIED] whether references bill once or per n: be safe
    text_in = math.ceil(prompt_chars / 4)
    units = (_unit("input_tokens", text_in, t["text_in"] * per),
             _unit("image_input_tokens", img_in, t["image_in"] * per),
             _unit("image_output_tokens", out_tok, t["image_out"] * per))
    usd = sum(u["qty"] * u["unit_price_usd"] for u in units)
    return PriceEstimate(usd=usd, provider="openai", operation=operation, units=tuple(u for u in units if u["qty"]))


# --------------------------------------------------------------------------------------------------------------
# Recraft
# --------------------------------------------------------------------------------------------------------------

def recraft_unit_price(op_or_model: str) -> float:
    t = prices()["recraft"]
    v = t.get(op_or_model)
    if v is None:   # unknown model id: assume the dearest vector price
        v = max(x for k, x in t.items() if k.startswith("recraftv4") and isinstance(x, (int, float)))
    return float(v)


def recraft_cost(model: str, *, operation: str, n: int = 1, api_units: float | None = None,
                 request_id: str | None = None) -> dict[str, Any]:
    price = recraft_unit_price(model)
    units = [_unit("images", n, price)]
    if api_units:
        units.append(_unit("api_units", api_units, 0.0))    # informational: Recraft's own figure (1000 units = $1)
    return cost_record("recraft", model, operation, units=units, usd=price * n, basis="usage",
                       credits=None, request_id=request_id)


def estimate_recraft(model_or_op: str, *, n: int = 1, operation: str | None = None) -> PriceEstimate:
    return PriceEstimate(usd=recraft_unit_price(model_or_op) * n, provider="recraft", operation=operation or model_or_op)


# --------------------------------------------------------------------------------------------------------------
# Tripo (credits)
# --------------------------------------------------------------------------------------------------------------

def tripo_credits(op: str, *, views: int = 1) -> float:
    """Credit estimate for a Tripo operation (``edit_multiview`` is per view)."""
    t = prices()["tripo"]
    base = float(t.get(op, t["multiview_to_model"]))
    return base * views if op == "edit_multiview" else base


def tripo_usd(credits: float) -> float:
    return float(credits) * float(prices()["tripo"]["usd_per_credit"])


def tripo_cost(model: str, *, operation: str, credits: float | None, task_id: str | None,
               fallback_op: str | None = None, views: int = 1, balance_before: float | None = None) -> dict[str, Any]:
    """``credits`` is ``credits_consumed`` from the task; when unknown the estimate for ``fallback_op`` is used
    (basis ``estimate``)."""
    basis = "credits"
    if credits is None:
        credits = tripo_credits(fallback_op or operation, views=views)
        basis = "estimate"
    usd = tripo_usd(credits)
    rec = cost_record("tripo", model, operation, units=[_unit("credits", credits, tripo_usd(1))], usd=usd,
                      credits=float(credits), basis=basis, remote_task_id=task_id)
    if balance_before is not None:
        rec["balance_before"] = balance_before
    return rec


def estimate_tripo(op: str, *, views: int = 1) -> PriceEstimate:
    c = tripo_credits(op, views=views)
    return PriceEstimate(usd=tripo_usd(c), credits=c, provider="tripo", operation=op)


# --------------------------------------------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------------------------------------------

def gemini_cost(model: str, usage: dict[str, Any] | None, *, operation: str, image: bool = False,
                today: date | None = None, request_id: str | None = None) -> dict[str, Any]:
    t = prices()["gemini"]
    if image:
        return cost_record("gemini", model, operation, units=[_unit("images", 1, t["image"])], usd=t["image"],
                           basis="estimate", request_id=request_id)
    u = usage or {}
    inp = int(u.get("promptTokenCount", u.get("input_tokens", 0)) or 0)
    out = int(u.get("candidatesTokenCount", u.get("output_tokens", 0)) or 0) + int(u.get("thoughtsTokenCount", 0) or 0)
    until = date.fromisoformat(t["judge_until"])
    rates = t["judge"] if (today or datetime.now(UTC).date()) <= until else t["judge_after"]
    units = [_unit("input_tokens", inp, rates["input"] * 1e-6), _unit("output_tokens", out, rates["output"] * 1e-6)]
    usd = sum(x["qty"] * x["unit_price_usd"] for x in units)
    return cost_record("gemini", model, operation, units=[x for x in units if x["qty"]], usd=usd,
                       basis="usage" if usage else "estimate", request_id=request_id)


def estimate_gemini_judge(*, input_tokens: int = 1500, output_tokens: int = 400) -> PriceEstimate:
    c = gemini_cost("gemini-3.8-flash", {"input_tokens": input_tokens, "output_tokens": output_tokens}, operation="judge")
    return PriceEstimate(usd=c["usd"], provider="gemini", operation="judge")


def as_mock(cost: dict[str, Any]) -> dict[str, Any]:
    """Re-label a real-provider cost record as the mock's (``provider="mock"``; the simulated usd is kept so
    budget tests behave as with real providers)."""
    out = dict(cost)
    out["provider"] = "mock"
    return out
