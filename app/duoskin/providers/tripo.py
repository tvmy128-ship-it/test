"""Tripo v3 REST adapter (APP_SPEC 7.5, bible 14, 15 and Appendix B).

Our own client on ``https://openapi.tripo3d.ai/v3`` (the official SDK still creates tasks through v2). Every
operation creates a task and returns its id; ``wait()`` polls it.

Safety rules (ENG-03, ENG-06, ENG-07, ACC-05, ACC-06):

* **A paid POST is never resent once its body may have reached Tripo.** ``ConnectError`` / ``ConnectTimeout`` (nothing
  sent) are retried; a ``ReadTimeout``, a connection reset or a 5xx after the body was sent raise
  ``submission_uncertain`` (a ``SubmissionUncertainError`` whose ``context`` holds ``endpoint``, ``submitted_at`` and
  ``body``) and ``reconcile_uncertain`` looks the task up in ``/account/usage`` and ``GET /tasks/{id}``, comparing
  ``input.model_seed``.
* The task id is handed to ``ctx.set_remote_ref(task_id)`` the moment Tripo answers and BEFORE the method returns,
  so a crash cannot lead to a second submit.
* ``P2Params.body()`` always sends ``face_limit``, ``quad=false``, ``pbr=false``, ``auto_size=false``, ``texture_quality=
  "standard"``, ``texture_alignment="original_image"`` and both seeds; ``compress``, ``export_uv``, ``generate_parts`` and
  ``return_multiview`` are never emitted (``compress`` / ``export_uv`` only when the caller sets them explicitly, as a
  test-day arm); ``smart_low_poly`` belongs to the H3.1 route only. ``check_body`` runs before every paid POST.
* Downloads use a separate client: host allowlist, no Authorization header, no redirects, 150 MB cap, magic bytes
  (``glTF``, ``Kaydara FBX Binary``, ``PK``), SHA-256; a 403/404 on a signed URL re-GETs the task for fresh URLs (up to 3 times).
* A poll soft timeout (20 minutes) marks the status ``slow`` and keeps polling; it never resubmits.
* Signed URLs and the Bearer key are never logged or put in error messages.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol

from pydantic import ConfigDict, Field, field_validator

from duoskin.models.common import Strict
from duoskin.providers._http import httpx, make_client
from duoskin.providers.base import (
    CallCtx,
    CapabilityFlags,
    Downloader,
    ProviderError,
    RateLimiter,
    backoff_delay,
    limiter_for,
    parse_retry_after,
    sleep_checked,
    sniff_kind,
    utcnow,
)
from duoskin.providers.pricing import tripo_cost, tripo_credits

PROVIDER = "tripo"
API_ROOT = "https://openapi.tripo3d.ai/v3"
DOWNLOAD_HOSTS = ("tripo-data.rg1.data.tripo3d.com", "*.tripo3d.ai")
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
P2, P1, H31 = "P2-20260801", "P1-20260311", "v3.1-20260211"
VIEWS = ("front", "left", "back", "right")           # "left" = the SUBJECT's own left side (90 degrees)
View = Literal["front", "left", "back", "right"]
FACE_LIMIT = {"plush_pet": 3000, "bag": 3000, "small_hat": 3000, "prop": 3000, "keychain_charm": 1500, "hair_custom": 3500}
FACE_LIMIT_RANGE = {P2: (48, 50000), P1: (48, 20000), H31: (500, 20000)}
SEEDS = (11, 29, 47)                                  # one at a time; stop at the first pass
FORBIDDEN_BODY_KEYS = frozenset({"compress", "generate_parts", "export_uv", "return_multiview"})
TERMINAL = ("success", "failed", "cancelled")

ENDPOINTS: dict[str, str] = {
    "upload": "/files",
    "image_to_multiview": "/generation/image-to-multiview",
    "edit_multiview": "/generation/edit-multiview",
    "multiview_to_model": "/generation/multiview-to-model",
    "image_to_model": "/generation/image-to-model",
    "text_to_model": "/generation/text-to-model",
    "convert": "/models/convert",
    "import_model": "/models/import",
    "texture_model": "/models/texture",
    "mesh_segment": "/mesh/segment",
    "mesh_complete": "/mesh/complete",
    "retopology": "/mesh/decimate",      # Tripo's decimate endpoint is the retopology call in the local v3 OpenAPI
}
_OP_OF_PATH = {v: k for k, v in ENDPOINTS.items()}
PAID_OPS = frozenset(set(ENDPOINTS) - {"upload", "import_model"})


# --------------------------------------------------------------------------------------------------------------
# Request parameter classes (one per route)
# --------------------------------------------------------------------------------------------------------------

class _RouteParams(Strict):
    """Fields shared by the P2, P1 and H3.1 routes. ``body()`` drops ``None`` fields."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    face_limit: int
    quad: Literal[False] = False
    texture: Literal[True] = True
    texture_quality: Literal["standard"] = "standard"      # 2K; never detailed/extreme
    pbr: Literal[False] = False                            # ComfyUI defaults it to true: always send false
    texture_alignment: Literal["original_image"] = "original_image"
    orientation: Literal["default", "align_image"] = "default"
    auto_size: Literal[False] = False
    model_seed: int
    texture_seed: int
    route: Literal["p2", "p1", "h31"] = Field(default="p2", exclude=True)

    _MODEL: ClassVar[str] = P2
    _EXTRAS: ClassVar[tuple[str, ...]] = ()

    @field_validator("face_limit")
    @classmethod
    def _range(cls, v: int) -> int:
        lo, hi = FACE_LIMIT_RANGE[cls._MODEL]
        if not lo <= v <= hi:
            raise ValueError(f"face_limit must be {lo}..{hi} for {cls._MODEL}")
        return v

    @classmethod
    def for_seed(cls, face_limit: int, seed: int = SEEDS[0], **kw: Any):
        """One seed for both ``model_seed`` and ``texture_seed`` (11 -> 29 -> 47, one at a time)."""
        return cls(face_limit=face_limit, model_seed=seed, texture_seed=seed, **kw)

    def body(self) -> dict[str, Any]:
        raise NotImplementedError

    def explicit_extras(self) -> frozenset[str]:
        """Body keys the caller asked for explicitly although they are normally forbidden (test-day arms)."""
        return frozenset(k for k in self._EXTRAS if getattr(self, k, None) is not None)


