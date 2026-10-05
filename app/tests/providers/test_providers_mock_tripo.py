"""MockTripo: state machine, models, views, hair with the grey head, faults, reconcile (APP_SPEC 7.5, 7.8)."""
from __future__ import annotations

import io
import random
from datetime import UTC, datetime

import numpy as np
import pytest
import trimesh
from PIL import Image
from prov_helpers import png_bytes

from duoskin.providers import tripo as T
from duoskin.providers.base import CallCtx, Cancelled, ProviderError, SubmissionUncertainError
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock import meshes as M
from duoskin.providers.mock.tripo import MockTripo

P2 = T.P2Params.for_seed(3000, 11)


def green_png() -> bytes:
    return D.to_png(D.over_background(D.shape_rgba(512, 512, (40, 160, 90), random.Random(1))))


def views_of(m: MockTripo, mv: str) -> dict[str, str]:
    files = m.download(mv, [f"{v}_view_url" for v in T.VIEWS])
    return {k.replace("_view_url", ""): m.upload(v, name=k + ".png") for k, v in files.items()}


def load(glb: bytes):
    scene = trimesh.load(io.BytesIO(glb), file_type="glb", force="scene")
    return scene


def test_state_machine_queued_running_running_success():
    m = MockTripo()
    tok = m.upload(green_png(), name="front.png")
    tid = m.image_to_multiview(tok)
    seq = [m.task(tid).status for _ in range(5)]
    assert seq == ["queued", "running", "running", "success", "success"]
    st = m.task(tid)
    assert st.credits_consumed == 10 and st.progress == 100 and set(st.output_urls) == {f"{v}_view_url" for v in T.VIEWS}
    assert all(u.startswith("https://tripo-data.rg1.data.tripo3d.com/") for u in st.output_urls.values())
    with pytest.raises(ProviderError) as ei:
        m.task("no-such-task")
    assert ei.value.kind == "not_found"


def test_full_flow_views_then_p2_model_with_costs_and_remote_refs():
    refs = []
    ctx = CallCtx(set_remote_ref=refs.append)
    m = MockTripo()
    tok = m.upload(green_png(), name="front.png")
    mv = m.image_to_multiview(tok, ctx=ctx)
    assert m.wait_success(mv, ctx=ctx).credits_consumed == 10
    views = m.download(mv, [f"{v}_view_url" for v in T.VIEWS])
    sizes = {k: Image.open(io.BytesIO(v)).size for k, v in views.items()}
    assert len(set(sizes.values())) == 1 and all(v[:8] == b"\x89PNG\r\n\x1a\n" for v in views.values())
    t3 = m.multiview_to_model(views_of(m, mv), P2, ctx=ctx)
    st = m.wait_success(t3, ctx=ctx)
    assert st.credits_consumed == 110 and st.input["model_seed"] == 11 and st.input["model"] == "P2-20260801"
    files = m.download_files(t3, ["model_url", "rendered_image_url"])
    assert files["model_url"].kind == "glb" and files["rendered_image_url"].kind == "png"
    assert refs == [mv, t3]                                                  # remote_ref is persisted at creation
    assert [r.operation for r in m.requests] == ["image_to_multiview", "multiview_to_model"]
    assert [c["credits"] for c in m.costs] == [10.0, 110.0] and all(c["provider"] == "mock" for c in m.costs)
    assert m.costs[1]["usd"] == pytest.approx(1.10)


def test_the_glb_is_a_textured_mesh_of_about_two_thousand_triangles_with_one_material():
    m = MockTripo()
    tid = m.image_to_model(m.upload(green_png(), name="a.png"), P2)
    m.wait_success(tid)
    glb = m.download(tid, ["model_url"])["model_url"]
    assert glb[:4] == b"glTF"
    stats = M.mesh_stats(glb)
    assert 1000 <= stats["triangles"] <= 3200 and stats["materials"] == 1 and stats["texture_size"] == (1024, 1024)
    mesh = next(iter(load(glb).geometry.values()))
    welded = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=True)
    assert welded.is_watertight


