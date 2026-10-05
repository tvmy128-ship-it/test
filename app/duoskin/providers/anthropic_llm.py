"""Claude adapter (APP_SPEC 7.2, bible 2.8 and 9.0).

One function does every model call: ``AnthropicProvider.call(route, system=..., content=..., out=Model, ...)``.

* Every call streams (``client.messages.stream``; Opus routes use ``client.beta.messages.stream`` with
  ``betas=["server-side-fallback-2026-07-01"]`` and ``fallbacks="default"``; Sonnet routes only when the
  ``anthropic.sonnet_fallbacks`` capability flag says the account allows it).
* Thinking is adaptive (``display="summarized"``, for the UI only); effort comes from the route table.
  ``temperature``, ``top_p``, ``top_k``, ``budget_tokens`` and an assistant prefill are never sent (a pre-flight
  assertion rejects them), and ``messages.parse()`` is never used (X18): the adapter validates the text itself so
  ``stop_reason`` is read first.
* Structured output: ``output_config.format`` carries ``make_all_required(anthropic.transform_schema(Model))``.
* ``stop_reason`` ``refusal`` raises ``RefusalError``; ``max_tokens`` / ``model_context_window_exceeded`` raise
  ``kind="truncated"`` (the engine retries once with ``max_tokens_override`` doubled); a missing text block is
  ``truncated`` too; a Pydantic failure is ``kind="validation"``. Every outcome after the request was accepted
  carries the call's ``cost`` (also pushed to ``cost_sink``), because the tokens were billed.
* Requested and served model are both logged on the result; a cache monitor raises a SOFT alert (CHK-X04).
"""
from __future__ import annotations

import base64
import concurrent.futures
import copy
import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from duoskin.checks.model import CheckResult, not_run
from duoskin.providers.base import (
    PNG_MAGIC,
    CallCtx,
    Cancelled,
    CapabilityFlags,
    ProviderError,
    RateLimiter,
    limiter_for,
    png_size,
    request_hash,
    scrub,
)
from duoskin.providers.pricing import claude_cost

T = TypeVar("T", bound=BaseModel)

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS_BETA = "server-side-fallback-2026-06-01"
FILES_BETA = "files-api-2025-04-14"
MIN_IMAGE_SIDE = 256
MAX_IMAGE_EDGE = 2576
MAX_IMAGE_EDGE_MANY = 2000      # when the request carries more than 20 images
FORBIDDEN_PARAMS = ("temperature", "top_p", "top_k", "budget_tokens")

Route = Literal["L1_reference", "L2_taste", "L3_planner", "L4_critic", "L5_pairwise", "L6_reviser", "L7_change",
                "L9_hair_match", "L10_repair", "L11_checker", "L12_duo_judge", "L12_duo_review", "L13_ip",
                "L14_similarity", "smoke"]


@dataclass(frozen=True)
class RouteCfg:
    model: str
    effort: str
    max_tokens: int
    fallbacks: bool
    thinking: bool = True       # False only for the schema smoke test (CHK-S10)


def build_routes(*, planner: str = "claude-opus-5", critic: str = "claude-opus-5", judge: str = "claude-opus-5",
                 checker: str = "claude-sonnet-5") -> dict[str, RouteCfg]:
    """The route table of bible 9.0. Model ids come from the project's ``VersionPins`` (planner, critic, judge,
    checker)."""
    return {
        "L1_reference": RouteCfg(planner, "high", 32000, True),
        "L2_taste": RouteCfg(checker, "medium", 16000, False),
        "L3_planner": RouteCfg(planner, "high", 64000, True),
        "L4_critic": RouteCfg(critic, "medium", 32000, True),
        "L5_pairwise": RouteCfg(critic, "medium", 32000, True),
        "L6_reviser": RouteCfg(planner, "medium", 32000, True),
        "L7_change": RouteCfg(planner, "medium", 32000, True),
        "L9_hair_match": RouteCfg(checker, "medium", 16000, False),
        "L10_repair": RouteCfg(checker, "medium", 16000, False),
        "L11_checker": RouteCfg(checker, "medium", 16000, False),
        "L12_duo_judge": RouteCfg(judge, "high", 32000, True),
        "L12_duo_review": RouteCfg(judge, "high", 32000, True),
        "L13_ip": RouteCfg(judge, "high", 16000, True),
        "L14_similarity": RouteCfg(judge, "high", 16000, True),
        "smoke": RouteCfg(checker, "low", 2048, False, thinking=False),
    }


ROUTES: dict[str, RouteCfg] = build_routes()


# --------------------------------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------------------------------

