"""Gemini adapter, optional (APP_SPEC 7.6, bible 17.5, 17.6 and Appendix C).

Two roles on ``generateContent`` over httpx REST (the transport can be swapped for the Interactions API later):

* ``judge`` (G1): a second-opinion judge. One image per call, ``responseMimeType=application/json`` with a response
  schema that puts ``evidence`` before ``pass``, ``thinkingLevel`` LOW or MEDIUM (MINIMAL is invalid on 3.8),
  ``mediaResolution=MEDIA_RESOLUTION_HIGH``; no temperature, top_p or top_k. An empty answer is a FAIL (retried once);
  a rule that is missing from the answer is a FAIL too (VLM-03).
* ``generate`` (G2): Nano Banana 2 as the "other model" rung. The bytes are sniffed (a JPEG is never a final
  texture), parts with ``thought`` are skipped, ``IMAGE_SAFETY`` / ``IMAGE_PROHIBITED_CONTENT`` raise ``moderation``,
  ``IMAGE_RECITATION`` raises ``recitation``; the output is never alpha and carries SynthID (noted in the meta).

Model ids are pinned (no ``-latest``). An image marked private goes to Gemini only when its key is marked billed
(CHK-P11). The key travels in the ``x-goog-api-key`` header and never appears in a URL, message or log.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any, Literal, Protocol

from duoskin.models.common import Strict
from duoskin.providers._http import httpx, make_client
from duoskin.providers.base import (
    PNG_MAGIC,
    CallCtx,
    CapabilityFlags,
    ProviderError,
    RateLimiter,
    backoff_delay,
    error_from_status,
    limiter_for,
    sleep_checked,
    sniff_kind,
)
from duoskin.providers.pricing import gemini_cost

PROVIDER = "gemini"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
JUDGE_MODEL = "gemini-3.8-flash"
IMAGE_MODEL = "gemini-3.1-flash-image"
MAX_RULES_PER_CALL = 5
MODERATION_REASONS = frozenset({"IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"})
RECITATION_REASONS = frozenset({"IMAGE_RECITATION", "RECITATION"})

# Evidence BEFORE pass (bible Appendix C).
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["checks"],
    "properties": {"checks": {"type": "array", "items": {
        "type": "object", "required": ["rule_id", "evidence", "pass"],
        "properties": {"rule_id": {"type": "string"}, "evidence": {"type": "string"}, "pass": {"type": "boolean"}}}}},
}


class RuleSpec(Strict):
    rule_id: str
    statement: str


class RuleVerdict(Strict):
    rule_id: str
    evidence: str
    passed: bool


class JudgeProvider(Protocol):
    def judge(self, png: bytes, rules: list[RuleSpec], *, thinking_level: Literal["LOW", "MEDIUM"], ctx: CallCtx) -> list[RuleVerdict]: ...


class BackupImageProvider(Protocol):
    def generate(self, *, prompt: str, images: list[bytes], aspect: str, size: Literal["1K"], ctx: CallCtx) -> tuple[bytes, Literal["png", "jpeg"], dict]: ...


def _inline(data: bytes, mime: str) -> dict[str, Any]:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


def rules_text(rules: list[RuleSpec]) -> str:
    """The user text of a judge call: closed yes/no statements with stable ids; pass means the statement is true."""
    lines = [
        ("Judge the attached image against each rule below. For every rule, first write the evidence you can see in the image, "
         "then answer pass=true only if the statement is true. Answer every rule_id exactly once."),
        "",
        "<rules>",
    ]
    lines += [f"- {r.rule_id}: {r.statement}" for r in rules]
    lines.append("</rules>")
    return "\n".join(lines)


def extract_text(resp: dict[str, Any]) -> str:
    """The concatenated non-thought text parts of the first candidate (the equivalent of ``resp.text``)."""
    cands = resp.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str))


def moderation_error(reason: str, *, request_id: str | None = None) -> ProviderError:
    return ProviderError(PROVIDER, "moderation", f"Gemini blocked the request ({reason})", code=reason, billed="no", request_id=request_id,
                         user_hint="Gemini's safety filter blocked this image request. Rewrite it once, then change the design.")


def recitation_error(reason: str, *, request_id: str | None = None) -> ProviderError:
    return ProviderError(PROVIDER, "recitation", f"Gemini stopped because the result was too close to existing material ({reason})", code=reason,
                         billed="yes", request_id=request_id,
                         user_hint="This is an originality failure: change the design or plan again instead of retrying.")


def validate_judge_args(png: bytes, rules: list[RuleSpec], thinking_level: str) -> None:
    """Pre-flight of ``judge`` (shared with the mock)."""
    if thinking_level not in ("LOW", "MEDIUM"):
        raise ProviderError(PROVIDER, "bad_request", "thinking_level must be LOW or MEDIUM (MINIMAL is invalid on 3.8)", code="bad_thinking_level", billed="no")
    if not rules or len(rules) > MAX_RULES_PER_CALL or len({r.rule_id for r in rules}) != len(rules):
        raise ProviderError(PROVIDER, "bad_request", f"1..{MAX_RULES_PER_CALL} distinct rules per call", code="bad_rules", billed="no")
    if not png.startswith(PNG_MAGIC):
        raise ProviderError(PROVIDER, "bad_request", "the judged image must be a PNG", code="not_png", billed="no")


def validate_image_args(prompt: str, images: list[bytes], aspect: str, size: str) -> None:
    """Pre-flight of ``generate`` (shared with the mock)."""
    import re
    if size != "1K":
        raise ProviderError(PROVIDER, "bad_request", "image size must be '1K'", code="bad_size", billed="no")
    if not re.fullmatch(r"\d{1,2}:\d{1,2}", aspect or ""):
        raise ProviderError(PROVIDER, "bad_request", f"aspect {aspect!r} must look like '1:1'", code="bad_aspect", billed="no")
    if not prompt.strip() or len(images) > 14:
        raise ProviderError(PROVIDER, "bad_request", "a prompt and at most 14 reference images are required", code="bad_input", billed="no")
    for im in images:
        if not im.startswith(PNG_MAGIC):
            raise ProviderError(PROVIDER, "bad_request", "reference images must be PNG", code="not_png", billed="no")


def check_privacy(private: bool, key_billed: bool) -> None:
    """CHK-P11: an image marked private goes to Gemini only when its key is marked billed."""
    if private and not key_billed:
        raise ProviderError(PROVIDER, "permission", "a private image may only go to Gemini through a billed key", code="private_image_unbilled_key",
                            billed="no", user_hint="This image is private. Mark the Gemini key as billed in Settings, or skip the Gemini step.")


class GeminiProvider:
    """Real Gemini provider (both protocols). ``transport`` is for tests."""

    name = PROVIDER

    def __init__(self, api_key: str, *, transport: Any = None, flags: CapabilityFlags | None = None, limiter: RateLimiter | None = None,
                 cost_sink: Callable[[dict[str, Any]], None] | None = None, key_billed: bool = False, base_url: str = BASE_URL,
                 judge_model: str = JUDGE_MODEL, image_model: str = IMAGE_MODEL, sleep: Callable[[float], None] | None = None,
                 server_retries: int = 2, timeout: float = 120.0) -> None:
        for m in (judge_model, image_model):
            if m.endswith("-latest") or "-latest-" in m:
                raise ValueError("model aliases (-latest) are not allowed: pin a model id")
        self._client = make_client(timeout=timeout, transport=transport, headers={"x-goog-api-key": api_key})
        self.base_url = base_url.rstrip("/")
        self.flags = flags or CapabilityFlags()
        self.limiter = limiter or limiter_for(PROVIDER)
        self.cost_sink = cost_sink
        self.key_billed = key_billed
        self.judge_model = judge_model
        self.image_model = image_model
        import time as _time
        self._sleep = sleep or _time.sleep
        self.server_retries = server_retries
        self.last_body: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return "GeminiProvider()"

    # ----- HTTP ---------------------------------------------------------------------------------------------
    def _generate(self, model: str, body: dict[str, Any], ctx: CallCtx) -> tuple[dict[str, Any], str | None]:
        url = f"{self.base_url}/models/{model}:generateContent"
        self.last_body = body
        attempt = 0
        while True:
            try:
                with self.limiter.acquire(ctx=ctx):
                    ctx.tick()
                    resp = self._client.post(url, json=body)
            except httpx.TimeoutException:
                raise ProviderError(PROVIDER, "timeout", "the request to Gemini timed out", retryable=True) from None
            except httpx.TransportError as e:
                raise ProviderError(PROVIDER, "network", f"could not reach Gemini ({type(e).__name__})", retryable=True) from None
            rid = resp.headers.get("x-goog-request-id") or resp.headers.get("x-request-id")
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except ValueError:
                    raise ProviderError(PROVIDER, "validation", "Gemini returned a body that is not JSON", code="not_json", request_id=rid) from None
                return (data if isinstance(data, dict) else {}), rid
            err = self._map_error(resp, rid)
            if err.kind in ("rate_limit", "overloaded", "server") and attempt < self.server_retries:
                wait = err.retry_after_s if err.retry_after_s is not None else backoff_delay(attempt)
                if err.kind == "rate_limit":
                    self.limiter.penalize(wait)
                sleep_checked(wait, ctx, sleep=self._sleep)
                attempt += 1
                continue
            raise err

    @staticmethod
    def _map_error(resp: Any, rid: str | None) -> ProviderError:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        e = data.get("error", {}) if isinstance(data, dict) else {}
        msg = str(e.get("message") or f"HTTP {resp.status_code}")
        status = str(e.get("status") or "")
        low = msg.lower()
        err = error_from_status(PROVIDER, resp.status_code, msg, headers=dict(resp.headers), code=status or None, request_id=rid)
        if resp.status_code == 400 and ("api key not valid" in low or "api_key_invalid" in low):
            return ProviderError(PROVIDER, "auth", msg, http=400, code=status or None, request_id=rid, billed="no")
        if resp.status_code == 503 or status == "UNAVAILABLE":
            return ProviderError(PROVIDER, "overloaded", msg, http=resp.status_code, code=status or None, retryable=True, request_id=rid, billed="no")
        if resp.status_code == 429 or status == "RESOURCE_EXHAUSTED":
            return ProviderError(PROVIDER, "rate_limit", msg, http=resp.status_code, code=status or None, retryable=True, request_id=rid,
                                 billed="no", retry_after_s=err.retry_after_s)
        if err.kind in ("bad_request", "auth", "permission", "not_found"):
            err.billed = "no"
        return err

    def _record(self, cost: dict[str, Any]) -> None:
        if self.cost_sink is not None:
            try:
                self.cost_sink(cost)
            except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide the result
                pass

    def _privacy(self, private: bool) -> None:
        check_privacy(private, self.key_billed)

    def test_key(self) -> dict[str, Any]:
        """Settings "Test key": ``GET /models/{judge model}`` (free)."""
        try:
            with self.limiter.acquire():
                resp = self._client.get(f"{self.base_url}/models/{self.judge_model}")
        except httpx.TimeoutException:
            return {"ok": False, "message": "Gemini took too long to answer.", "kind": "timeout"}
        except httpx.TransportError:
            return {"ok": False, "message": "Could not reach Gemini.", "kind": "network"}
        if resp.status_code >= 400:
            err = self._map_error(resp, None)
            return {"ok": False, "message": err.user_message, "kind": err.kind}
        return {"ok": True, "message": f"The Gemini key works ({self.judge_model})."}

    # ----- G1: judge ----------------------------------------------------------------------------------------
    def judge(self, png: bytes, rules: list[RuleSpec], *, thinking_level: Literal["LOW", "MEDIUM"], ctx: CallCtx,
              private: bool = False) -> list[RuleVerdict]:
        """One image, up to five rules. Returns one verdict per requested rule, in order; unanswered rules FAIL."""
        validate_judge_args(png, rules, thinking_level)
        self._privacy(private)
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [_inline(png, "image/png"), {"text": rules_text(rules)}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": JUDGE_SCHEMA,
                                 "thinkingConfig": {"thinkingLevel": thinking_level}, "mediaResolution": "MEDIA_RESOLUTION_HIGH"},
        }
        wanted = [r.rule_id for r in rules]
        answers: dict[str, RuleVerdict] = {}
        why = "no answer"
        for attempt in range(2):                                    # an empty or incomplete answer is retried once
            data, rid = self._generate(self.judge_model, body, ctx)
            usage = data.get("usageMetadata")
            self._record(gemini_cost(self.judge_model, usage, operation="generateContent:judge", request_id=rid))
            text = extract_text(data).strip()
            if not text:
                why = "empty response"
                continue
            try:
                checks = json.loads(text).get("checks", [])
            except (ValueError, AttributeError):
                why = "unparseable response"
                continue
            for c in checks if isinstance(checks, list) else []:
                if isinstance(c, dict) and c.get("rule_id") in wanted and c["rule_id"] not in answers and isinstance(c.get("pass"), bool):
                    answers[c["rule_id"]] = RuleVerdict(rule_id=c["rule_id"], evidence=str(c.get("evidence", "")), passed=c["pass"])
            if len(answers) == len(wanted):
                break
            why = "rules missing from the answer"
        return [answers.get(rid) or RuleVerdict(rule_id=rid, evidence=f"FAIL: {why}", passed=False) for rid in wanted]

    # ----- G2: Nano Banana 2 --------------------------------------------------------------------------------
    def generate(self, *, prompt: str, images: list[bytes], aspect: str, size: Literal["1K"], ctx: CallCtx,
                 private: bool = False) -> tuple[bytes, Literal["png", "jpeg"], dict]:
        """One backup image. Returns ``(bytes, "png" | "jpeg", meta)``; never alpha; the caller keeps JPEGs out of final textures."""
        validate_image_args(prompt, images, aspect, size)
        self._privacy(private)
        body = {"contents": [{"role": "user", "parts": [*[_inline(im, "image/png") for im in images], {"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect, "imageSize": size},
                                     "thinkingConfig": {"thinkingLevel": "MINIMAL"}}}
        data, rid = self._generate(self.image_model, body, ctx)
        cands = data.get("candidates") or []
        fb = data.get("promptFeedback") or {}
        if not cands:
            reason = str(fb.get("blockReason") or "NO_CANDIDATES")
            raise moderation_error(reason, request_id=rid)
        cand = cands[0] or {}
        finish = str(cand.get("finishReason") or "")
        if finish in MODERATION_REASONS:
            raise moderation_error(finish, request_id=rid)
        if finish in RECITATION_REASONS:
            raise recitation_error(finish, request_id=rid)
        parts = [p for p in ((cand.get("content") or {}).get("parts") or [])
                 if isinstance(p, dict) and p.get("inlineData") and not p.get("thought")]
        if not parts:
            raise ProviderError(PROVIDER, "other", f"Gemini returned no image ({finish or 'no finish reason'})", code=finish or "NO_IMAGE",
                                retryable=finish in ("", "NO_IMAGE", "IMAGE_OTHER", "OTHER"), billed="unknown", request_id=rid)
        try:
            raw = base64.b64decode(parts[-1]["inlineData"].get("data", ""), validate=False)
        except (binascii.Error, ValueError):
            raise ProviderError(PROVIDER, "validation", "the image data could not be decoded", code="bad_b64", billed="yes", request_id=rid) from None
        kind = sniff_kind(raw)
        if kind not in ("png", "jpeg"):
            raise ProviderError(PROVIDER, "validation", f"unexpected image format ({kind})", code=f"bad_magic_{kind}", billed="yes", request_id=rid)
        cost = gemini_cost(self.image_model, data.get("usageMetadata"), operation="generateContent:image", image=True, request_id=rid)
        self._record(cost)
        meta = {"model": self.image_model, "finish_reason": finish or None, "synthid": True, "request_id": rid, "cost": cost,
                "usage": data.get("usageMetadata"), "has_alpha": False}
        return raw, kind, meta  # type: ignore[return-value]


__all__ = [
    "BASE_URL", "IMAGE_MODEL", "JUDGE_MODEL", "JUDGE_SCHEMA", "BackupImageProvider", "GeminiProvider", "JudgeProvider", "RuleSpec",
    "RuleVerdict", "check_privacy", "extract_text", "rules_text", "validate_image_args", "validate_judge_args",
]
