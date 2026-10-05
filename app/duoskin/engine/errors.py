"""Error classification and the retry policy of APP_SPEC §8.6.

Provider adapters raise ``providers.base.ProviderError`` (attributes ``provider, kind, message, http, code, retryable,
billed, request_id, user_hint, retry_after_s``). The engine never imports that class: it reads those attributes
(duck typing), so any exception carrying a ``kind`` attribute is classified, and plain Python exceptions map to
``timeout`` / ``network`` / ``other``. Handlers may also raise ``StepFailure`` directly.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Literal

from duoskin.models.job import Step, StepError

ERROR_KINDS = frozenset({
    "auth", "billing", "permission", "not_found", "bad_request", "capability", "moderation", "recitation", "refusal",
    "truncated", "validation", "schema_too_complex", "rate_limit", "concurrency", "overloaded", "server", "timeout",
    "network", "submission_uncertain", "remote_failed", "other",
})
RETRY_BACKOFF_KINDS = frozenset({"rate_limit", "overloaded", "server", "network", "timeout", "concurrency"})
NEVER_BILLED = frozenset({"rate_limit", "concurrency", "auth", "permission", "not_found", "bad_request", "billing",
                          "overloaded", "server", "schema_too_complex", "moderation"})
FAIL_KINDS = frozenset({"refusal", "moderation", "bad_request", "permission", "auth", "not_found", "recitation",
                        "billing", "schema_too_complex", "other"})

# The "UI" column of the §8.6 table (provider-specific hints come from ProviderError.user_hint when it has one).
USER_HINTS: dict[str, str] = {
    "rate_limit": "Waiting (rate limit). The step retries by itself.",
    "overloaded": "The provider is busy. The step retries by itself.",
    "server": "The provider had a server error. The step retries by itself.",
    "network": "Network problem. The step retries by itself.",
    "timeout": "The provider took too long. The step retries by itself.",
    "truncated": "The model ran out of room.",
    "validation": "The model's answer did not match the schema; it goes back to the plan loop as findings.",
    "schema_too_complex": "Planner schema too large for the kit; switched to checked strings.",
    "moderation": "The provider's safety filter blocked the request. See the refused prompt and the rewrite.",
    "refusal": "The model declined this request.",
    "recitation": "Originality failure: change the part or plan again.",
    "billing": "Add credits for this provider, then resume the queue.",
    "auth": "The API key was rejected. Check it in Settings.",
    "permission": "The provider refused access (for OpenAI: the organization must be verified).",
    "not_found": "Update the model ID in Settings.",
    "bad_request": "The request was rejected as invalid. This is a bug; see the logs.",
    "capability": "A parameter is not supported; retrying without it.",
    "concurrency": "Waiting for our own running tasks to finish.",
    "submission_uncertain": "Checking whether the provider received the job...",
    "remote_failed": "The remote task failed.",
    "other": "The step failed. See the message and the logs.",
}


class Cancelled(Exception):
    """Raised by ``StepContext.check_cancel()`` when the step was cancelled, lost its lease or the app is stopping."""


def is_cancelled(exc: BaseException) -> bool:
    """True for our ``Cancelled`` and for any other class named ``Cancelled`` (the provider layer may define its own)."""
    return isinstance(exc, Cancelled) or type(exc).__name__ == "Cancelled"


class StepFailure(Exception):
    """A handler's explicit failure. ``kind`` is an ``ErrorKind``; ``retryable`` and ``billed`` are honoured."""

    def __init__(self, message: str, *, kind: str = "other", retryable: bool = False,
                 billed: Literal["no", "yes", "unknown"] = "no", code: str | None = None, user_hint: str = "",
                 retry_after_s: float | None = None, provider: str | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.billed = billed
        self.code = code
        self.user_hint = user_hint
        self.retry_after_s = retry_after_s
        self.provider = provider
        self.request_id = request_id


@dataclass
class ErrorDecision:
    action: Literal["retry", "fail", "validation_ok", "wait_remote"]
    error: StepError
    delay_s: float = 0.0
    pause_queue: bool = False
    pause_provider: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)
    remote_state: str | None = None


