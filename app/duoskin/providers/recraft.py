"""Recraft V4.1 vector adapter (APP_SPEC 7.4, bible 11.1, 11.5 and Appendix A).

``https://external.api.recraft.ai/v1``, ``Authorization: Bearer ...``. Rules enforced here (CHK-P03, GEN-09):

* ``*_styles_*`` models require a ``style_id``; a ``style_id`` never travels with a ``style`` preset or style
  reference URLs (those are never sent). ``negative_prompt``, ``no_text``, ``artistic_level`` and ``style`` are never
  sent (V4 ignores or rejects them). ``size`` must be a V4 pixel preset.
* Raster and vector ``style_id`` registries are kept apart (``StyleRegistry``): a vector style on a raster model
  returns SVG bytes. SVG is not accepted as a style reference: ``create_style`` takes rasterised PNGs.
* Output is requested as ``b64_json`` (no download, no URL to expire); a URL answer is downloaded at once through the
  allow-listed, auth-free ``Downloader``. Every result is magic-byte sniffed (``<svg`` vs ``PNG``).
* Token bucket from ``LIMITER_DEFAULTS`` (100 images/min, 5 requests/s). 429 backs off with jitter (honouring
  ``Retry-After``) a few times; 5xx is retried at most twice.
* Multipart field name for ``vectorize`` / ``removeBackground``: ``file``, falling back to ``image`` once and storing
  the ``recraft.file_field_name`` flag [UNVERIFIED].
* Every SVG still goes through ``imaging/svg.py`` before any of its pixels are used (the caller's job).
"""
from __future__ import annotations

import base64
import binascii
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from duoskin.providers._http import httpx, make_client
from duoskin.providers.base import (
    CallCtx,
    CapabilityFlags,
    Downloader,
    ProviderError,
    RateLimiter,
    backoff_delay,
    error_from_status,
    limiter_for,
    png_size,
    request_hash,
    sleep_checked,
    sniff_kind,
)
from duoskin.providers.pricing import recraft_cost

PROVIDER = "recraft"
BASE_URL = "https://external.api.recraft.ai/v1"
DEFAULT_DOWNLOAD_HOSTS = ("*.recraft.ai", "recraft.ai")

VECTOR_MODELS = ("recraftv4_1_vector", "recraftv4_1_utility_vector", "recraftv4_styles_vector", "recraftv4_1_pro_vector",
                 "recraftv4_1_utility_pro_vector", "recraftv4_styles_pro_vector")
STYLES_MODELS = frozenset(m for m in VECTOR_MODELS if "_styles_" in m)
PRO_MODELS = frozenset(m for m in VECTOR_MODELS if "_pro_" in m)
V4_SIZES = ("1024x1024", "1536x768", "768x1536", "1280x832", "832x1280", "1216x896", "896x1216", "1152x896", "896x1152",
            "832x1344", "1280x896", "896x1280", "1344x768", "768x1344")
V4_PRO_SIZES = ("2048x2048", "3072x1536", "1536x3072", "2560x1664", "1664x2560", "2432x1792", "1792x2432", "2304x1792",
                "1792x2304", "1664x2688", "2560x1792", "1792x2560", "2688x1536", "1536x2688")
STYLE_MODELS = ("recraftv4_styles_vector", "recraftv4_styles")
MAX_STYLE_REFS = 10
MAX_STYLE_BYTES = 10 * 1024 * 1024
FORBIDDEN_BODY_KEYS = ("negative_prompt", "no_text", "artistic_level", "style", "style_reference_urls", "text_layout")


@dataclass(frozen=True)
class VectorRequest:
    model: Literal["recraftv4_1_vector", "recraftv4_1_utility_vector", "recraftv4_styles_vector",
                   "recraftv4_1_pro_vector", "recraftv4_1_utility_pro_vector", "recraftv4_styles_pro_vector"]
    prompt: str                                   # flat form, <=5 numbered constraints, no hex, no style words when style_id is set
    size: str                                     # a V4 preset only (1024x1024, 1536x768, 768x1536, ...)
    n: int                                        # 1..6
    style_id: str | None = None                   # required by *_styles_* models
    style_match: Literal["precise", "flexible"] = "precise"
    colors: tuple[tuple[int, int, int], ...] = ()          # controls.colors ("preferable", not guaranteed)
    background_rgb: tuple[int, int, int] | None = None     # the sentinel colour (controls.background_color)
    # ---- additive fields (not sent) ----
    nonce: str = ""                               # "Reimagine" changes only the nonce; part of the mock seed
    tag: str = ""                                 # step template id (R1, R2) for mock fault selectors and logs


