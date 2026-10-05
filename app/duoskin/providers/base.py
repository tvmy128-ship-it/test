"""Provider layer foundations (APP_SPEC section 7.1).

Everything here is synchronous: the engine runs steps in worker threads and every provider call blocks
the calling thread. Contents:

* ``ProviderError`` and its small subclass hierarchy (``kind`` is the authoritative ``ErrorKind``; the subclasses
  give the engine coarse groups: transient, rate limited, moderation blocked, refusal, bad request, auth, budget,
  submission uncertain). ``user_message`` is the plain-English hint that the UI shows.
* ``CallCtx``: heartbeat / cancel / progress hooks passed in by the step context.
* ``RateLimiter``: token buckets (requests/min, requests/s, images/min) plus an adjustable concurrency limit,
  shared by all threads of one provider. ``limiter_for(provider)`` returns the process-wide instance.
* ``CapabilityFlags``: the probe results of section 7.1, with the conservative default for every unknown flag.
* Small helpers shared by the adapters and the mocks: ``scrub`` (secret redaction), ``jsonable`` / ``request_hash``
  (canonical hashing of requests that contain bytes), ``sniff_kind`` (magic bytes), ``Downloader`` (allow-listed,
  auth-free, size-capped downloads), ``backoff_delay`` and ``sleep_checked``.

Keys never appear in any message, repr or log line produced here.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import math
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from duoskin.models.common import sha256_of

ErrorKind = Literal[
    "auth", "billing", "permission", "not_found", "bad_request", "capability", "moderation", "recitation",
    "refusal", "truncated", "validation", "schema_too_complex", "rate_limit", "concurrency", "overloaded",
    "server", "timeout", "network", "submission_uncertain", "remote_failed", "other",
]
Billed = Literal["no", "yes", "unknown"]


# --------------------------------------------------------------------------------------------------------------
# Secret scrubbing
# --------------------------------------------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://[^\s\"'<>)\]]+"), "<url>"),                       # signed URLs never leave the process
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(x-api-key|x-goog-api-key|authorization)\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-<redacted>"),
    (re.compile(r"\btsk_[A-Za-z0-9_\-]{4,}"), "tsk_<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{16,}"), "AIza<redacted>"),
)


def scrub(text: Any, *, limit: int = 2000) -> str:
    """Remove API keys, bearer tokens and URLs (which may be signed) from ``text``; cap its length."""
    s = str(text)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s if len(s) <= limit else s[:limit] + "..."


# --------------------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------------------

_DEFAULT_HINTS: dict[str, str] = {
    "auth": "The provider rejected the API key. Open Settings, then Keys, and enter it again.",
    "billing": "The provider says the account is out of credit. Add credit, then resume the queue.",
    "permission": "The provider refused access to this model or feature. Check the account's access level.",
    "not_found": "The provider does not know this model ID (it may have been retired). Update the model ID in Settings.",
    "bad_request": "The request was invalid. This is a bug in the app; nothing was retried.",
    "capability": "The provider does not support one of the parameters. The app drops it and remembers that.",
    "moderation": "The provider's safety filter blocked this request. Reword it or change the design; it was not retried.",
    "recitation": "The result was too close to existing material. Change the design or plan again.",
    "refusal": "The model declined this request. Reword it or change the design.",
    "truncated": "The model ran out of room before it finished. It will be retried once with more room.",
    "validation": "The provider's answer did not match the expected shape.",
    "schema_too_complex": "The planner's output schema is too large for the provider; the app falls back to checked strings.",
    "rate_limit": "The provider asked us to slow down. The app waits and retries.",
    "concurrency": "The provider is already running as many jobs as it allows for this account. The app waits for them.",
    "overloaded": "The provider is overloaded right now. The app waits and retries.",
    "server": "The provider had an internal error. The app waits and retries.",
    "timeout": "The provider took too long to answer.",
    "network": "The network connection to the provider failed.",
    "submission_uncertain": "The app cannot tell whether the provider received the job. It checks before doing anything else.",
    "remote_failed": "The provider's job failed.",
    "other": "Something unexpected went wrong with the provider.",
}

_GROUP_OF_KIND: dict[str, str] = {
    "overloaded": "transient", "server": "transient", "timeout": "transient", "network": "transient",
    "rate_limit": "rate_limited", "concurrency": "rate_limited",
    "moderation": "moderation_blocked", "recitation": "moderation_blocked",
    "refusal": "refusal",
    "bad_request": "bad_request", "capability": "bad_request", "validation": "bad_request",
    "schema_too_complex": "bad_request", "truncated": "bad_request",
    "auth": "auth", "permission": "auth", "not_found": "auth",
    "billing": "budget",
    "submission_uncertain": "submission_uncertain",
}


class ProviderError(Exception):
    """Every provider failure. ``kind`` is the ErrorKind of APP_SPEC 7.1; the subclass gives its coarse group.

    Constructing ``ProviderError(provider, kind, message)`` returns the matching subclass automatically, so
    ``except RefusalError`` works however the error was raised.
    """

    provider: str
    kind: str
    message: str
    http: int | None
    code: str | None
    retryable: bool
    billed: str
    request_id: str | None
    user_hint: str
    retry_after_s: float | None
    context: dict[str, Any]
    cost: dict[str, Any] | None
    partial: Any

    def __new__(cls, provider: str = "", kind: str = "other", *args: Any, **kwargs: Any):
        target = cls
        if cls is ProviderError:
            target = _CLASS_OF_GROUP.get(_GROUP_OF_KIND.get(kind, ""), ProviderError)
        return super().__new__(target)

    def __init__(self, provider: str, kind: ErrorKind | str, message: str, *, http: int | None = None,
                 code: str | None = None, retryable: bool = False, billed: Billed = "unknown",
                 request_id: str | None = None, user_hint: str = "", retry_after_s: float | None = None,
                 context: dict[str, Any] | None = None, cost: dict[str, Any] | None = None) -> None:
        self.provider = provider
        self.kind = kind
        self.message = scrub(message)
        self.http = http
        self.code = code
        self.retryable = retryable
        self.billed = billed
        self.request_id = request_id
        self.user_hint = user_hint
        self.retry_after_s = retry_after_s
        self.context = dict(context or {})
        self.cost = cost
        self.partial = None
        super().__init__(f"[{provider}:{kind}] {self.message}")

    def __reduce__(self):  # keep pickling/copying simple and lossless
        return (_rebuild_error, (self.__class__, self.provider, self.kind, self.message, self.http, self.code,
                                 self.retryable, self.billed, self.request_id, self.user_hint, self.retry_after_s,
                                 self.context, self.cost))

    @property
    def group(self) -> str:
        """transient | rate_limited | moderation_blocked | refusal | bad_request | auth | budget | submission_uncertain | other"""
        return _GROUP_OF_KIND.get(self.kind, "other")

    @property
    def user_message(self) -> str:
        """Plain-English hint for the UI: the adapter's own hint, else the default for this kind."""
        return self.user_hint or _DEFAULT_HINTS.get(self.kind, _DEFAULT_HINTS["other"])

    def to_step_error(self) -> dict[str, Any]:
        """The ``StepError`` fields of APP_SPEC 8.6 (never contains secrets)."""
        return {"kind": self.kind, "code": self.code, "message": self.message, "retryable": self.retryable,
                "billed": self.billed, "provider_request_id": self.request_id, "user_hint": self.user_message,
                "provider": self.provider, "http": self.http, "retry_after_s": self.retry_after_s}


