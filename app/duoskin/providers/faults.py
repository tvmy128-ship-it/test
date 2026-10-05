"""Fault injection for the mock providers (APP_SPEC 16).

``DUOSKIN_MOCK_FAULTS`` is a comma-separated list of ``provider:fault@selector`` entries, for example::

    openai:moderation_blocked@first,tripo:2008@seed29,anthropic:refusal@L13,anthropic:truncated@L3,
    anthropic:schema_invalid@L3,openai:size_drift@I2,tripo:read_timeout_after_send@T3,recraft:429@first,
    tripo:1007@first,tripo:2000@first,openai:timeout@I4

* ``@first`` the first faultable call of that provider; ``@nK`` the K-th; ``@always`` every call;
  ``@seedN`` a Tripo call whose ``model_seed`` is N; anything else is a tag: the Claude route (``L3``, ``L13`` match
  ``L3_planner``, ``L13_ip``), the image step id (``I2``) or the Tripo step (``T1``..``T5``).
* ``*N`` after the selector repeats the fault N times (``recraft:429@first*3``); ``*`` alone repeats it forever.
  A tag or seed fault otherwise fires once; ``always`` is unlimited.
* A fault either raises the very ``ProviderError`` the real adapter raises for that situation (``raise_for``) or
  changes the mock's behaviour (``refusal`` is an error; ``schema_invalid``, ``size_drift``, ``hostile_svg``,
  ``opaque_alpha``, ``usage_none``, ``read_timeout_after_send``, ``slow`` ... are behaviours the mock then runs through
  the real validation code, so the error is the real one).

Unknown providers or fault names are rejected when the spec is parsed, so a typo in a demo fails loudly.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from duoskin.providers.base import ProviderError

ENV_VAR = "DUOSKIN_MOCK_FAULTS"

# Faults that raise an error (the mock raises ``raise_for(...)``) ...
ERROR_FAULTS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"refusal", "truncated", "context_exceeded", "rate_limit", "429", "overloaded", "529", "auth", "401", "billing", "402",
                            "schema_too_complex", "timeout", "server", "500"}),
    "openai": frozenset({"moderation_blocked", "timeout", "rate_limit", "429", "billing", "insufficient_quota", "org_unverified", "permission", "403",
                         "unknown_parameter", "server", "500", "auth", "401", "network"}),
    "recraft": frozenset({"429", "rate_limit", "500", "server", "401", "auth", "402", "billing", "moderation", "style_required", "timeout"}),
    "tripo": frozenset({"1007", "2000", "2010", "2015", "rate_limit", "concurrency", "auth", "401", "402", "billing", "connect_error"}),
    "gemini": frozenset({"image_safety", "image_prohibited", "recitation", "no_image", "rate_limit", "429", "server", "500", "auth", "401"}),
}
# ... and faults that change what the mock returns or does.
BEHAVIOUR_FAULTS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"schema_invalid", "validation", "no_text", "fail_rule"}),
    "openai": frozenset({"size_drift", "usage_none", "opaque_alpha"}),
    "recraft": frozenset({"hostile_svg", "png_instead_of_svg"}),
    "tripo": frozenset({"read_timeout_after_send", "task_failed", "moderation", "queue_expired", "slow"}),
    "gemini": frozenset({"empty_text", "fail_rule"}),
}
KNOWN_FAULTS: dict[str, frozenset[str]] = {p: ERROR_FAULTS[p] | BEHAVIOUR_FAULTS.get(p, frozenset()) for p in ERROR_FAULTS}
_NUMERIC = re.compile(r"^\d{3,5}$")
_ENTRY = re.compile(r"^\s*([a-z]+):([A-Za-z0-9_]+)(?:@([A-Za-z0-9_.#\-]+))?(?:\*(\d*))?\s*$")


@dataclass
class Fault:
    provider: str
    name: str
    selector: str = "first"
    limit: int | None = 1          # None = unlimited
    fired: int = 0

    def matches(self, counter: int, tag: str, seed: int | None, aliases: Iterable[str]) -> bool:
        if self.limit is not None and self.fired >= self.limit:
            return False
        sel = self.selector.lower()
        if sel == "always":
            return True
        if sel == "first":
            return counter <= (self.limit if self.limit is not None else 10**9)
        m = re.fullmatch(r"n(?:th)?(\d+)", sel)
        if m:
            return counter == int(m.group(1))
        m = re.fullmatch(r"seed(\d+)", sel)
        if m:
            return seed is not None and seed == int(m.group(1))
        names = {tag.lower(), *(a.lower() for a in aliases)}
        return any(n == sel or n.startswith(sel + "_") for n in names if n)


def parse_spec(spec: str | None) -> list[Fault]:
    """Parse a ``DUOSKIN_MOCK_FAULTS`` string. Raises ``ValueError`` for unknown providers or faults."""
    faults: list[Fault] = []
    for raw in (spec or "").split(","):
        if not raw.strip():
            continue
        m = _ENTRY.match(raw)
        if not m:
            raise ValueError(f"bad fault entry {raw.strip()!r}: expected provider:fault@selector")
        provider, name, selector, star = m.group(1), m.group(2), m.group(3) or "first", m.group(4)
        known = KNOWN_FAULTS.get(provider)
        if known is None:
            raise ValueError(f"unknown provider {provider!r} in fault entry {raw.strip()!r}")
        if name not in known and not (provider == "tripo" and _NUMERIC.match(name)):
            raise ValueError(f"unknown {provider} fault {name!r}; known: {', '.join(sorted(known))} (and Tripo error codes)")
        if star is None:
            limit: int | None = None if selector.lower() == "always" else 1
        else:
            limit = int(star) if star else None
        faults.append(Fault(provider, name, selector, limit))
    return faults


class FaultInjector:
    """Holds the parsed faults and the per-provider call counters. Thread-safe."""

    def __init__(self, spec: str | None = None) -> None:
        self.spec = spec or ""
        self.faults = parse_spec(spec)
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()
        self.fired: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FaultInjector:
        return cls((env if env is not None else os.environ).get(ENV_VAR, ""))

    def __bool__(self) -> bool:
        return bool(self.faults)

    def check(self, provider: str, *, tag: str = "", seed: int | None = None, aliases: Iterable[str] = ()) -> str | None:
        """Count one faultable call of ``provider`` and return the fault to inject (consuming one use), or ``None``."""
        with self._lock:
            counter = self._counters[provider] = self._counters.get(provider, 0) + 1
            for f in self.faults:
                if f.provider == provider and f.matches(counter, tag, seed, list(aliases)):
                    f.fired += 1
                    self.fired.append({"provider": provider, "fault": f.name, "tag": tag, "seed": seed, "call": counter})
                    return f.name
        return None

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self.fired.clear()
            for f in self.faults:
                f.fired = 0


_DEFAULT: FaultInjector | None = None
_DEFAULT_SPEC: str | None = None
_DEFAULT_LOCK = threading.Lock()


def default_injector() -> FaultInjector:
    """The process-wide injector built from ``DUOSKIN_MOCK_FAULTS``. It is rebuilt (counters reset) when the variable changes."""
    global _DEFAULT, _DEFAULT_SPEC
    spec = os.environ.get(ENV_VAR, "")
    with _DEFAULT_LOCK:
        if _DEFAULT is None or spec != _DEFAULT_SPEC:
            _DEFAULT, _DEFAULT_SPEC = FaultInjector(spec), spec
        return _DEFAULT


def reset_default_injector() -> None:
    global _DEFAULT, _DEFAULT_SPEC
    with _DEFAULT_LOCK:
        _DEFAULT, _DEFAULT_SPEC = None, None


# --------------------------------------------------------------------------------------------------------------
# Errors: the same ProviderError the real adapter raises
# --------------------------------------------------------------------------------------------------------------

def _http_error(provider: str, status: int, message: str, *, retry_after: float | None = None, request_id: str | None = None) -> ProviderError:
    from duoskin.providers.base import error_from_status
    err = error_from_status(provider, status, message, headers={"retry-after": str(retry_after)} if retry_after is not None else None,
                            request_id=request_id)
    if err.kind in ("auth", "billing", "permission", "bad_request", "not_found"):
        err.billed = "no"
    return err


def raise_for(provider: str, fault: str, *, tag: str = "", request_id: str | None = None) -> None:
    """Raise the ``ProviderError`` for an error fault; return ``None`` for a behavioural fault (the mock acts on it)."""
    rid = request_id or f"mock-{provider}-fault"
    if provider == "anthropic":
        from duoskin.providers import anthropic_llm as A
        if fault == "refusal":
            raise A.refusal_error({"category": "mock_refusal", "explanation": "injected fault"}, rid)
        if fault == "truncated":
            raise A.truncated_error("max_tokens", rid)
        if fault == "context_exceeded":
            raise A.truncated_error("model_context_window_exceeded", rid)
        if fault in ("rate_limit", "429"):
            raise _http_error(provider, 429, "injected rate limit", retry_after=2.0, request_id=rid)
        if fault in ("overloaded", "529"):
            raise ProviderError(provider, "overloaded", "injected overload", http=529, retryable=True, billed="no", request_id=rid)
        if fault in ("auth", "401"):
            raise _http_error(provider, 401, "injected invalid x-api-key", request_id=rid)
        if fault in ("billing", "402"):
            raise ProviderError(provider, "billing", "injected: credit balance is too low", http=402, billed="no", request_id=rid)
        if fault == "schema_too_complex":
            raise ProviderError(provider, "schema_too_complex", "injected: Schema is too complex for compilation", http=400, code="schema_too_complex",
                                billed="no", request_id=rid, user_hint="Planner schema too large for the kit; switched to checked strings.")
        if fault == "timeout":
            raise ProviderError(provider, "timeout", "injected timeout", retryable=True)
        if fault in ("server", "500"):
            raise _http_error(provider, 500, "injected server error", request_id=rid)
    elif provider == "openai":
        from duoskin.providers import openai_images as O
        if fault == "moderation_blocked":
            raise O.moderation_error(rid)
        if fault == "timeout":
            raise ProviderError(provider, "timeout", "injected timeout", retryable=True, billed="unknown", code="timeout",
                                context={"possible_double_bill": True},
                                user_hint="OpenAI took too long. The app retries once and logs it, because a timeout may have been billed.")
        if fault in ("rate_limit", "429"):
            raise _http_error(provider, 429, "injected rate limit", retry_after=3.0, request_id=rid)
        if fault in ("billing", "insufficient_quota"):
            raise ProviderError(provider, "billing", "injected: You exceeded your current quota", http=429, code="insufficient_quota", billed="no",
                                request_id=rid, user_hint="OpenAI says the account is out of credit or over its limit. Add credit, then resume the queue.")
        if fault in ("org_unverified", "permission", "403"):
            raise ProviderError(provider, "permission", "injected: your organization must be verified", http=403, billed="no", request_id=rid,
                                user_hint="OpenAI requires this organization to be verified before it allows GPT Image models. "
                                          "Verify the organization in your OpenAI account settings, then try again.")
        if fault == "unknown_parameter":
            raise ProviderError(provider, "capability", "injected: Unknown parameter: 'user'", http=400, code="unknown_parameter", billed="no",
                                request_id=rid, context={"param": "user"})
        if fault in ("server", "500"):
            raise _http_error(provider, 500, "injected server error", request_id=rid)
        if fault in ("auth", "401"):
            raise _http_error(provider, 401, "injected incorrect API key", request_id=rid)
        if fault == "network":
            raise ProviderError(provider, "network", "injected connection error", retryable=True)
    elif provider == "recraft":
        if fault in ("429", "rate_limit"):
            raise _http_error(provider, 429, "injected rate limit", retry_after=1.0, request_id=rid)
        if fault in ("500", "server"):
            raise _http_error(provider, 500, "injected server error", request_id=rid)
        if fault in ("401", "auth"):
            raise _http_error(provider, 401, "injected invalid token", request_id=rid)
        if fault in ("402", "billing"):
            raise ProviderError(provider, "billing", "injected: not enough API units", http=402, billed="no", request_id=rid,
                                user_hint="Recraft says the account has no API units left. Buy units, then resume the queue.")
        if fault == "moderation":
            raise ProviderError(provider, "moderation", "injected: the prompt violates the content policy", http=400, billed="no", request_id=rid)
        if fault == "style_required":
            raise ProviderError(provider, "bad_request", "injected: style_id is required for styles models", http=400, code="style_id_required", billed="no")
        if fault == "timeout":
            raise ProviderError(provider, "timeout", "injected timeout", retryable=True)
    elif provider == "tripo":
        from duoskin.providers import tripo as T
        if fault in ("1007", "rate_limit"):
            raise T.error_from_response(429, {"code": 1007, "message": "injected rate limit", "request_id": rid}, {"retry-after": "2"})
        if fault in ("2000", "concurrency"):
            raise T.error_from_response(429, {"code": 2000, "message": "injected: too many concurrent tasks", "request_id": rid})
        if fault == "2010" or fault in ("402", "billing"):
            raise T.error_from_response(403, {"code": 2010, "message": "injected: not enough credits", "request_id": rid})
        if fault == "2015":
            raise T.error_from_response(400, {"code": 2015, "message": "injected: model version not available", "request_id": rid})
        if fault in ("auth", "401"):
            raise T.error_from_response(401, {"code": 1002, "message": "injected invalid api key", "request_id": rid})
        if fault == "connect_error":
            raise ProviderError(provider, "network", "could not connect to Tripo (injected)", retryable=True, billed="no")
    elif provider == "gemini":
        from duoskin.providers import gemini as G
        if fault in ("image_safety", "image_prohibited"):
            raise G.moderation_error("IMAGE_SAFETY" if fault == "image_safety" else "IMAGE_PROHIBITED_CONTENT", request_id=rid)
        if fault == "recitation":
            raise G.recitation_error("IMAGE_RECITATION", request_id=rid)
        if fault == "no_image":
            raise ProviderError(provider, "other", "Gemini returned no image (NO_IMAGE)", code="NO_IMAGE", retryable=True, request_id=rid)
        if fault in ("rate_limit", "429"):
            raise _http_error(provider, 429, "injected quota", retry_after=2.0, request_id=rid)
        if fault in ("server", "500"):
            raise _http_error(provider, 500, "injected server error", request_id=rid)
        if fault in ("auth", "401"):
            raise _http_error(provider, 401, "injected invalid key", request_id=rid)


__all__ = [
    "BEHAVIOUR_FAULTS", "ENV_VAR", "ERROR_FAULTS", "KNOWN_FAULTS", "Fault", "FaultInjector", "default_injector", "parse_spec",
    "raise_for", "reset_default_injector",
]