def backoff_s(attempt: int, *, rng: random.Random | None = None) -> float:
    """1, 2, 4 ... 32 seconds (``attempt`` is the attempt that just failed, starting at 1) plus up to 25% jitter."""
    base = float(min(2 ** max(attempt - 1, 0), 32))
    r = (rng or random).random()
    return base * (1.0 + 0.25 * r)


def _kind_of(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind in ERROR_KINDS:
        return kind
    names = {c.__name__ for c in type(exc).__mro__}
    if names & {"TimeoutError", "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout", "TimeoutException"}:
        return "timeout"
    if names & {"ConnectionError", "ConnectError", "NetworkError", "RemoteProtocolError", "ReadError", "WriteError",
                "ConnectionResetError", "BrokenPipeError"}:
        return "network"
    return "other"


def classify(exc: BaseException, step: Step, handler_provider: str | None = None) -> StepError:
    kind = _kind_of(exc)
    billed = getattr(exc, "billed", None)
    if billed not in ("no", "yes", "unknown"):
        billed = "no" if kind in NEVER_BILLED or kind == "other" else "unknown"
    elif billed == "unknown" and kind in NEVER_BILLED:
        billed = "no"   # a rejected request never bills, whatever the adapter defaulted to
    retryable = bool(getattr(exc, "retryable", kind in RETRY_BACKOFF_KINDS))
    hint = getattr(exc, "user_hint", "") or USER_HINTS.get(kind, "")
    message = str(exc) or type(exc).__name__
    return StepError(kind=kind, code=getattr(exc, "code", None), message=message[:2000], retryable=retryable,
                     billed=billed, provider_request_id=getattr(exc, "request_id", None), user_hint=hint)


def decide(step: Step, exc: BaseException, handler_provider: str | None = None, *, rng: random.Random | None = None) -> ErrorDecision:
    """Apply the §8.6 table to ``exc`` raised by ``step``'s handler (``step.attempt`` is the attempt that failed)."""
    err = classify(exc, step, handler_provider)
    kind = err.kind
    attempts_left = step.attempt < step.max_attempts
    retry_after = getattr(exc, "retry_after_s", None)
    provider = getattr(exc, "provider", None) or handler_provider

    if kind == "validation":
        return ErrorDecision("validation_ok", err)
    if kind == "submission_uncertain":
        return ErrorDecision("wait_remote", err, delay_s=5.0, remote_state="submission_uncertain")
    if kind == "truncated":
        if not step.result.get("_truncated_retry"):
            return ErrorDecision("retry", err, delay_s=0.0, hints={"max_tokens_scale": 2, "_truncated_retry": True})
        return ErrorDecision("fail", err)
    if kind == "capability":
        if not step.result.get("_capability_retry"):
            return ErrorDecision("retry", err, delay_s=0.0, hints={"drop_param": err.code, "_capability_retry": True})
        return ErrorDecision("fail", err)
    if kind in ("billing", "auth"):
        return ErrorDecision("fail", err, pause_queue=(kind == "billing"),
                             pause_provider=provider if kind == "auth" else None)
    if kind == "remote_failed":
        # Tripo 2018 -> resubmit once; others -> next seed once; the handler marks those retryable. Then the user.
        if err.retryable and attempts_left:
            return ErrorDecision("retry", err, delay_s=backoff_s(step.attempt, rng=rng))
        return ErrorDecision("fail", err)
    if kind in RETRY_BACKOFF_KINDS or (kind not in FAIL_KINDS and err.retryable):
        if attempts_left and err.retryable:
            delay = backoff_s(step.attempt, rng=rng)
            if isinstance(retry_after, (int, float)) and retry_after > delay:
                delay = float(retry_after)
            if kind == "concurrency":
                delay = max(delay, 10.0)
            return ErrorDecision("retry", err, delay_s=delay)
        err = err.model_copy(update={"retryable": False,
                                     "user_hint": (err.user_hint + " It ran out of retries.").strip()})
        return ErrorDecision("fail", err)
    return ErrorDecision("fail", err.model_copy(update={"retryable": False}))