class P2Params(_RouteParams):
    """Route ``p2``: T3 multiview-to-model and the P2 image-to-model fallback (APP_SPEC 7.5)."""

    model: Literal["P2-20260801"] = "P2-20260801"
    orthographic_projection: bool | None = None            # A/B flag only [UNVERIFIED for P2]
    texture_version: str | None = None                     # A/B flag only [UNVERIFIED], together with delight=False
    delight: bool | None = None
    export_uv: bool | None = None                          # never emitted unless asked
    compress: Literal["geometry"] | None = None            # meshopt: never emitted unless asked
    route: Literal["p2"] = Field(default="p2", exclude=True)
    _MODEL: ClassVar[str] = P2
    _EXTRAS: ClassVar[tuple[str, ...]] = ("export_uv", "compress")

    def body(self) -> dict[str, Any]:
        b: dict[str, Any] = {"model": self.model, "face_limit": self.face_limit, "quad": False, "texture": True,
                             "texture_quality": "standard", "pbr": False, "texture_alignment": "original_image",
                             "orientation": self.orientation, "auto_size": False, "model_seed": self.model_seed,
                             "texture_seed": self.texture_seed}
        for k in ("orthographic_projection", "texture_version", "delight", "export_uv", "compress"):
            v = getattr(self, k)
            if v is not None:
                b[k] = v
        return b


class P1Params(_RouteParams):
    """Route ``p1``: the P1 multiview fallback (``P1-20260311``). Fields beyond the shared set are [UNVERIFIED]."""

    model: Literal["P1-20260311"] = "P1-20260311"
    route: Literal["p1"] = Field(default="p1", exclude=True)
    _MODEL: ClassVar[str] = P1

    def body(self) -> dict[str, Any]:
        return {"model": self.model, "face_limit": self.face_limit, "quad": False, "texture": True, "texture_quality": "standard",
                "pbr": False, "texture_alignment": "original_image", "orientation": self.orientation, "auto_size": False,
                "model_seed": self.model_seed, "texture_seed": self.texture_seed}


class H31Params(_RouteParams):
    """Route ``h31``: H3.1 + ``smart_low_poly`` for simple plush (``smart_low_poly`` belongs to this route only)."""

    model: Literal["v3.1-20260211"] = "v3.1-20260211"
    face_limit: int = 3000
    smart_low_poly: Literal[True] = True
    route: Literal["h31"] = Field(default="h31", exclude=True)
    _MODEL: ClassVar[str] = H31

    def body(self) -> dict[str, Any]:
        return {"model": self.model, "face_limit": self.face_limit, "quad": False, "texture": True, "texture_quality": "standard",
                "pbr": False, "texture_alignment": "original_image", "orientation": self.orientation, "auto_size": False,
                "model_seed": self.model_seed, "texture_seed": self.texture_seed, "smart_low_poly": True}


RouteParams = P2Params | P1Params | H31Params
_ROUTE_MODEL = {"p2": P2, "p1": P1, "h31": H31}


def check_body(b: dict[str, Any], route: str = "p2", allow: Iterable[str] = ()) -> dict[str, Any]:
    """CHK-P04 / ACC-02: runs before every paid generation POST. Raises ``ProviderError(kind="bad_request")``."""
    def bad(msg: str) -> ProviderError:
        return ProviderError(PROVIDER, "bad_request", f"Tripo body check failed: {msg}", code="body_check", billed="no",
                             user_hint="The app built an invalid Tripo request. This is a bug; nothing was sent.")
    model = _ROUTE_MODEL.get(route)
    if model is None:
        raise bad(f"unknown route {route!r}")
    lo, hi = FACE_LIMIT_RANGE[model]
    if b.get("model") != model:
        raise bad(f"model must be {model} on route {route}")
    if b.get("pbr") is not False or b.get("quad") is not False or b.get("auto_size") is not False:
        raise bad("pbr, quad and auto_size must be false")
    fl = b.get("face_limit")
    if not isinstance(fl, int) or not lo <= fl <= hi:
        raise bad(f"face_limit must be present and {lo}..{hi}")
    banned = (FORBIDDEN_BODY_KEYS & b.keys()) - set(allow)
    if banned:
        raise bad(f"forbidden keys {sorted(banned)}")
    if ("smart_low_poly" in b) != (route == "h31"):
        raise bad("smart_low_poly is required on h31 and forbidden elsewhere")
    if b.get("texture_quality") != "standard":
        raise bad("texture_quality must be 'standard'")
    if b.get("texture_alignment") != "original_image":
        raise bad("texture_alignment must be 'original_image'")
    if not isinstance(b.get("model_seed"), int) or not isinstance(b.get("texture_seed"), int):
        raise bad("model_seed and texture_seed are required")
    return b


def views_inputs(views: dict[str, str]) -> list[dict[str, str]]:
    """Named view objects in the fixed order: ``[{"front": tok}, {"left": tok}, ...]`` (never positional)."""
    if "front" not in views or len(views) < 2 or not set(views) <= set(VIEWS):
        raise ProviderError(PROVIDER, "bad_request", "multiview needs the front view plus at least one of left, back, right",
                            code="bad_views", billed="no")
    for v, tok in views.items():
        if not isinstance(tok, str) or not tok:
            raise ProviderError(PROVIDER, "bad_request", f"view {v!r} has no file token", code="bad_views", billed="no")
    return [{v: views[v]} for v in VIEWS if v in views]