def _rebuild_error(cls, provider, kind, message, http, code, retryable, billed, request_id, user_hint,
                   retry_after_s, context, cost):
    e = cls(provider, kind, message, http=http, code=code, retryable=retryable, billed=billed,
            request_id=request_id, user_hint=user_hint, retry_after_s=retry_after_s, context=context, cost=cost)
    return e


class TransientError(ProviderError):
    """overloaded, server, timeout, network: back off and retry."""


class RateLimitedError(ProviderError):
    """rate_limit, concurrency: wait (``retry_after_s`` when the provider said how long)."""


class ModerationBlockedError(ProviderError):
    """moderation, recitation: never retried unchanged."""


class RefusalError(ProviderError):
    """The model declined: never retried unchanged."""


class BadRequestError(ProviderError):
    """bad_request, capability, validation, schema_too_complex, truncated: a code bug or an output problem."""


class AuthError(ProviderError):
    """auth, permission, not_found: the key, the access level or the model ID needs the user."""


class BudgetError(ProviderError):
    """billing: the account is out of credit; the whole queue pauses."""


class SubmissionUncertainError(ProviderError):
    """A paid POST whose answer was lost. ``context`` holds ``endpoint``, ``submitted_at`` and ``body`` for
    ``reconcile_uncertain``; the request is never resent."""


