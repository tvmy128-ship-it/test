"""Errors, call context, rate limiter, capability flags and small helpers (APP_SPEC 7.1)."""
from __future__ import annotations

import copy
import pickle
import threading
import time

import pytest
from prov_helpers import Spy

from duoskin.providers import base as B
from duoskin.providers._http import httpx

# ----- errors ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("kind", "cls", "group"), [
    ("overloaded", B.TransientError, "transient"), ("server", B.TransientError, "transient"), ("timeout", B.TransientError, "transient"),
    ("network", B.TransientError, "transient"), ("rate_limit", B.RateLimitedError, "rate_limited"),
    ("concurrency", B.RateLimitedError, "rate_limited"), ("moderation", B.ModerationBlockedError, "moderation_blocked"),
    ("recitation", B.ModerationBlockedError, "moderation_blocked"), ("refusal", B.RefusalError, "refusal"),
    ("bad_request", B.BadRequestError, "bad_request"), ("capability", B.BadRequestError, "bad_request"),
    ("validation", B.BadRequestError, "bad_request"), ("schema_too_complex", B.BadRequestError, "bad_request"),
    ("truncated", B.BadRequestError, "bad_request"), ("auth", B.AuthError, "auth"), ("permission", B.AuthError, "auth"),
    ("not_found", B.AuthError, "auth"), ("billing", B.BudgetError, "budget"),
    ("submission_uncertain", B.SubmissionUncertainError, "submission_uncertain"), ("remote_failed", B.ProviderError, "other"),
    ("other", B.ProviderError, "other"),
])
def test_kind_selects_the_subclass_and_group(kind, cls, group):
    e = B.ProviderError("x", kind, "boom")
    assert type(e) is cls and isinstance(e, B.ProviderError) and e.group == group and e.kind == kind
    assert e.user_message and "boom" not in e.user_message or kind in ("other",)


def test_user_hint_wins_over_the_default_and_step_error_shape():
    e = B.ProviderError("openai", "moderation", "blocked", http=400, code="moderation_blocked", billed="no", request_id="r1",
                        user_hint="Reword it.", retry_after_s=2.0)
    assert e.user_message == "Reword it."
    se = e.to_step_error()
    assert se == {"kind": "moderation", "code": "moderation_blocked", "message": "blocked", "retryable": False, "billed": "no",
                  "provider_request_id": "r1", "user_hint": "Reword it.", "provider": "openai", "http": 400, "retry_after_s": 2.0}
    assert B.ProviderError("a", "billing", "x").user_message.startswith("The provider says the account is out of credit")


def test_errors_survive_pickle_and_copy():
    e = B.SubmissionUncertainError("tripo", "submission_uncertain", "lost", context={"endpoint": "/x"}, code="c", billed="unknown")
    for e2 in (pickle.loads(pickle.dumps(e)), copy.copy(e)):
        assert type(e2) is B.SubmissionUncertainError and e2.context == {"endpoint": "/x"} and e2.code == "c" and str(e2) == str(e)


def test_messages_are_scrubbed_of_keys_and_urls():
    e = B.ProviderError("x", "auth", "bad key sk-ant-api03-ABCDEF123456 and Bearer tsk_abc123456 at https://x.tripo3d.ai/f.glb?sig=zzz AIzaSyABCDEFGHIJKLMNOPQRST")
    for secret in ("sk-ant-api03", "tsk_abc", "sig=zzz", "AIzaSyABC", "https://"):
        assert secret not in str(e), secret
    assert B.scrub("x-api-key: sk-123456789") == "x-api-key: <redacted>"
    assert len(B.scrub("a" * 5000)) < 2100


def test_cancelled_and_ctx_defaults():
    ctx = B.CallCtx.null()
    ctx.heartbeat()
    ctx.check_cancel()
    ctx.progress(0.5, "x")
    ctx.set_remote_ref("t")
    ctx.tick()
    assert ctx.step_id is None and ctx.tag == ""
    beats = []
    B.CallCtx(heartbeat=lambda: beats.append(1), check_cancel=lambda: beats.append(2)).tick()
    assert beats == [1, 2]
    assert issubclass(B.Cancelled, Exception)


