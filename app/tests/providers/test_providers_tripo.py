"""Tripo v3 adapter: bodies, named views, retry table, submission_uncertain, polling, downloads (APP_SPEC 7.5)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prov_helpers import Spy, json_response, png_bytes

from duoskin.providers import tripo as T
from duoskin.providers._http import httpx
from duoskin.providers.base import (
    CallCtx,
    Cancelled,
    CapabilityFlags,
    ProviderError,
    RateLimiter,
    SubmissionUncertainError,
)

GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 40
FBX = b"Kaydara FBX Binary  \x00" + b"\x00" * 30
ZIP = b"PK\x03\x04" + b"\x00" * 30
MODEL_URL = "https://tripo-data.rg1.data.tripo3d.com/tcli_abc/model.glb?X-Sig=secret"
IMG_URL = "https://cdn1.tripo3d.ai/render.webp?sig=zzz"


def created(task_id="t-1"):
    return json_response({"code": 0, "data": {"task_id": task_id}})


def task_json(task_id="t-1", status="success", **kw):
    data = {"task_id": task_id, "type": "multiview_to_model", "status": status, "progress": 100 if status == "success" else 10,
            "input": {"model": "P2-20260801", "model_seed": 11, "texture_seed": 11}, "output": {}, "credits_consumed": 110}
    data.update(kw)
    return json_response({"code": 0, "data": data})


def err(status, code, message="nope", headers=None):
    return httpx.Response(status, json={"code": code, "message": message, "suggestion": "fix it", "request_id": "rq-1"}, headers=headers or {})


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


def api(spy: Spy, *, dl: Spy | None = None, **kw) -> T.TripoApi:
    clk = kw.pop("clk", None) or Clock()
    kw.setdefault("sleep", clk.sleep)
    kw.setdefault("clock", clk.now)
    kw.setdefault("limiter", RateLimiter(concurrent=2, clock=clk.now, sleep=clk.sleep))
    a = T.TripoApi("tsk_secretkey0000", transport=spy.transport, download_transport=(dl.transport if dl else None), **kw)
    a.clk = clk   # type: ignore[attr-defined]
    return a


P2 = T.P2Params.for_seed(3000, 11)


# ----- params and body builders -----------------------------------------------------------------------------------

def test_p2_body_always_has_the_pinned_fields_and_never_the_forbidden_ones():
    for seed in T.SEEDS:
        b = T.P2Params.for_seed(3000, seed).body()
        assert b == {"model": "P2-20260801", "face_limit": 3000, "quad": False, "texture": True, "texture_quality": "standard",
                     "pbr": False, "texture_alignment": "original_image", "orientation": "default", "auto_size": False,
                     "model_seed": seed, "texture_seed": seed}
        assert not (T.FORBIDDEN_BODY_KEYS | {"smart_low_poly", "enable_image_autofix"}) & b.keys()


def test_p2_extras_only_when_asked():
    b = T.P2Params(face_limit=1500, model_seed=29, texture_seed=29, orientation="align_image", orthographic_projection=True,
                   texture_version="v3.5-20260815", delight=False).body()
    assert b["orientation"] == "align_image" and b["orthographic_projection"] is True and b["texture_version"] == "v3.5-20260815" and b["delight"] is False
    assert "compress" not in b and "export_uv" not in b
    asked = T.P2Params(face_limit=3000, model_seed=11, texture_seed=11, compress="geometry", export_uv=True)
    assert asked.body()["compress"] == "geometry" and asked.explicit_extras() == {"compress", "export_uv"}
    assert P2.explicit_extras() == frozenset()


@pytest.mark.parametrize("bad", [{"quad": True}, {"pbr": True}, {"texture_quality": "detailed"}, {"auto_size": True}, {"model": "P1-20260311"},
                                 {"face_limit": 10}, {"face_limit": 60000}, {"texture": False}, {"unknown": 1}])
def test_p2_params_reject_forbidden_values(bad):
    kw = {"face_limit": 3000, "model_seed": 1, "texture_seed": 1, **bad}
    with pytest.raises(ValueError):
        T.P2Params(**kw)


def test_face_limit_ranges_per_route():
    assert T.P1Params.for_seed(20000).body()["model"] == "P1-20260311"
    with pytest.raises(ValueError):
        T.P1Params.for_seed(20001)
    with pytest.raises(ValueError):
        T.H31Params.for_seed(100)
    assert T.H31Params.for_seed(3000).body()["smart_low_poly"] is True and "smart_low_poly" not in T.P1Params.for_seed(3000).body()


@pytest.mark.parametrize(("mut", "route"), [
    ({"pbr": True}, "p2"), ({"quad": True}, "p2"), ({"auto_size": True}, "p2"), ({"compress": "geometry"}, "p2"),
    ({"export_uv": True}, "p2"), ({"generate_parts": True}, "p2"), ({"return_multiview": True}, "p2"), ({"smart_low_poly": True}, "p2"),
    ({"texture_quality": "extreme"}, "p2"), ({"model": "P1-20260311"}, "p2"), ({"texture_alignment": "geometry"}, "p2"),
    ({"face_limit": None}, "p2"), ({"model_seed": None}, "p2"),
])
def test_check_body_rejects_bad_bodies(mut, route):
    body = {**P2.body(), **mut}
    body = {k: v for k, v in body.items() if v is not None}
    with pytest.raises(ProviderError) as ei:
        T.check_body(body, route)
    assert ei.value.kind == "bad_request" and ei.value.code == "body_check"


def test_check_body_is_keyed_by_route():
    T.check_body(T.P1Params.for_seed(3000).body(), "p1")
    T.check_body(T.H31Params.for_seed(3000).body(), "h31")
    with pytest.raises(ProviderError):
        T.check_body(T.H31Params.for_seed(3000).body(), "p2")           # smart_low_poly forbidden on p2
    with pytest.raises(ProviderError):
        b = T.P1Params.for_seed(3000).body()
        T.check_body({k: v for k, v in b.items() if k != "face_limit"}, "p1")
    T.check_body({**P2.body(), "compress": "geometry"}, "p2", allow={"compress"})   # only when asked


def test_views_are_named_objects_in_fixed_order():
    assert T.views_inputs({"right": "r", "front": "f", "back": "b", "left": "l"}) == [{"front": "f"}, {"left": "l"}, {"back": "b"}, {"right": "r"}]
    assert T.views_inputs({"back": "b", "front": "f"}) == [{"front": "f"}, {"back": "b"}]
    for bad in ({"front": "f"}, {"left": "l", "back": "b"}, {"front": "f", "top": "t"}, {"front": "f", "left": ""}):
        with pytest.raises(ProviderError) as ei:
            T.views_inputs(bad)
        assert ei.value.code == "bad_views"


# ----- task creation -----------------------------------------------------------------------------------------------

def test_multiview_to_model_posts_named_inputs_with_auth_and_persists_ref_before_returning():
    order = []
    spy = Spy(created("t-42"))
    a = api(spy)
    ctx = CallCtx(set_remote_ref=lambda ref: order.append(("ref", ref)))
    tid = a.multiview_to_model({"right": "tr", "front": "tf", "left": "tl", "back": "tb"}, P2, ctx=ctx)
    order.append(("returned", tid))
    spy.assert_hit(1)
    assert order == [("ref", "t-42"), ("returned", "t-42")]
    http, body = spy.requests[0], spy.bodies[0]
    assert str(http.url) == "https://openapi.tripo3d.ai/v3/generation/multiview-to-model" and http.headers["authorization"] == "Bearer tsk_secretkey0000"
    assert body["inputs"] == [{"front": "tf"}, {"left": "tl"}, {"back": "tb"}, {"right": "tr"}]
    assert body["model"] == "P2-20260801" and body["face_limit"] == 3000 and body["pbr"] is False and body["model_seed"] == 11
    assert not T.FORBIDDEN_BODY_KEYS & body.keys()


def test_multiview_reuse_task_and_bad_route_params():
    spy = Spy(created())
    api(spy).multiview_to_model("mv-task-1", P2)
    assert spy.bodies[0]["inputs"] == [{"task_id": "mv-task-1"}]


def test_image_to_model_disables_autofix():
    spy = Spy(created())
    api(spy).image_to_model("tok", T.P2Params.for_seed(1500, 29))
    assert spy.requests[0].url.path == "/v3/generation/image-to-model"
    b = spy.bodies[0]
    assert b["input"] == "tok" and b["enable_image_autofix"] is False and b["face_limit"] == 1500 and b["model_seed"] == 29


def test_fallback_routes_send_their_own_bodies():
    spy = Spy(created("a"), created("b"))
    a = api(spy)
    a.multiview_to_model({"front": "f", "back": "b"}, T.P1Params.for_seed(3000))
    a.multiview_to_model({"front": "f", "back": "b"}, T.H31Params.for_seed(3000))
    assert spy.bodies[0]["model"] == "P1-20260311" and "smart_low_poly" not in spy.bodies[0]
    assert spy.bodies[1]["model"] == "v3.1-20260211" and spy.bodies[1]["smart_low_poly"] is True


def test_text_to_model_edit_multiview_convert_import():
    spy = Spy(created("t1"), created("t2"), created("t3"), created("t4"), created("t5"))
    a = api(spy)
    a.text_to_model("a small blue backpack", P2, negative_prompt="text")
    assert spy.requests[0].url.path == "/v3/generation/text-to-model" and spy.bodies[0]["prompt"] == "a small blue backpack"
    assert spy.bodies[0]["negative_prompt"] == "text"
    a.edit_multiview("mv-1", {"back": "no logo", "front": "bluer"})
    assert spy.bodies[1] == {"input": "mv-1", "prompts": [{"view": "front", "prompt": "bluer"}, {"view": "back", "prompt": "no logo"}]}
    a.convert("task-9", fmt="GLTF", face_limit=3000)
    assert spy.requests[2].url.path == "/v3/models/convert"
    assert spy.bodies[2] == {"input": "task-9", "format": "GLTF", "texture_size": 1024, "texture_format": "PNG", "export_vertex_colors": False,
                             "pack_uv": True, "face_limit": 3000}
    a.convert("task-9", fmt="OBJ", face_limit=None)
    assert "face_limit" not in spy.bodies[3]
    a.import_model("file-tok")
    assert spy.requests[4].url.path == "/v3/models/import" and spy.bodies[4] == {"input": "file-tok"}


def test_convert_on_raw_file_token_is_refused_until_flagged():
    a = api(Spy(created()))
    with pytest.raises(ProviderError) as ei:
        a.convert("file-tok", fmt="GLTF", face_limit=1000, source_kind="file")
    assert ei.value.code == "convert_on_file_token_unverified"
    spy = Spy(created())
    api(spy, flags=CapabilityFlags({"tripo.convert_on_file_token": True})).convert("file-tok", fmt="GLTF", face_limit=1000, source_kind="file")
    spy.assert_hit(1)


def test_edit_and_text_validation():
    a = api(Spy())
    for bad in ({}, {"top": "x"}, {"front": ""}, {"front": "x" * 1025}):
        with pytest.raises(ProviderError):
            a.edit_multiview("mv", bad)   # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        a.text_to_model("", P2)
    with pytest.raises(ProviderError):
        a.convert("t", fmt="STL", face_limit=None)   # type: ignore[arg-type]


def test_mesh_endpoints_and_texture_body():
    spy = Spy(created("s1"), created("c1"), created("r1"), created("x1"))
    a = api(spy)
    a.segment_mesh("task-1", granularity="balanced")
    a.complete_mesh("s1", part_names=["wing"], completion_mode="quick_cap")
    a.retopology("task-1", face_limit=5000)
    a.texture_model("task-1", texture_seed=29)
    paths = [r.url.path for r in spy.requests]
    assert paths == ["/v3/mesh/segment", "/v3/mesh/complete", "/v3/mesh/decimate", "/v3/models/texture"]
    assert spy.bodies[0] == {"input": "task-1", "segmentation_granularity": "balanced"}
    assert spy.bodies[1] == {"input": "s1", "part_names": ["wing"], "completion_mode": "quick_cap"}
    assert spy.bodies[2] == {"input": "task-1", "face_limit": 5000, "quad": False}
    assert spy.bodies[3]["pbr"] is False and spy.bodies[3]["texture_quality"] == "standard" and "compress" not in spy.bodies[3]
    with pytest.raises(ProviderError):
        a.retopology("t", face_limit=500)


def test_upload_is_multipart_png_only():
    spy = Spy(json_response({"code": 0, "data": {"file_token": "ft-1"}}))
    a = api(spy)
    assert a.upload(png_bytes(64, 64), name="front.png") == "ft-1"
    assert spy.requests[0].headers["content-type"].startswith("multipart/form-data") and b'filename="front.png"' in spy.requests[0].content
    with pytest.raises(ProviderError) as ei:
        a.upload(b"GIF89a", name="x.gif")
    assert ei.value.code == "bad_upload"
    spy2 = Spy(json_response({"code": 0, "data": {"file_token": "ft-2"}}))
    assert api(spy2).upload(GLB, name="m.glb") == "ft-2" and b"model/gltf-binary" in spy2.requests[0].content


# ----- retry table ---------------------------------------------------------------------------------------------------

def test_429_1007_backs_off_with_x_ratelimit_reset_delta_then_succeeds():
    spy = Spy(err(429, 1007, "rate", {"x-ratelimit-reset": "7"}), err(429, 1007, "rate"), created())
    a = api(spy)
    assert a.image_to_multiview("tok") == "t-1"
    spy.assert_hit(3)
    assert sum(a.clk.sleeps) >= 8.0      # 7 s from X-RateLimit-Reset (delta) plus the next backoff


def test_429_1007_with_epoch_reset():
    import time as _time
    reset = str(int(_time.time()) + 20)
    spy = Spy(err(429, 1007, "rate", {"x-ratelimit-reset": reset}), created())
    a = api(spy)
    a.image_to_multiview("tok")
    assert 15 <= sum(a.clk.sleeps) <= 22


def test_429_1007_exhausted_raises_rate_limit():
    spy = Spy(*[err(429, 1007, "rate")] * 6)
    with pytest.raises(ProviderError) as ei:
        api(spy).image_to_multiview("tok")
    spy.assert_hit(6)
    assert ei.value.kind == "rate_limit" and ei.value.group == "rate_limited" and ei.value.code == "1007" and ei.value.retryable


def test_429_2000_lowers_slots_and_is_not_retried():
    lim = RateLimiter(concurrent=2)
    spy = Spy(err(429, 2000, "too many concurrent tasks"))
    with pytest.raises(ProviderError) as ei:
        api(spy, limiter=lim).multiview_to_model({"front": "f", "back": "b"}, P2)
    spy.assert_hit(1)
    assert ei.value.kind == "concurrency" and ei.value.code == "2000" and lim.concurrent == 1


@pytest.mark.parametrize(("status", "code", "kind"), [(400, 1003, "bad_request"), (401, 1002, "auth"), (403, 1004, "permission"),
                                                       (403, 2010, "billing"), (400, 2015, "not_found"), (400, 2008, "moderation")])
def test_client_errors_are_not_retried(status, code, kind):
    spy = Spy(err(status, code))
    with pytest.raises(ProviderError) as ei:
        api(spy).image_to_model("tok", P2)
    spy.assert_hit(1)
    assert ei.value.kind == kind and not ei.value.retryable and ei.value.request_id == "rq-1"


def test_connect_errors_are_retried_because_nothing_was_sent():
    spy = Spy(httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), created("t-9"))
    assert api(spy).multiview_to_model({"front": "f", "back": "b"}, P2) == "t-9"
    spy.assert_hit(3)


def test_connect_errors_exhausted():
    spy = Spy(*[httpx.ConnectError("refused")] * 4)
    with pytest.raises(ProviderError) as ei:
        api(spy).image_to_model("tok", P2)
    spy.assert_hit(4)
    assert ei.value.kind == "network" and ei.value.billed == "no"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ReadError("reset"), httpx.RemoteProtocolError("closed"),
                                     httpx.WriteTimeout("w")])
def test_paid_post_transport_failure_after_send_is_submission_uncertain_and_never_resent(failure):
    spy = Spy(failure)
    a = api(spy)
    before = datetime.now(UTC)
    with pytest.raises(SubmissionUncertainError) as ei:
        a.multiview_to_model({"front": "f", "back": "b"}, T.P2Params.for_seed(3000, 29))
    spy.assert_hit(1)
    e = ei.value
    assert e.kind == "submission_uncertain" and e.group == "submission_uncertain" and not e.retryable
    ctx = e.context
    assert ctx["endpoint"] == "/generation/multiview-to-model" and ctx["op"] == "multiview_to_model"
    assert ctx["body"]["model_seed"] == 29 and ctx["submitted_at"] >= before and "Checking" in e.user_message


@pytest.mark.parametrize("status", [500, 502, 503])
def test_paid_post_5xx_after_send_is_submission_uncertain(status):
    spy = Spy(httpx.Response(status, text="upstream error"))
    with pytest.raises(ProviderError) as ei:
        api(spy).multiview_to_model({"front": "f", "back": "b"}, P2)
    spy.assert_hit(1)
    assert ei.value.kind == "submission_uncertain" and ei.value.context["body"]["face_limit"] == 3000


def test_free_post_timeout_is_a_plain_retryable_error():
    spy = Spy(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as ei:
        api(spy).upload(png_bytes(8, 8), name="a.png")
    assert ei.value.kind == "timeout" and ei.value.retryable


def test_get_retries_network_errors_and_5xx_up_to_five_times():
    spy = Spy(httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.Response(500, text="x"), httpx.Response(502, text="x"),
              httpx.ConnectError("x"), task_json(status="running"))
    st = api(spy).task("t-1")
    spy.assert_hit(6)
    assert st.status == "running"
    spy2 = Spy(*[httpx.Response(500, text="x")] * 6)
    with pytest.raises(ProviderError) as ei:
        api(spy2).task("t-1")
    spy2.assert_hit(6)
    assert ei.value.kind == "server"


def test_get_does_not_retry_client_errors():
    spy = Spy(err(404, 1005, "no such task"))
    with pytest.raises(ProviderError) as ei:
        api(spy).task("nope")
    spy.assert_hit(1)
    assert ei.value.kind == "not_found"


def test_envelope_code_nonzero_on_200_is_an_error():
    spy = Spy(json_response({"code": 2010, "message": "insufficient credit"}))
    with pytest.raises(ProviderError) as ei:
        api(spy).image_to_multiview("tok")
    assert ei.value.kind == "billing"


# ----- task parsing and failure mapping ------------------------------------------------------------------------------------

def test_parse_task_fields_urls_and_legacy_statuses():
    st = T.parse_task({"task_id": "x", "status": "success", "progress": 100, "credits_consumed": 110.0,
                       "input": {"model_seed": 29}, "type": "multiview_to_model",
                       "output": {"model_url": MODEL_URL, "rendered_image_url": IMG_URL, "generate_multiview_image": {"front_view_url": "https://a.tripo3d.ai/f.png"},
                                  "model_urls": ["https://a.tripo3d.ai/m0.glb"], "riggable": True}})
    assert st.status == "success" and st.credits_consumed == 110.0 and st.input["model_seed"] == 29 and st.done
    assert st.output_urls["model_url"] == MODEL_URL and st.output_urls["front_view_url"].endswith("f.png") and "model_urls[0]" in st.output_urls
    assert "secret" not in repr(st) and "sig=zzz" not in repr(st)
    for legacy, code in (("banned", 2008), ("expired", 2018), ("unknown", None)):
        s = T.parse_task({"task_id": "x", "status": legacy})
        assert s.status == "failed" and s.error_code == code and s.failure_error() is not None
    assert T.parse_task({"task_id": "x", "status": "canceled"}).status == "cancelled"


def test_failure_errors_follow_the_table():
    e2008 = T.parse_task({"task_id": "x", "status": "failed", "error_code": 2008, "error_message": "policy"}).failure_error()
    assert e2008.kind == "moderation" and not e2008.retryable and "not resubmitted" in e2008.user_message
    e2018 = T.parse_task({"task_id": "x", "status": "failed", "error_code": 2018}).failure_error()
    assert e2018.kind == "remote_failed" and e2018.retryable and e2018.code == "2018"
    other = T.parse_task({"task_id": "x", "status": "failed", "error_code": 5000, "credits_consumed": 0}).failure_error()
    assert other.kind == "remote_failed" and not other.retryable and other.billed == "no" and other.code == "5000"
    assert T.parse_task({"task_id": "x", "status": "running"}).failure_error() is None


# ----- polling ------------------------------------------------------------------------------------------------------------

def test_poll_intervals_are_5_then_3_growing_by_1_4_to_15():
    it = iter(T.poll_intervals())
    seq = [next(it) for _ in range(9)]
    assert seq[0] == 5.0 and seq[1] == 3.0 and seq[2] == pytest.approx(4.2) and seq[3] == pytest.approx(5.88)
    assert max(seq) == 15.0 and seq[-1] == 15.0


def test_wait_polls_until_success_and_records_cost_once():
    costs = []
    spy = Spy(task_json(status="queued"), task_json(status="running"), task_json(status="running"), task_json(status="success"), task_json(status="success"))
    a = api(spy, cost_sink=costs.append)
    a._ops["t-1"] = ("multiview_to_model", "P2-20260801", 1)
    progress = []
    st = a.wait("t-1", ctx=CallCtx(progress=lambda f, m: progress.append((f, m))))
    spy.assert_hit(4)
    assert st.status == "success" and not st.slow and sum(a.clk.sleeps[:10]) == pytest.approx(5.0)   # first poll after 5 s
    assert len(costs) == 1 and costs[0]["credits"] == 110.0 and costs[0]["usd"] == pytest.approx(1.10) and costs[0]["basis"] == "credits"
    assert costs[0]["remote_task_id"] == "t-1" and progress
    a._finish(st)
    assert len(costs) == 1


def test_wait_soft_timeout_marks_slow_keeps_polling_and_never_resubmits():
    n_running = 200
    spy = Spy(*([task_json(status="running", credits_consumed=None)] * n_running), task_json(status="success"))
    a = api(spy)
    msgs = []
    st = a.wait("t-1", ctx=CallCtx(progress=lambda f, m: msgs.append(m)))
    assert st.status == "success" and st.slow is True
    assert any("slow" in m for m in msgs)
    assert all(r.method == "GET" for r in spy.requests), "polling never POSTs (ENG-07)"
    assert a.clk.t - 1000.0 > 1200.0


def test_wait_hard_timeout_and_cancel():
    spy = Spy(*([task_json(status="running")] * 400))
    with pytest.raises(ProviderError) as ei:
        api(spy).wait("t-1", soft_timeout_s=10, hard_timeout_s=60)
    assert ei.value.code == "poll_timeout"
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 3:
            raise Cancelled()

    with pytest.raises(Cancelled):
        api(Spy(*([task_json(status="running")] * 50))).wait("t-1", ctx=CallCtx(check_cancel=cancel))


def test_wait_success_raises_failure_error_but_wait_returns_the_status():
    spy = Spy(task_json(status="failed", error_code=2008, error_message="blocked", credits_consumed=0))
    st = api(spy).wait("t-1")
    assert st.status == "failed"
    spy2 = Spy(task_json(status="failed", error_code=2008, error_message="blocked", credits_consumed=0))
    with pytest.raises(ProviderError) as ei:
        api(spy2).wait_success("t-1")
    assert ei.value.kind == "moderation"


def test_tasks_chunks_of_100_and_reports_missed():
    ids = [f"t{i}" for i in range(150)]

    def handler(request):
        import json
        want = json.loads(request.content)["task_ids"]
        return json_response({"code": 0, "data": {"tasks": {i: {"status": "running", "progress": 5} for i in want if i != "t7"},
                                                  "missed": [i for i in want if i == "t7"]}})

    spy = Spy(handler, handler)
    found, missed = api(spy).tasks(ids)
    spy.assert_hit(2)
    assert len(spy.bodies[0]["task_ids"]) == 100 and len(spy.bodies[1]["task_ids"]) == 50
    assert len(found) == 149 and missed == ["t7"] and found["t0"].status == "running"


# ----- account ------------------------------------------------------------------------------------------------------------

def test_balance_usage_and_credit_checks():
    spy = Spy(json_response({"code": 0, "data": {"balance": 500.0, "frozen": 120.0}}),
              json_response({"code": 0, "data": [{"task_id": "a", "type": "multiview_to_model", "credits_consumed": 110, "created_at": "2026-10-01T10:00:00Z"}]}),
              json_response({"code": 0, "data": {"balance": 500.0, "frozen": 120.0}}),
              json_response({"code": 0, "data": {"balance": 500.0, "frozen": 120.0}}),
              json_response({"code": 0, "data": {"balance": 500.0, "frozen": 120.0}}))
    a = api(spy)
    assert a.balance() == (500.0, 120.0)
    rows = a.usage(limit=10, offset=5)
    assert rows[0]["task_id"] == "a" and spy.requests[1].url.params["limit"] == "10" and spy.requests[1].url.params["offset"] == "5"
    assert a.credits_available() == 380.0
    assert a.ensure_credits(110) == 380.0
    with pytest.raises(ProviderError) as ei:
        a.ensure_credits(110, budget_left_credits=50)
    assert ei.value.kind == "billing" and ei.value.code == "insufficient_credits"
    a2 = api(Spy(json_response({"code": 0, "data": {"balance": 500.0, "frozen": 120.0}})), flags=CapabilityFlags({"tripo.balance_excludes_frozen": True}))
    assert a2.credits_available() == 500.0


def test_check_balance_option_blocks_a_paid_post():
    spy = Spy(json_response({"code": 0, "data": {"balance": 50.0, "frozen": 0.0}}))
    with pytest.raises(ProviderError) as ei:
        api(spy, check_balance=True).multiview_to_model({"front": "f", "back": "b"}, P2)
    spy.assert_hit(1)
    assert ei.value.kind == "billing" and spy.requests[0].method == "GET"


# ----- downloads ------------------------------------------------------------------------------------------------------------

def success_with_urls(**urls):
    return task_json(status="success", output=urls)


def test_download_uses_a_separate_auth_free_client_and_checks_magic_bytes():
    api_spy = Spy(success_with_urls(model_url=MODEL_URL, rendered_image_url=IMG_URL))
    dl = Spy(httpx.Response(200, content=GLB), httpx.Response(200, content=png_bytes(16, 16)))
    files = api(api_spy, dl=dl).download_files("t-1", ["model_url", "rendered_image_url"])
    api_spy.assert_hit(1)
    dl.assert_hit(2)
    assert all("authorization" not in r.headers for r in dl.requests)
    assert api_spy.requests[0].headers["authorization"].startswith("Bearer ")
    assert files["model_url"].kind == "glb" and files["rendered_image_url"].kind == "png"
    import hashlib
    assert files["model_url"].sha256 == hashlib.sha256(GLB).hexdigest() and files["model_url"].size == len(GLB)


@pytest.mark.parametrize(("blob", "kind"), [(GLB, "glb"), (FBX, "fbx"), (ZIP, "zip")], ids=["glb", "fbx", "zip"])
def test_download_accepts_the_three_model_formats(blob, kind):
    a = api(Spy(success_with_urls(model_url=MODEL_URL)), dl=Spy(httpx.Response(200, content=blob)))
    f = a.download_files("t-1", ["model_url"])["model_url"]
    assert f.data == blob and f.kind == kind


def test_download_rejects_wrong_magic_bytes():
    a = api(Spy(success_with_urls(model_url=MODEL_URL)), dl=Spy(httpx.Response(200, content=b"<html>error</html>")))
    with pytest.raises(ProviderError) as ei:
        a.download("t-1", ["model_url"])
    assert ei.value.kind == "validation" and ei.value.code == "bad_magic_unknown"


def test_download_refuses_hosts_off_the_allowlist():
    a = api(Spy(success_with_urls(model_url="https://evil.example.com/m.glb")), dl=Spy())
    with pytest.raises(ProviderError) as ei:
        a.download("t-1", ["model_url"])
    assert ei.value.code == "host_not_allowed"
    # look-alike hosts do not match *.tripo3d.ai
    for url in ("https://tripo3d.ai.evil.com/m.glb", "https://tripo3d.ai/m.glb", "http://cdn.tripo3d.ai/m.glb", "https://user@cdn.tripo3d.ai@evil.com/m.glb"):
        a2 = api(Spy(success_with_urls(model_url=url)), dl=Spy())
        with pytest.raises(ProviderError) as e2:
            a2.download("t-1", ["model_url"])
        assert e2.value.code == "host_not_allowed", url


def test_expired_signed_url_re_gets_the_task_up_to_three_times():
    api_spy = Spy(success_with_urls(model_url=MODEL_URL),
                  success_with_urls(model_url="https://tripo-data.rg1.data.tripo3d.com/new1.glb"),
                  success_with_urls(model_url="https://tripo-data.rg1.data.tripo3d.com/new2.glb"))
    dl = Spy(httpx.Response(403), httpx.Response(404), httpx.Response(200, content=GLB))
    out = api(api_spy, dl=dl).download("t-1", ["model_url"])
    assert out["model_url"] == GLB and len(api_spy.requests) == 3 and len(dl.requests) == 3
    assert str(dl.requests[2].url).endswith("new2.glb")


def test_expired_url_gives_up_after_three_re_gets():
    api_spy = Spy(*[success_with_urls(model_url=MODEL_URL)] * 4)
    dl = Spy(*[httpx.Response(403)] * 4)
    with pytest.raises(ProviderError) as ei:
        api(api_spy, dl=dl).download("t-1", ["model_url"])
    assert ei.value.code == "download_403" and len(api_spy.requests) == 4


def test_download_requires_a_successful_task_and_present_key():
    with pytest.raises(ProviderError) as e1:
        api(Spy(task_json(status="running"))).download("t-1", ["model_url"])
    assert e1.value.code == "not_success"
    with pytest.raises(ProviderError) as e2:
        api(Spy(success_with_urls(model_url=MODEL_URL))).download("t-1", ["rendered_image_url"])
    assert e2.value.code == "missing_output"


def test_download_size_cap_and_redirects():
    from duoskin.providers.base import Downloader
    dl = Downloader("tripo", T.DOWNLOAD_HOSTS, max_bytes=100, transport=Spy(httpx.Response(200, content=b"x" * 200)).transport)
    with pytest.raises(ProviderError) as ei:
        dl.fetch("https://cdn.tripo3d.ai/x.glb")
    assert ei.value.code == "too_large"
    dl2 = Downloader("tripo", T.DOWNLOAD_HOSTS, transport=Spy(httpx.Response(302, headers={"location": "https://evil.example.com"})).transport)
    with pytest.raises(ProviderError) as e2:
        dl2.fetch("https://cdn.tripo3d.ai/x.glb")
    assert e2.value.code == "download_redirect"


# ----- reconciliation ----------------------------------------------------------------------------------------------------------

def _usage_row(tid, typ="multiview_to_model", at=None):
    return {"task_id": tid, "type": typ, "status": "success", "credits_consumed": 110,
            "created_at": (at or datetime.now(UTC)).isoformat()}


def _usage(rows):
    return json_response({"code": 0, "data": rows})


def test_reconcile_finds_the_task_with_the_same_seed_within_two_minutes():
    t0 = datetime.now(UTC)
    rows = [_usage_row("other-seed", at=t0), _usage_row("too-old", at=t0 - timedelta(minutes=10)), _usage_row("wrong-type", "image_to_model", t0),
            _usage_row("ours", at=t0 + timedelta(seconds=20))]
    spy = Spy(_usage(rows),
              task_json("other-seed", input={"model_seed": 11, "texture_seed": 11}),
              task_json("ours", input={"model_seed": 29, "texture_seed": 29}))
    found = api(spy).reconcile_uncertain(endpoint="/generation/multiview-to-model", submitted_at=t0, body=T.P2Params.for_seed(3000, 29).body())
    assert found == "ours"
    paths = [r.url.path for r in spy.requests]
    assert paths[0] == "/v3/account/usage" and "/v3/tasks/other-seed" in paths and "/v3/tasks/ours" in paths
    assert "/v3/tasks/too-old" not in paths and "/v3/tasks/wrong-type" not in paths


def test_reconcile_returns_none_so_the_engine_may_resubmit_only_when_nothing_matches():
    t0 = datetime.now(UTC)
    spy = Spy(_usage([_usage_row("a", at=t0)]), task_json("a", input={"model_seed": 11}), _usage([]), _usage([]))
    a = api(spy)
    assert a.reconcile_uncertain(endpoint="/generation/multiview-to-model", submitted_at=t0, body={"model_seed": 29}, attempts=1) is None
    assert a.reconcile_uncertain(endpoint="/generation/multiview-to-model", submitted_at=t0, body={"model_seed": 29}, attempts=2, wait_s=5.0) is None
    assert sum(a.clk.sleeps) == pytest.approx(5.0)      # one wait between the two attempts


def test_reconcile_waits_for_lagging_usage_rows():
    t0 = datetime.now(UTC)
    spy = Spy(_usage([]), _usage([_usage_row("late", at=t0)]), task_json("late", input={"model_seed": 29}))
    assert api(spy).reconcile_uncertain(endpoint="/generation/multiview-to-model", submitted_at=t0, body={"model_seed": 29}) == "late"


def test_reconcile_without_seeds_picks_the_closest_row_and_honours_exclude():
    t0 = datetime.now(UTC)
    rows = [_usage_row("known", "image_to_multiview", t0), _usage_row("new", "image_to_multiview", t0 + timedelta(seconds=30))]
    spy = Spy(_usage(rows))
    found = api(spy).reconcile_uncertain(endpoint="/generation/image-to-multiview", submitted_at=t0, body={"input": "tok"}, exclude=["known"])
    assert found == "new"


def test_uncertain_error_context_feeds_reconcile_directly():
    t_spy = Spy(httpx.ReadTimeout("slow"))
    a = api(t_spy)
    with pytest.raises(SubmissionUncertainError) as ei:
        a.image_to_multiview("tok")
    ctx = ei.value.context
    spy2 = Spy(_usage([_usage_row("mv-1", "image_to_multiview", ctx["submitted_at"])]))
    assert api(spy2).reconcile_uncertain(endpoint=ctx["endpoint"], submitted_at=ctx["submitted_at"], body=ctx["body"]) == "mv-1"


def test_no_secret_in_errors_or_repr():
    spy = Spy(err(401, 1002, "bad token tsk_secretkey0000"))
    with pytest.raises(ProviderError) as ei:
        api(spy).image_to_model("tok", P2)
    assert "tsk_secretkey0000" not in str(ei.value) and "tsk_secretkey0000" not in repr(api(Spy()))


def test_test_key_and_startup_probe():
    bal = json_response({"code": 0, "data": {"balance": 480.0, "frozen": 20.0}})
    spy = Spy(bal)
    r = api(spy).test_key()
    assert r["ok"] is True and "480" in r["message"] and "20 frozen" in r["message"]
    spy2 = Spy(bal, json_response({"code": 0, "data": {"file_token": "ft"}}))
    p = api(spy2).startup_probe()
    assert p["ok"] is True and p["upload_ok"] is True and spy2.requests[1].url.path == "/v3/files"
    spy3 = Spy(bal, err(400, 1003, "image too small"))
    p3 = api(spy3).startup_probe()
    assert p3["ok"] is True and p3["upload_ok"] is False and "1x1" in p3["message"]
    bad = api(Spy(err(401, 1002, "bad key"))).test_key()
    assert bad["ok"] is False and bad["kind"] == "auth"