def test_outputs_follow_the_input_colour_and_are_deterministic():
    def model_colour(png: bytes):
        m = MockTripo()
        tid = m.image_to_model(m.upload(png, name="a.png"), P2)
        m.wait_success(tid)
        return m.download(tid, ["model_url"])["model_url"]

    g1, g2 = model_colour(green_png()), model_colour(green_png())
    red = D.to_png(D.over_background(D.shape_rgba(512, 512, (200, 40, 40), random.Random(1))))
    assert g1 == g2 and g1 != model_colour(red)
    scene = load(g1)
    tex = np.asarray(next(iter(scene.geometry.values())).visual.material.baseColorTexture.convert("RGB")).astype(int)
    assert tex[..., 1].mean() > tex[..., 0].mean()                           # greenish texture for a green input


def test_four_views_share_scale_and_ground_line():
    m = MockTripo()
    mv = m.image_to_multiview(m.upload(green_png(), name="a.png"))
    m.wait(mv)
    vs = {k: np.asarray(Image.open(io.BytesIO(v)).convert("RGB")).astype(int) for k, v in m.download(mv, [f"{v}_view_url" for v in T.VIEWS]).items()}
    obj = {k: (np.abs(v - 255).sum(axis=-1) > 12) for k, v in vs.items()}
    heights = {k: int(np.ptp(np.nonzero(o.any(axis=1))[0])) for k, o in obj.items()}
    assert max(heights.values()) - min(heights.values()) <= 4                # equal object height across views
    ground = {k: int(np.nonzero(o.any(axis=1))[0].max()) for k, o in obj.items()}
    assert max(ground.values()) - min(ground.values()) <= 2                  # one ground line


def test_one_sided_prop_makes_left_and_right_views_differ():
    mesh = M.build_mesh("one_sided_prop", (90, 170, 110), 3)
    views = M.render_views(mesh, 256)
    assert views["left"] != views["right"] and views["front"] != views["back"]
    # +X (the bump) is the subject's left in the glTF convention used here: the "left" camera sees it as a silhouette bump
    left = np.asarray(Image.open(io.BytesIO(views["left"])).convert("L")) < 250
    right = np.asarray(Image.open(io.BytesIO(views["right"])).convert("L")) < 250
    assert left.sum() != right.sum()


def test_edit_multiview_changes_only_the_edited_views():
    m = MockTripo()
    mv = m.image_to_multiview(m.upload(green_png(), name="a.png"))
    m.wait(mv)
    ed = m.edit_multiview(mv, {"back": "make the back gold"})
    st = m.wait_success(ed)
    assert st.credits_consumed == 5
    keys = [f"{v}_view_url" for v in T.VIEWS]
    a, b = m.download(mv, keys), m.download(ed, keys)
    assert a["front_view_url"] == b["front_view_url"] and a["back_view_url"] != b["back_view_url"]
    with pytest.raises(ProviderError):
        m.edit_multiview("nope", {"back": "x"})


def test_hair_requests_return_a_shell_with_the_grey_cube_head_still_attached():
    m = MockTripo()
    for how in ("face_limit", "tag", "name"):
        png = green_png()
        if how == "face_limit":
            tid = m.image_to_model(m.upload(png, name="a.png"), T.P2Params.for_seed(3500, 11))
        elif how == "tag":
            tid = m.image_to_model(m.upload(png, name="b.png"), P2, ctx=CallCtx(tag="hair.model"))
        else:
            tid = m.image_to_model(m.upload(png, name="a.hair_front.png"), P2)
        m.wait_success(tid)
        glb = m.download(tid, ["model_url"])["model_url"]
        tex = np.asarray(next(iter(load(glb).geometry.values())).visual.material.baseColorTexture.convert("RGB"))
        grey = (np.abs(tex.astype(int) - 0x9A).sum(axis=-1) == 0).mean()
        assert 0.2 < grey < 0.3, how                                         # the #9A9A9A head tile (a quarter of the atlas)
    plush = m.image_to_model(m.upload(green_png(), name="p.png"), P2)
    m.wait(plush)
    plush_scene = load(m.download(plush, ["model_url"])["model_url"])
    tex2 = np.asarray(next(iter(plush_scene.geometry.values())).visual.material.baseColorTexture.convert("RGB"))
    assert (np.abs(tex2.astype(int) - 0x9A).sum(axis=-1) == 0).mean() < 0.01