def multiview_body(views: dict[str, str] | str, p: RouteParams) -> dict[str, Any]:
    """T3 body. A ``str`` reuses a multiview task (``inputs: [{"task_id": ...}]``)."""
    inputs = [{"task_id": views}] if isinstance(views, str) else views_inputs(views)
    return {**p.body(), "inputs": inputs}


def image_body(token: str, p: RouteParams) -> dict[str, Any]:
    """T4 body: ``enable_image_autofix`` is always false."""
    return {**p.body(), "input": token, "enable_image_autofix": False}


def edit_views_body(mv_task_id: str, prompts: dict[str, str]) -> dict[str, Any]:
    if not 1 <= len(prompts) <= 4 or not set(prompts) <= set(VIEWS):
        raise ProviderError(PROVIDER, "bad_request", "edit-multiview takes 1 to 4 prompts keyed front/left/back/right", code="bad_prompts", billed="no")
    for v, text in prompts.items():
        if not text or len(text) > 1024:
            raise ProviderError(PROVIDER, "bad_request", f"the prompt for {v} must be 1..1024 characters", code="bad_prompts", billed="no")
    return {"input": mv_task_id, "prompts": [{"view": v, "prompt": prompts[v]} for v in VIEWS if v in prompts]}


# --------------------------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------------------------

class RemoteStatus(Strict):
    task_id: str
    status: Literal["queued", "running", "success", "failed", "cancelled"]   # legacy banned/expired/unknown -> failed
    progress: int | None = None
    error_code: int | None = None
    error_message: str | None = None
    credits_consumed: float | None = None
    input: dict[str, Any] = Field(default_factory=dict)                      # the parameters actually used (model_seed...)
    output_urls: dict[str, str] = Field(default_factory=dict, repr=False)    # model_url, rendered_image_url, view urls (never logged)
    type: str | None = None
    slow: bool = False                                                        # set by wait() after the soft timeout

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def failure_error(self) -> ProviderError | None:
        """The ProviderError for a failed or cancelled task (``None`` while running or on success).

        2008 (moderation) stops and shows the user, never resubmit the same images; 2018 (queue expiry) is
        ``remote_failed`` with ``retryable=True`` (resubmit once); anything else is ``remote_failed`` (next seed once,
        then the user)."""
        if self.status not in ("failed", "cancelled"):
            return None
        code = self.error_code
        msg = self.error_message or f"Tripo task {self.status}"
        billed = "yes" if (self.credits_consumed or 0) > 0 else "no"
        if code == 2008:
            return ProviderError(PROVIDER, "moderation", msg, code="2008", billed=billed, context={"task_id": self.task_id},
                                 user_hint="Tripo's safety filter rejected these images. They were not resubmitted; change the design.")
        if code == 2018:
            return ProviderError(PROVIDER, "remote_failed", msg, code="2018", retryable=True, billed=billed, context={"task_id": self.task_id},
                                 user_hint="The Tripo queue expired the job before it started. The app resubmits it once.")
        if code == 2010:
            return ProviderError(PROVIDER, "billing", msg, code="2010", billed=billed, context={"task_id": self.task_id})
        if code == 2015:
            return ProviderError(PROVIDER, "not_found", msg, code="2015", billed=billed, context={"task_id": self.task_id},
                                 user_hint="Tripo does not accept this model ID any more. Update the model ID in Settings.")
        return ProviderError(PROVIDER, "remote_failed", msg, code=str(code) if code is not None else self.status, billed=billed,
                             context={"task_id": self.task_id})


@dataclass(frozen=True)
class DownloadedFile:
    key: str
    data: bytes
    kind: str                # glb | fbx | zip | png | jpeg | webp
    sha256: str
    size: int


class TripoProvider(Protocol):
    def balance(self) -> tuple[float, float]: ...
    def usage(self, *, limit: int = 50, offset: int = 0) -> list[dict]: ...
    def upload(self, png: bytes, *, name: str) -> str: ...
    def image_to_multiview(self, token: str) -> str: ...
    def edit_multiview(self, mv_task_id: str, prompts: dict[View, str]) -> str: ...
    def multiview_to_model(self, views: dict[View, str] | str, p: RouteParams) -> str: ...
    def image_to_model(self, token: str, p: RouteParams) -> str: ...
    def convert(self, source: str, *, fmt: Literal["GLTF", "FBX", "OBJ"], face_limit: int | None,
                texture_size: int = 1024, texture_format: Literal["PNG"] = "PNG") -> str: ...
    def import_model(self, token: str) -> str: ...
    def task(self, task_id: str) -> RemoteStatus: ...
    def tasks(self, ids: list[str]) -> tuple[dict[str, RemoteStatus], list[str]]: ...
    def download(self, task_id: str, keys: list[str]) -> dict[str, bytes]: ...
    def reconcile_uncertain(self, *, endpoint: str, submitted_at: datetime, body: dict) -> str | None: ...


# --------------------------------------------------------------------------------------------------------------
# Parsing and error mapping (shared with the mock)
# --------------------------------------------------------------------------------------------------------------

def parse_task(data: dict[str, Any]) -> RemoteStatus:
    """``RemoteStatus`` from the ``data`` object of ``GET /tasks/{id}`` (legacy statuses become ``failed``)."""
    raw = str(data.get("status", "")).lower()
    code = data.get("error_code")
    msg = data.get("error_message")
    status: str
    if raw in ("queued", "running", "success", "failed", "cancelled"):
        status = raw
    elif raw == "canceled":
        status = "cancelled"
    elif raw in ("pending", "waiting"):
        status = "queued"
    elif raw in ("processing", "in_progress"):
        status = "running"
    else:                                              # banned / expired / unknown / anything new: never treated as success
        status = "failed"
        if code is None:
            code = {"banned": 2008, "expired": 2018}.get(raw)
        msg = msg or f"Tripo reported status {raw or 'unknown'!r}"
    urls: dict[str, str] = {}
    out = data.get("output") or {}
    if isinstance(out, dict):
        for k, v in out.items():
            if isinstance(v, str) and v.startswith("http") and k.endswith(("url", "_urls")):
                urls[k] = v
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, str) and v2.startswith("http"):
                        urls.setdefault(k2, v2)
            elif isinstance(v, list):
                for i, v2 in enumerate(v):
                    if isinstance(v2, str) and v2.startswith("http"):
                        urls[f"{k}[{i}]"] = v2
    prog = data.get("progress")
    cc = data.get("credits_consumed")
    return RemoteStatus(task_id=str(data.get("task_id", "")), status=status, progress=int(prog) if isinstance(prog, (int, float)) else None,
                        error_code=int(code) if isinstance(code, (int, float)) else None, error_message=str(msg) if msg else None,
                        credits_consumed=float(cc) if isinstance(cc, (int, float)) else None,
                        input=dict(data.get("input") or {}), output_urls=urls, type=data.get("type"))