# ----- rate limiter ----------------------------------------------------------------------------------------------------

class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += s


def test_ipm_bucket_allows_a_burst_then_waits():
    ft = FakeTime()
    lim = B.RateLimiter(ipm=6, concurrent=1, clock=ft.now, sleep=ft.sleep)
    with lim.acquire(images=6):
        pass
    assert ft.slept == 0
    with lim.acquire(images=3):
        pass
    assert ft.slept == pytest.approx(30.0, abs=1.0)           # 3 images at 6 per minute = 30 s
    with lim.acquire(images=0):                              # a call with no images only needs a slot
        pass


def test_rpm_and_rps_buckets():
    ft = FakeTime()
    lim = B.RateLimiter(rps=2, rpm=60, concurrent=1, clock=ft.now, sleep=ft.sleep)
    for _ in range(2):
        with lim.acquire():
            pass
    assert ft.slept == 0
    with lim.acquire():
        pass
    assert 0 < ft.slept <= 1.5


def test_request_larger_than_the_bucket_is_clamped_not_deadlocked():
    ft = FakeTime()
    lim = B.RateLimiter(ipm=4, concurrent=1, clock=ft.now, sleep=ft.sleep)
    with lim.acquire(images=10):
        pass
    assert lim.ipm_capacity == 4 and B.RateLimiter(concurrent=1).ipm_capacity is None


def test_penalize_makes_everybody_wait():
    ft = FakeTime()
    lim = B.RateLimiter(concurrent=2, clock=ft.now, sleep=ft.sleep)
    lim.penalize(20)
    with lim.acquire():
        pass
    assert ft.slept >= 20


def test_concurrency_limit_blocks_and_releases_across_threads():
    lim = B.RateLimiter(concurrent=2)
    inside, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal inside, peak
        with lim.acquire():
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.03)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2 and lim.in_flight == 0


def test_lower_concurrency_never_below_minimum():
    lim = B.RateLimiter(concurrent=2)
    assert lim.lower_concurrency() == 1 and lim.lower_concurrency() == 1 and lim.concurrent == 1
    lim.set_concurrent(4)
    assert lim.concurrent == 4
    lim.set_ipm(10)
    assert lim.ipm_capacity == 10


def test_cancel_while_waiting_for_a_slot():
    lim = B.RateLimiter(concurrent=1, slice_s=0.01)
    cancelled = []

    def check():
        cancelled.append(1)
        if len(cancelled) > 2:
            raise B.Cancelled()

    with lim.acquire(), pytest.raises(B.Cancelled), lim.acquire(ctx=B.CallCtx(check_cancel=check)):
        pass
    with lim.acquire():            # the slot was not leaked by the cancelled waiter
        pass


def test_exception_inside_acquire_releases_the_slot():
    lim = B.RateLimiter(concurrent=1)
    with pytest.raises(RuntimeError), lim.acquire():
        raise RuntimeError("x")
    assert lim.in_flight == 0


def test_limiter_for_is_shared_and_updated_in_place():
    a = B.limiter_for("openai", ipm=5, concurrent=3)
    b = B.limiter_for("openai")
    assert a is b and a.ipm_capacity == 5 and a.concurrent == 3
    B.limiter_for("openai", ipm=8, concurrent=1)
    assert a.ipm_capacity == 8 and a.concurrent == 1
    assert B.limiter_for("recraft").ipm_capacity == 100


# ----- capability flags -------------------------------------------------------------------------------------------------