def test_convert_formats_and_magic_bytes():
    m = MockTripo()
    base = m.image_to_model(m.upload(green_png(), name="a.png"), P2)
    m.wait(base)
    for fmt, kind, credits in (("GLTF", "glb", 10), ("OBJ", "zip", 10), ("FBX", "fbx", 10)):
        c = m.convert(base, fmt=fmt, face_limit=1500)
        st = m.wait_success(c)
        assert st.credits_consumed == credits
        assert m.download_files(c, ["model_url"])["model_url"].kind == kind
    import zipfile
    zc = m.convert(base, fmt="OBJ", face_limit=None)
    m.wait(zc)
    z = zipfile.ZipFile(io.BytesIO(m.download(zc, ["model_url"])["model_url"]))
    assert set(z.namelist()) == {"model.obj", "model.mtl", "texture.png"}
    with pytest.raises(ProviderError):
        m.convert("unknown", fmt="GLTF", face_limit=None)
    with pytest.raises(ProviderError) as ei:
        m.convert(m.upload(green_png(), name="x.png"), fmt="GLTF", face_limit=None, source_kind="file")
    assert ei.value.code == "convert_on_file_token_unverified"


def test_import_and_mesh_tools():
    m = MockTripo()
    glb = M.build_glb("icosphere")
    imp = m.import_model(m.upload(glb, name="mine.glb"))
    assert m.wait_success(imp).credits_consumed == 0 and m.download(imp, ["model_url"])["model_url"] == glb
    base = m.image_to_model(m.upload(green_png(), name="a.png"), P2)
    m.wait(base)
    for tid in (m.retopology(base, face_limit=2000), m.texture_model(base, texture_seed=29), m.segment_mesh(base)):
        assert m.wait_success(tid).status == "success" and m.download(tid, ["model_url"])["model_url"][:4] == b"glTF"


def test_builders_and_validation_are_shared_with_the_real_adapter():
    m = MockTripo()
    tok = m.upload(green_png(), name="a.png")
    with pytest.raises(ProviderError) as e1:
        m.multiview_to_model({"front": tok}, P2)                              # front alone is not enough
    assert e1.value.code == "bad_views"
    with pytest.raises(ProviderError) as e2:
        m.multiview_to_model({"front": "unknown-token", "back": tok}, P2)
    assert e2.value.code == "unknown_file_token"
    with pytest.raises(ProviderError):
        m.upload(b"GIF89a", name="x.gif")
    assert all(r.operation != "multiview_to_model" for r in m.requests)               # rejected before anything was "sent"
    # compress is allowed only when the caller asked for it explicitly
    assert m.image_to_model(tok, T.P2Params(face_limit=3000, model_seed=1, texture_seed=1, compress="geometry"))


def test_balance_is_2000_zero_unless_tracked():
    m = MockTripo()
    assert m.balance() == (2000.0, 0.0)
    m.wait(m.image_to_model(m.upload(green_png(), name="a.png"), P2))
    assert m.balance() == (2000.0, 0.0)
    t = MockTripo(track_balance=True)
    tid = t.image_to_model(t.upload(green_png(), name="a.png"), P2)
    assert t.balance() == (2000.0, 110.0)
    t.wait(tid)
    assert t.balance() == (1890.0, 0.0) and t.credits_available() == 1890.0
    assert t.ensure_credits(110) == 1890.0
    with pytest.raises(ProviderError):
        t.ensure_credits(5000)


def test_usage_rows_newest_first_and_tasks_batch():
    t = [datetime(2026, 10, 1, 10, 0, s, tzinfo=UTC) for s in range(0, 40, 10)]
    clock = iter(t)
    m = MockTripo(now=lambda: next(clock))
    tok = m.upload(green_png(), name="a.png")
    ids = [m.image_to_multiview(tok), m.image_to_model(tok, P2), m.import_model(tok)]
    rows = m.usage()
    assert [r["task_id"] for r in rows] == ids[::-1] and rows[0]["type"] == "import_model"
    assert m.usage(limit=1, offset=1)[0]["task_id"] == ids[1]
    found, missed = m.tasks([*ids, "ghost"])
    assert set(found) == set(ids) and missed == ["ghost"]