def error_from_response(status: int, payload: Any, headers: dict[str, str] | None = None, *, paid_post: bool = False,
                        request_id: str | None = None) -> ProviderError:
    """Map an HTTP error (or a ``code != 0`` envelope) to a ``ProviderError`` per the retry table of APP_SPEC 7.5."""
    data = payload if isinstance(payload, dict) else {}
    code_raw = data.get("code")
    code = int(code_raw) if isinstance(code_raw, (int, float)) and not isinstance(code_raw, bool) else None
    msg = str(data.get("message") or data.get("error") or f"HTTP {status}")
    rid = str(data.get("request_id") or request_id or "") or None
    hint = str(data.get("suggestion") or "")
    base: dict[str, Any] = {"http": status or None, "code": str(code) if code is not None else None, "request_id": rid, "user_hint": ""}
    retry_after = parse_retry_after(headers)
    if code == 2008:
        return ProviderError(PROVIDER, "moderation", msg, billed="no", **base)
    if code == 2000 or (status == 429 and "concurren" in msg.lower()):
        return ProviderError(PROVIDER, "concurrency", msg, retryable=True, billed="no", retry_after_s=retry_after, **base)
    if code == 1007 or status == 429:
        return ProviderError(PROVIDER, "rate_limit", msg, retryable=True, billed="no", retry_after_s=retry_after, **base)
    if code == 2010:
        return ProviderError(PROVIDER, "billing", msg, billed="no", **{**base, "user_hint": "Tripo says the account is out of credits. Add credits, then resume."})
    if code == 2015:
        return ProviderError(PROVIDER, "not_found", msg, billed="no",
                             **{**base, "user_hint": "Tripo does not accept this model ID any more. Update the model ID in Settings."})
    if status == 401 or code in (1001, 1002):
        return ProviderError(PROVIDER, "auth", msg, billed="no", **base)
    if status == 403:
        return ProviderError(PROVIDER, "permission", msg, billed="no", **{**base, "user_hint": hint or ""})
    if status == 404:
        return ProviderError(PROVIDER, "not_found", msg, billed="no", **base)
    if status in (400, 405, 409, 413, 422) or (status == 200 and code not in (None, 0)):
        return ProviderError(PROVIDER, "bad_request", msg, billed="no", **{**base, "user_hint": hint or ""})
    if status >= 500:
        if paid_post:       # the body was sent: the task may exist
            return ProviderError(PROVIDER, "submission_uncertain", f"HTTP {status} after the request body was sent", billed="unknown", **base)
        return ProviderError(PROVIDER, "server", msg, retryable=True, **base)
    return ProviderError(PROVIDER, "other", msg, **base)


def _norm_type(t: Any) -> str:
    return str(t or "").lower().replace("-", "_").replace("/", "_").strip("_")


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v)
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def pick_reconciled(candidates: list[dict[str, Any]], statuses: dict[str, RemoteStatus], body: dict[str, Any],
                    submitted_at: datetime, exclude: set[str]) -> str | None:
    """The shared reconcile decision (the mock uses it too): among ``/account/usage`` rows of the same type within
    +-2 minutes, prefer the one whose ``input.model_seed`` (and ``texture_seed``) equals the body's; with no seeds in
    the body, the row closest to ``submitted_at``."""
    seeded = [k for k in ("model_seed", "texture_seed") if k in body]
    scored: list[tuple[float, str]] = []
    for row in candidates:
        tid = str(row.get("task_id", ""))
        if not tid or tid in exclude:
            continue
        st = statuses.get(tid)
        if seeded and (st is None or any(st.input.get(k) != body[k] for k in seeded)):
            continue
        ts = _parse_ts(row.get("created_at"))
        dt = abs((ts - submitted_at).total_seconds()) if ts else 1e9
        scored.append((dt, tid))
    if not scored:
        return None
    scored.sort()
    return scored[0][1]


# --------------------------------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------------------------------

def poll_intervals(first: float = 5.0, start: float = 3.0, factor: float = 1.4, cap: float = 15.0) -> Iterable[float]:
    """5 s, then 3 s growing by x1.4 up to 15 s."""
    yield first
    cur = start
    while True:
        yield cur
        cur = min(cap, cur * factor)