def make_all_required(schema: dict[str, Any]) -> dict[str, Any]:
    """Deep copy of ``schema`` with ``required`` set to every property of every object and
    ``additionalProperties: false`` (structured outputs reject optional fields)."""
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = list(props.keys())
            node.setdefault("additionalProperties", False)
            for sub in props.values():
                walk(sub)
        for key in ("items", "additionalProperties", "not", "if", "then", "else"):
            if isinstance(node.get(key), dict):
                walk(node[key])
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            if isinstance(node.get(key), list):
                for sub in node[key]:
                    walk(sub)
        for key in ("$defs", "definitions"):
            if isinstance(node.get(key), dict):
                for sub in node[key].values():
                    walk(sub)

    walk(out)
    return out


def count_unions_and_optionals(schema: dict[str, Any]) -> dict[str, int]:
    """LLM-04 helper: number of ``anyOf``/``oneOf`` nodes and of optional properties in ``schema``."""
    unions = optionals = 0

    def walk(node: Any) -> None:
        nonlocal unions, optionals
        if isinstance(node, dict):
            if "anyOf" in node or "oneOf" in node:
                unions += 1
            props = node.get("properties")
            if isinstance(props, dict):
                optionals += len(set(props) - set(node.get("required", [])))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return {"anyOf": unions, "optional": optionals}


class SchemaCache:
    """``SCHEMA_CACHE[M] = make_all_required(anthropic.transform_schema(M))``, built once per class.

    ``version`` (the kit manifest sha) invalidates every entry when it changes, so kit-enum schemas are rebuilt
    only when the inventory changes."""

    def __init__(self) -> None:
        self._d: dict[Any, tuple[dict[str, Any], str]] = {}
        self._lock = threading.Lock()
        self.version = ""

    def set_version(self, version: str) -> None:
        with self._lock:
            if version != self.version:
                self.version = version
                self._d.clear()

    def get(self, model: type[BaseModel] | dict[str, Any]) -> tuple[dict[str, Any], str]:
        """(schema, schema_hash)."""
        key: Any = model if isinstance(model, type) else request_hash(model)
        with self._lock:
            hit = self._d.get(key)
        if hit is not None:
            return hit
        import anthropic
        raw = anthropic.transform_schema(model)
        schema = make_all_required(raw)
        entry = (schema, request_hash(schema))
        with self._lock:
            self._d[key] = entry
        return entry

    def __getitem__(self, model: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
        return self.get(model)[0]

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


SCHEMA_CACHE = SchemaCache()


# --------------------------------------------------------------------------------------------------------------
# Content helpers (images to Claude, cache control)
# --------------------------------------------------------------------------------------------------------------

def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_block(png: bytes) -> dict[str, Any]:
    """A base64 PNG image block. Only PNG goes to Claude."""
    if not png.startswith(PNG_MAGIC):
        raise ProviderError(PROVIDER, "bad_request", "images sent to Claude must be PNG", code="not_png", billed="no")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode("ascii")}}


def file_image_block(file_id: str) -> dict[str, Any]:
    """An image by Files API id (the cached style references)."""
    return {"type": "image", "source": {"type": "file", "file_id": file_id}}


def with_cache_control(blocks: Sequence[dict[str, Any]], *, ttl: Literal["5m", "1h"] = "5m") -> list[dict[str, Any]]:
    """Copy of ``blocks`` with ``cache_control`` on the last one (``ttl="1h"`` while a gate is open)."""
    out = [dict(b) for b in blocks]
    if out:
        cc: dict[str, Any] = {"type": "ephemeral"}
        if ttl == "1h":
            cc["ttl"] = "1h"
        out[-1]["cache_control"] = cc
    return out