_CLASS_OF_GROUP: dict[str, type[ProviderError]] = {
    "transient": TransientError, "rate_limited": RateLimitedError, "moderation_blocked": ModerationBlockedError,
    "refusal": RefusalError, "bad_request": BadRequestError, "auth": AuthError, "budget": BudgetError,
    "submission_uncertain": SubmissionUncertainError,
}


class Cancelled(Exception):
    """Raised by ``CallCtx.check_cancel`` when the user cancelled the step."""


# --------------------------------------------------------------------------------------------------------------
# Call context
# --------------------------------------------------------------------------------------------------------------

def _noop() -> None:
    return None


def _noop_progress(frac: float, msg: str) -> None:
    return None


def _noop_ref(ref: str) -> None:
    return None


@dataclass(frozen=True)
class CallCtx:
    """Passed in by the step context so providers can heartbeat and honour cancellation.

    ``set_remote_ref`` and ``tag`` are additive: Tripo adapters call ``set_remote_ref(task_id)`` right after the
    task is created and before any return (a crash after that point can resume polling instead of paying twice),
    and the mocks match fault selectors such as ``@I2`` or ``@T3`` against ``tag`` (the step's template id).
    """

    step_id: str | None = None
    heartbeat: Callable[[], None] = _noop
    check_cancel: Callable[[], None] = _noop
    progress: Callable[[float, str], None] = _noop_progress
    set_remote_ref: Callable[[str], None] = _noop_ref
    tag: str = ""

    @classmethod
    def null(cls, **kw: Any) -> CallCtx:
        """A context that does nothing (tests, probes, library builds)."""
        return cls(**kw)

    def tick(self) -> None:
        """Heartbeat then cancellation check: call this inside every wait loop."""
        self.heartbeat()
        self.check_cancel()


# --------------------------------------------------------------------------------------------------------------
# Waiting helpers
# --------------------------------------------------------------------------------------------------------------