class TripoApi:
    """Real Tripo v3 client on httpx (``transport`` / ``download_transport`` are for tests)."""

    name = PROVIDER

    def __init__(self, api_key: str, *, transport: Any = None, download_transport: Any = None,
                 flags: CapabilityFlags | None = None, limiter: RateLimiter | None = None,
                 cost_sink: Callable[[dict[str, Any]], None] | None = None, base_url: str = API_ROOT,
                 download_hosts: tuple[str, ...] = DOWNLOAD_HOSTS, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None, get_retries: int = 5, rate_limit_retries: int = 5,
                 connect_retries: int = 3, timeout: float = 60.0, check_balance: bool = False) -> None:
        self._client = make_client(timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {api_key}"})
        self._downloader = Downloader(PROVIDER, download_hosts, max_bytes=MAX_DOWNLOAD_BYTES, timeout=120.0,
                                      transport=download_transport if download_transport is not None else transport)
        self.base_url = base_url.rstrip("/")
        self.flags = flags or CapabilityFlags()
        self.limiter = limiter or limiter_for(PROVIDER)
        self.cost_sink = cost_sink
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.get_retries = get_retries
        self.rate_limit_retries = rate_limit_retries
        self.connect_retries = connect_retries
        self.check_balance = check_balance
        self._ops: dict[str, tuple[str, str, int]] = {}      # task_id -> (operation, model, views) for the cost record
        self._costed: set[str] = set()
        self.last_body: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return "TripoApi()"

    # ----- low-level HTTP -----------------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _payload(resp: Any) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    def _envelope(self, resp: Any, *, paid_post: bool) -> dict[str, Any]:
        """Return ``data`` of a 2xx envelope, or raise the mapped error."""
        payload = self._payload(resp)
        if resp.status_code >= 400:
            raise error_from_response(resp.status_code, payload, dict(resp.headers), paid_post=paid_post,
                                      request_id=resp.headers.get("x-request-id"))
        if not isinstance(payload, dict):
            if paid_post:
                raise ProviderError(PROVIDER, "submission_uncertain", "Tripo answered 2xx with an unreadable body", code="unreadable_body")
            raise ProviderError(PROVIDER, "validation", "Tripo returned an unreadable body", code="unreadable_body")
        code = payload.get("code")
        if isinstance(code, (int, float)) and code != 0:
            raise error_from_response(resp.status_code, payload, dict(resp.headers), paid_post=paid_post)
        data = payload.get("data")
        return data if isinstance(data, (dict, list)) else {}

    def _get(self, path: str, ctx: CallCtx | None = None, *, params: dict[str, Any] | None = None) -> dict[str, Any] | list:
        """GET with up to ``get_retries`` retries on a network error or a 5xx (backoff), 429 backoff, no other retries."""
        ctx = ctx or CallCtx.null()
        net = rl = 0
        while True:
            try:
                with self.limiter.acquire(ctx=ctx):
                    resp = self._client.get(self._url(path), params=params)
            except httpx.TransportError as e:
                if net < self.get_retries:
                    sleep_checked(backoff_delay(net), ctx, sleep=self._sleep)
                    net += 1
                    continue
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "network"
                raise ProviderError(PROVIDER, kind, f"Tripo GET {path} failed ({type(e).__name__})", retryable=True, billed="no") from None
            try:
                return self._envelope(resp, paid_post=False)
            except ProviderError as err:
                if err.kind == "server" and net < self.get_retries:
                    sleep_checked(backoff_delay(net), ctx, sleep=self._sleep)
                    net += 1
                    continue
                if err.kind == "rate_limit" and rl < self.rate_limit_retries:
                    wait = err.retry_after_s if err.retry_after_s is not None else backoff_delay(rl)
                    self.limiter.penalize(wait)
                    sleep_checked(wait, ctx, sleep=self._sleep)
                    rl += 1
                    continue
                raise

    def _post(self, op: str, body: dict[str, Any] | None, ctx: CallCtx, *, files: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST with the retry table of APP_SPEC 7.5. A paid POST is never resent after its body may have been received."""
        path = ENDPOINTS[op]
        paid = op in PAID_OPS
        submitted_at = utcnow()
        connect = rl = srv = 0
        while True:
            try:
                with self.limiter.acquire(ctx=ctx):
                    ctx.tick()
                    resp = (self._client.post(self._url(path), files=files) if files is not None
                            else self._client.post(self._url(path), json=body))
            except (httpx.ConnectError, httpx.ConnectTimeout):                   # nothing was sent: safe to retry
                if connect < self.connect_retries:
                    sleep_checked(backoff_delay(connect), ctx, sleep=self._sleep)
                    connect += 1
                    continue
                raise ProviderError(PROVIDER, "network", "could not connect to Tripo", retryable=True, billed="no") from None
            except httpx.TransportError as e:                                      # the body may have been sent
                if paid:
                    raise self._uncertain(op, path, body, submitted_at, f"{type(e).__name__} after the request was sent") from None
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "network"
                raise ProviderError(PROVIDER, kind, f"Tripo POST {path} failed ({type(e).__name__})", retryable=True) from None
            try:
                return self._envelope(resp, paid_post=paid)  # type: ignore[return-value]
            except ProviderError as err:
                if err.kind == "submission_uncertain":
                    err.context.update(self._uncertain_context(op, path, body, submitted_at))
                    raise
                if err.kind == "rate_limit" and rl < self.rate_limit_retries:     # nothing was created: back off 1 -> 32 s
                    wait = err.retry_after_s if err.retry_after_s is not None else backoff_delay(rl)
                    self.limiter.penalize(wait)
                    sleep_checked(wait, ctx, sleep=self._sleep)
                    rl += 1
                    continue
                if err.kind == "concurrency":
                    self.limiter.lower_concurrency()
                if err.kind == "server" and not paid and srv < 2:
                    sleep_checked(backoff_delay(srv), ctx, sleep=self._sleep)
                    srv += 1
                    continue
                raise

    @staticmethod
    def _uncertain_context(op: str, path: str, body: dict[str, Any] | None, submitted_at: datetime) -> dict[str, Any]:
        return {"op": op, "endpoint": path, "submitted_at": submitted_at, "body": dict(body or {})}

    def _uncertain(self, op: str, path: str, body: dict[str, Any] | None, submitted_at: datetime, why: str) -> ProviderError:
        return ProviderError(PROVIDER, "submission_uncertain", f"Tripo {path}: {why}; not resent", code="submission_uncertain",
                             billed="unknown", context=self._uncertain_context(op, path, body, submitted_at),
                             user_hint="Checking whether Tripo received the job. The app will not send it a second time.")

    # ----- task creation ------------------------------------------------------------------------------------
    def _create(self, op: str, body: dict[str, Any], ctx: CallCtx | None, *, model: str = P2, views: int = 1,
                route: str | None = None, allow: Iterable[str] = ()) -> str:
        ctx = ctx or CallCtx.null()
        if route is not None:
            check_body(body, route, allow)
        if self.check_balance and op in PAID_OPS:
            self.ensure_credits(tripo_credits(op, views=views))
        self.last_body = body
        data = self._post(op, body, ctx)
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            raise ProviderError(PROVIDER, "submission_uncertain", "Tripo answered without a task_id", code="no_task_id",
                                context=self._uncertain_context(op, ENDPOINTS[op], body, utcnow()))
        task_id = str(task_id)
        self._ops[task_id] = (op, model, views)
        try:
            ctx.set_remote_ref(task_id)                    # persisted BEFORE returning (ENG-06)
        except Exception as e:  # noqa: BLE001 - never lose the task id even if persisting failed
            raise ProviderError(PROVIDER, "other", f"could not persist the remote reference: {type(e).__name__}", code="remote_ref_failed",
                                context={"task_id": task_id}, billed="yes") from None
        return task_id

    def upload(self, png: bytes, *, name: str, ctx: CallCtx | None = None) -> str:
        """``POST /files`` (free): returns the ``file_token``. Upload just before use (tokens expire)."""
        kind = sniff_kind(png)
        if kind not in ("png", "jpeg") or len(png) > 20 * 1024 * 1024:
            raise ProviderError(PROVIDER, "bad_request", "Tripo image uploads must be PNG or JPEG, at most 20 MB", code="bad_upload", billed="no")
        data = self._post("upload", None, ctx or CallCtx.null(), files={"file": (name, png, f"image/{'png' if kind == 'png' else 'jpeg'}")})
        token = data.get("file_token") if isinstance(data, dict) else None
        if not token:
            raise ProviderError(PROVIDER, "validation", "Tripo returned no file_token", code="no_file_token")
        return str(token)

    def image_to_multiview(self, token: str, *, ctx: CallCtx | None = None) -> str:
        """T1 (10 credits)."""
        return self._create("image_to_multiview", {"input": token}, ctx, model="image_to_multiview")

    def edit_multiview(self, mv_task_id: str, prompts: dict[View, str], *, ctx: CallCtx | None = None) -> str:
        """T2 (5 credits per view; once per set)."""
        body = edit_views_body(mv_task_id, prompts)
        return self._create("edit_multiview", body, ctx, model="edit_multiview", views=len(prompts))

    def multiview_to_model(self, views: dict[View, str] | str, p: RouteParams, *, ctx: CallCtx | None = None) -> str:
        """T3 (110 credits on P2). ``views``: named file tokens (front required), or a task id to reuse a multiview task."""
        body = multiview_body(views, p)
        return self._create("multiview_to_model", body, ctx, model=body["model"], route=p.route, allow=p.explicit_extras())

    def image_to_model(self, token: str, p: RouteParams, *, ctx: CallCtx | None = None) -> str:
        """T4 (``enable_image_autofix`` is always false)."""
        body = image_body(token, p)
        return self._create("image_to_model", body, ctx, model=body["model"], route=p.route, allow=p.explicit_extras())

    def text_to_model(self, prompt: str, p: RouteParams, *, negative_prompt: str = "", ctx: CallCtx | None = None) -> str:
        if not prompt or len(prompt) > 1024 or len(negative_prompt) > 255:
            raise ProviderError(PROVIDER, "bad_request", "prompt must be 1..1024 characters (negative_prompt <= 255)", code="bad_prompt", billed="no")
        body = {**p.body(), "prompt": prompt}
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        return self._create("text_to_model", body, ctx, model=body["model"], route=p.route, allow=p.explicit_extras())

    def convert(self, source: str, *, fmt: Literal["GLTF", "FBX", "OBJ"], face_limit: int | None, texture_size: int = 1024,
                texture_format: Literal["PNG"] = "PNG", source_kind: Literal["task", "file"] = "task",
                ctx: CallCtx | None = None) -> str:
        """T5 (5-10 credits). Converting a raw file token is refused until the ``tripo.convert_on_file_token`` flag says it works."""
        if fmt not in ("GLTF", "FBX", "OBJ"):
            raise ProviderError(PROVIDER, "bad_request", f"unsupported format {fmt!r}", code="bad_format", billed="no")
        if source_kind == "file" and not self.flags.get("tripo.convert_on_file_token"):
            raise ProviderError(PROVIDER, "bad_request", "convert on a raw file token is unverified (flag tripo.convert_on_file_token)",
                                code="convert_on_file_token_unverified", billed="no")
        body: dict[str, Any] = {"input": source, "format": fmt, "texture_size": texture_size, "texture_format": texture_format,
                                "export_vertex_colors": False, "pack_uv": True}
        if face_limit is not None:
            body["face_limit"] = int(face_limit)
        return self._create("convert", body, ctx, model="convert")

    def import_model(self, token: str, *, ctx: CallCtx | None = None) -> str:
        """``POST /models/import`` (free)."""
        return self._create("import_model", {"input": token}, ctx, model="import")

    def texture_model(self, source: str, *, texture_seed: int, texture_version: str | None = None,
                      texture_prompt: dict[str, Any] | None = None, ctx: CallCtx | None = None) -> str:
        """``POST /models/texture``: standard quality, no PBR, ``original_image`` alignment, no compression."""
        body: dict[str, Any] = {"input": source, "texture": True, "texture_quality": "standard", "pbr": False,
                                "texture_alignment": "original_image", "texture_seed": int(texture_seed)}
        if texture_version:
            body["model"] = texture_version
        if texture_prompt:
            body["texture_prompt"] = texture_prompt
        return self._create("texture_model", body, ctx, model=texture_version or "texture")

    def segment_mesh(self, source: str, *, model: str | None = None, granularity: str | None = None,
                     split_by_connectivity: bool | None = None, ctx: CallCtx | None = None) -> str:
        """``POST /mesh/segment``."""
        body: dict[str, Any] = {"input": source}
        if model:
            body["model"] = model
        if granularity:
            body["segmentation_granularity"] = granularity
        if split_by_connectivity is not None:
            body["split_by_connectivity"] = split_by_connectivity
        return self._create("mesh_segment", body, ctx, model=model or "segment")

    def complete_mesh(self, seg_task_id: str, *, part_names: list[str] | None = None, completion_mode: str | None = None,
                      model: str | None = None, ctx: CallCtx | None = None) -> str:
        """``POST /mesh/complete`` on a segmentation task."""
        body: dict[str, Any] = {"input": seg_task_id}
        if part_names:
            body["part_names"] = list(part_names)
        if completion_mode:
            body["completion_mode"] = completion_mode
        if model:
            body["model"] = model
        return self._create("mesh_complete", body, ctx, model=model or "complete")

    def retopology(self, source: str, *, face_limit: int, quad: bool = False, bake: bool | None = None,
                   ctx: CallCtx | None = None) -> str:
        """Retopology / decimation (``POST /mesh/decimate``, 1000..20000 faces; triangles unless ``quad``)."""
        if not 1000 <= face_limit <= 20000:
            raise ProviderError(PROVIDER, "bad_request", "face_limit must be 1000..20000 for retopology", code="bad_face_limit", billed="no")
        body: dict[str, Any] = {"input": source, "face_limit": int(face_limit), "quad": bool(quad)}
        if bake is not None:
            body["bake"] = bake
        return self._create("retopology", body, ctx, model="retopology")

    # ----- account ------------------------------------------------------------------------------------------
    def balance(self) -> tuple[float, float]:
        """(balance, frozen)."""
        d = self._get("/account/balance")
        if not isinstance(d, dict) or "balance" not in d:
            raise ProviderError(PROVIDER, "validation", "Tripo returned no balance", code="no_balance")
        return float(d["balance"]), float(d.get("frozen", 0.0) or 0.0)

    def credits_available(self) -> float:
        """``balance - frozen`` (conservative until the ``tripo.balance_excludes_frozen`` flag says ``balance`` already excludes it)."""
        bal, frozen = self.balance()
        return bal if self.flags.get("tripo.balance_excludes_frozen") else bal - frozen

    def ensure_credits(self, est_credits: float, *, budget_left_credits: float | None = None) -> float:
        """Raise ``billing`` unless ``est_credits <= min(balance - frozen, budget left)``; returns the available credits."""
        avail = self.credits_available()
        limit = avail if budget_left_credits is None else min(avail, budget_left_credits)
        if est_credits > limit:
            raise ProviderError(PROVIDER, "billing", f"needs {est_credits:g} credits but only {limit:g} are available", code="insufficient_credits",
                                billed="no", user_hint="Not enough Tripo credits for this step. Add credits or raise the budget.")
        return avail

    def usage(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        d = self._get("/account/usage", params={"limit": limit, "offset": offset})
        rows = d if isinstance(d, list) else (d.get("items") or d.get("data") or [] if isinstance(d, dict) else [])
        return [r for r in rows if isinstance(r, dict)]

    # ----- tasks --------------------------------------------------------------------------------------------
    def task(self, task_id: str, *, ctx: CallCtx | None = None) -> RemoteStatus:
        d = self._get(f"/tasks/{task_id}", ctx)
        if not isinstance(d, dict) or not d:
            raise ProviderError(PROVIDER, "validation", "Tripo returned an empty task", code="empty_task")
        d = {"task_id": task_id, **d}
        return parse_task(d)

    def tasks(self, ids: list[str]) -> tuple[dict[str, RemoteStatus], list[str]]:
        """``POST /tasks/list`` in chunks of 100: (statuses by id, missed ids)."""
        found: dict[str, RemoteStatus] = {}
        missed: list[str] = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            ctx = CallCtx.null()
            try:
                with self.limiter.acquire(ctx=ctx):
                    resp = self._client.post(self._url("/tasks/list"), json={"task_ids": chunk})
            except httpx.TransportError as e:
                raise ProviderError(PROVIDER, "network", f"Tripo task list failed ({type(e).__name__})", retryable=True, billed="no") from None
            d = self._envelope(resp, paid_post=False)
            tasks = d.get("tasks", {}) if isinstance(d, dict) else {}
            for tid, t in tasks.items():
                if isinstance(t, dict):
                    found[str(tid)] = parse_task({"task_id": tid, **t})
            missed += [str(m) for m in (d.get("missed", []) if isinstance(d, dict) else [])]
        return found, missed

    def wait(self, task_id: str, *, ctx: CallCtx | None = None, soft_timeout_s: float = 1200.0,
             hard_timeout_s: float | None = None) -> RemoteStatus:
        """Poll until the task is terminal: 5 s, then 3 -> 15 s (x1.4). After ``soft_timeout_s`` (20 min) the status is
        marked ``slow`` and polling continues; it never resubmits. ``hard_timeout_s`` (default none) raises ``timeout``;
        cancellation comes through ``ctx.check_cancel``."""
        ctx = ctx or CallCtx.null()
        start = self._clock()
        slow = False
        for delay in poll_intervals():
            sleep_checked(delay, ctx, sleep=self._sleep)
            st = self.task(task_id, ctx=ctx)
            ctx.progress((st.progress or 0) / 100.0, f"Tripo task {st.status}" + (" (slow)" if slow else ""))
            if st.done:
                st.slow = slow
                self._finish(st)
                return st
            elapsed = self._clock() - start
            if not slow and elapsed >= soft_timeout_s:
                slow = True
                ctx.progress((st.progress or 0) / 100.0, "Tripo is slow; still waiting (the job is not resubmitted)")
            if hard_timeout_s is not None and elapsed >= hard_timeout_s:
                raise ProviderError(PROVIDER, "timeout", "gave up polling Tripo", code="poll_timeout", retryable=False, billed="unknown",
                                    context={"task_id": task_id})
        raise AssertionError("unreachable")  # pragma: no cover

    def wait_success(self, task_id: str, *, ctx: CallCtx | None = None, **kw: Any) -> RemoteStatus:
        """``wait`` that raises ``status.failure_error()`` for a failed or cancelled task."""
        st = self.wait(task_id, ctx=ctx, **kw)
        err = st.failure_error()
        if err is not None:
            raise err
        return st

    def _finish(self, st: RemoteStatus) -> None:
        """Push the final cost of a terminal task once."""
        if st.task_id in self._costed or self.cost_sink is None:
            return
        credits = st.credits_consumed
        if st.status != "success" and not credits:
            return
        op, model, views = self._ops.get(st.task_id, (st.type or "task", str(st.input.get("model", "")), 1))
        self._costed.add(st.task_id)
        try:
            self.cost_sink(tripo_cost(str(st.input.get("model") or model), operation=op, credits=credits, task_id=st.task_id,
                                      fallback_op=op if op in ENDPOINTS else None, views=views))
        except Exception:  # noqa: BLE001, S110 - a ledger failure must not hide the result
            pass

    def cost_for(self, st: RemoteStatus) -> dict[str, Any]:
        op, model, views = self._ops.get(st.task_id, (st.type or "task", str(st.input.get("model", "")), 1))
        return tripo_cost(str(st.input.get("model") or model), operation=op, credits=st.credits_consumed, task_id=st.task_id,
                          fallback_op=op if op in ENDPOINTS else None, views=views)

    # ----- downloads ----------------------------------------------------------------------------------------
    @staticmethod
    def _expected_kinds(key: str) -> tuple[str, ...]:
        return ("glb", "fbx", "zip") if "model" in key else ("png", "jpeg", "webp")

    def download_files(self, task_id: str, keys: list[str], *, ctx: CallCtx | None = None, max_reget: int = 3,
                       status: RemoteStatus | None = None) -> dict[str, DownloadedFile]:
        """Fetch ``keys`` (``model_url``, ``rendered_image_url``, view urls) of a successful task AT ONCE.

        Host allowlist, no Authorization header, no redirects, 150 MB cap, magic bytes, SHA-256. A 403/404 (an
        expired signed URL) re-GETs the task for fresh URLs, up to ``max_reget`` times."""
        ctx = ctx or CallCtx.null()
        st = status or self.task(task_id, ctx=ctx)
        if st.status != "success":
            raise ProviderError(PROVIDER, "bad_request", f"task {task_id} is {st.status}, not success", code="not_success", billed="no")
        out: dict[str, DownloadedFile] = {}
        regets = 0
        for key in keys:
            while True:
                url = st.output_urls.get(key)
                if not url:
                    raise ProviderError(PROVIDER, "validation", f"the task has no {key}", code="missing_output", billed="yes")
                try:
                    data = self._downloader.fetch(url, ctx)
                    break
                except ProviderError as e:
                    if e.code in ("download_403", "download_404") and regets < max_reget:
                        regets += 1
                        st = self.task(task_id, ctx=ctx)
                        continue
                    raise
            kind = sniff_kind(data)
            if kind not in self._expected_kinds(key):
                raise ProviderError(PROVIDER, "validation", f"{key} has unexpected content ({kind})", code=f"bad_magic_{kind}", billed="yes")
            out[key] = DownloadedFile(key=key, data=data, kind=kind, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        return out

    def download(self, task_id: str, keys: list[str], *, ctx: CallCtx | None = None) -> dict[str, bytes]:
        return {k: f.data for k, f in self.download_files(task_id, keys, ctx=ctx).items()}

    # ----- reconciliation -----------------------------------------------------------------------------------
    def reconcile_uncertain(self, *, endpoint: str, submitted_at: datetime, body: dict, exclude: Iterable[str] = (),
                            attempts: int = 3, wait_s: float = 5.0, window_s: float = 120.0, op: str | None = None) -> str | None:
        """After a ``submission_uncertain``: find the task Tripo may have created.

        Looks at ``/account/usage`` for rows of the same type within +-2 minutes of ``submitted_at`` (usage can lag, so
        it retries ``attempts`` times ``wait_s`` apart), fetches each candidate with ``GET /tasks/{id}`` and compares
        ``input.model_seed`` / ``texture_seed`` with the body. Returns the task id, or ``None`` when no row matches
        (then, and only then, the engine may resubmit)."""
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        wanted = _norm_type(op or _OP_OF_PATH.get(endpoint) or endpoint.split("/")[-1])
        skip = {str(x) for x in exclude}
        lo, hi = submitted_at - timedelta(seconds=window_s), submitted_at + timedelta(seconds=window_s)
        for attempt in range(max(1, attempts)):
            rows = self.usage(limit=50)
            cands = []
            for r in rows:
                t = _norm_type(r.get("type"))
                ts = _parse_ts(r.get("created_at"))
                if t and (t == wanted or wanted in t or t in wanted) and ts and lo <= ts <= hi:
                    cands.append(r)
            statuses: dict[str, RemoteStatus] = {}
            if cands and any(k in body for k in ("model_seed", "texture_seed")):
                for r in cands:
                    tid = str(r.get("task_id", ""))
                    if tid and tid not in skip:
                        try:
                            statuses[tid] = self.task(tid)
                        except ProviderError:
                            continue
            found = pick_reconciled(cands, statuses, body, submitted_at, skip)
            if found:
                return found
            if attempt + 1 < attempts:
                sleep_checked(wait_s, None, sleep=self._sleep)
        return None


__all__ = [
    "API_ROOT", "DOWNLOAD_HOSTS", "ENDPOINTS", "FACE_LIMIT", "FACE_LIMIT_RANGE", "FORBIDDEN_BODY_KEYS", "H31", "P1", "P2", "SEEDS",
    "VIEWS", "DownloadedFile", "H31Params", "P1Params", "P2Params", "RemoteStatus", "RouteParams", "TripoApi", "TripoProvider",
    "View", "check_body", "edit_views_body", "error_from_response", "image_body", "multiview_body", "parse_task", "pick_reconciled",
    "poll_intervals", "views_inputs",
]
