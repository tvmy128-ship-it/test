"""Sample meshes, an end-to-end flow on the mocks through the registry, and package hygiene."""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

from duoskin.providers import registry as R
from duoskin.providers.base import CallCtx
from duoskin.providers.mock import meshes as M
from duoskin.providers.openai_images import ImageRequest
from duoskin.providers.recraft import VectorRequest
from duoskin.providers.tripo import P2Params

FIX = Path(M.__file__).resolve().parent.parent / "fixtures" / "meshes"


# ----- sample meshes ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(M.SAMPLES))
def test_sample_meshes_exist_load_and_are_deterministic(name):
    path = FIX / name
    assert path.is_file(), f"{name} missing: run duoskin.providers.mock.meshes.write_samples(providers/fixtures/meshes)"
    data = path.read_bytes()
    assert data[:4] == b"glTF"
    kind, color, seed = M.SAMPLES[name]
    assert data == M.build_glb(kind, color, seed), "the checked-in sample no longer matches the generator"
    stats = M.mesh_stats(data)
    assert stats["materials"] == 1 and stats["texture_size"] == (1024, 1024) and 1000 <= stats["triangles"] <= 3000 and stats["geometries"] == 1
    scene = trimesh.load(io.BytesIO(data), file_type="glb", force="scene")
    mesh = next(iter(scene.geometry.values()))
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals) and mesh.visual.uv.shape[0] == len(mesh.vertices)
    assert np.isfinite(mesh.vertices).all() and mesh.bounds[1][1] - mesh.bounds[0][1] > 0.5


def test_samples_cover_the_cases_the_mesh_track_needs():
    names = set(M.SAMPLES)
    assert {"icosphere_2k.glb", "rounded_box_2k.glb", "one_sided_prop.glb", "plush_pet.glb", "hair_with_grey_head.glb"} <= names
    hair = M.build_mesh("hair_with_head")
    tex = np.asarray(hair.visual.material.baseColorTexture.convert("RGB")).astype(int)
    assert (np.abs(tex - 0x9A).sum(axis=-1) == 0).mean() > 0.2          # the guide-grey head tile for hair.register
    assert M.render_views(hair, 64).keys() == {"front", "left", "back", "right"}


def test_write_samples_is_idempotent(tmp_path):
    a = M.write_samples(tmp_path)
    first = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in a}
    b = M.write_samples(tmp_path)
    assert first == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in b}


def test_unknown_mesh_kind_is_rejected():
    with pytest.raises(ValueError):
        M.build_mesh("teapot")
    views = M.render_glb_views(M.build_glb("icosphere"), 64)         # a GLB round-trips through the renderer
    assert set(views) == {"front", "left", "back", "right"}


# ----- a whole demo flow on the mocks, through the registry -----------------------------------------------------------------------

def test_demo_flow_through_the_registry_in_mock_mode(monkeypatch):
    monkeypatch.setenv("DUOSKIN_PROVIDERS", "mock")
    R.reset()
    ctx = CallCtx.null()
    llm, images, recraft, tripo, gemini = (R.get(p) for p in ("anthropic", "openai", "recraft", "tripo", "gemini"))
    from pydantic import BaseModel

    class Plan(BaseModel):
        title: str
        palette: list[str]

    plan = llm.call("L3_planner", system=[{"type": "text", "text": "ctx"}], content=[{"type": "text", "text": "a teal and gold duo"}], out=Plan, ctx=ctx, prompt_version=1).parsed
    assert plan.title

    concept = images.generate(ImageRequest(model="gpt-image-2.5-flare-2026-09-08", prompt="A character concept sheet, teal jacket, gold trousers", size="1536x1024",
                                           quality="low", background="opaque", n=2, tag="I1"), ctx)
    assert len(concept.images) == 2

    vec = recraft.generate(VectorRequest(model="recraftv4_1_vector", prompt="1. One round teal iris.", size="1024x1024", n=2), ctx)
    assert len(vec.svgs) == 2

    sticker = images.run_many(ImageRequest(model="gpt-image-2.5-flare-2026-09-08", prompt="A gold star accessory", size="1024x1024", quality="low",
                                           background="transparent", n=1, tag="I5"), 4, ctx)
    tok = tripo.upload(sticker.images[0], name="front.png")
    mv = tripo.image_to_multiview(tok)
    tripo.wait_success(mv)
    views = tripo.download(mv, [f"{v}_view_url" for v in ("front", "left", "back", "right")])
    toks = {k.removesuffix("_view_url"): tripo.upload(v, name=k) for k, v in views.items()}
    t3 = tripo.multiview_to_model(toks, P2Params.for_seed(3000, 11))
    st = tripo.wait_success(t3)
    glb = tripo.download(t3, ["model_url"])["model_url"]
    assert st.credits_consumed == 110 and glb[:4] == b"glTF"

    from duoskin.providers.gemini import RuleSpec
    from duoskin.providers.mock.recraft import SENTINEL_RGB
    verdicts = gemini.judge(sticker.images[0], [RuleSpec(rule_id="fp_round", statement="The shape is round.")], thinking_level="LOW", ctx=ctx)
    assert [v.passed for v in verdicts] == [True] and SENTINEL_RGB == (255, 0, 255)
    total_usd = sum(c["usd"] for p in (llm, images, recraft, tripo) for c in p.costs)
    assert total_usd > 1.1 and all(c["provider"] == "mock" for p in (llm, images, recraft, tripo) for c in p.costs)


def test_the_same_demo_gives_the_same_pictures_twice(monkeypatch):
    monkeypatch.setenv("DUOSKIN_PROVIDERS", "mock")

    def run():
        R.reset()
        img = R.get("openai").generate(ImageRequest(model="gpt-image-2.5-flare-2026-09-08", prompt="A teal gem", size="1024x1024", quality="low",
                                                    background="opaque", n=2, nonce="x"), CallCtx.null())
        return img.raw_sha256

    assert run() == run()


# ----- hygiene ----------------------------------------------------------------------------------------------------------------------

def test_importing_the_package_does_not_import_sdks_or_adapters():
    code = ("import sys; import duoskin.providers; import duoskin.providers.registry; "
            "bad = [m for m in ('anthropic', 'openai', 'trimesh', 'duoskin.providers.anthropic_llm', 'duoskin.providers.mock.llm', 'duoskin.providers.tripo') if m in sys.modules]; "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_source_never_reads_keys_into_logs_or_prints():
    root = Path(M.__file__).resolve().parent.parent
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "print(" not in text, f"print() in {py.name}"
        assert "logging" not in text or "api_key" not in text.split("logging", 1)[1][:200], py.name