def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 32.0, jitter: float = 0.25,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff 1, 2, 4 ... ``cap`` seconds with up to ``jitter`` extra (attempt starts at 0)."""
    d = min(cap, base * (2 ** max(0, attempt)))
    r = (rng or random).random()
    return d * (1.0 + jitter * r)


def sleep_checked(seconds: float, ctx: CallCtx | None = None, *, sleep: Callable[[float], None] = time.sleep,
                  slice_s: float = 0.5) -> None:
    """Sleep in short slices, calling ``ctx.tick()`` between them so heartbeats and cancellation keep working."""
    remaining = max(0.0, float(seconds))
    while remaining > 1e-9:
        step = min(slice_s, remaining)
        sleep(step)
        remaining -= step
        if ctx is not None:
            ctx.tick()


def parse_retry_after(headers: Mapping[str, str] | None, *, now: Callable[[], float] = time.time) -> float | None:
    """Seconds to wait from ``Retry-After`` (seconds or HTTP date) or ``X-RateLimit-Reset`` (epoch or delta)."""
    if not headers:
        return None
    low = {str(k).lower(): str(v) for k, v in headers.items()}
    ra = low.get("retry-after")
    if ra:
        try:
            return max(0.0, float(ra))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                return max(0.0, parsedate_to_datetime(ra).timestamp() - now())
            except (TypeError, ValueError):
                pass
    reset = low.get("x-ratelimit-reset") or low.get("x-ratelimit-reset-requests")
    if reset:
        try:
            v = float(reset)
        except ValueError:
            return None
        return max(0.0, v - now()) if v > 1e9 else max(0.0, v)
    return None


# --------------------------------------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------------------------------------

class _Bucket:
    """Continuous-refill token bucket. Not thread-safe by itself: ``RateLimiter`` holds its lock around it."""

    def __init__(self, per_minute: float | None = None, *, per_second: float | None = None, now: float = 0.0) -> None:
        if per_second is not None:
            self.rate = float(per_second)
            self.capacity = max(1.0, float(per_second))
        elif per_minute is not None:
            self.rate = float(per_minute) / 60.0
            self.capacity = max(1.0, float(per_minute))
        else:  # pragma: no cover - guarded by the caller
            raise ValueError("a bucket needs a rate")
        self.tokens = self.capacity
        self.last = now

    def refill(self, now: float) -> None:
        if now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = max(self.last, now)

    def wait_for(self, need: float) -> float:
        need = min(need, self.capacity)
        if self.tokens >= need:
            return 0.0
        return (need - self.tokens) / self.rate if self.rate > 0 else math.inf

    def take(self, need: float) -> None:
        self.tokens -= min(need, self.capacity)


class RateLimiter:
    """Token buckets (rpm, rps, ipm) plus a concurrency limit, shared by every thread of one provider.

    ``acquire(images=n, ctx=ctx)`` is a context manager: it waits (heartbeating, honouring cancel) for a free
    slot and for ``1`` request token and ``n`` image tokens, holds the slot for the ``with`` body and releases it.
    A request for more images than the bucket holds is clamped to the bucket size (it would never fit otherwise).
    ``penalize(seconds)`` makes everybody wait after a 429.
    """

    def __init__(self, *, rpm: float | None = None, rps: float | None = None, ipm: float | None = None,
                 concurrent: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, slice_s: float = 0.25) -> None:
        self._clock = clock
        self._sleep = sleep
        self._slice = slice_s
        self._cv = threading.Condition()
        now = clock()
        self._rpm = _Bucket(per_minute=rpm, now=now) if rpm else None
        self._rps = _Bucket(per_second=rps, now=now) if rps else None
        self._ipm = _Bucket(per_minute=ipm, now=now) if ipm else None
        self._limit = max(1, int(concurrent))
        self._in_flight = 0
        self._blocked_until = 0.0
        self.waited_s = 0.0
        self.acquired = 0

    # ----- configuration ------------------------------------------------------------------------------------
    def set_concurrent(self, n: int) -> None:
        with self._cv:
            self._limit = max(1, int(n))
            self._cv.notify_all()

    def lower_concurrency(self, by: int = 1, minimum: int = 1) -> int:
        """Tripo 429 + 2000: take one slot away. Returns the new limit."""
        with self._cv:
            self._limit = max(minimum, self._limit - by)
            return self._limit

    def set_ipm(self, ipm: float | None) -> None:
        with self._cv:
            self._ipm = _Bucket(per_minute=ipm, now=self._clock()) if ipm else None

    @property
    def concurrent(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def penalize(self, seconds: float) -> None:
        """After a 429 everyone waits."""
        with self._cv:
            self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, float(seconds)))

    # ----- acquisition --------------------------------------------------------------------------------------
    def _take_slot(self, ctx: CallCtx | None) -> None:
        while True:
            with self._cv:
                if self._in_flight < self._limit:
                    self._in_flight += 1
                    return
                self._cv.wait(timeout=self._slice)
            if ctx is not None:
                ctx.tick()

    def _release_slot(self) -> None:
        with self._cv:
            self._in_flight = max(0, self._in_flight - 1)
            self._cv.notify()

    def _take_tokens(self, images: int, ctx: CallCtx | None) -> None:
        while True:
            with self._cv:
                now = self._clock()
                wait = max(0.0, self._blocked_until - now)
                if wait == 0.0:
                    buckets = [(b, n) for b, n in ((self._rpm, 1), (self._rps, 1), (self._ipm, images)) if b and n > 0]
                    for b, _ in buckets:
                        b.refill(now)
                    wait = max((b.wait_for(n) for b, n in buckets), default=0.0)
                    if wait <= 0.0:
                        for b, n in buckets:
                            b.take(n)
                        self.acquired += 1
                        return
            wait = min(wait, self._slice * 4)
            t0 = self._clock()
            self._sleep(wait)
            self.waited_s += max(0.0, self._clock() - t0) or wait
            if ctx is not None:
                ctx.tick()

    @contextlib.contextmanager
    def acquire(self, *, images: int = 0, ctx: CallCtx | None = None) -> Iterator[None]:
        self._take_slot(ctx)
        try:
            self._take_tokens(max(0, int(images)), ctx)
        except BaseException:
            self._release_slot()
            raise
        try:
            yield
        finally:
            self._release_slot()


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()

# Defaults from APP_SPEC 7.1, 7.3, 7.4 and 14.1 (``ProviderSettings``).
LIMITER_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {"concurrent": 3},
    "openai": {"ipm": 5, "concurrent": 3},
    "recraft": {"ipm": 100, "rps": 5, "concurrent": 2},
    "tripo": {"rps": 5, "concurrent": 2},
    "gemini": {"concurrent": 2},
    "fal": {"concurrent": 1},
}


def limiter_for(provider: str, **overrides: Any) -> RateLimiter:
    """The shared limiter of ``provider`` (created on first use from ``LIMITER_DEFAULTS`` plus ``overrides``).

    Later calls with overrides update the existing limiter in place so threads keep sharing one object."""
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(provider)
        if lim is None:
            cfg = {**LIMITER_DEFAULTS.get(provider, {"concurrent": 1}), **overrides}
            lim = _LIMITERS[provider] = RateLimiter(**cfg)
        else:
            if overrides.get("concurrent"):
                lim.set_concurrent(overrides["concurrent"])
            if "ipm" in overrides:
                lim.set_ipm(overrides["ipm"])
        return lim


def reset_limiters() -> None:
    """Forget every shared limiter (tests)."""
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


# --------------------------------------------------------------------------------------------------------------
# Capability flags
# --------------------------------------------------------------------------------------------------------------

# An unknown flag always selects the conservative path (APP_SPEC 7.1).
CONSERVATIVE_FLAGS: dict[str, Any] = {
    "openai.mask_multi_ok": False,
    "openai.rgba_image1_ok": False,
    "openai.usage_present": False,
    "recraft.background_color_honoured": False,
    "recraft.file_field_name": "file",
    "recraft.style_match_ok": True,
    "tripo.orthographic_projection": False,
    "tripo.texture_version_delight": False,
    "tripo.balance_excludes_frozen": False,
    "tripo.convert_on_file_token": False,
    "anthropic.sonnet_fallbacks": False,
}


class CapabilityFlags:
    """Thread-safe store of the capability flags (``settings.capabilities``).

    ``on_change(name, value)`` lets the owner persist a flag the moment a probe or an error proves it."""

    def __init__(self, initial: Mapping[str, Any] | None = None,
                 on_change: Callable[[str, Any], None] | None = None) -> None:
        self._d: dict[str, Any] = dict(initial or {})
        self._lock = threading.Lock()
        self.on_change = on_change

    def get(self, name: str, default: Any = None) -> Any:
        missing = object()
        with self._lock:
            v = self._d.get(name, missing)
        if v is not missing:
            return v
        if name in CONSERVATIVE_FLAGS:
            return CONSERVATIVE_FLAGS[name]
        if name.startswith("anthropic.schema_ok."):
            return True
        return default

    def is_set(self, name: str) -> bool:
        with self._lock:
            return name in self._d

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            changed = self._d.get(name, object()) != value
            self._d[name] = value
        if changed and self.on_change is not None:
            try:
                self.on_change(name, value)
            except Exception:  # noqa: BLE001, S110 - persisting a flag must never fail a provider call
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._d)


# --------------------------------------------------------------------------------------------------------------
# Hashing helpers (cache keys, mock determinism, provenance)
# --------------------------------------------------------------------------------------------------------------

def jsonable(obj: Any) -> Any:
    """Turn requests into canonical-JSON-able data: bytes become ``{"sha256", "len"}``, dataclasses and
    pydantic models become dicts, sets are sorted, enums give their value."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        b = bytes(obj)
        return {"sha256": hashlib.sha256(b).hexdigest(), "len": len(b)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="python"))
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((jsonable(x) for x in obj), key=lambda v: repr(v))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, type):
        return f"{obj.__module__}.{obj.__qualname__}"
    return obj