def test_unknown_flags_select_the_conservative_path():
    f = B.CapabilityFlags()
    assert f.get("openai.mask_multi_ok") is False and f.get("openai.rgba_image1_ok") is False
    assert f.get("recraft.file_field_name") == "image" and f.get("tripo.balance_excludes_frozen") is False
    assert f.get("anthropic.sonnet_fallbacks") is False and f.get("anthropic.schema_ok.L3_planner") is True
    assert f.get("totally.unknown") is None and f.get("totally.unknown", 7) == 7
    assert not f.is_set("openai.mask_multi_ok")


def test_flag_changes_are_reported_once_and_never_fail_the_caller():
    seen = []
    f = B.CapabilityFlags({"a": 1}, on_change=lambda k, v: seen.append((k, v)))
    f.set("a", 1)
    f.set("a", 2)
    f.set("b", True)
    assert seen == [("a", 2), ("b", True)] and f.snapshot() == {"a": 2, "b": True}

    def boom(k, v):
        raise RuntimeError("disk full")

    B.CapabilityFlags(on_change=boom).set("x", 1)


# ----- hashing and sniffing -----------------------------------------------------------------------------------------------

def test_request_hash_handles_bytes_dataclasses_sets_and_is_order_independent():
    from duoskin.providers.openai_images import NamedPng
    a = B.request_hash({"x": NamedPng("a.png", b"abc"), "s": {3, 1, 2}, "k": 1, "z": (1, 2)})
    b = B.request_hash({"z": [1, 2], "k": 1, "s": {1, 2, 3}, "x": NamedPng("a.png", b"abc")})
    assert a == b and len(a) == 64
    assert B.request_hash({"x": NamedPng("a.png", b"abd")}) != B.request_hash({"x": NamedPng("a.png", b"abc")})
    assert B.jsonable(b"xyz")["len"] == 3 and B.seed_from_hash(a) == B.seed_from_hash(a) and 0 <= B.seed_from_hash(a) < 2 ** 32


