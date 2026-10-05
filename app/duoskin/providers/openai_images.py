"""OpenAI GPT Image 2.5 adapter (APP_SPEC 7.3, bible 2.1, 2.6, 2.7 and 2.11).

Rules enforced here (CHK-P02, CHK-P08, GEN-01..GEN-08):

* Always passes ``model`` (a pinned name), ``quality`` (never ``auto``), ``background``, ``output_format="png"``,
  ``size``, ``n`` and ``user``; never sends ``moderation``, ``input_fidelity``, ``output_compression`` or ``stream``.
* ``generate`` takes no input images; anything with a reference, guide or mask uses ``edit`` (Image 1 is the image
  being edited and the mask applies to it).
* ``n`` is at most ``min(IPM, 4)`` per request (CHK-P02). ``run_many(req, n_total, ctx)`` splits ``n_total`` into
  ``ceil(n_total / 4)`` requests (``plan_batches(6) == [3, 3]``) with the same prompt; request ``k`` carries the nonce
  ``<nonce>#k`` and each passes the images-per-minute limiter on its own (``ImageResult.batches`` lists them).
* The SDK client is built with ``max_retries=0`` and ``timeout=900``: a timeout followed by an SDK retry could bill
  twice. The adapter retries only 5xx (twice, with backoff); timeouts go back to the queue.
* The decoded size of every image must equal the REQUESTED size, always; it must also equal Image 1's size when
  ``image1_role == "edit_target"`` (I0, I1, I1e, I3, I4, I8, I11); I2, I5, I6 and I10 send a reference crop as Image 1
  and are checked against the request alone. A mismatch is a size-drift failure: the
  result is rejected (``kind="validation"``, ``code="size_drift"``), never resized.
* ``background="transparent"`` may come back opaque: the caller must verify alpha (``ImageResult.alpha_present``
  and ``has_real_alpha`` help). ``mask`` is guidance only (the model re-renders the whole image); paste-back and the
  ring check belong to the caller.
* Capability flags decide the mask and RGBA Image 1 rules (``openai.mask_multi_ok``, ``openai.rgba_image1_ok``).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from duoskin.providers.base import (
    PNG_MAGIC,
    CallCtx,
    CapabilityFlags,
    ProviderError,
    RateLimiter,
    backoff_delay,
    limiter_for,
    parse_retry_after,
    png_size,
    scrub,
    sleep_checked,
)
from duoskin.providers.pricing import openai_image_cost

PROVIDER = "openai"
MAX_N_PER_REQUEST = 4
MAX_N_TOTAL = 32
MAX_INPUT_IMAGES = 16
MAX_MASK_BYTES = 4 * 1024 * 1024
FLATTEN_COLOR = (0xF2, 0xF2, 0xF2)
MODEL_RE = re.compile(r"^(gpt-image-2\.5-(flare|sunburst)|gpt-image-2)(-\d{4}-\d{2}-\d{2})?$")
SNAPSHOT_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
QUALITIES = ("low", "medium", "high", "xhigh", "max")      # never "auto"
Quality = Literal["low", "medium", "high", "xhigh", "max"]
DROPPABLE_PARAMS = ("user", "output_format", "background")   # dropped after a `capability` error proved them unsupported


# --------------------------------------------------------------------------------------------------------------
# Requests and results
# --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class NamedPng:
    name: str
    data: bytes                                   # sent as (name, bytes, "image/png")


@dataclass(frozen=True)
class ImageRequest:
    model: str                                    # pinned snapshot, e.g. "gpt-image-2.5-flare-2026-09-08"
    prompt: str                                   # compiled + linted text (CompiledPrompt.text)
    size: str                                     # "WxH"; valid_size() asserted before sending
    quality: Quality
    background: Literal["opaque", "transparent"]
    n: int                                        # 1..4 PER REQUEST and <= the images-per-minute setting; more: run_many()
    images: tuple[NamedPng, ...] = ()             # edit only; images[0] is the image being edited; <=16
    image1_role: Literal["edit_target", "reference"] = "edit_target"   # decides the size assert (from the template front matter)
    mask: NamedPng | None = None                  # RGBA PNG, same size as images[0], alpha 0 = editable, <4 MB
    output_format: Literal["png"] = "png"
    user: str = "duoskin-local"                   # a fixed hashed identifier; never the user's email
    # ---- additive fields (not sent to OpenAI) ----
    nonce: str = ""                               # "Reimagine" changes only the nonce; part of the cache key and mock seed
    tag: str = ""                                 # step template id (I1..I11), used by mock fault selectors and logs


@dataclass
class ImageResult:
    images: list[bytes]                           # decoded PNG bytes, archived untouched (raw_sha256)
    size: str                                     # r.size (asserted == requested)
    usage: dict | None                            # r.usage may be None
    request_id: str | None                        # r._request_id (of the first request)
    model: str
    # ---- additive fields ----
    raw_sha256: list[str] = field(default_factory=list)
    alpha_present: list[bool] = field(default_factory=list)   # does each image have real transparency?
    batches: list[dict[str, Any]] = field(default_factory=list)   # [{"batch_index", "n", "request_id", "usage"}]
    request_ids: list[str | None] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)      # what the adapter changed before sending (mask dropped, ...)
    warnings: list[str] = field(default_factory=list)
    cost: dict[str, Any] | None = None            # CostEntry-like; basis "usage" or "estimate" (when usage is None)
    usage_present: bool = False
    n_total: int = 0                              # run_many: the total asked for (provenance records n_total and the batch sizes)
    batch_sizes: list[int] = field(default_factory=list)


class ImageGenProvider(Protocol):
    def generate(self, req: ImageRequest, ctx: CallCtx) -> ImageResult: ...   # req.images must be empty
    def edit(self, req: ImageRequest, ctx: CallCtx) -> ImageResult: ...       # req.images non-empty
    def run_many(self, req: ImageRequest, n_total: int, ctx: CallCtx) -> ImageResult: ...   # n_total > 4: see plan_batches
    def probe(self) -> dict[str, bool]: ...                                   # FM-T6 capability probes


# --------------------------------------------------------------------------------------------------------------
# Pure helpers (shared with the mock)
# --------------------------------------------------------------------------------------------------------------

def valid_size(w: int, h: int) -> bool:
    """Legal GPT Image sizes: sides divisible by 16, ratio at most 3:1, 655,360 to 8,294,400 pixels, long edge < 3840."""
    return (w > 0 and h > 0 and w % 16 == 0 and h % 16 == 0 and max(w, h) / min(w, h) <= 3
            and 655_360 <= w * h <= 8_294_400 and max(w, h) < 3840)


def parse_size(size: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", size or "")
    if not m:
        raise ProviderError(PROVIDER, "bad_request", f"size {size!r} is not 'WxH' (never 'auto')", code="bad_size", billed="no")
    return int(m.group(1)), int(m.group(2))


def plan_batches(n_total: int, per_request: int = MAX_N_PER_REQUEST) -> list[int]:
    """Split ``n_total`` into ``ceil(n_total / per_request)`` requests of balanced size: 6 -> [3, 3], 8 -> [4, 4],
    5 -> [3, 2], 7 -> [4, 3], 4 -> [4], 1 -> [1]."""
    n_total = max(1, int(n_total))
    per_request = max(1, min(MAX_N_PER_REQUEST, int(per_request)))
    k = -(-n_total // per_request)
    base, rem = divmod(n_total, k)
    return [base + 1] * rem + [base] * (k - rem)


def is_pinned_snapshot(model: str) -> bool:
    """``gpt-image-2.5-flare-2026-09-08`` is pinned; the bare family name is accepted by ``validate_request`` but is not."""
    return bool(MODEL_RE.match(model)) and bool(SNAPSHOT_RE.search(model))


def _open(data: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(data))


def alpha_stats(png: bytes) -> tuple[int, float]:
    """(minimum alpha, share of pixels with alpha < 255). An image without an alpha channel gives (255, 0.0)."""
    import numpy as np
    im = _open(png)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        a = np.asarray(im.convert("RGBA"))[..., 3]
        return int(a.min()), float((a < 255).mean())
    return 255, 0.0


def has_real_alpha(png: bytes, *, min_share: float = 0.001) -> bool:
    """True when at least ``min_share`` of the pixels are not fully opaque (background=transparent may return opaque)."""
    return alpha_stats(png)[1] >= min_share


def flatten_on(png: bytes, color: tuple[int, int, int] = FLATTEN_COLOR) -> bytes:
    """Composite an RGBA image on a flat colour (reference crops are sent opaque, bible U26)."""
    from PIL import Image
    im = _open(png).convert("RGBA")
    bg = Image.new("RGBA", im.size, (*color, 255))
    bg.alpha_composite(im)
    buf = io.BytesIO()
    bg.save(buf, "PNG")
    return buf.getvalue()


def _bad(msg: str, code: str, hint: str = "") -> ProviderError:
    return ProviderError(PROVIDER, "bad_request", msg, code=code, billed="no", user_hint=hint)


def validate_request(req: ImageRequest, *, edit: bool, flags: CapabilityFlags | None = None,
                     per_request_cap: int = MAX_N_PER_REQUEST) -> tuple[int, int]:
    """Pre-flight (CHK-P02). Raises ``ProviderError(kind="bad_request")`` for a code bug; returns the (W, H).

    ``per_request_cap`` is ``min(IPM, 4)``: a request with more images must go through ``run_many``."""
    if not MODEL_RE.match(req.model):
        raise _bad(f"model {req.model!r} is not a pinned GPT Image model (edit would default to gpt-image-1.5)", "model_not_pinned")
    if req.quality not in QUALITIES:
        raise _bad(f"quality {req.quality!r} is not one of {QUALITIES} (never 'auto')", "bad_quality")
    if req.background not in ("opaque", "transparent"):
        raise _bad(f"background {req.background!r} must be 'opaque' or 'transparent'", "bad_background")
    if req.output_format != "png":
        raise _bad("output_format must be 'png'", "bad_output_format")
    w, h = parse_size(req.size)
    if not valid_size(w, h):
        raise _bad(f"size {req.size} is not a legal GPT Image size (multiples of 16, <=3:1, 655,360..8,294,400 px)", "illegal_size",
                   "The app asked for an illegal image size. This is a bug; nothing was sent.")
    if not isinstance(req.n, int) or isinstance(req.n, bool) or req.n < 1:
        raise _bad(f"n={req.n!r} must be a positive integer", "bad_n")
    if req.n > per_request_cap:
        raise _bad(f"n={req.n} exceeds {per_request_cap} images per request (min of the images-per-minute setting and 4); use run_many()",
                   "n_exceeds_request_cap", "The app asked for too many images in one request. This is a bug; use run_many for more than four.")
    if not req.user or "@" in req.user:
        raise _bad("user must be the fixed hashed app id, never an email", "bad_user")
    if req.prompt is None or not req.prompt.strip():
        raise _bad("empty prompt", "empty_prompt")
    if edit and not req.images:
        raise _bad("edit() needs at least one image; use generate() for text-only calls", "edit_needs_image")
    if not edit and (req.images or req.mask):
        raise _bad("generate() takes no input images; anything with a reference, guide or mask uses edit()", "generate_with_images")
    if len(req.images) > MAX_INPUT_IMAGES:
        raise _bad(f"at most {MAX_INPUT_IMAGES} input images", "too_many_images")
    for im in req.images:
        if not isinstance(im.data, (bytes, bytearray)) or not bytes(im.data).startswith(PNG_MAGIC):
            raise _bad(f"input image {im.name!r} is not a PNG", "input_not_png")
    if req.mask is not None:
        _validate_mask(req, w, h)
    if edit and req.image1_role not in ("edit_target", "reference"):
        raise _bad("image1_role must be 'edit_target' or 'reference'", "bad_image1_role")
    if edit and req.image1_role == "edit_target":
        s1 = png_size(bytes(req.images[0].data))
        if s1 != (w, h):
            raise _bad(f"Image 1 is {s1} but the request asks for {w}x{h}: an edit target must itself be a legal output size",
                       "image1_size_mismatch",
                       "The picture being edited is not the requested size. This is a bug; the app should pad it onto a legal canvas.")
    return w, h


def _validate_mask(req: ImageRequest, w: int, h: int) -> None:
    import numpy as np
    m = req.mask
    assert m is not None
    if not req.images:
        raise _bad("a mask needs an image to apply to", "mask_without_image")
    data = bytes(m.data)
    if len(data) >= MAX_MASK_BYTES:
        raise _bad("mask must be smaller than 4 MB", "mask_too_big")
    if not data.startswith(PNG_MAGIC):
        raise _bad("mask must be a PNG", "mask_not_png")
    im = _open(data)
    if im.mode != "RGBA":
        raise _bad(f"mask must be RGBA (a greyscale mask has no alpha channel); got {im.mode}", "mask_not_rgba",
                   "The edit mask was not RGBA. This is a bug; masks must be built with make_mask().")
    if im.size != png_size(bytes(req.images[0].data)):
        raise _bad(f"mask size {im.size} must equal Image 1's size", "mask_size_mismatch")
    a = np.asarray(im)[..., 3]
    vals = set(np.unique(a).tolist())
    if not vals <= {0, 255}:
        raise _bad("mask alpha must be exactly 0 (editable) or 255 (keep)", "mask_alpha_values")
    if not (a == 0).any():
        raise _bad("mask has no editable area (alpha 0 means the model may change it)", "mask_empty",
                   "The edit mask leaves nothing to change. This is a bug; check the mask polarity.")


# --------------------------------------------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------------------------------------------

def moderation_error(request_id: str | None = None, message: str = "Your request was rejected by the safety system.") -> ProviderError:
    """OpenAI ``moderation_blocked`` (fast and reportedly unbilled). Never retried unchanged."""
    return ProviderError(PROVIDER, "moderation", message, http=400, code="moderation_blocked", billed="no", request_id=request_id,
                         user_hint="OpenAI's safety filter blocked this prompt. The app rewrites it once; if that is blocked too, reword the idea.")


def size_drift_error(requested: str, got: str, *, request_id: str | None = None, cost: dict[str, Any] | None = None) -> ProviderError:
    return ProviderError(PROVIDER, "validation", f"size drift: requested {requested}, got {got}", code="size_drift", billed="yes",
                         request_id=request_id, cost=cost, context={"requested": requested, "got": got},
                         user_hint="The image came back at the wrong size, so it was rejected (the app never resizes before paste-back).")


def map_openai_error(e: BaseException) -> ProviderError:
    import openai

    if isinstance(e, ProviderError):
        return e
    if isinstance(e, openai.APIStatusError):
        status = getattr(e, "status_code", 0) or 0
        body = e.body if isinstance(e.body, dict) else {}
        err = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
        msg = str(err.get("message") or e.message or f"HTTP {status}")
        code = err.get("code") or getattr(e, "code", None)
        code = str(code) if code else None
        param = err.get("param") or getattr(e, "param", None)
        rid = getattr(e, "request_id", None)
        low = msg.lower()
        headers = getattr(getattr(e, "response", None), "headers", None)
        retry_after = parse_retry_after(dict(headers) if headers is not None else None)
        if code == "moderation_blocked" or (status == 400 and "safety system" in low):
            return moderation_error(rid, msg)
        if code == "insufficient_quota" or "exceeded your current quota" in low or "billing hard limit" in low:
            return ProviderError(PROVIDER, "billing", msg, http=status, code=code or "insufficient_quota", request_id=rid, billed="no",
                                 user_hint="OpenAI says the account is out of credit or over its limit. Add credit, then resume the queue.")
        if status == 403 and ("must be verified" in low or "verify" in low and "organization" in low):
            return ProviderError(PROVIDER, "permission", msg, http=status, code=code, request_id=rid, billed="no",
                                 user_hint="OpenAI requires this organization to be verified before it allows GPT Image models. "
                                           "Verify the organization in your OpenAI account settings, then try again.")
        if status == 400 and (code in ("unknown_parameter", "unsupported_parameter") or "unknown parameter" in low
                              or "unrecognized request argument" in low or "unsupported parameter" in low):
            return ProviderError(PROVIDER, "capability", msg, http=status, code=code or "unknown_parameter", request_id=rid, billed="no",
                                 context={"param": param})
        if status == 401:
            return ProviderError(PROVIDER, "auth", msg, http=status, code=code, request_id=rid, billed="no")
        if status == 403:
            return ProviderError(PROVIDER, "permission", msg, http=status, code=code, request_id=rid, billed="no")
        if status == 404:
            return ProviderError(PROVIDER, "not_found", msg, http=status, code=code, request_id=rid, billed="no",
                                 user_hint="OpenAI does not know this image model ID. Update the model ID in Settings.")
        if status == 429:
            return ProviderError(PROVIDER, "rate_limit", msg, http=status, code=code, retryable=True, request_id=rid, billed="no",
                                 retry_after_s=retry_after)
        if status >= 500:
            return ProviderError(PROVIDER, "server", msg, http=status, code=code, retryable=True, request_id=rid)
        hint = ""
        if code in ("invalid_image_file", "invalid_mask", "invalid_value") or "mask" in low:
            hint = "OpenAI rejected an image or mask. The app forces RGBA, matches Image 1's size and sends named PNG tuples."
        return ProviderError(PROVIDER, "bad_request", msg, http=status, code=code, request_id=rid, billed="no", user_hint=hint,
                             context={"param": param})
    if isinstance(e, openai.APITimeoutError):
        return ProviderError(PROVIDER, "timeout", "the request to OpenAI timed out", retryable=True, billed="unknown", code="timeout",
                             context={"possible_double_bill": True},
                             user_hint="OpenAI took too long. The app retries once and logs it, because a timeout may have been billed.")
    if isinstance(e, openai.APIConnectionError):
        return ProviderError(PROVIDER, "network", f"could not reach OpenAI ({type(e).__name__})", retryable=True)
    return ProviderError(PROVIDER, "other", f"{type(e).__name__}: {scrub(e)}")


# --------------------------------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------------------------------

def check_response_sizes(requested: str, response_size: str | None, decoded: list[tuple[int, int]],
                         image1_size: tuple[int, int] | None, *, request_id: str | None = None,
                         cost: dict[str, Any] | None = None) -> None:
    """CHK-P08 post-call: ``r.size`` and every decoded W x H equal the REQUEST; equal Image 1 as well when Image 1 is
    the edit target (``image1_size`` given). Raises the size-drift error; never resizes."""
    w, h = parse_size(requested)
    if response_size and response_size != requested:
        raise size_drift_error(requested, response_size, request_id=request_id, cost=cost)
    for dw, dh in decoded:
        if (dw, dh) != (w, h):
            raise size_drift_error(requested, f"{dw}x{dh}", request_id=request_id, cost=cost)
        if image1_size is not None and (dw, dh) != image1_size:
            raise size_drift_error(f"{image1_size[0]}x{image1_size[1]}", f"{dw}x{dh}", request_id=request_id, cost=cost)


def sum_usage(usages: list[dict | None]) -> dict | None:
    """Add the usage dicts of several requests (token counts, including the detail dicts); ``None`` if none had any."""
    have = [u for u in usages if u]
    if not have:
        return None
    total: dict[str, Any] = {}
    for u in have:
        for k, v in u.items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
            elif isinstance(v, dict):
                sub = total.setdefault(k, {})
                for k2, v2 in v.items():
                    if isinstance(v2, (int, float)):
                        sub[k2] = sub.get(k2, 0) + v2
    return total


def prepare_inputs(req: ImageRequest, flags: CapabilityFlags) -> tuple[list[NamedPng], NamedPng | None, list[str]]:
    """Apply the mask/multi-image rule and the Image 1 alpha rule (shared with the mock).

    * mask AND several images while ``openai.mask_multi_ok`` is false: opaque Image 1 -> drop the mask (paste-back and the
      ring check still run); transparent Image 1 -> drop the extra images (bible D17);
    * an RGBA Image 1 with transparent pixels and no mask is flattened on ``#F2F2F2`` unless ``openai.rgba_image1_ok``
      (bible U26).
    Returns (images, mask, adjustments)."""
    images, mask, notes = list(req.images), req.mask, []
    if not images:
        return images, mask, notes
    transparent1 = alpha_stats(bytes(images[0].data))[1] > 0
    if mask is not None and len(images) > 1 and not flags.get("openai.mask_multi_ok"):
        if transparent1:
            images = images[:1]
            notes.append("extra_images_dropped:mask_multi_ok=false")
        else:
            mask = None
            notes.append("mask_dropped:mask_multi_ok=false")
    if mask is None and transparent1 and not flags.get("openai.rgba_image1_ok"):
        images[0] = NamedPng(images[0].name, flatten_on(bytes(images[0].data)))
        notes.append("image1_flattened_on_F2F2F2:rgba_image1_ok=false")
    return images, mask, notes


def aggregate_results(results: list[ImageResult], req: ImageRequest, n_total: int) -> ImageResult:
    """Merge the results of the sub-requests of one ``run_many`` call."""
    images = [im for r in results for im in r.images]
    batches: list[dict[str, Any]] = []
    for k, r in enumerate(results):
        for b in r.batches:
            batches.append({**b, "batch_index": k})
    usage = sum_usage([r.usage for r in results])
    adjustments: list[str] = []
    for r in results:
        for a in r.adjustments:
            if a not in adjustments:
                adjustments.append(a)
    warnings = [w for r in results for w in r.warnings]
    return ImageResult(
        images=images, size=req.size, usage=usage, request_id=results[0].request_id if results else None, model=req.model,
        raw_sha256=[x for r in results for x in r.raw_sha256], alpha_present=[x for r in results for x in r.alpha_present],
        batches=batches, request_ids=[x for r in results for x in r.request_ids], adjustments=adjustments, warnings=warnings,
        cost=_merge_costs([r.cost for r in results if r.cost]), usage_present=usage is not None,
        n_total=n_total, batch_sizes=[len(r.images) for r in results])


class ImageProviderBase:
    """``run_many`` for any image provider (the real adapter and the mock share it)."""

    flags: CapabilityFlags
    limiter: RateLimiter

    def per_request_cap(self) -> int:
        """``min(IPM, 4)``."""
        cap = getattr(self.limiter, "ipm_capacity", None)
        return max(1, min(MAX_N_PER_REQUEST, int(cap))) if cap else MAX_N_PER_REQUEST

    def _single(self, req: ImageRequest, ctx: CallCtx, *, edit: bool) -> ImageResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def generate(self, req: ImageRequest, ctx: CallCtx) -> ImageResult:
        return self._single(req, ctx, edit=False)

    def edit(self, req: ImageRequest, ctx: CallCtx) -> ImageResult:
        return self._single(req, ctx, edit=True)

    def run_many(self, req: ImageRequest, n_total: int, ctx: CallCtx) -> ImageResult:
        """``n_total`` images as ``ceil(n_total / min(IPM, 4))`` requests with the same prompt. Request ``k`` carries the nonce
        ``<nonce>#k``; each passes the limiter on its own. If a later request fails, the exception's ``partial`` holds the
        images that were already paid for."""
        if not isinstance(n_total, int) or isinstance(n_total, bool) or not 1 <= n_total <= MAX_N_TOTAL:
            raise _bad(f"n_total={n_total!r} must be 1..{MAX_N_TOTAL}", "bad_n")
        sizes = plan_batches(n_total, self.per_request_cap())
        edit = bool(req.images)
        results: list[ImageResult] = []
        for k, n in enumerate(sizes):
            sub = replace(req, n=n, nonce=f"{req.nonce}#{k}")
            try:
                results.append(self._single(sub, ctx, edit=edit))
            except ProviderError as pe:
                if results and pe.partial is None:
                    pe.partial = aggregate_results(results, req, n_total)
                raise
        return aggregate_results(results, req, n_total)


class OpenAIImages(ImageProviderBase):
    """Real GPT Image provider on the official SDK (injectable ``client`` / ``http_client`` for tests)."""

    name = PROVIDER

    def __init__(self, api_key: str | None = None, *, client: Any = None, http_client: Any = None,
                 flags: CapabilityFlags | None = None, limiter: RateLimiter | None = None,
                 cost_sink: Callable[[dict[str, Any]], None] | None = None, timeout: float = 900.0,
                 server_error_retries: int = 2, sleep: Callable[[float], None] | None = None,
                 require_snapshot: bool = False, probe_model: str = "gpt-image-2.5-flare-2026-09-08") -> None:
        if client is None:
            import openai
            kw: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
            if http_client is not None:
                kw["http_client"] = http_client
            client = openai.OpenAI(**kw)
        self.client = client
        self.flags = flags or CapabilityFlags()
        self.limiter = limiter or limiter_for(PROVIDER)
        self.cost_sink = cost_sink
        self.server_error_retries = server_error_retries
        import time as _time
        self._sleep = sleep or _time.sleep
        self.require_snapshot = require_snapshot
        self.probe_model = probe_model
        self._key_probed = False
        self.last_kwargs: dict[str, Any] | None = None     # the last SDK kwargs, for tests and diagnostics

    def __repr__(self) -> str:
        return "OpenAIImages()"

    # ----- public API (generate / edit / run_many come from ImageProviderBase) -------------------------------
    def _prepare_inputs(self, req: ImageRequest) -> tuple[list[NamedPng], NamedPng | None, list[str]]:
        return prepare_inputs(req, self.flags)

    def _kwargs(self, req: ImageRequest, n: int, images: list[NamedPng], mask: NamedPng | None, edit: bool) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": req.model, "prompt": req.prompt, "size": req.size, "quality": req.quality,
                              "background": req.background, "output_format": req.output_format, "n": n, "user": req.user}
        for p in DROPPABLE_PARAMS:
            if self.flags.get(f"openai.unsupported.{p}"):
                kw.pop(p, None)
        if edit:
            kw["image"] = [(im.name, bytes(im.data), "image/png") for im in images]
            if mask is not None:
                kw["mask"] = (mask.name, bytes(mask.data), "image/png")
        return kw

    def _send(self, kw: dict[str, Any], edit: bool, ctx: CallCtx, images_count: int) -> Any:
        """One HTTP request with the limiter, 5xx retries and error mapping."""
        attempt = 0
        while True:
            try:
                with self.limiter.acquire(images=images_count, ctx=ctx):
                    ctx.tick()
                    self.last_kwargs = kw
                    resp = self.client.images.edit(**kw) if edit else self.client.images.generate(**kw)
                return resp
            except ProviderError:
                raise
            except Exception as e:  # noqa: BLE001 - mapped below
                err = map_openai_error(e)
                if err.kind in ("rate_limit",):
                    self.limiter.penalize(err.retry_after_s or 10.0)
                if err.kind == "capability" and err.context.get("param") in DROPPABLE_PARAMS:
                    self.flags.set(f"openai.unsupported.{err.context['param']}", True)
                if err.kind == "server" and attempt < self.server_error_retries:
                    attempt += 1
                    sleep_checked(backoff_delay(attempt - 1), ctx, sleep=self._sleep)
                    continue
                raise err from None

    def _single(self, req: ImageRequest, ctx: CallCtx, *, edit: bool) -> ImageResult:
        w, h = validate_request(req, edit=edit, flags=self.flags, per_request_cap=self.per_request_cap())
        if self.require_snapshot and not is_pinned_snapshot(req.model):
            raise _bad(f"model {req.model!r} is not a dated snapshot", "model_not_pinned")
        images, mask, adjustments = self._prepare_inputs(req) if edit else ([], None, [])
        image1_size = png_size(bytes(images[0].data)) if (edit and req.image1_role == "edit_target" and images) else None
        ctx.tick()
        kw = self._kwargs(req, req.n, images, mask, edit)
        resp = self._send(kw, edit, ctx, req.n)
        got, usage, rid, cost = self._decode(req, resp, req.n, image1_size, edit=edit, n_input=len(images))
        warnings: list[str] = []
        if len(got) < req.n:
            warnings.append(f"asked for {req.n} images, got {len(got)}")
        if self.cost_sink is not None:
            try:
                self.cost_sink(cost)
            except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide the images that were paid for
                pass
        alpha = [has_real_alpha(b) for b in got]
        if req.background == "transparent" and not any(alpha):
            warnings.append("transparent_requested_but_opaque")
        if usage is not None:
            self.flags.set("openai.usage_present", True)
        return ImageResult(images=got, size=f"{w}x{h}", usage=usage, request_id=rid, model=req.model,
                           raw_sha256=[hashlib.sha256(b).hexdigest() for b in got], alpha_present=alpha,
                           batches=[{"batch_index": 0, "n": req.n, "returned": len(got), "request_id": rid, "usage": usage}],
                           request_ids=[rid], adjustments=adjustments, warnings=warnings, cost=cost, usage_present=usage is not None,
                           n_total=req.n, batch_sizes=[len(got)])

    def _decode(self, req: ImageRequest, resp: Any, n: int, image1_size: tuple[int, int] | None, *, edit: bool,
                n_input: int) -> tuple[list[bytes], dict | None, str | None, dict[str, Any]]:
        rid = getattr(resp, "_request_id", None)
        usage_obj = getattr(resp, "usage", None)
        usage = usage_obj.model_dump(exclude_none=True) if usage_obj is not None and hasattr(usage_obj, "model_dump") else (dict(usage_obj) if usage_obj else None)
        cost = openai_image_cost(req.model, usage, operation=f"images.{'edit' if edit else 'generate'}:{req.tag or 'img'}",
                                 quality=req.quality, n=n, n_input_images=n_input, prompt_chars=len(req.prompt), request_id=rid)
        data = list(getattr(resp, "data", None) or [])
        if not data:
            raise ProviderError(PROVIDER, "validation", "OpenAI returned no images", code="no_images", billed="unknown", request_id=rid, cost=cost)
        decoded: list[bytes] = []
        for d in data:
            b64 = getattr(d, "b64_json", None)
            if not b64:
                raise ProviderError(PROVIDER, "validation", "an image came back without b64_json", code="no_b64", billed="yes", request_id=rid, cost=cost)
            try:
                raw = base64.b64decode(b64, validate=False)
            except (binascii.Error, ValueError):
                raise ProviderError(PROVIDER, "validation", "b64_json could not be decoded", code="bad_b64", billed="yes", request_id=rid, cost=cost) from None
            if not raw.startswith(PNG_MAGIC):
                raise ProviderError(PROVIDER, "validation", "the image is not a PNG", code="not_png", billed="yes", request_id=rid, cost=cost)
            decoded.append(raw)
        sizes = [png_size(b) or (0, 0) for b in decoded]
        check_response_sizes(req.size, getattr(resp, "size", None), sizes, image1_size, request_id=rid, cost=cost)
        return decoded, usage, rid, cost

    def test_key(self, *, paid: bool = True) -> dict[str, Any]:
        """Settings "Test key": a free ``models.list`` (is the key accepted?) and, once per adapter (that is, per key), one
        1024 x 1024 Flare ``low`` image (about $0.006) that proves the image model and the organisation verification work."""
        try:
            next(iter(self.client.models.list()), None)
        except Exception as e:  # noqa: BLE001
            err = map_openai_error(e)
            return {"ok": False, "message": err.user_message, "kind": err.kind}
        if paid and not self._key_probed:
            try:
                self.generate(ImageRequest(model=self.probe_model, prompt="A flat blue square on a plain light background.", size="1024x1024",
                                           quality="low", background="opaque", n=1, tag="probe"), CallCtx.null())
            except ProviderError as err:
                return {"ok": False, "message": err.user_message, "kind": err.kind}
            self._key_probed = True
            return {"ok": True, "message": "The OpenAI key works and the image model answered (one small test image, about $0.006)."}
        return {"ok": True, "message": "The OpenAI key is accepted."}

    # ----- FM-T6 probes -------------------------------------------------------------------------------------
    def probe(self) -> dict[str, bool]:
        """Capability probes (FM-T6). Each costs about one low-quality image; the caller asks the user first.

        * ``openai.mask_multi_ok``: is a mask accepted together with two images?
        * ``openai.rgba_image1_ok``: does an RGBA Image 1 without a mask come back with its transparent half read as
          an implicit mask? (heuristic: the opaque half must stay close to the input)
        * ``openai.usage_present``: does the response carry ``usage``?
        Results are stored in the flags and returned."""
        import numpy as np
        from PIL import Image
        model = "gpt-image-2.5-flare-2026-09-08"
        size = "816x816"
        ctx = CallCtx.null()

        def png(arr: np.ndarray) -> bytes:
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, "PNG")
            return buf.getvalue()

        base = np.zeros((816, 816, 4), np.uint8)
        base[..., :3] = (120, 160, 220)
        base[..., 3] = 255
        ref = np.zeros((816, 816, 4), np.uint8)
        ref[..., :3] = (220, 120, 60)
        ref[..., 3] = 255
        mask = np.zeros((816, 816, 4), np.uint8)
        mask[..., 3] = 255
        mask[300:500, 300:500, 3] = 0
        results: dict[str, bool] = {}

        def req(images: list[bytes], mask_png: bytes | None = None) -> ImageRequest:
            return ImageRequest(model=model, prompt="A flat blue square with a small orange square in the middle.", size=size, quality="low",
                                background="opaque", n=1, images=tuple(NamedPng(f"i{k}.png", b) for k, b in enumerate(images)),
                                mask=NamedPng("mask.png", mask_png) if mask_png else None, tag="probe")

        # mask + several images
        saved_multi = self.flags.get("openai.mask_multi_ok")
        self.flags.set("openai.mask_multi_ok", True)                    # send the combination as it is
        try:
            r = self.edit(req([png(base), png(ref)], png(mask)), ctx)
            results["openai.mask_multi_ok"] = bool(r.images)
            results["openai.usage_present"] = r.usage is not None
        except ProviderError as e:
            if e.kind not in ("capability", "bad_request"):
                self.flags.set("openai.mask_multi_ok", saved_multi)
                raise
            results["openai.mask_multi_ok"] = False
        # RGBA Image 1 without a mask
        rgba = base.copy()
        rgba[:, :408, 3] = 0
        saved_rgba = self.flags.get("openai.rgba_image1_ok")
        self.flags.set("openai.rgba_image1_ok", True)
        try:
            r2 = self.edit(req([png(rgba)]), ctx)
            out = np.asarray(Image.open(io.BytesIO(r2.images[0])).convert("RGB")).astype(int)
            kept = float(np.abs(out[:, 408:, :] - base[:, 408:, :3].astype(int)).mean())
            results["openai.rgba_image1_ok"] = kept < 40.0
            results.setdefault("openai.usage_present", r2.usage is not None)
        except ProviderError as e:
            if e.kind not in ("capability", "bad_request"):
                self.flags.set("openai.rgba_image1_ok", saved_rgba)
                raise
            results["openai.rgba_image1_ok"] = False
        for k, v in results.items():
            self.flags.set(k, v)
        return results


def _merge_costs(costs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not costs:
        return None
    if len(costs) == 1:
        return costs[0]
    out = dict(costs[0])
    out["usd"] = round(sum(c["usd"] for c in costs), 6)
    units: dict[str, dict[str, Any]] = {}
    for c in costs:
        for u in c["units"]:
            cur = units.setdefault(u["name"], {**u, "qty": 0.0})
            cur["qty"] += u["qty"]
    out["units"] = list(units.values())
    out["basis"] = "usage" if all(c["basis"] == "usage" for c in costs) else "estimate"
    out["request_id"] = costs[0].get("request_id")
    return out


def estimate_call(req: ImageRequest) -> float:
    """Pre-call USD estimate for a request (all its sub-requests)."""
    from duoskin.providers.pricing import estimate_openai_image
    return estimate_openai_image(quality=req.quality, n=req.n, n_input_images=len(req.images), prompt_chars=len(req.prompt)).usd


__all__ = [
    "MAX_N_PER_REQUEST", "QUALITIES", "ImageGenProvider", "ImageProviderBase", "ImageRequest", "ImageResult", "NamedPng", "OpenAIImages",
    "Quality", "alpha_stats", "check_response_sizes", "estimate_call", "flatten_on", "has_real_alpha", "is_pinned_snapshot",
    "map_openai_error", "moderation_error", "parse_size", "plan_batches", "prepare_inputs", "size_drift_error", "sum_usage", "valid_size",
    "validate_request",
]
