"""MockImages: deterministic GPT Image stand-in (APP_SPEC 7.8).

Same interface and the same pre-flight and post-call code as ``OpenAIImages`` (``validate_request``, the mask and
RGBA rules of ``prepare_inputs``, ``check_response_sizes``), so a pipeline bug that the real adapter would reject is
rejected here too.

* ``edit`` / ``generate`` return ``n`` PNGs of exactly the requested size.
* Guide-based calls (a mask is present: I1, I1e, I4, I8) paint flat colours from the prompt into the editable area of
  Image 1 and leave every other pixel untouched, so paste-back and the Gate A checks run for real.
* Concept calls (I1, I1e, C2, or a prompt that asks for a character sheet) draw a blocky front/back character sheet in
  the colours named in the prompt.
* Transparent calls draw one centred shape with real anti-aliased alpha; opaque ones put it on a flat light backdrop.
* FINALIZE (the "Final-quality redraw" template) returns a lightly sharpened copy of Image 1 (passes A_DRIFT).
* ``usage`` holds realistic token counts (the price table's output tokens per quality); the cost row says ``provider="mock"``.
* Faults (APP_SPEC 16): ``moderation_blocked``, ``timeout``, ``rate_limit``, ``billing``, ``permission``, ``unknown_parameter``,
  ``server``, ``auth``, ``network`` raise the real errors; ``size_drift``, ``opaque_alpha`` and ``usage_none`` change the output.
"""
from __future__ import annotations

import hashlib
import io
import math
import random
from collections.abc import Callable
from typing import Any

from PIL import Image

from duoskin.providers.base import CallCtx, CapabilityFlags, RateLimiter, png_size, request_hash, seed_from_hash
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock._common import MockBase
from duoskin.providers.openai_images import (
    ImageProviderBase,
    ImageRequest,
    ImageResult,
    check_response_sizes,
    has_real_alpha,
    parse_size,
    prepare_inputs,
    validate_request,
)
from duoskin.providers.pricing import openai_image_cost, prices

CONCEPT_TAGS = ("I1", "I1e", "C2")


def _is_finalize(req: ImageRequest) -> bool:
    return "final-quality redraw" in req.prompt.lower() or req.tag.lower().endswith("finalize") or req.tag in ("I0", "I0.finalize")


def _is_concept(req: ImageRequest) -> bool:
    p = req.prompt.lower()
    return req.tag in CONCEPT_TAGS or "character sheet" in p or ("concept" in p and "character" in p)