@pytest.mark.parametrize(("data", "kind"), [
    (b"glTF\x02\x00\x00\x00", "glb"), (b"Kaydara FBX Binary  \x00", "fbx"), (b"PK\x03\x04", "zip"), (B.PNG_MAGIC + b"x", "png"),
    (b"\xff\xd8\xff\xe0", "jpeg"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"), (b'<?xml version="1.0"?><svg xmlns=""/>', "svg"),
    (b"  \n<svg viewBox='0 0 1 1'/>", "svg"), (b"<html><body>no</body></html>", "unknown"), (b"", "unknown"),
])
def test_sniff_kind(data, kind):
    assert B.sniff_kind(data) == kind


def test_png_size_reads_the_header_only():
    from prov_helpers import png_bytes
    assert B.png_size(png_bytes(30, 20)) == (30, 20) and B.png_size(b"nope") is None and B.png_size(B.PNG_MAGIC) is None


def test_host_allowlist():
    al = B.HostAllowlist(["tripo-data.rg1.data.tripo3d.com", "*.tripo3d.ai"])
    assert al.allows("tripo-data.rg1.data.tripo3d.com") and al.allows("CDN.Tripo3d.AI") and al.allows("a.b.tripo3d.ai")
    for bad in ("tripo3d.ai", "evil.com", "tripo3d.ai.evil.com", "xtripo3d.ai", "", None):
        assert not al.allows(bad), bad


def test_downloader_rejects_http_userinfo_and_hosts_without_calling():
    spy = Spy()
    d = B.Downloader("x", ["*.tripo3d.ai"], transport=spy.transport)
    for url in ("http://a.tripo3d.ai/x", "https://u@a.tripo3d.ai/x", "https://evil.com/x", "ftp://a.tripo3d.ai/x"):
        with pytest.raises(B.ProviderError) as ei:
            d.fetch(url)
        assert ei.value.code == "host_not_allowed"
    assert not spy.requests


def test_downloader_status_mapping_and_streaming_cap():
    cases = [(403, "download_403"), (404, "download_404"), (410, "download_410"), (302, "download_redirect")]
    for status, code in cases:
        d = B.Downloader("x", ["*.tripo3d.ai"], transport=Spy(httpx.Response(status, headers={"location": "https://evil"})).transport)
        with pytest.raises(B.ProviderError) as ei:
            d.fetch("https://a.tripo3d.ai/x")
        assert ei.value.code == code
    d500 = B.Downloader("x", ["*.tripo3d.ai"], transport=Spy(httpx.Response(503)).transport)
    with pytest.raises(B.ProviderError) as e5:
        d500.fetch("https://a.tripo3d.ai/x")
    assert e5.value.kind == "server" and e5.value.retryable
    d400 = B.Downloader("x", ["*.tripo3d.ai"], transport=Spy(httpx.Response(400)).transport)
    with pytest.raises(B.ProviderError) as e4:
        d400.fetch("https://a.tripo3d.ai/x")
    assert e4.value.kind == "bad_request"
    ok = B.Downloader("x", ["*.tripo3d.ai"], transport=Spy(httpx.Response(200, content=b"abc")).transport)
    assert ok.fetch("https://a.tripo3d.ai/x") == b"abc"
    t = B.Downloader("x", ["*.tripo3d.ai"], transport=Spy(httpx.ConnectError("x"), httpx.ReadTimeout("x")).transport)
    with pytest.raises(B.ProviderError) as en:
        t.fetch("https://a.tripo3d.ai/x")
    assert en.value.kind == "network"
    with pytest.raises(B.ProviderError) as et:
        t.fetch("https://a.tripo3d.ai/x")
    assert et.value.kind == "timeout"


# ----- waiting helpers -----------------------------------------------------------------------------------------------------

def test_backoff_delay_doubles_to_the_cap_with_bounded_jitter():
    import random
    rng = random.Random(1)
    for attempt, base in [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (9, 32)]:
        d = B.backoff_delay(attempt, rng=rng)
        assert base <= d <= base * 1.25


def test_sleep_checked_ticks_between_slices():
    ft = FakeTime()
    ticks = []
    B.sleep_checked(2.0, B.CallCtx(heartbeat=lambda: ticks.append(1)), sleep=ft.sleep, slice_s=0.5)
    assert ft.slept == pytest.approx(2.0) and len(ticks) == 4
    B.sleep_checked(0, None, sleep=ft.sleep)


def test_parse_retry_after_variants():
    assert B.parse_retry_after({"Retry-After": "7"}) == 7.0
    assert B.parse_retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:30 GMT"}, now=lambda: 1792567700.0) == pytest.approx(
        __import__("email.utils", fromlist=["x"]).parsedate_to_datetime("Wed, 21 Oct 2026 07:28:30 GMT").timestamp() - 1792567700.0)
    assert B.parse_retry_after({"x-ratelimit-reset": "12"}) == 12.0
    assert B.parse_retry_after({"x-ratelimit-reset": "1000"}, now=lambda: 990.0) == 1000.0                  # a delta below 1e9
    assert B.parse_retry_after({"X-RateLimit-Reset": "1800000030"}, now=lambda: 1800000000.0) == 30.0       # an epoch
    assert B.parse_retry_after({}) is None and B.parse_retry_after(None) is None and B.parse_retry_after({"retry-after": "soon"}) is None


@pytest.mark.parametrize(("status", "msg", "kind"), [
    (401, "", "auth"), (402, "", "billing"), (403, "", "permission"), (404, "", "not_found"), (429, "", "rate_limit"), (529, "", "overloaded"),
    (500, "", "server"), (503, "", "server"), (400, "your credit balance is too low", "billing"), (400, "blocked by content policy", "moderation"),
    (400, "plain", "bad_request"), (418, "", "other"),
])
def test_kind_for_status(status, msg, kind):
    assert B.kind_for_status(status, msg) == kind
    e = B.error_from_status("x", status, msg or "m", headers={"retry-after": "3"})
    assert e.kind == kind and e.http == status and (e.retry_after_s == 3.0)