def prepare_judge_images(png: bytes, *, many_images: bool = False) -> dict[str, bytes]:
    """The two renderings a judge gets for one RGBA asset: ``{"grey": ..., "checker": ...}``.

    The asset is composited on ``#808080`` and on an 8 px checkerboard (the prompt says which is which), enlarged
    with nearest-neighbour so each side is at least 256 px, and shrunk if the long edge exceeds 2576 px (2000 px
    when the request carries more than 20 images). PNG out."""
    import io

    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    scale = max(1, -(-MIN_IMAGE_SIDE // min(w, h)))
    if scale > 1:
        im = im.resize((w * scale, h * scale), Image.NEAREST)
    cap = MAX_IMAGE_EDGE_MANY if many_images else MAX_IMAGE_EDGE
    if max(im.size) > cap:
        f = cap / max(im.size)
        im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
    W, H = im.size
    yy, xx = np.mgrid[0:H, 0:W]
    checker = np.where(((xx // 8) + (yy // 8)) % 2 == 0, 204, 153).astype(np.uint8)
    checker_rgb = np.stack([checker] * 3, axis=-1)
    outs: dict[str, bytes] = {}
    for name, bg in (("grey", Image.new("RGB", (W, H), (128, 128, 128))), ("checker", Image.fromarray(checker_rgb, "RGB"))):
        canvas = bg.convert("RGBA")
        canvas.alpha_composite(im)
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, "PNG")
        outs[name] = buf.getvalue()
    return outs


def check_content_images(content: Sequence[dict[str, Any]]) -> None:
    """CHK-P05 pre-flight: base64 images are PNG, at least 256 px per side, long edge within the cap."""
    imgs = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]
    cap = MAX_IMAGE_EDGE_MANY if len(imgs) > 20 else MAX_IMAGE_EDGE
    for b in imgs:
        src = b.get("source", {})
        if src.get("type") != "base64":
            continue
        if src.get("media_type") != "image/png":
            raise ProviderError(PROVIDER, "bad_request", "only PNG images may be sent to Claude", code="not_png", billed="no")
        data = base64.b64decode(src.get("data", ""), validate=False)
        size = png_size(data)
        if size is None:
            raise ProviderError(PROVIDER, "bad_request", "image block is not a PNG", code="not_png", billed="no")
        if min(size) < MIN_IMAGE_SIDE:
            raise ProviderError(PROVIDER, "bad_request", f"image {size[0]}x{size[1]} is smaller than {MIN_IMAGE_SIDE}px on a side",
                                code="image_too_small", billed="no",
                                user_hint="An image for the judge was too small; the app should have enlarged it.")
        if max(size) > cap:
            raise ProviderError(PROVIDER, "bad_request", f"image long edge {max(size)} exceeds {cap}px", code="image_too_large", billed="no")


def _count_cache_breakpoints(system: Sequence[dict[str, Any]], content: Sequence[dict[str, Any]]) -> int:
    return sum(1 for b in (*system, *content) if isinstance(b, dict) and "cache_control" in b)


def _uses_file_source(content: Sequence[dict[str, Any]]) -> bool:
    return any(isinstance(b, dict) and isinstance(b.get("source"), dict) and b["source"].get("type") == "file" for b in content)


# --------------------------------------------------------------------------------------------------------------
# Results and protocol
# --------------------------------------------------------------------------------------------------------------

@dataclass
class LLMResult(Generic[T]):
    parsed: T                                  # validated model (never None on return; errors raise ProviderError)
    stop_reason: str                           # always "end_turn" on success
    raw_text: str
    usage: dict[str, int]                      # input, output, cache_read_input_tokens, cache_creation_input_tokens
    request_id: str | None
    requested_model: str
    served_model: str                          # different when a fallback answered
    thinking_summary: str                      # display="summarized"; UI only
    schema_hash: str
    prompt_version: int
    cost: dict[str, Any] | None = None         # CostEntry-like (basis="usage")
    route: str = ""

    @property
    def fallback_used(self) -> bool:
        return bool(self.served_model) and self.served_model != self.requested_model


@dataclass
class BatchItem:
    custom_id: str
    route: str
    system: list[dict[str, Any]]
    content: list[dict[str, Any]]
    out: type[BaseModel]
    prompt_version: int = 1


@dataclass
class BatchItemResult:
    """``status`` is ``succeeded`` only for a usable result; anything else must be re-run through ``call()``."""
    custom_id: str
    status: Literal["succeeded", "errored", "canceled", "expired", "refused", "truncated", "invalid"]
    parsed: BaseModel | None = None
    raw_text: str = ""
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    served_model: str | None = None
    error: str | None = None
    cost: dict[str, Any] | None = None

    @property
    def rerun(self) -> bool:
        return self.status != "succeeded"


class LLMProvider(Protocol):
    def call(self, route: str, *, system: list[dict], content: list[dict], out: type[T], ctx: CallCtx,
             prompt_version: int, max_tokens_override: int | None = None) -> LLMResult[T]: ...
    def upload_file(self, data: bytes, *, name: str, mime: str) -> str: ...
    def delete_file(self, file_id: str) -> None: ...
    def batch_submit(self, items: list[BatchItem]) -> str: ...
    def batch_status(self, batch_id: str) -> Literal["in_progress", "ended", "canceled", "expired"]: ...
    def batch_results(self, batch_id: str) -> Iterator[BatchItemResult]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def allowed_fallbacks(self, model: str) -> list[str]: ...
    def smoke_test(self, route: str, out: type[BaseModel]) -> CheckResult: ...


# --------------------------------------------------------------------------------------------------------------
# Cache monitor (CHK-X04, SOFT)
# --------------------------------------------------------------------------------------------------------------

class CacheMonitor:
    """Alert when the second call of a route inside the cache TTL reads nothing from the cache."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 on_alert: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._clock = clock
        self._last: dict[str, float] = {}
        self.alerts: list[dict[str, Any]] = []
        self.on_alert = on_alert
        self._lock = threading.Lock()

    def observe(self, route: str, usage: dict[str, int], *, ttl: str = "5m") -> dict[str, Any] | None:
        now = self._clock()
        ttl_s = 3600.0 if ttl == "1h" else 300.0
        alert = None
        with self._lock:
            last = self._last.get(route)
            if last is not None and now - last <= ttl_s and not usage.get("cache_read_input_tokens", 0):
                alert = {"check_id": "CHK-X04", "kind": "soft", "route": route,
                         "message": f"second call of {route} inside the cache TTL read 0 cached tokens"}
                self.alerts.append(alert)
            self._last[route] = now
        if alert and self.on_alert:
            self.on_alert(alert)
        return alert


# --------------------------------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------------------------------

def refusal_error(stop_details: Any, request_id: str | None, cost: dict[str, Any] | None = None) -> ProviderError:
    category = getattr(stop_details, "category", None) or (stop_details.get("category") if isinstance(stop_details, dict) else None)
    detail = getattr(stop_details, "explanation", None) or (stop_details.get("explanation") if isinstance(stop_details, dict) else None)
    msg = f"Claude declined the request ({category or 'no category'})" + (f": {detail}" if detail else "")
    return ProviderError(PROVIDER, "refusal", msg, code=str(category) if category else None, request_id=request_id,
                         billed="yes", cost=cost,
                         user_hint=f"Claude declined: {category or 'this request'}. Reword it or change the design.")


def truncated_error(stop_reason: str, request_id: str | None, cost: dict[str, Any] | None = None) -> ProviderError:
    return ProviderError(PROVIDER, "truncated", f"stop_reason {stop_reason}: the model ran out of room", code=stop_reason,
                         retryable=True, request_id=request_id, billed="yes", cost=cost)


def map_sdk_error(e: BaseException) -> ProviderError:
    """Map an ``anthropic`` SDK exception to a ``ProviderError`` (shared with the mock's fault injection)."""
    import anthropic

    if isinstance(e, ProviderError):
        return e
    if isinstance(e, anthropic.APIStatusError):
        status = getattr(e, "status_code", None) or 0
        body = e.body if isinstance(e.body, dict) else {}
        err = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
        msg = str(err.get("message") or e.message or f"HTTP {status}")
        etype = str(err.get("type") or "")
        low = msg.lower()
        rid = getattr(e, "request_id", None)
        headers = getattr(getattr(e, "response", None), "headers", None)
        from duoskin.providers.base import parse_retry_after
        retry_after = parse_retry_after(dict(headers) if headers is not None else None)
        if "schema is too complex" in low or "too complex for compilation" in low:
            return ProviderError(PROVIDER, "schema_too_complex", msg, http=status, code="schema_too_complex", request_id=rid,
                                 billed="no", user_hint="Planner schema too large for the kit; switched to checked strings.")
        if status == 402 or "credit balance" in low or etype == "billing_error":
            return ProviderError(PROVIDER, "billing", msg, http=status, code=etype or None, request_id=rid, billed="no")
        if status == 401:
            return ProviderError(PROVIDER, "auth", msg, http=status, code=etype or None, request_id=rid, billed="no")
        if status == 403:
            return ProviderError(PROVIDER, "permission", msg, http=status, code=etype or None, request_id=rid, billed="no",
                                 user_hint="The Anthropic key has no access to this model. Check the key's workspace and model access.")
        if status == 404:
            return ProviderError(PROVIDER, "not_found", msg, http=status, code=etype or None, request_id=rid, billed="no",
                                 user_hint="Anthropic does not know this model ID. Update the model ID in Settings.")
        if status == 413:
            return ProviderError(PROVIDER, "bad_request", msg, http=status, code="request_too_large", request_id=rid, billed="no",
                                 user_hint="The request was too large. The app should downscale images or use file IDs.")
        if status == 429:
            return ProviderError(PROVIDER, "rate_limit", msg, http=status, code=etype or None, retryable=True, request_id=rid,
                                 billed="no", retry_after_s=retry_after)
        if status == 529 or etype == "overloaded_error":
            return ProviderError(PROVIDER, "overloaded", msg, http=status, code=etype or None, retryable=True, request_id=rid,
                                 billed="no", retry_after_s=retry_after)
        if status >= 500:
            return ProviderError(PROVIDER, "server", msg, http=status, code=etype or None, retryable=True, request_id=rid)
        return ProviderError(PROVIDER, "bad_request", msg, http=status, code=etype or None, request_id=rid, billed="no")
    if isinstance(e, anthropic.APITimeoutError):
        return ProviderError(PROVIDER, "timeout", "the request to Anthropic timed out", retryable=True)
    if isinstance(e, anthropic.APIConnectionError):
        return ProviderError(PROVIDER, "network", f"could not reach Anthropic ({type(e).__name__})", retryable=True)
    return ProviderError(PROVIDER, "other", f"{type(e).__name__}: {scrub(e)}")


def finish_call(*, route: str, cfg: RouteCfg, out: type[T], text: str | None, stop: str | None, rid: str | None, usage: dict[str, int],
                served: str, cost: dict[str, Any], schema_hash: str, prompt_version: int, thinking_summary: str = "",
                stop_details: Any = None) -> LLMResult[T]:
    """Everything after the model answered (shared with the mock): branch on ``stop_reason`` first, then take the text
    block, then validate. Raises ``refusal`` / ``truncated`` / ``validation``; the call's ``cost`` rides on the error."""
    if stop == "refusal":                                    # branch on stop_reason, never on stop_details
        raise refusal_error(stop_details, rid, cost)
    if stop in ("max_tokens", "model_context_window_exceeded"):
        raise truncated_error(stop, rid, cost)
    if text is None:
        raise ProviderError(PROVIDER, "truncated", "the response has no text block", code="no_text", retryable=True,
                            request_id=rid, billed="yes", cost=cost)
    try:
        parsed = out.model_validate_json(text)               # enums are lowercased by the models' BeforeValidator
    except ValidationError as ve:
        raw = ve.json(include_url=False)
        err = ProviderError(PROVIDER, "validation", raw, request_id=rid, billed="yes", cost=cost, code="schema_violation")
        try:
            err.context["errors"] = json.loads(raw)
        except ValueError:
            pass
        err.context["raw_text"] = text
        raise err from None
    return LLMResult(parsed=parsed, stop_reason=stop or "end_turn", raw_text=text, usage=usage, request_id=rid,
                     requested_model=cfg.model, served_model=served, thinking_summary=thinking_summary[:8000],
                     schema_hash=schema_hash, prompt_version=prompt_version, cost=cost, route=route)


class AnthropicProvider:
    """Real Claude provider. Build it with a key (or an injected ``client``) and call ``call()``.

    ``http_client`` is for tests: ``anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))``.
    The key is read by the caller (``keystore.get_key``), kept only inside the SDK client and never logged.
    """

    name = PROVIDER

    def __init__(self, api_key: str | None = None, *, client: Any = None, http_client: Any = None,
                 routes: dict[str, RouteCfg] | None = None, flags: CapabilityFlags | None = None,
                 limiter: RateLimiter | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 max_retries: int = 2, timeout: float = 900.0, schema_cache: SchemaCache | None = None,
                 cache_monitor: CacheMonitor | None = None) -> None:
        if client is None:
            import anthropic
            kw: dict[str, Any] = {"api_key": api_key, "max_retries": max_retries, "timeout": timeout}
            if http_client is not None:
                kw["http_client"] = http_client
            client = anthropic.Anthropic(**kw)
        self.client = client
        self.routes: dict[str, RouteCfg] = dict(routes or ROUTES)
        self.flags = flags or CapabilityFlags()
        self.limiter = limiter or limiter_for(PROVIDER)
        self.cost_sink = cost_sink
        self.schemas = schema_cache or SCHEMA_CACHE
        self.monitor = cache_monitor or CacheMonitor()
        self.last_request: dict[str, Any] | None = None     # the last request kwargs, for tests and diagnostics
        self._batch_items: dict[str, dict[str, BatchItem]] = {}

    def __repr__(self) -> str:
        return f"AnthropicProvider(routes={len(self.routes)})"

    def with_pins(self, pins: Any) -> AnthropicProvider:
        """A view of this provider that uses the model ids of a project's ``VersionPins`` (shares client, limiter,
        flags and monitor)."""
        clone = copy.copy(self)
        clone.routes = build_routes(planner=pins.planner, critic=pins.critic, judge=pins.judge, checker=pins.checker)
        return clone

    # ----- request building ---------------------------------------------------------------------------------
    def use_fallbacks(self, cfg: RouteCfg) -> bool:
        """Opus routes always; Sonnet routes only when the account allows server-side fallbacks (flag)."""
        if cfg.fallbacks:
            return True
        return "sonnet" in cfg.model.lower() and bool(self.flags.get("anthropic.sonnet_fallbacks"))

    def build_request(self, route: str, *, system: Sequence[dict[str, Any]], content: Sequence[dict[str, Any]],
                      schema: dict[str, Any], max_tokens_override: int | None = None,
                      cfg_override: RouteCfg | None = None) -> dict[str, Any]:
        """The kwargs of the streaming call (no network). Never contains sampling params, budget_tokens or a prefill."""
        if cfg_override is None and route not in self.routes:
            raise ProviderError(PROVIDER, "bad_request", f"unknown route {route!r}", billed="no")
        cfg = cfg_override or self.routes[route]
        kw: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": int(max_tokens_override or cfg.max_tokens),
            "thinking": ({"type": "adaptive", "display": "summarized"} if cfg.thinking else {"type": "disabled"}),
            "output_config": {"effort": cfg.effort, "format": {"type": "json_schema", "schema": schema}},
            "system": list(system),
            "messages": [{"role": "user", "content": list(content)}],
        }
        self.preflight(kw, content)
        return kw

    @staticmethod
    def preflight(kw: dict[str, Any], content: Sequence[dict[str, Any]] = ()) -> None:
        """CHK-P05: no forbidden params, no prefill, valid images, at most four cache breakpoints."""
        for k in FORBIDDEN_PARAMS:
            if k in kw:
                raise ProviderError(PROVIDER, "bad_request", f"{k} must never be sent to these models", code="forbidden_param", billed="no")
        thinking = kw.get("thinking") or {}
        if "budget_tokens" in thinking:
            raise ProviderError(PROVIDER, "bad_request", "budget_tokens must never be sent", code="forbidden_param", billed="no")
        effort = (kw.get("output_config") or {}).get("effort")
        if thinking.get("type") == "disabled" and effort in ("xhigh", "max"):
            raise ProviderError(PROVIDER, "bad_request", "thinking disabled with effort xhigh/max is rejected", code="forbidden_param", billed="no")
        msgs = kw.get("messages") or []
        if not msgs or msgs[-1].get("role") != "user":
            raise ProviderError(PROVIDER, "bad_request", "the last message must be the user's (no assistant prefill)", code="prefill", billed="no")
        if not (msgs[-1].get("content")):
            raise ProviderError(PROVIDER, "bad_request", "empty user content", code="empty_content", billed="no")
        check_content_images(content)
        if _count_cache_breakpoints(kw.get("system") or [], content) > 4:
            raise ProviderError(PROVIDER, "bad_request", "more than 4 cache breakpoints", code="cache_breakpoints", billed="no")

    @staticmethod
    def _usage_dict(u: Any) -> dict[str, int]:
        d = {"input": int(getattr(u, "input_tokens", 0) or 0), "output": int(getattr(u, "output_tokens", 0) or 0),
             "cache_read_input_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
             "cache_creation_input_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0)}
        cc = getattr(u, "cache_creation", None)
        if cc is not None:
            d["cache_creation_5m"] = int(getattr(cc, "ephemeral_5m_input_tokens", 0) or 0)
            d["cache_creation_1h"] = int(getattr(cc, "ephemeral_1h_input_tokens", 0) or 0)
        return d

    @staticmethod
    def _cache_ttl(system: Sequence[dict[str, Any]]) -> str:
        return "1h" if any((b.get("cache_control") or {}).get("ttl") == "1h" for b in system if isinstance(b, dict)) else "5m"

    def _record(self, cost: dict[str, Any]) -> None:
        if self.cost_sink is not None:
            try:
                self.cost_sink(cost)
            except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide the call's result
                pass

    # ----- the one call function ----------------------------------------------------------------------------
    def call(self, route: str, *, system: list[dict], content: list[dict], out: type[T], ctx: CallCtx,
             prompt_version: int, max_tokens_override: int | None = None) -> LLMResult[T]:
        return self._call(route, system=system, content=content, out=out, ctx=ctx, prompt_version=prompt_version,
                          max_tokens_override=max_tokens_override, on_first_event=None)

    def _call(self, route: str, *, system: Sequence[dict[str, Any]], content: Sequence[dict[str, Any]],
              out: type[T], ctx: CallCtx, prompt_version: int, max_tokens_override: int | None,
              on_first_event: Callable[[], None] | None, cfg_override: RouteCfg | None = None) -> LLMResult[T]:
        cfg = cfg_override or self.routes.get(route)
        if cfg is None:
            raise ProviderError(PROVIDER, "bad_request", f"unknown route {route!r}", billed="no")
        schema, schema_hash = self.schemas.get(out)
        kw = self.build_request(route, system=system, content=content, schema=schema, max_tokens_override=max_tokens_override,
                                cfg_override=cfg_override)
        self.last_request = kw
        betas: list[str] = []
        if _uses_file_source(content):
            betas.append(FILES_BETA)
        thinking_parts: list[str] = []
        fired = False
        try:
            with self.limiter.acquire(ctx=ctx):
                if self.use_fallbacks(cfg):
                    mgr = self.client.beta.messages.stream(betas=[FALLBACK_BETA, *betas], fallbacks="default", **kw)
                elif betas:
                    mgr = self.client.beta.messages.stream(betas=betas, **kw)
                else:
                    mgr = self.client.messages.stream(**kw)
                with mgr as s:
                    for ev in s:
                        ctx.tick()
                        if not fired:
                            fired = True
                            if on_first_event is not None:
                                on_first_event()
                        if getattr(ev, "type", "") == "content_block_delta" and getattr(ev.delta, "type", "") == "thinking_delta":
                            thinking_parts.append(ev.delta.thinking)
                            if len(thinking_parts) % 8 == 1:
                                ctx.progress(0.0, "Claude is thinking")
                    r = s.get_final_message()
                    rid = getattr(s, "request_id", None) or getattr(r, "_request_id", None)
        except (Cancelled, ProviderError):
            raise
        except Exception as e:  # noqa: BLE001 - every SDK failure is mapped to a ProviderError
            err = map_sdk_error(e)
            if err.kind in ("rate_limit", "overloaded"):
                self.limiter.penalize(err.retry_after_s or 5.0)
            if err.kind == "schema_too_complex":
                self.flags.set(f"anthropic.schema_ok.{route}", False)
            raise err from None

        usage = self._usage_dict(r.usage)
        served = getattr(r, "model", None) or cfg.model
        cost = claude_cost(served, usage, operation=f"messages.stream:{route}", request_id=rid, cache_ttl=self._cache_ttl(system))
        self._record(cost)
        self.monitor.observe(route, usage, ttl=self._cache_ttl(system))
        text = next((b.text for b in r.content if getattr(b, "type", "") == "text"), None)   # thinking blocks come first
        result = finish_call(route=route, cfg=cfg, out=out, text=text, stop=getattr(r, "stop_reason", None), rid=rid, usage=usage,
                             served=served, cost=cost, schema_hash=schema_hash, prompt_version=prompt_version,
                             thinking_summary="".join(thinking_parts), stop_details=getattr(r, "stop_details", None))
        if not self.flags.is_set(f"anthropic.schema_ok.{route}"):
            self.flags.set(f"anthropic.schema_ok.{route}", True)
        return result

    # ----- fan-out ------------------------------------------------------------------------------------------
    def call_fanout(self, route: str, requests: Sequence[dict[str, Any]], *, ctx: CallCtx,
                    max_workers: int | None = None) -> list[LLMResult | ProviderError]:
        """N calls that share a cached prefix: send the first, wait for its first streamed token (a cache entry is
        readable only after the first response starts), then send the other N-1 concurrently.

        Each request dict has ``system``, ``content``, ``out``, ``prompt_version`` and optionally
        ``max_tokens_override``. Returns results in order; a failed call is returned as its ``ProviderError``
        (``Cancelled`` propagates)."""
        if not requests:
            return []
        results: list[Any] = [None] * len(requests)
        first_started = threading.Event()

        def run(i: int, on_first: Callable[[], None] | None) -> None:
            rq = requests[i]
            try:
                results[i] = self._call(route, system=rq["system"], content=rq["content"], out=rq["out"], ctx=ctx,
                                        prompt_version=rq.get("prompt_version", 1),
                                        max_tokens_override=rq.get("max_tokens_override"), on_first_event=on_first)
            except Cancelled as c:
                results[i] = c
            except ProviderError as pe:
                results[i] = pe
            finally:
                if on_first is not None:
                    first_started.set()          # also unblocks the others when the first call failed early

        workers = max_workers or min(8, len(requests))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(run, 0, first_started.set)]
            first_started.wait()
            futs += [pool.submit(run, i, None) for i in range(1, len(requests))]
            for f in futs:
                f.result()
        for r in results:
            if isinstance(r, Cancelled):
                raise r
        return results

    # ----- Files API ----------------------------------------------------------------------------------------
    def upload_file(self, data: bytes, *, name: str, mime: str) -> str:
        try:
            meta = self.client.beta.files.upload(file=(name, data, mime))
        except Exception as e:  # noqa: BLE001
            raise map_sdk_error(e) from None
        return str(meta.id)

    def delete_file(self, file_id: str) -> None:
        try:
            self.client.beta.files.delete(file_id)
        except Exception as e:  # noqa: BLE001
            raise map_sdk_error(e) from None

    # ----- Batches ------------------------------------------------------------------------------------------
    def batch_submit(self, items: list[BatchItem]) -> str:
        """The same params as ``call()`` but WITHOUT ``fallbacks`` (rejected on batches)."""
        reqs = []
        for it in items:
            cfg = self.routes[it.route]
            schema, _ = self.schemas.get(it.out)
            kw = self.build_request(it.route, system=it.system, content=it.content, schema=schema)
            kw.pop("model")
            reqs.append({"custom_id": it.custom_id, "params": {"model": cfg.model, **kw}})
        try:
            b = self.client.messages.batches.create(requests=reqs)
        except Exception as e:  # noqa: BLE001
            raise map_sdk_error(e) from None
        self._batch_items[b.id] = {it.custom_id: it for it in items}
        return str(b.id)

    def batch_status(self, batch_id: str) -> Literal["in_progress", "ended", "canceled", "expired"]:
        try:
            b = self.client.messages.batches.retrieve(batch_id)
        except Exception as e:  # noqa: BLE001
            raise map_sdk_error(e) from None
        if b.processing_status != "ended":
            return "in_progress"
        c = b.request_counts
        total = sum(int(getattr(c, k, 0) or 0) for k in ("succeeded", "errored", "canceled", "expired", "processing"))
        if total and int(getattr(c, "canceled", 0) or 0) == total:
            return "canceled"
        if total and int(getattr(c, "expired", 0) or 0) == total:
            return "expired"
        return "ended"

    def batch_results(self, batch_id: str) -> Iterator[BatchItemResult]:
        """Every result must be ``succeeded`` with a usable ``stop_reason``; anything else is flagged for a
        synchronous re-run through ``call()`` (LLM-09)."""
        items = self._batch_items.get(batch_id, {})
        try:
            stream = self.client.messages.batches.results(batch_id)
        except Exception as e:  # noqa: BLE001
            raise map_sdk_error(e) from None
        for entry in stream:
            cid = entry.custom_id
            res = entry.result
            rtype = getattr(res, "type", "errored")
            if rtype != "succeeded":
                err = getattr(getattr(res, "error", None), "error", None)
                yield BatchItemResult(cid, rtype if rtype in ("errored", "canceled", "expired") else "errored",
                                      error=scrub(getattr(err, "message", rtype)))
                continue
            msg = res.message
            usage = self._usage_dict(msg.usage)
            cost = claude_cost(msg.model, usage, operation=f"messages.batch:{items[cid].route if cid in items else '?'}", batch=True)
            self._record(cost)
            stop = getattr(msg, "stop_reason", None)
            text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
            base = {"custom_id": cid, "raw_text": text, "stop_reason": stop, "usage": usage, "served_model": msg.model, "cost": cost}
            if stop == "refusal":
                yield BatchItemResult(status="refused", **base)
            elif stop in ("max_tokens", "model_context_window_exceeded") or not text:
                yield BatchItemResult(status="truncated", **base)
            else:
                item = items.get(cid)
                if item is None:
                    yield BatchItemResult(status="succeeded", **base)
                    continue
                try:
                    parsed = item.out.model_validate_json(text)
                except ValidationError as ve:
                    yield BatchItemResult(status="invalid", error=scrub(ve.json(include_url=False)), **base)
                    continue
                yield BatchItemResult(status="succeeded", parsed=parsed, **base)

    # ----- Capabilities and smoke test ----------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        """``models.retrieve(id).capabilities`` for every model the route table uses."""
        out: dict[str, Any] = {}
        for model in sorted({c.model for c in self.routes.values()}):
            try:
                info = self.client.models.retrieve(model)
                caps = getattr(info, "capabilities", None)
                out[model] = caps.model_dump() if hasattr(caps, "model_dump") else caps
            except Exception as e:  # noqa: BLE001
                err = map_sdk_error(e)
                out[model] = {"error": err.kind, "message": err.message}
        return out

    def allowed_fallbacks(self, model: str) -> list[str]:
        """``allowed_fallback_models`` from the beta models endpoint; empty when unsupported. Also updates the
        ``anthropic.sonnet_fallbacks`` flag for Sonnet models."""
        try:
            info = self.client.beta.models.retrieve(model, betas=[FALLBACK_MODELS_BETA])
            allowed = list(getattr(info, "allowed_fallback_models", None) or [])
        except Exception:  # noqa: BLE001
            allowed = []
        if "sonnet" in model.lower():
            self.flags.set("anthropic.sonnet_fallbacks", bool(allowed))
        return allowed

    def smoke_test(self, route: str, out: type[BaseModel]) -> CheckResult:
        """CHK-S10: does the API accept this route's schema (thinking off, small ``max_tokens``)? Passes when the
        schema compiles, even if the tiny reply is cut off or breaks a Pydantic-only constraint."""
        try:
            cfg0 = self.routes[route]
            cfg = dataclasses.replace(cfg0, thinking=False, effort="low", max_tokens=min(cfg0.max_tokens, 2048), fallbacks=False)
            try:
                self._call(route, system=[text_block("Schema smoke test.")], content=[text_block("Return any short valid object.")],
                           out=out, ctx=CallCtx.null(), prompt_version=0, max_tokens_override=None, on_first_event=None,
                           cfg_override=cfg)
                note = "schema accepted"
            except ProviderError as e:
                if e.kind in ("truncated", "validation"):
                    note = f"schema accepted ({e.kind} reply ignored)"
                else:
                    if e.kind == "schema_too_complex":
                        self.flags.set(f"anthropic.schema_ok.{route}", False)
                    return CheckResult(check_id="CHK-S10", fm_ids=["LLM-04"], kind="assert", passed=False, metric="schema_smoke",
                                       evidence=f"{route}: {e.kind}: {e.message}", fix_hint="human")
            self.flags.set(f"anthropic.schema_ok.{route}", True)
            return CheckResult(check_id="CHK-S10", fm_ids=["LLM-04"], kind="assert", passed=True, metric="schema_smoke", evidence=f"{route}: {note}")
        except Exception as e:  # noqa: BLE001 - fail closed
            return not_run("CHK-S10", "assert", f"{type(e).__name__}: {scrub(e)}", fm_ids=["LLM-04"])


def provider_from_key(api_key: str, **kw: Any) -> AnthropicProvider:
    """Convenience constructor used by the registry."""
    return AnthropicProvider(api_key, **kw)


__all__ = [
    "FALLBACK_BETA", "ROUTES", "SCHEMA_CACHE", "AnthropicProvider", "BatchItem", "BatchItemResult", "CacheMonitor",
    "LLMProvider", "LLMResult", "Route", "RouteCfg", "SchemaCache", "build_routes", "check_content_images",
    "count_unions_and_optionals", "file_image_block", "finish_call", "image_block", "make_all_required", "map_sdk_error",
    "prepare_judge_images", "provider_from_key", "refusal_error", "text_block", "truncated_error", "with_cache_control",
]