def test_wait_slow_failures_and_cancel():
    m = MockTripo(running_polls=2)
    tid = m.image_to_model(m.upload(green_png(), name="a.png"), P2)
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 4:
            raise Cancelled()

    with pytest.raises(Cancelled):
        m.wait(tid, ctx=CallCtx(check_cancel=cancel))
    s = MockTripo(faults=FaultInjector("tripo:slow@first"))
    sid = s.image_to_model(s.upload(green_png(), name="a.png"), P2)
    st = s.wait(sid, soft_timeout_s=60)
    assert st.status == "success" and st.slow is True


# ----- faults ------------------------------------------------------------------------------------------------------------

def test_submit_faults_raise_the_real_errors_and_create_no_task():
    m = MockTripo(faults=FaultInjector("tripo:1007@first,tripo:2000@n2,tripo:2010@n3"))
    tok = m.upload(green_png(), name="a.png")
    for kind, code in (("rate_limit", "1007"), ("concurrency", "2000"), ("billing", "2010")):
        with pytest.raises(ProviderError) as ei:
            m.image_to_model(tok, P2)
        assert ei.value.kind == kind and ei.value.code == code
    assert m.usage() == [] and m.costs == []
    assert m.image_to_model(tok, P2)


@pytest.mark.parametrize(("fault", "code", "kind", "retryable"), [("tripo:2008@seed29", 2008, "moderation", False), ("tripo:moderation@seed29", 2008, "moderation", False),
                                                                    ("tripo:2018@seed29", 2018, "remote_failed", True),
                                                                    ("tripo:queue_expired@seed29", 2018, "remote_failed", True),
                                                                    ("tripo:task_failed@seed29", 5000, "remote_failed", False), ("tripo:4242@seed29", 4242, "remote_failed", False)])
def test_task_level_failures_by_seed(fault, code, kind, retryable):
    m = MockTripo(faults=FaultInjector(fault))
    tok = m.upload(green_png(), name="a.png")
    ok = m.image_to_model(tok, T.P2Params.for_seed(3000, 11))
    assert m.wait_success(ok).status == "success"
    bad = m.image_to_model(tok, T.P2Params.for_seed(3000, 29))
    st = m.wait(bad)
    assert st.status == "failed" and st.error_code == code and st.credits_consumed == 0
    err = st.failure_error()
    assert err.kind == kind and err.retryable is retryable
    with pytest.raises(ProviderError):
        m.download(bad, ["model_url"])
    assert len(m.costs) == 1                                                  # a failed task costs nothing


def test_read_timeout_after_send_creates_the_task_and_reconciles():
    m = MockTripo(faults=FaultInjector("tripo:read_timeout_after_send@T3"))
    tok = m.upload(green_png(), name="a.png")
    views = {"front": tok, "back": tok}
    refs = []
    with pytest.raises(SubmissionUncertainError) as ei:
        m.multiview_to_model(views, T.P2Params.for_seed(3000, 29), ctx=CallCtx(set_remote_ref=refs.append))
    ctx = ei.value.context
    assert refs == [] and ctx["endpoint"] == "/generation/multiview-to-model" and ctx["body"]["model_seed"] == 29
    assert len(m.requests) == 1                                               # never resent
    found = m.reconcile_uncertain(endpoint=ctx["endpoint"], submitted_at=ctx["submitted_at"], body=ctx["body"], attempts=1)
    assert found is not None and m.task(found).input["model_seed"] == 29
    assert m.reconcile_uncertain(endpoint=ctx["endpoint"], submitted_at=ctx["submitted_at"], body={"model_seed": 47}, attempts=1) is None
    assert m.wait_success(found).status == "success"


def test_connect_error_fault_and_env_faults(monkeypatch):
    m = MockTripo(faults=FaultInjector("tripo:connect_error@first"))
    tok = m.upload(green_png(), name="a.png")
    with pytest.raises(ProviderError) as ei:
        m.image_to_multiview(tok)
    assert ei.value.kind == "network" and ei.value.billed == "no"
    monkeypatch.setenv("DUOSKIN_MOCK_FAULTS", "tripo:2010@first")
    env_mock = MockTripo()                                                    # no injector given: DUOSKIN_MOCK_FAULTS applies
    with pytest.raises(ProviderError) as e2:
        env_mock.image_to_multiview(env_mock.upload(png_bytes(8, 8), name="x.png"))
    assert e2.value.code == "2010"
