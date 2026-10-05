"""MockGemini: the judge passes every rule; the image model returns a JPEG on white (exercises the unmix path).

Faults: ``image_safety`` / ``image_prohibited`` (moderation), ``recitation``, ``no_image``, ``rate_limit``, ``server``, ``auth`` raise
the real errors; ``empty_text`` makes the judge answer nothing (every rule FAILs, as the real adapter does after its retry);
``fail_rule`` makes the first rule fail.
"""
from __future__ import annotations

import io
import random
from collections.abc import Callable
from typing import Any, Literal

from PIL import Image

from duoskin.providers.base import CallCtx, CapabilityFlags, request_hash, seed_from_hash
from duoskin.providers.faults import FaultInjector
from duoskin.providers.gemini import (
    IMAGE_MODEL,
    JUDGE_MODEL,
    RuleSpec,
    RuleVerdict,
    check_privacy,
    validate_image_args,
    validate_judge_args,
)
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock._common import MockBase
from duoskin.providers.pricing import gemini_cost


class MockGemini(MockBase):
    name = "gemini"

    def __init__(self, *, faults: FaultInjector | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 flags: CapabilityFlags | None = None, key_billed: bool = False) -> None:
        super().__init__(faults=faults, cost_sink=cost_sink, flags=flags)
        self.key_billed = key_billed

    def __repr__(self) -> str:
        return "MockGemini()"

    def judge(self, png: bytes, rules: list[RuleSpec], *, thinking_level: Literal["LOW", "MEDIUM"], ctx: CallCtx,
              private: bool = False) -> list[RuleVerdict]:
        validate_judge_args(png, rules, thinking_level)
        check_privacy(private, self.key_billed)
        self.record("gemini", "generateContent:judge", {"op": "judge", "png": png, "rules": rules, "level": thinking_level})
        ctx.tick()
        behaviour = self.fault("gemini", tag="G1")
        self.record_cost(gemini_cost(JUDGE_MODEL, {"promptTokenCount": 800, "candidatesTokenCount": 40 * len(rules)}, operation="generateContent:judge"))
        if behaviour == "empty_text":
            return [RuleVerdict(rule_id=r.rule_id, evidence="FAIL: empty response", passed=False) for r in rules]
        out = []
        for i, r in enumerate(rules):
            ok = not (behaviour == "fail_rule" and i == 0)
            out.append(RuleVerdict(rule_id=r.rule_id, evidence=f"Mock evidence for {r.rule_id}: {'the statement holds' if ok else 'injected failure'}.", passed=ok))
        return out

    def generate(self, *, prompt: str, images: list[bytes], aspect: str, size: Literal["1K"], ctx: CallCtx,
                 private: bool = False) -> tuple[bytes, Literal["png", "jpeg"], dict]:
        validate_image_args(prompt, images, aspect, size)
        check_privacy(private, self.key_billed)
        digest = request_hash({"op": "g2", "prompt": prompt, "images": images, "aspect": aspect})
        self.record("gemini", "generateContent:image", {"op": "g2", "prompt": prompt, "images": images, "aspect": aspect})
        ctx.tick()
        self.fault("gemini", tag="G2")
        a, b = (int(v) for v in aspect.split(":"))
        long_edge = 1024
        w, h = (long_edge, max(64, round(long_edge * b / a))) if a >= b else (max(64, round(long_edge * a / b)), long_edge)
        rng = random.Random(seed_from_hash(digest))
        pal = D.palette_for(prompt, random.Random(digest[:8]), 2)
        shape = D.shape_rgba(w, h, pal[0], rng)
        flat = D.over_background(shape, (255, 255, 255))
        buf = io.BytesIO()
        flat.save(buf, "JPEG", quality=90)
        cost = self.record_cost(gemini_cost(IMAGE_MODEL, None, operation="generateContent:image", image=True, request_id=f"mock-gemini-{digest[:12]}"))
        meta = {"model": IMAGE_MODEL, "finish_reason": "STOP", "synthid": True, "request_id": f"mock-gemini-{digest[:12]}", "cost": cost,
                "usage": {"promptTokenCount": 300}, "has_alpha": False}
        Image.open(io.BytesIO(buf.getvalue())).verify()
        return buf.getvalue(), "jpeg", meta