def request_hash(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj`` (bytes are hashed first). The mocks derive every output from this."""
    return sha256_of(jsonable(obj))


def seed_from_hash(h: str, *, bits: int = 32) -> int:
    """A stable integer seed from a hex hash."""
    return int(h[:16], 16) % (1 << bits)


# --------------------------------------------------------------------------------------------------------------
# Magic bytes and safe downloads
# --------------------------------------------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sniff_kind(data: bytes) -> str:
    """glb | fbx | zip | png | jpeg | webp | svg | unknown, from the first bytes (never from a file name)."""
    head = bytes(data[:64])
    if head.startswith(b"glTF"):
        return "glb"
    if head.startswith(b"Kaydara FBX Binary"):
        return "fbx"
    if head.startswith(b"PK"):
        return "zip"
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    text = bytes(data[:2048]).lstrip(b"\xef\xbb\xbf \t\r\n")
    if text.startswith((b"<svg", b"<?xml", b"<!--", b"<!DOCTYPE svg", b"<!doctype svg")) and b"<svg" in bytes(data[:8192]):
        return "svg"
    return "unknown"


class HostAllowlist:
    """Exact hosts (``tripo-data.rg1.data.tripo3d.com``) and ``*.suffix`` patterns (sub-domains only)."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = tuple(p.lower() for p in patterns)

    def allows(self, host: str | None) -> bool:
        if not host:
            return False
        host = host.lower().rstrip(".")
        for p in self.patterns:
            if p.startswith("*."):
                if host.endswith(p[1:]) and len(host) > len(p) - 1:
                    return True
            elif host == p:
                return True
        return False


class Downloader:
    """Fetches an output URL: host on the allowlist, https only, no Authorization header, no redirects, size cap.

    The client is built without any default headers on purpose: the provider's Bearer token must never reach a
    storage host. Raises ``ProviderError`` with ``code`` ``host_not_allowed``, ``download_403``/``download_404``
    (expired signed URL: the caller re-GETs the task), ``too_large`` or ``download_redirect``.
    """

    def __init__(self, provider: str, allow: Iterable[str], *, max_bytes: int = 150 * 1024 * 1024,
                 timeout: float = 120.0, client: Any = None, transport: Any = None) -> None:
        from duoskin.providers._http import make_client
        self.provider = provider
        self.allow = HostAllowlist(allow)
        self.max_bytes = int(max_bytes)
        self._client = client or make_client(timeout=timeout, follow_redirects=False, transport=transport)

    def fetch(self, url: str, ctx: CallCtx | None = None) -> bytes:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or "@" in parts.netloc or not self.allow.allows(host):
            raise ProviderError(self.provider, "bad_request", f"download host {host!r} is not allowed",
                                code="host_not_allowed", billed="no",
                                user_hint="The provider returned a download link on an unexpected host, so the app did not fetch it.")
        from duoskin.providers._http import httpx
        try:
            with self._client.stream("GET", url) as resp:
                status = resp.status_code
                if 300 <= status < 400:
                    raise ProviderError(self.provider, "other", "download redirected", http=status, code="download_redirect")
                if status in (403, 404, 410):
                    raise ProviderError(self.provider, "other", f"download returned HTTP {status}", http=status,
                                        code=f"download_{status}", retryable=True)
                if status >= 500:
                    raise ProviderError(self.provider, "server", f"download returned HTTP {status}", http=status, retryable=True)
                if status >= 400:
                    raise ProviderError(self.provider, "bad_request", f"download returned HTTP {status}", http=status)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise ProviderError(self.provider, "bad_request", "download is larger than the size cap", code="too_large")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > self.max_bytes:
                        raise ProviderError(self.provider, "bad_request", "download is larger than the size cap", code="too_large")
                    if ctx is not None:
                        ctx.tick()
                return bytes(buf)
        except ProviderError:
            raise
        except httpx.TimeoutException as e:
            raise ProviderError(self.provider, "timeout", f"download timed out: {type(e).__name__}", retryable=True) from None
        except httpx.TransportError as e:
            raise ProviderError(self.provider, "network", f"download failed: {type(e).__name__}", retryable=True) from None


# --------------------------------------------------------------------------------------------------------------
# Generic HTTP status mapping for the REST adapters
# --------------------------------------------------------------------------------------------------------------

def kind_for_status(status: int, message: str = "") -> ErrorKind:
    """Best-effort ErrorKind for an HTTP status (adapters refine it with provider-specific codes first)."""
    m = message.lower()
    if status == 400:
        if "credit" in m and ("balance" in m or "insufficient" in m or "low" in m) or "insufficient_quota" in m:
            return "billing"
        if "moderation" in m or "content policy" in m or "safety" in m:
            return "moderation"
        return "bad_request"
    return {401: "auth", 402: "billing", 403: "permission", 404: "not_found", 408: "timeout", 409: "bad_request",
            413: "bad_request", 422: "bad_request", 429: "rate_limit", 529: "overloaded"}.get(
        status, "server" if status >= 500 else "other")


def error_from_status(provider: str, status: int, message: str, *, headers: Mapping[str, str] | None = None,
                      code: str | None = None, request_id: str | None = None, billed: Billed = "unknown",
                      user_hint: str = "") -> ProviderError:
    kind = kind_for_status(status, message)
    retryable = kind in ("rate_limit", "overloaded", "server", "timeout", "network")
    return ProviderError(provider, kind, message, http=status, code=code, retryable=retryable, billed=billed,
                         request_id=request_id, user_hint=user_hint, retry_after_s=parse_retry_after(headers))


# --------------------------------------------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------------------------------------------

def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG header without decoding it; ``None`` if it is not a PNG."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CallRecord:
    """What a mock (or a test spy) remembers about one call."""
    provider: str
    operation: str
    request: dict[str, Any]
    request_sha: str
    at: float = field(default_factory=time.time)


def first_not_none(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


__all__ = [
    "CONSERVATIVE_FLAGS",
    "LIMITER_DEFAULTS",
    "PNG_MAGIC",
    "AuthError",
    "BadRequestError",
    "Billed",
    "BudgetError",
    "CallCtx",
    "CallRecord",
    "Cancelled",
    "CapabilityFlags",
    "Downloader",
    "ErrorKind",
    "HostAllowlist",
    "ModerationBlockedError",
    "ProviderError",
    "RateLimitedError",
    "RateLimiter",
    "RefusalError",
    "SubmissionUncertainError",
    "TransientError",
    "backoff_delay",
    "error_from_status",
    "first_not_none",
    "jsonable",
    "kind_for_status",
    "limiter_for",
    "parse_retry_after",
    "png_size",
    "request_hash",
    "reset_limiters",
    "scrub",
    "seed_from_hash",
    "sleep_checked",
    "sniff_kind",
    "utcnow",
]