class MockImages(MockBase, ImageProviderBase):
    """Deterministic image provider. ``requests`` holds every call; ``costs`` the mock cost rows."""

    name = "openai"

    def __init__(self, *, faults: FaultInjector | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 flags: CapabilityFlags | None = None, ipm: int | None = None) -> None:
        MockBase.__init__(self, faults=faults, cost_sink=cost_sink, flags=flags)
        self.limiter = RateLimiter(ipm=ipm, concurrent=8) if ipm else RateLimiter(concurrent=8)

    def __repr__(self) -> str:
        return "MockImages()"

    # ----- one request --------------------------------------------------------------------------------------
    def _single(self, req: ImageRequest, ctx: CallCtx, *, edit: bool) -> ImageResult:
        w, h = validate_request(req, edit=edit, flags=self.flags, per_request_cap=self.per_request_cap())
        images, mask, adjustments = prepare_inputs(req, self.flags) if edit else ([], None, [])
        image1_size = png_size(bytes(images[0].data)) if (edit and req.image1_role == "edit_target" and images) else None
        canon = {"op": "edit" if edit else "generate", "req": req, "images": [bytes(i.data) for i in images],
                 "mask": bytes(mask.data) if mask else None}
        digest = request_hash(canon)
        self.record("openai", f"images.{'edit' if edit else 'generate'}", canon)
        ctx.tick()
        behaviour = self.fault("openai", tag=req.tag)
        ctx.progress(0.5, "mock image")
        base_seed = seed_from_hash(digest)
        outs: list[bytes] = []
        for i in range(req.n):
            rng = random.Random(base_seed + 7919 * i)
            outs.append(self._draw(req, edit, images, mask, w, h, rng, i))
        if behaviour == "size_drift":                                 # the model answers at the wrong size: run the real check
            outs = [D.to_png(Image.open(io.BytesIO(b)).crop((0, 0, w, h - 16)) if h > 16 else Image.new("RGB", (w, h + 16))) for b in outs]
        if behaviour == "opaque_alpha" and req.background == "transparent":
            outs = [D.to_png(D.over_background(Image.open(io.BytesIO(b)).convert("RGBA"))) for b in outs]
        rid = f"mock-openai-{digest[:12]}"
        usage = None if behaviour == "usage_none" else self._usage(req, len(images), req.n)
        cost = openai_image_cost(req.model, usage, operation=f"images.{'edit' if edit else 'generate'}:{req.tag or 'img'}", quality=req.quality,
                                 n=req.n, n_input_images=len(images), prompt_chars=len(req.prompt), request_id=rid)
        sizes = [png_size(b) or (0, 0) for b in outs]
        reported = f"{sizes[0][0]}x{sizes[0][1]}" if behaviour == "size_drift" else req.size
        mock_cost = self.record_cost(cost)
        check_response_sizes(req.size, reported, sizes, image1_size, request_id=rid, cost=mock_cost)
        alpha = [has_real_alpha(b) for b in outs]
        warnings = ["transparent_requested_but_opaque"] if req.background == "transparent" and not any(alpha) else []
        if usage is not None:
            self.flags.set("openai.usage_present", True)
        return ImageResult(images=outs, size=f"{w}x{h}", usage=usage, request_id=rid, model=req.model,
                           raw_sha256=[hashlib.sha256(b).hexdigest() for b in outs], alpha_present=alpha,
                           batches=[{"batch_index": 0, "n": req.n, "returned": len(outs), "request_id": rid, "usage": usage}],
                           request_ids=[rid], adjustments=adjustments, warnings=warnings, cost=mock_cost,
                           usage_present=usage is not None, n_total=req.n, batch_sizes=[len(outs)])

    # ----- drawing ------------------------------------------------------------------------------------------
    def _draw(self, req: ImageRequest, edit: bool, images: list, mask: Any, w: int, h: int, rng: random.Random, i: int) -> bytes:
        pal = D.palette_for(req.prompt, random.Random(request_hash({"p": req.prompt})[:8]), 4)
        pal = [D.shade(c, 6 * ((i + j) % 3 - 1)) if i else c for j, c in enumerate(pal)]   # small per-variant shifts
        if edit and _is_finalize(req) and images:
            return D.to_png(D.lightly_sharpened(D.decode(bytes(images[0].data))))
        if edit and mask is not None and images:
            base = D.decode(bytes(images[0].data))
            color = pal[i % len(pal)] if len(D.colors_from_text(req.prompt)) > 1 else D.shade(pal[0], 10 * i)
            return D.to_png(D.paint_masked(base, bytes(mask.data), color))
        if _is_concept(req):
            return D.to_png(D.blocky_sheet(w, h, pal, rng))
        shape = D.shape_rgba(w, h, pal[0], rng, margin=0.13 + 0.012 * (i % 5))
        return D.to_png(shape if req.background == "transparent" else D.over_background(shape))

    @staticmethod
    def _usage(req: ImageRequest, n_images_in: int, n: int) -> dict[str, Any]:
        t = prices()["openai_image"]
        out_tok = int(t["out_tokens"].get(req.quality, t["out_tokens"]["high"])) * n
        text_in = max(1, math.ceil(len(req.prompt) / 4))
        img_in = int(t["ref_image_tokens"]) * n_images_in
        return {"input_tokens": text_in + img_in, "output_tokens": out_tok, "total_tokens": text_in + img_in + out_tok,
                "input_tokens_details": {"text_tokens": text_in, "image_tokens": img_in}}

    # ----- FM-T6 probes ---------------------------------------------------------------------------------------
    def probe(self) -> dict[str, bool]:
        """The mock supports everything: sets and returns the three capability flags as True."""
        res = {"openai.mask_multi_ok": True, "openai.rgba_image1_ok": True, "openai.usage_present": True}
        for k, v in res.items():
            self.flags.set(k, v)
        return res


def parse(size: str) -> tuple[int, int]:  # re-exported for tests that build sizes by hand
    return parse_size(size)
