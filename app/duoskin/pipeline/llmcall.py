"""Claude calls from step handlers: compile an LLM template, attach images, call the provider, record the cost (APP_SPEC §7.2, §8.2).

``ask_llm(ctx, template_id, inputs, images=[...], operation=...)`` is the one way a pipeline handler talks to Claude. It compiles the
role template (``prompts.llm.compile_llm``), builds the content blocks (images first, then the text), calls the provider's ``call``
and records the cost with a distinct ``operation`` per call (the ledger is idempotent per ``(step, attempt, operation)``).

``gate_b_provider(ctx, images)`` adapts that to the ``checks.gate_b.run_gate_b`` provider contract: one L11 call per rule group, the
candidate shown on grey and on a checkerboard (the ``vlm_image`` preparation of ``imaging.guides``).
"""
from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PIL import Image

from duoskin.models import llm_io
from duoskin.pipeline import common

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext

#: template id family -> provider route name (providers/anthropic_llm.build_routes)
ROUTES: dict[str, str] = {
    "L1": "L1_reference", "L2": "L2_taste", "L3": "L3_planner", "L4": "L4_critic", "L5": "L5_pairwise", "L6": "L6_reviser",
    "L7": "L7_change", "L9": "L9_hair_match", "L10": "L10_repair", "L11": "L11_checker", "L12": "L12_duo_judge",
    "L13": "L13_ip", "L14": "L14_similarity",
}


def route_of(template_id: str) -> str:
    family = template_id.split(".")[0]
    return ROUTES.get(family, family)


def image_blocks(images: list[bytes | Image.Image]) -> list[dict[str, Any]]:
    from duoskin.providers.anthropic_llm import image_block

    out = []
    for im in images:
        data = im if isinstance(im, (bytes, bytearray)) else common.png_bytes(im)
        out.append(image_block(bytes(data)))
    return out


class CallCounter:
    """Gives each LLM call of a handler a distinct ledger operation (``messages.stream:L11#3``)."""

    def __init__(self) -> None:
        self.n = 0

    def next(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}#{self.n}"


def make_call_ctx(ctx: StepContext, tag: str = "") -> Any:
    """A ``providers.base.CallCtx`` that also persists remote task ids (``set_remote_ref``) and carries the template tag."""
    base = ctx.call_ctx()
    try:
        return dataclasses.replace(base, set_remote_ref=ctx.set_remote_ref, tag=tag)
    except TypeError:                                   # the local fallback context has no such fields
        return base


def ask_llm(ctx: StepContext, template_id: str, inputs: dict[str, Any], *, images: list[bytes | Image.Image] | None = None,
            counter: CallCounter | None = None, route: str | None = None, out: type | None = None, inventory: Any = None) -> Any:
    """Compile and send one LLM template. Returns the validated answer (a pydantic model). Provider errors propagate as ``ProviderError``."""
    from duoskin.prompts.llm import compile_llm, schema_class
    from duoskin.providers.anthropic_llm import text_block

    prompt = compile_llm(template_id, inputs, inventory=inventory)
    schema = out or schema_class(prompt)
    content = image_blocks(images or []) + [text_block(prompt.user_text)]
    adapter = ctx.provider("anthropic")
    route_name = route or route_of(template_id)
    result = adapter.call(route_name, system=prompt.system, content=content, out=schema, ctx=make_call_ctx(ctx, template_id),
                          prompt_version=prompt.template_version)
    op = (counter.next(f"messages.stream:{template_id.split('.')[0]}") if counter else f"messages.stream:{template_id.split('.')[0]}")
    common.record_cost(ctx, getattr(result, "cost", None), op, fallback_provider="anthropic")
    return result.parsed


# ---------------------------------------------------------------------------------------------------- Gate B (L11)
def vlm_views(im: Image.Image) -> list[Image.Image]:
    """The candidate on mid grey and on a checkerboard (what the judge sees, APP_SPEC §2.9)."""
    from duoskin.imaging import guides

    rgba = im.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] == 255:                 # opaque image: one view is enough
        return [guides.vlm_image(rgba, with_checkerboard=False)]
    return [guides.vlm_image(rgba, with_checkerboard=False), guides.vlm_image(rgba, with_checkerboard=True)]


def gate_b_provider(ctx: StepContext, images: Callable[[], list[bytes | Image.Image]] | list[bytes | Image.Image],
                    counter: CallCounter | None = None) -> Callable[[Any], Any]:
    """The ``ProviderFn`` of ``checks.gate_b.run_gate_b``: sends the compiled L11 prompt with the candidate images."""
    from duoskin.providers.anthropic_llm import text_block

    counter = counter or CallCounter()

    def fn(req: Any) -> Any:
        imgs = images() if callable(images) else images
        content = image_blocks(imgs) + [text_block(req.prompt.user_text)]
        adapter = ctx.provider("anthropic")
        result = adapter.call("L11_checker", system=req.prompt.system, content=content, out=llm_io.AssetCheck,
                              ctx=make_call_ctx(ctx, "L11"), prompt_version=req.prompt.template_version)
        common.record_cost(ctx, getattr(result, "cost", None), counter.next("messages.stream:L11"), fallback_provider="anthropic")
        return result.parsed

    return fn


def run_rules(ctx: StepContext, rule_requests: list[Any], images: list[bytes | Image.Image], *, measured_facts: str = "",
              subject_sha: str = "", votes: int = 1, counter: CallCounter | None = None) -> Any:
    """``checks.gate_b.run_gate_b`` with the Claude provider function. The hard IP rules are sent in their own call (the library's rule)."""
    from duoskin.checks import gate_b

    if not rule_requests:
        return gate_b.GateBResult(results=[], verdicts={}, calls=0, escalate=[])
    return gate_b.run_gate_b(gate_b_provider(ctx, images, counter), rule_requests, measured_facts=measured_facts, subject_sha=subject_sha, votes=votes)


def rule_requests(rule_ids: list[str], slots: dict[str, dict[str, str]] | None = None) -> list[Any]:
    from duoskin.checks.gate_b import RuleRequest

    return [RuleRequest(r, (slots or {}).get(r, {})) for r in dict.fromkeys(rule_ids)]


def estimate_llm_usd(template_id: str, *, calls: int = 1, image_count: int = 1) -> float:
    """A conservative USD estimate for ``calls`` Claude calls of this template (for step estimates, never for billing)."""
    from duoskin.providers import pricing

    model = "claude-sonnet-5" if template_id.split(".")[0] in ("L9", "L10", "L11") else "claude-opus-5"
    est = pricing.estimate_claude(model, input_tokens=1800 + 1200 * image_count, output_tokens=500)
    return float(est.usd) * calls


def pil(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))