@dataclass
class VectorResult:
    svgs: list[bytes]                             # downloaded at once (URLs live ~24 h) or b64_json
    image_ids: list[str]
    credits: float | None
    style_id: str | None
    request_json: dict                            # stored in provenance (V4 has no seed)
    cost: dict[str, Any] | None = None
    request_id: str | None = None


class VectorGenProvider(Protocol):
    def generate(self, req: VectorRequest, ctx: CallCtx) -> VectorResult: ...
    def create_style(self, pngs: list[bytes], *, model: Literal["recraftv4_styles_vector", "recraftv4_styles"],
                     style: Literal["vector_illustration", "any"]) -> str: ...
    def vectorize(self, png: bytes, *, max_num_shapes: int | None = None) -> bytes: ...
    def remove_background(self, png: bytes) -> bytes: ...


class StyleRegistry:
    """Separate raster and vector ``style_id`` registries (GEN-09). Keys are the app's own names for a style."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, str]] = {"raster": {}, "vector": {}}
        self._lock = threading.Lock()

    def register(self, kind: Literal["raster", "vector"], key: str, style_id: str) -> None:
        with self._lock:
            self._d[kind][key] = style_id

    def get(self, kind: Literal["raster", "vector"], key: str) -> str | None:
        with self._lock:
            return self._d[kind].get(key)

    def kind_of(self, style_id: str) -> str | None:
        with self._lock:
            for kind, d in self._d.items():
                if style_id in d.values():
                    return kind
        return None


def style_kind_for_model(model: str) -> Literal["raster", "vector"]:
    return "vector" if model.endswith("_vector") else "raster"


def validate_request(req: VectorRequest, *, registry: StyleRegistry | None = None) -> None:
    """CHK-P03 pre-flight. Raises ``ProviderError(kind="bad_request")`` for a code bug."""
    def bad(msg: str, code: str) -> ProviderError:
        return ProviderError(PROVIDER, "bad_request", msg, code=code, billed="no")
    if req.model not in VECTOR_MODELS:
        raise bad(f"model {req.model!r} is not a Recraft V4.x vector model", "bad_model")
    if req.model in STYLES_MODELS and not req.style_id:
        raise bad(f"{req.model} requires a style_id (Recraft rejects it otherwise)", "style_id_required")
    if req.style_id and registry is not None:
        kind = registry.kind_of(req.style_id)
        if kind == "raster":
            raise bad("a raster style_id was given to a vector model (keep the registries separate)", "style_kind_mismatch")
    sizes = V4_PRO_SIZES if req.model in PRO_MODELS else V4_SIZES
    if req.size not in sizes:
        raise bad(f"size {req.size!r} is not a V4 preset for {req.model}: one of {', '.join(sizes)}", "bad_size")
    if not isinstance(req.n, int) or not 1 <= req.n <= 6:
        raise bad(f"n={req.n!r} must be 1..6", "bad_n")
    if not req.prompt or not req.prompt.strip() or len(req.prompt) > 10000:
        raise bad("prompt must be 1..10000 characters", "bad_prompt")
    if req.style_match not in ("precise", "flexible"):
        raise bad("style_match must be 'precise' or 'flexible'", "bad_style_match")
    for rgb in (*req.colors, *([req.background_rgb] if req.background_rgb else [])):
        if len(rgb) != 3 or not all(isinstance(c, int) and 0 <= c <= 255 for c in rgb):
            raise bad(f"colour {rgb!r} must be three integers 0..255", "bad_color")


def build_body(req: VectorRequest, *, response_format: str = "b64_json", style_match_ok: bool = True) -> dict[str, Any]:
    """The JSON body of ``POST /images/generations``. Contains no negative_prompt, no_text, artistic_level or style."""
    body: dict[str, Any] = {"prompt": req.prompt, "model": req.model, "size": req.size, "n": req.n, "response_format": response_format}
    if req.style_id:
        body["style_id"] = req.style_id
        if style_match_ok:
            body["style_match"] = req.style_match
    controls: dict[str, Any] = {}
    if req.colors:
        controls["colors"] = [{"rgb": list(c)} for c in req.colors]
    if req.background_rgb:
        controls["background_color"] = {"rgb": list(req.background_rgb)}
    if controls:
        body["controls"] = controls
    return body


def ensure_svg(raw: bytes) -> bytes:
    """Magic-byte sniff of a vector result (shared with the mock): ``<svg`` is fine, a PNG or anything else is a
    ``validation`` error (``billed="yes"``: the call was paid)."""
    kind = sniff_kind(raw)
    if kind != "svg":
        raise ProviderError(PROVIDER, "validation", f"expected SVG bytes, got {kind}", code=f"not_svg_{kind}", billed="yes",
                            user_hint="Recraft returned a picture instead of a vector. A raster style may have been used on a vector model.")
    return raw


def validate_style_inputs(pngs: list[bytes], model: str, style: str) -> None:
    """Pre-flight of ``create_style`` (shared with the mock)."""
    if model not in STYLE_MODELS:
        raise ProviderError(PROVIDER, "bad_request", f"model {model!r} cannot create styles", code="bad_model", billed="no")
    if (model.endswith("_vector")) != (style == "vector_illustration"):
        raise ProviderError(PROVIDER, "bad_request", "a vector style model needs style='vector_illustration' and a raster one 'any'",
                            code="style_model_mismatch", billed="no")
    if not 1 <= len(pngs) <= MAX_STYLE_REFS or sum(len(p) for p in pngs) > MAX_STYLE_BYTES:
        raise ProviderError(PROVIDER, "bad_request", f"1..{MAX_STYLE_REFS} images and at most 10 MB in total", code="style_refs_limits", billed="no")
    for p in pngs:
        if sniff_kind(p) != "png":
            raise ProviderError(PROVIDER, "bad_request", "style references must be PNG (SVG is not accepted: rasterise first)",
                                code="style_ref_not_png", billed="no")


def check_image_input(png: bytes, what: str) -> None:
    size = png_size(png)
    if size is None:
        raise ProviderError(PROVIDER, "bad_request", f"{what} must be a PNG", code="input_not_png", billed="no")
    if len(png) > 5 * 1024 * 1024 or min(size) < 256 or max(size) > 4096 or size[0] * size[1] > 16_000_000:
        raise ProviderError(PROVIDER, "bad_request", f"{what} {size[0]}x{size[1]} ({len(png)} bytes) is outside Recraft's limits "
                            "(5 MB, 256..4096 px per side, 16 MP)", code="input_limits", billed="no")


class RecraftProvider:
    """Real Recraft provider on httpx (injectable ``transport`` for tests)."""

    name = PROVIDER

    def __init__(self, api_key: str, *, transport: Any = None, flags: CapabilityFlags | None = None,
                 limiter: RateLimiter | None = None, registry: StyleRegistry | None = None,
                 cost_sink: Callable[[dict[str, Any]], None] | None = None, base_url: str = BASE_URL,
                 response_format: str | None = None, download_hosts: tuple[str, ...] = DEFAULT_DOWNLOAD_HOSTS,
                 download_transport: Any = None, sleep: Callable[[float], None] | None = None,
                 rate_limit_retries: int = 3, server_error_retries: int = 2, timeout: float = 120.0) -> None:
        self._client = make_client(timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {api_key}"})
        self._downloader = Downloader(PROVIDER, download_hosts, max_bytes=50 * 1024 * 1024, timeout=timeout,
                                      transport=download_transport if download_transport is not None else transport)
        self.base_url = base_url.rstrip("/")
        self.flags = flags or CapabilityFlags()
        self.limiter = limiter or limiter_for(PROVIDER)
        self.styles = registry or StyleRegistry()
        self.cost_sink = cost_sink
        self._response_format = response_format
        import time as _time
        self._sleep = sleep or _time.sleep
        self.rate_limit_retries = rate_limit_retries
        self.server_error_retries = server_error_retries
        self.last_request: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return "RecraftProvider()"

    @property
    def response_format(self) -> str:
        return self._response_format or str(self.flags.get("recraft.response_format", "b64_json"))

    # ----- HTTP ---------------------------------------------------------------------------------------------
    def _map_response_error(self, resp: Any) -> ProviderError:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        msg = ""
        code = None
        if isinstance(data, dict):
            err_obj = data.get("error")
            if isinstance(err_obj, dict):
                msg, code = str(err_obj.get("message") or ""), err_obj.get("code")
            else:
                msg, code = str(data.get("message") or err_obj or ""), data.get("code")
        msg = msg or f"HTTP {resp.status_code}"
        err = error_from_status(PROVIDER, resp.status_code, msg, headers=dict(resp.headers), code=str(code) if code else None,
                                request_id=resp.headers.get("x-request-id"))
        low = msg.lower()
        if resp.status_code in (400, 402, 403) and ("units" in low or "balance" in low or "insufficient" in low):
            err = ProviderError(PROVIDER, "billing", msg, http=resp.status_code, code=err.code, billed="no",
                                user_hint="Recraft says the account has no API units left. Buy units, then resume the queue.")
        if err.kind in ("bad_request", "auth", "permission", "billing"):
            err.billed = "no"
        return err

    def _request(self, method: str, path: str, ctx: CallCtx, *, images: int = 0, **kw: Any) -> Any:
        """One request through the limiter with 429 backoff (jitter, Retry-After) and 5xx retries (at most two)."""
        url = f"{self.base_url}{path}"
        rl = srv = 0
        while True:
            try:
                with self.limiter.acquire(images=images, ctx=ctx):
                    ctx.tick()
                    resp = self._client.request(method, url, **kw)
            except httpx.TimeoutException:
                raise ProviderError(PROVIDER, "timeout", "the request to Recraft timed out", retryable=True) from None
            except httpx.TransportError as e:
                raise ProviderError(PROVIDER, "network", f"could not reach Recraft ({type(e).__name__})", retryable=True) from None
            if resp.status_code < 400:
                return resp
            err = self._map_response_error(resp)
            if err.kind == "rate_limit":
                wait = err.retry_after_s if err.retry_after_s is not None else backoff_delay(rl)
                self.limiter.penalize(wait)
                if rl < self.rate_limit_retries:
                    rl += 1
                    sleep_checked(wait, ctx, sleep=self._sleep)
                    continue
            elif err.kind in ("server", "overloaded") and srv < self.server_error_retries:
                srv += 1
                sleep_checked(backoff_delay(srv - 1), ctx, sleep=self._sleep)
                continue
            raise err

    def _json(self, resp: Any) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(PROVIDER, "validation", "Recraft returned a body that is not JSON", code="not_json", billed="unknown") from None
        if not isinstance(data, dict):
            raise ProviderError(PROVIDER, "validation", "Recraft returned an unexpected JSON shape", code="bad_shape", billed="unknown")
        return data

    def _bytes_of(self, item: Any, ctx: CallCtx) -> bytes:
        """Bytes of one returned image: ``b64_json`` inline, else downloaded at once from ``url``."""
        if not isinstance(item, dict):
            raise ProviderError(PROVIDER, "validation", "an image entry has an unexpected shape", code="bad_shape")
        b64 = item.get("b64_json")
        if b64:
            try:
                return base64.b64decode(b64, validate=False)
            except (binascii.Error, ValueError):
                raise ProviderError(PROVIDER, "validation", "b64_json could not be decoded", code="bad_b64", billed="yes") from None
        url = item.get("url")
        if url:
            return self._downloader.fetch(str(url), ctx)
        raise ProviderError(PROVIDER, "validation", "an image entry has neither b64_json nor url", code="no_image", billed="yes")

    # ----- generation ---------------------------------------------------------------------------------------
    def generate(self, req: VectorRequest, ctx: CallCtx) -> VectorResult:
        validate_request(req, registry=self.styles)
        body = build_body(req, response_format=self.response_format, style_match_ok=bool(self.flags.get("recraft.style_match_ok", True)))
        assert not set(FORBIDDEN_BODY_KEYS) & set(body), "forbidden Recraft body key"
        self.last_request = body
        resp = self._request("POST", "/images/generations", ctx, images=req.n, json=body)
        data = self._json(resp)
        items = data.get("data") or []
        rid = resp.headers.get("x-request-id")
        svgs: list[bytes] = []
        for it in items:
            svgs.append(ensure_svg(self._bytes_of(it, ctx)))
        if not svgs:
            raise ProviderError(PROVIDER, "validation", "Recraft returned no images", code="no_images", billed="unknown", request_id=rid)
        credits = data.get("credits")
        ids = [str(it.get("image_id", "")) for it in items if isinstance(it, dict)]
        cost = recraft_cost(req.model, operation=f"images.generate:{req.tag or 'vec'}", n=len(svgs),
                            api_units=float(credits) if isinstance(credits, (int, float)) else None, request_id=rid)
        self._record(cost)
        returned_style = data.get("style_id") or req.style_id
        provenance = {**body, "nonce": req.nonce, "request_sha": request_hash(body)}
        return VectorResult(svgs=svgs, image_ids=ids, credits=float(credits) if isinstance(credits, (int, float)) else None,
                            style_id=str(returned_style) if returned_style else None, request_json=provenance, cost=cost, request_id=rid)

    # ----- styles -------------------------------------------------------------------------------------------
    def create_style(self, pngs: list[bytes], *, model: Literal["recraftv4_styles_vector", "recraftv4_styles"],
                     style: Literal["vector_illustration", "any"], ctx: CallCtx | None = None) -> str:
        """``POST /styles`` from 1..10 rasterised PNGs (SVG is not accepted). Returns the style id and registers it
        in the matching (raster or vector) registry under ``"style:<id>"``."""
        ctx = ctx or CallCtx.null()
        validate_style_inputs(pngs, model, style)
        files = [(f"file{i + 1}", (f"ref{i + 1}.png", p, "image/png")) for i, p in enumerate(pngs)]
        resp = self._request("POST", "/styles", ctx, files=files, data={"style": style, "model": model})
        data = self._json(resp)
        sid = data.get("id")
        if not sid:
            raise ProviderError(PROVIDER, "validation", "Recraft returned no style id", code="no_style_id", billed="unknown")
        self._record(recraft_cost("style_create", operation="styles.create", request_id=resp.headers.get("x-request-id")))
        self.styles.register(style_kind_for_model(model), f"style:{sid}", str(sid))
        return str(sid)

    # ----- utility endpoints --------------------------------------------------------------------------------
    def _file_endpoint(self, path: str, png: bytes, extra: dict[str, str], ctx: CallCtx) -> dict[str, Any]:
        """Multipart call with the ``file`` / ``image`` field-name fallback."""
        names = [str(self.flags.get("recraft.file_field_name", "file"))]
        names.append("image" if names[0] == "file" else "file")
        last: ProviderError | None = None
        for i, field_name in enumerate(names):
            try:
                resp = self._request("POST", path, ctx, files={field_name: ("image.png", png, "image/png")},
                                     data={"response_format": self.response_format, **extra})
            except ProviderError as e:
                if e.kind == "bad_request" and i == 0:
                    last = e
                    continue
                raise
            if i == 1:
                self.flags.set("recraft.file_field_name", field_name)
            return self._json(resp) | {"_rid": resp.headers.get("x-request-id")}
        assert last is not None
        raise last

    def vectorize(self, png: bytes, *, max_num_shapes: int | None = None, ctx: CallCtx | None = None) -> bytes:
        """PNG to SVG ($0.01)."""
        ctx = ctx or CallCtx.null()
        check_image_input(png, "vectorize input")
        extra: dict[str, str] = {}
        if max_num_shapes is not None:
            if max_num_shapes < 1:
                raise ProviderError(PROVIDER, "bad_request", "max_num_shapes must be >= 1", code="bad_shapes", billed="no")
            extra = {"limit_num_shapes": "on", "max_num_shapes": str(int(max_num_shapes))}
        data = self._file_endpoint("/images/vectorize", png, extra, ctx)
        raw = self._bytes_of(data.get("image"), ctx)
        if sniff_kind(raw) != "svg":
            raise ProviderError(PROVIDER, "validation", "vectorize did not return an SVG", code="not_svg", billed="yes")
        self._record(recraft_cost("vectorize", operation="images.vectorize", request_id=data.get("_rid")))
        return raw

    def remove_background(self, png: bytes, *, ctx: CallCtx | None = None) -> bytes:
        """PNG with its background removed ($0.01); the result is RGBA PNG."""
        ctx = ctx or CallCtx.null()
        check_image_input(png, "removeBackground input")
        data = self._file_endpoint("/images/removeBackground", png, {}, ctx)
        raw = self._bytes_of(data.get("image"), ctx)
        if sniff_kind(raw) != "png":
            raise ProviderError(PROVIDER, "validation", "removeBackground did not return a PNG", code="not_png", billed="yes")
        self._record(recraft_cost("remove_background", operation="images.removeBackground", request_id=data.get("_rid")))
        return raw

    def _record(self, cost: dict[str, Any]) -> None:
        if self.cost_sink is not None:
            try:
                self.cost_sink(cost)
            except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide paid results
                pass


__all__ = [
    "BASE_URL", "PRO_MODELS", "STYLES_MODELS", "V4_PRO_SIZES", "V4_SIZES", "VECTOR_MODELS", "RecraftProvider", "StyleRegistry",
    "VectorGenProvider", "VectorRequest", "VectorResult", "build_body", "check_image_input", "ensure_svg", "style_kind_for_model",
    "validate_request", "validate_style_inputs",
]
