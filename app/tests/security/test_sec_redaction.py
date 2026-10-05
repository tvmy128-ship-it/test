"""Security lens 3: API keys never reach a log line, an error text, an event or a signed-URL record.

A canary key (``CNRY``) is pushed through the log redactor, the provider error scrubber, the step-error model, the event bus and every
real provider adapter (on a hostile transport that echoes the request headers and URL back in its error body).
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from duoskin import logsetup
from duoskin.logsetup import REDACTED, RedactFilter, redact, redact_data, register_secret

CNRY = "CNRYsecretvalue0123456789abcdef"                    # no known prefix: only the exact-value masking can catch it
SK = "sk-proj-CNRY" + "Ab12Cd34Ef56" * 2
TSK = "tsk_CNRY" + "Ab12Cd34Ef56" * 2
AIZA = "AIzaCNRY" + "Ab12Cd34Ef56" * 2
KEYS = (CNRY, SK, TSK, AIZA)


@pytest.fixture(autouse=True)
def _registered():
    for k in KEYS:
        register_secret(k)
    yield
    for k in KEYS:
        logsetup.forget_secret(k)


# ------------------------------------------------------------------------------------------------- the redactor
@pytest.mark.parametrize("text", [
    f"GET https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key={AIZA}",
    f"GET https://api.example.com/v1/x?foo=1&api_key={CNRY}&bar=2",
    f"GET https://api.example.com/v1/x?apikey={CNRY}",
    f"Authorization: Bearer {CNRY}",
    f"authorization: Basic {CNRY}",
    f"'Authorization': 'Bearer {SK}'",
    f'{{"x-api-key": "{SK}"}}',
    f"headers={{'x-goog-api-key': '{AIZA}'}}",
    f"x-api-key: {SK}",
    f"Proxy-Authorization: Basic {CNRY}",
    f"Cookie: session={CNRY}; other=1",
    f"OPENAI_API_KEY={SK}",
    f"RECRAFT_API_TOKEN={CNRY}",
    f"https://user:{CNRY}@proxy.example.com:8080/",
    f"url=https://tripo-data.rg1.data.tripo3d.com/v2/a.glb?Policy={CNRY}&Signature={CNRY}&Key-Pair-Id=K2ABCDEFGHIJKL",
    f"https://oaidalleapiprodscus.blob.core.windows.net/p/img.png?st=2026&se=2027&sp=r&sig={CNRY}",
    f"https://s3.amazonaws.com/b/k?X-Amz-Algorithm=AWS4&X-Amz-Credential={CNRY}&X-Amz-Signature={CNRY}",
    f"https://s3.amazonaws.com/b/k?X-Amz-Security-Token={CNRY}",
    f"https://storage.googleapis.com/b/o?X-Goog-Credential={CNRY}&X-Goog-Signature={CNRY}",
    f"https://x.example.com/dl?access_token={CNRY}",
    f'{{"password": "{CNRY}", "token": "{CNRY}"}}',
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
])
def test_the_redactor_masks_every_secret_shape(text):
    out = redact(text)
    for needle in (*KEYS, "eyJzdWIi", "Ab12Cd34Ef56", "K2ABCDEFGHIJKL", "dozjgNry"):
        assert needle not in out, (text, out)
    assert REDACTED in out


def test_the_redactor_masks_url_encoded_and_json_escaped_forms():
    odd = 'CNRY"quote\\slash+plus/slash=eq&amp'
    register_secret(odd)
    try:
        from urllib.parse import quote, quote_plus

        for form in (odd, quote(odd, safe=""), quote_plus(odd), json.dumps(odd)[1:-1], repr(odd)[1:-1]):
            assert "CNRY" not in redact(f"before {form} after"), form
    finally:
        logsetup.forget_secret(odd)


def test_the_redactor_leaves_ordinary_text_alone():
    ok = "the risk-averse task-based plan used max_tokens=4096, input_tokens: 100 and a secret garden; key lime pie"
    assert redact(ok) == ok


def test_redact_data_walks_dicts_lists_and_keys():
    data = {"a": [f"x {CNRY}", {"b": (f"y {SK}",)}], f"k{CNRY}": 1, "n": 5}
    out = redact_data(data)
    blob = json.dumps(out)
    assert CNRY not in blob and SK not in blob and out["n"] == 5


def test_the_log_filter_masks_message_args_exception_and_stack():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logsetup.JsonFormatter())
    handler.addFilter(RedactFilter())
    log = logging.getLogger("duoskin.test_sec_filter")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        try:
            raise RuntimeError(f"upstream refused Authorization: Bearer {CNRY} for ?key={AIZA}")
        except RuntimeError:
            log.exception("call failed for %s", SK, extra={"hdr": f"x-api-key: {TSK}"}, stack_info=True)
    finally:
        log.removeHandler(handler)
    text = stream.getvalue()
    assert text and not any(k in text for k in KEYS)


# ------------------------------------------------------------------------------------------------- error texts
def test_provider_error_scrubs_registered_and_patterned_keys_and_urls():
    from duoskin.providers.base import ProviderError

    msg = f"bad key {CNRY}; url https://files.example.com/x?Signature={CNRY}; Authorization: Bearer {SK}; AIza {AIZA}"
    err = ProviderError("recraft", "auth", msg, request_id=f"req-{CNRY}")
    blob = json.dumps([str(err), err.message, err.to_step_error(), err.user_message])
    assert not any(k in blob for k in KEYS), blob[:300]


def test_step_error_never_holds_a_key_whoever_builds_it():
    from duoskin.engine.errors import classify
    from duoskin.models.job import Step, StepError

    e = StepError(kind="other", message=f"x {CNRY}", user_hint=f"y {SK}", code=f"c-{AIZA}", provider_request_id=f"r-{TSK}")
    assert not any(k in e.model_dump_json() for k in KEYS)
    from datetime import UTC, datetime

    step = Step(id="stp_x", job_id="job_x", kind="t", created_at=datetime.now(UTC))
    err = classify(RuntimeError(f"500 from https://x.example.com/?key={CNRY} with Authorization: Bearer {CNRY}"), step)
    assert not any(k in err.model_dump_json() for k in KEYS)


def test_a_key_split_by_the_2000_character_cut_is_not_left_half_visible():
    from datetime import UTC, datetime

    from duoskin.engine.errors import classify
    from duoskin.models.job import Step

    step = Step(id="stp_x", job_id="job_x", kind="t", created_at=datetime.now(UTC))
    text = "z" * (2000 - 10) + CNRY                     # the key straddles the 2000-character boundary
    err = classify(RuntimeError(text), step)
    assert CNRY[:10] not in err.message


def test_events_are_redacted_before_they_reach_the_table_or_a_stream(tmp_path):
    from duoskin.engine.testkit import make_runtime

    rt = make_runtime(tmp_path / "home")
    try:
        rt.bus.emit("toast", {"message": f"failed: Authorization: Bearer {CNRY}", "detail": [f"?key={AIZA}", {"k": SK}]})
        rows = rt.db.conn().execute("SELECT payload FROM events").fetchall()
        blob = "".join(r["payload"] for r in rows)
        assert blob and not any(k in blob for k in KEYS)
        assert not any(k in e.sse_data() for e in rt.bus.events_after(0) for k in KEYS)
    finally:
        rt.shutdown()


def test_a_key_test_message_is_redacted_before_it_is_saved(tmp_path):
    from duoskin import config
    from duoskin.keystore import KeyStore

    ks = KeyStore(config.paths(tmp_path / "home"), keyring_module=False, environ={})
    res = ks.record_test("openai", False, f"rejected {SK} at https://api.example.com/?key={CNRY}")
    assert not any(k in res.message for k in KEYS)
    assert not any(k in (ks._tests_path().read_text(encoding="utf-8")) for k in KEYS)


# ------------------------------------------------------------------------------------------------- adapters on a hostile transport
def _hostile(request):
    """Every request fails with a body that echoes the headers, the URL and the key (the worst a buggy gateway can do)."""
    from duoskin.providers._http import httpx

    echo = f"denied: {dict(request.headers)} {request.url} {CNRY}"
    return httpx.Response(401, json={"error": {"message": echo, "type": "invalid_request_error", "code": "bad", "status": "UNAUTHENTICATED"},
                                     "code": 401, "message": echo, "detail": echo},
                          headers={"x-request-id": "req-1", "request-id": "req-1"})


def _no_key_anywhere(*objs) -> None:
    blob = "\n".join(o if isinstance(o, str) else json.dumps(o, default=repr) for o in objs)
    assert not any(k in blob for k in KEYS), blob[:400]


def _png(n: int = 300) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (n, n), (200, 40, 40, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_every_adapter_reports_a_rejected_key_without_the_key():
    from duoskin.providers._http import httpx
    from duoskin.providers.base import CallCtx, ProviderError, RateLimiter
    from duoskin.providers.gemini import GeminiProvider, RuleSpec
    from duoskin.providers.recraft import RecraftProvider
    from duoskin.providers.tripo import TripoApi

    def fast():
        return RateLimiter(concurrent=2)

    tr = httpx.MockTransport(_hostile)
    nosleep = (lambda _s: None)
    ctx = CallCtx.null()
    outcomes: list = []
    reprs: list[str] = []
    raised = 0
    for adapter, call in (
            (RecraftProvider(CNRY, transport=tr, limiter=fast(), sleep=nosleep), lambda a: a.test_key()),
            (RecraftProvider(CNRY, transport=tr, limiter=fast(), sleep=nosleep), lambda a: a.remove_background(_png(64))),
            (GeminiProvider(AIZA, transport=tr, limiter=fast(), sleep=nosleep), lambda a: a.test_key()),
            (GeminiProvider(AIZA, transport=tr, limiter=fast(), sleep=nosleep),
             lambda a: a.judge(_png(), [RuleSpec(rule_id="fp_round", statement="The iris is round.")], thinking_level="LOW", ctx=ctx)),
            (TripoApi(TSK, transport=tr, limiter=fast(), sleep=nosleep, get_retries=0), lambda a: a.test_key()),
            (TripoApi(TSK, transport=tr, limiter=fast(), sleep=nosleep, get_retries=0), lambda a: a.task("task-1", ctx=ctx)),
            (TripoApi(TSK, transport=tr, limiter=fast(), sleep=nosleep, get_retries=0), lambda a: a.upload(_png(64), name="x.png", ctx=ctx)),
    ):
        reprs.append(repr(adapter))
        try:
            outcomes.append(call(adapter))
        except ProviderError as e:
            raised += 1
            outcomes.append([str(e), e.message, e.to_step_error(), e.user_message, repr(e.context)])
    assert raised >= 4, "the hostile body must reach the error path, or this test proves nothing"
    assert any("denied" in json.dumps(o, default=repr) for o in outcomes), "the echoed body should survive in scrubbed form"
    _no_key_anywhere(outcomes, reprs)


def test_the_sdk_adapters_report_a_rejected_key_without_the_key():
    import anthropic
    import openai

    from duoskin.providers._http import httpx
    from duoskin.providers.anthropic_llm import AnthropicProvider
    from duoskin.providers.base import ProviderError
    from duoskin.providers.openai_images import OpenAIImages

    tr = httpx.MockTransport(_hostile)
    ant = AnthropicProvider(client=anthropic.Anthropic(api_key=SK, http_client=anthropic.DefaultHttpxClient(transport=tr), max_retries=0))
    oai = OpenAIImages(client=openai.OpenAI(api_key=SK, http_client=openai.DefaultHttpxClient(transport=tr), max_retries=0))
    out = [ant.test_key(), oai.test_key(paid=False), repr(ant), repr(oai)]
    try:
        from duoskin.providers.base import CallCtx
        from duoskin.providers.openai_images import ImageRequest

        oai.generate(ImageRequest(model="gpt-image-2.5-flare-2026-09-08", prompt="a flat blue square", size="1024x1024", quality="low",
                                  background="opaque", n=1), CallCtx.null())
    except ProviderError as e:
        out += [str(e), e.message, e.to_step_error()]
    _no_key_anywhere(out)


def test_the_key_travels_only_in_the_auth_header_of_the_api_host_and_never_to_a_download_host():
    """Recraft/Tripo/Gemini: the key is a header of the API client, never in a URL, a body or the (separate) download client."""
    from duoskin.providers._http import httpx
    from duoskin.providers.base import Downloader

    seen: list = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 40, headers={"content-type": "image/png"})

    from duoskin.providers.recraft import RecraftProvider

    rp = RecraftProvider(CNRY, transport=httpx.MockTransport(handler))
    rp._client.get("https://external.api.recraft.ai/v1/users/me")
    dl = Downloader("recraft", ["img.recraft.ai"], transport=httpx.MockTransport(handler))
    dl.fetch("https://img.recraft.ai/a.png")
    api_req, dl_req = seen
    assert api_req.headers["authorization"] == f"Bearer {CNRY}" and CNRY not in str(api_req.url) and CNRY not in api_req.content.decode("latin-1")
    assert not any(h in dl_req.headers for h in ("authorization", "x-api-key", "x-goog-api-key", "cookie"))
    assert CNRY not in str(dl_req.url)


def test_adapter_reprs_and_settings_never_show_a_key():
    from duoskin.models.settings import Settings
    from duoskin.providers.gemini import GeminiProvider
    from duoskin.providers.recraft import RecraftProvider
    from duoskin.providers.tripo import TripoApi

    for obj in (RecraftProvider(CNRY), GeminiProvider(AIZA), TripoApi(TSK)):
        assert not any(k in repr(obj) + str(obj) for k in KEYS)
    assert not any(k in repr(Settings()) + Settings().model_dump_json() for k in KEYS)


def test_keystore_status_shows_only_a_masked_tail():
    from duoskin.keystore import KeyStore, mask_key

    masked = mask_key(SK)
    assert SK not in masked and len(masked) <= 8 and masked.endswith(SK[-4:])
    assert mask_key("short") == "..."
    ks = KeyStore.__new__(KeyStore)
    assert not hasattr(ks, "__dict__") or CNRY not in repr(ks.__dict__)
