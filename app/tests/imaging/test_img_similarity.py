"""imaging/similarity.py: pHash/dHash, registry (exact forever + sliding window), reference leakage, clone band (degraded + DreamSim)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.checks import thresholds as TH
from duoskin.imaging import files as F
from duoskin.imaging import similarity as S


def pattern(seed: int, size: int = 128) -> Image.Image:
    """A distinctive random-blob picture: different seeds give pictures that are far apart in pHash."""
    rng = np.random.RandomState(seed)
    im = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x, y = rng.randint(0, size - 30, 2)
        w, h = rng.randint(15, 50, 2)
        d.ellipse([x, y, x + w, y + h], fill=tuple(int(c) for c in rng.randint(0, 255, 3)) + (255,))
    return im


def slightly_changed(im: Image.Image) -> Image.Image:
    arr = np.array(im)
    arr[0:3, 0:3] = (10, 10, 10, 255)
    return Image.fromarray(arr, "RGBA")


def row_of(im: Image.Image, kind="face_canvas", seq=1, listed=False, asset_id="x", embedding=None) -> S.RegistryRow:
    fp = S.registry_fingerprint(im)
    return S.RegistryRow(kind, fp["pixel_sha"], fp["phash"], seq, listed, embedding, asset_id)


# ---------------------------------------------------------------- hashes
def test_hashes_are_64_bit_ints_deterministic_and_discriminating():
    a, b = pattern(1), pattern(2)
    assert S.phash(a) == S.phash(a) and 0 <= S.phash(a) < 2 ** 64 and 0 <= S.dhash(a) < 2 ** 64
    assert S.hamming(S.phash(a), S.phash(a)) == 0
    assert S.hamming(S.phash(a), S.phash(slightly_changed(a))) <= 6
    assert S.hamming(S.phash(a), S.phash(b)) > 8
    assert S.hamming(0b1011, 0b0001) == 2 and len(S.hash_hex(5)) == 16


def test_hash_composites_transparent_assets_on_white_and_can_crop_to_subject():
    cut = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    ImageDraw.Draw(cut).ellipse([20, 20, 100, 100], fill=(200, 30, 30, 255))
    on_white = Image.new("RGBA", (128, 128), (255, 255, 255, 255))
    on_white.alpha_composite(cut)
    assert S.phash(cut) == S.phash(on_white)
    shifted = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    shifted.alpha_composite(cut.crop((20, 20, 101, 101)), (40, 30))
    assert S.hamming(S.phash(cut, crop_to_subject=True), S.phash(shifted, crop_to_subject=True)) <= 2


# ---------------------------------------------------------------- registry
def test_exact_reuse_is_blocked_forever_for_every_kind():
    img = pattern(3)
    old = row_of(img, kind="face_part", seq=-500)                       # 500 duos ago: far outside the window
    r = S.registry_check("face_part", img, [old], current_duo_seq=0)
    assert not r.passed and r.check_id == "A_REGISTRY" and r.kind == "hard" and "exact reuse" in r.evidence and "forever" in r.evidence
    assert S.registry_check("face_part", pattern(4), [old]).passed
    other_kind = row_of(img, kind="print")
    assert S.registry_check("face_part", img, [other_kind]).passed       # a registry of another kind does not count


def test_exact_match_ignores_metadata_and_invisible_rgb():
    img = pattern(5)
    arr = np.array(img)
    arr[0, 0] = (1, 2, 3, 0)
    same = Image.fromarray(arr, "RGBA")
    ph = S.registry_fingerprint(img)
    row = S.RegistryRow("print", ph["pixel_sha"], ph["phash"], 0)
    assert not S.registry_check("print", same, [row]).passed or F.pixel_sha(same) != F.pixel_sha(img)


def test_near_duplicates_only_for_face_canvas_and_whole_print_and_only_in_the_window():
    img = pattern(6)
    near = slightly_changed(img)
    in_window = row_of(img, "face_canvas", seq=10)
    r = S.registry_check("face_canvas", near, [in_window], current_duo_seq=20)
    assert not r.passed and "near-duplicate" in r.evidence and "mode=degraded" in r.evidence and r.value is not None and r.value <= 8
    too_old = row_of(img, "face_canvas", seq=-40)                        # more than 30 duos back
    assert S.registry_check("face_canvas", near, [too_old], current_duo_seq=0).passed
    listed = row_of(img, "face_canvas", seq=-400, listed=True)           # listed items stay in the near-duplicate set
    assert not S.registry_check("face_canvas", near, [listed], current_duo_seq=0).passed
    assert not S.registry_check("print", near, [row_of(img, "print", seq=1)], current_duo_seq=2).passed
    part_only = row_of(img, "face_part", seq=1)                          # per-part registries are exact-only
    assert S.registry_check("face_part", near, [part_only], current_duo_seq=2).passed
    assert S.registry_check("face_canvas", pattern(7), [in_window], current_duo_seq=20).passed
    window_edge = row_of(img, "face_canvas", seq=-10)                    # exactly 30 duos back: seq > current - window is False
    assert S.registry_check("face_canvas", near, [window_edge], current_duo_seq=20).passed
    assert not S.registry_check("face_canvas", near, [window_edge], current_duo_seq=19).passed


def test_registry_fails_closed_without_rows_and_passes_an_empty_registry():
    r = S.registry_check("print", pattern(1), None)
    assert not r.ran and not r.passed and r.status == "not_run"
    assert S.registry_check("print", pattern(1), []).passed


class FakeEmbeddingModel(S.DreamSimModel):
    """An embedding-style 'DreamSim' for tests: embeds an image as its coarse colour histogram (no onnxruntime)."""

    def __init__(self):
        self.kind = "embedding"

    def embed(self, im):                                                       # type: ignore[override]
        a = np.asarray(im.convert("RGB").resize((8, 8), Image.Resampling.BOX), dtype=float).reshape(-1)
        return a - a.mean()

    def distance(self, a, b):                                                  # type: ignore[override]
        return self.cosine_distance(self.embed(a), self.embed(b))


def test_dreamsim_branch_uses_embeddings_and_the_or_rule():
    m = FakeEmbeddingModel()
    img = pattern(8)
    fp = S.registry_fingerprint(img, m)
    assert "embedding" in fp and len(fp["embedding"]) == 192
    base = S.RegistryRow("face_canvas", "z" * 64, phash=S.phash(pattern(9)), duo_seq=1, embedding=fp["embedding"])
    # pHash is far (different picture hash stored) but the embedding is identical: DreamSim < 0.15 blocks (the OR rule)
    r = S.registry_check("face_canvas", img, [base], current_duo_seq=2, model=m)
    assert not r.passed and "dreamsim" in r.evidence and "mode=dreamsim" in r.evidence
    far = S.RegistryRow("face_canvas", "y" * 64, phash=S.phash(pattern(9)), duo_seq=1, embedding=S.registry_fingerprint(pattern(10), m)["embedding"])
    assert S.registry_check("face_canvas", img, [far], current_duo_seq=2, model=m).passed
    no_emb = S.RegistryRow("face_canvas", "w" * 64, phash=S.phash(pattern(9)), duo_seq=1)
    assert S.registry_check("face_canvas", img, [no_emb], current_duo_seq=2, model=m).passed          # that row falls back to pHash only


def test_pair_style_model_cannot_compare_against_stored_rows():
    class Pair(S.DreamSimModel):
        def __init__(self):
            self.kind = "pair"

    row = S.RegistryRow("face_canvas", "q" * 64, phash=5, duo_seq=1, embedding=[1.0])
    r = S.registry_check("face_canvas", pattern(1), [row], current_duo_seq=2, model=Pair())
    assert not r.ran and not r.passed


# ---------------------------------------------------------------- reimagine dedupe (SOFT)
def test_dedupe_against_rejected_drafts_is_soft():
    a = pattern(11)
    r = S.dedupe_check(slightly_changed(a), [S.phash(a)])
    assert not r.passed and r.kind == "soft" and r.check_id == "A_PHASH" and r.value <= TH.get("img.reimagine_phash_max")
    assert S.dedupe_check(pattern(12), [a, pattern(13)]).passed
    assert S.dedupe_check(pattern(12), []).passed


# ---------------------------------------------------------------- reference leakage (IMG-15)
def test_reference_leakage_blocks_copies_but_exempts_the_own_concept_crop_and_gated_user_refs():
    ref = pattern(14)
    refs = {"house_face": ref, "own_crop": pattern(15), "user_mood": pattern(16)}
    clean = pattern(17)
    assert S.check_reference_leakage([clean], refs).passed
    leak = S.check_reference_leakage([slightly_changed(ref)], refs)
    assert not leak.passed and leak.check_id == "A_REFLEAK" and "house_face" in leak.evidence and "mode=degraded" in leak.evidence
    own = S.check_reference_leakage([pattern(15)], refs, own_keys=["own_crop"])
    assert own.passed                                                       # copying the asset's own concept crop is the point
    user_copy = [pattern(16)]
    assert S.check_reference_leakage(user_copy, refs, user_reference_keys=["user_mood"], toggle_on=False).passed      # toggle OFF: not compared
    assert not S.check_reference_leakage(user_copy, refs, user_reference_keys=["user_mood"], toggle_on=True).passed


def test_reference_leakage_with_dreamsim_uses_the_or_rule():
    m = FakeEmbeddingModel()
    ref = pattern(18)
    r = S.check_reference_leakage([ref.copy()], {"sheet": ref}, model=m)
    assert not r.passed and "mode=dreamsim" in r.evidence


# ---------------------------------------------------------------- spec distance
def _char(**kw):
    base = {"dna": {"shape_language": "round_soft", "colour_plan": "block_50_50", "focal_location": "chest"},
            "hair": {"kit_style_id": "hair_a", "fringe_id": "kit_default", "back_id": "kit_default"},
            "face": {"eye_shape": "round", "iris_style": "oval_solid", "highlight_style": "dual_dot", "lash_style": "clean_line",
                     "brow_style": "thin_arched", "mouth_style": "smile_line", "cheek_mark": "none"},
            "top": {"recipe_id": "hoodie", "sleeve": "long", "hem": "hip_untucked", "neckline": "hood"},
            "bottom": {"recipe_id": "jeans", "leg": "full"}}
    for path, v in kw.items():
        cur = base
        *head, last = path.split("__")
        for p in head:
            cur = cur[p]
        cur[last] = v
    return base


def test_spec_distance_counts_differing_fields_and_normalises_text():
    a = _char()
    assert S.spec_distance(a, _char()) == 0.0
    b = _char(dna__shape_language="sharp_angular", face__eye_shape="narrow", top__recipe_id="tee", hair__kit_style_id="Hair_A ")
    compared = len([f for f in S.PLN03_FIELDS if S._get(a, f) is not None])
    assert S.spec_distance(a, b) == pytest.approx(3 / compared)         # hair id differs only by case and spaces: same
    assert len(S.PLN03_FIELDS) == 22
    assert S.spec_distance({}, {}) == 0.0
    assert S.spec_distance({"dna": {"energy": "calm"}}, {}) == 1.0     # present in one only: different


# ---------------------------------------------------------------- clone band
def figure(shirt, pants, hair=(60, 40, 30), size=96, shape="box"):
    im = Image.new("RGB", (size, size), (242, 242, 242))
    d = ImageDraw.Draw(im)
    d.rectangle([34, 8, 62, 30], fill=(227, 176, 142))
    d.rectangle([30, 4, 66, 14], fill=hair)
    d.rectangle([22, 30, 74, 62], fill=shirt)
    if shape == "wide":
        d.rectangle([4, 30, 92, 40], fill=shirt)
    d.rectangle([30, 62, 66, 92], fill=pants)
    return im


def views(shirt, pants, **kw):
    return {s: figure(shirt, pants, **kw) for s in S.SIDES}


def test_degraded_clone_fails_only_when_all_three_metrics_trip():
    a = views((31, 138, 138), (34, 51, 92))
    twin = views((31, 138, 138), (34, 51, 92))
    r = S.check_clone_band(a, twin, spec_distance_value=0.1, bg_hex="#f2f2f2")
    assert not r.passed and r.check_id == "CHK-D02" and r.kind == "hard" and "mode=degraded" in r.evidence and "clone check degraded" in r.evidence
    assert "tripped: phash,palette_overlap,spec_distance" in r.evidence
    # one trip: an obvious spec difference alone rescues the pair (a warning, never a block)
    r1 = S.check_clone_band(a, twin, spec_distance_value=0.9, bg_hex="#f2f2f2")
    assert r1.passed and "tripped: phash,palette_overlap" in r1.evidence and "degraded clone metric tripped" in r1.evidence
    # different colours: palette overlap does not trip; same silhouettes: pHash and spec trip -> two trips, pass
    diff = views((242, 115, 94), (244, 233, 210))
    r2 = S.check_clone_band(a, diff, spec_distance_value=0.1, bg_hex="#f2f2f2")
    assert r2.passed and "palette_overlap" not in r2.evidence.split("tripped:")[1].split(";")[0]
    # a different build and colours: no trip
    far = views((242, 115, 94), (244, 233, 210), shape="wide")
    assert S.check_clone_band(a, far, spec_distance_value=0.9, bg_hex="#f2f2f2").passed


def test_degraded_mode_metrics_and_thresholds():
    a, twin = views((31, 138, 138), (34, 51, 92)), views((31, 138, 138), (34, 51, 92))
    res = S.clone_band_metrics(a, twin, spec_distance_value=0.2, bg_hex="#f2f2f2")
    assert res.mode == "degraded" and not res.passed and res.label == "clone check degraded"
    assert res.metrics["phash_mean"] == 0 <= TH.get("duo.degraded_phash_clone_max")
    assert res.metrics["palette_overlap"] > TH.get("duo.degraded_palette_overlap_max") and res.metrics["spec_distance"] < TH.get("duo.degraded_spec_dist_min")
    assert set(res.per_side) == set(S.SIDES) and res.trips == list(S.DEGRADED_METRICS)


def test_degraded_clone_check_needs_the_spec_distance_and_matching_sides():
    a = views((31, 138, 138), (34, 51, 92))
    r = S.check_clone_band(a, a, bg_hex="#f2f2f2")
    assert not r.ran and "spec distance" in r.evidence
    assert not S.check_clone_band({"front": a["front"]}, {"back": a["back"]}, spec_distance_value=0.5).ran


def test_dreamsim_clone_band_edges_by_stage_and_soft_concept_stage():
    m = FakeEmbeddingModel()
    a = views((31, 138, 138), (34, 51, 92))
    twin = views((31, 138, 138), (34, 51, 92))
    r = S.check_clone_band(a, twin, model=m)
    assert not r.passed and r.kind == "hard" and "mode=dreamsim" in r.evidence and r.threshold.startswith(">= 0.3")
    different = views((242, 115, 94), (244, 233, 210))
    assert S.check_clone_band(a, different, model=m).passed
    concept = S.check_clone_band(a, twin, model=m, stage="concept")
    assert not concept.passed and concept.check_id == "CHK-G1-11" and concept.kind == "soft" and "con.clone_proxy_dreamsim_min" in concept.threshold
    no_model = S.check_clone_band(a, twin, stage="concept")
    assert no_model.passed and no_model.check_id == "CHK-G1-11" and "not shown while DreamSim is absent" in no_model.evidence


# ---------------------------------------------------------------- DreamSim wrapper without onnxruntime
class FakeSession:
    def __init__(self, n_inputs, fn):
        self._n, self._fn = n_inputs, fn

    def get_inputs(self):
        return [type("I", (), {"name": f"in{i}"}) for i in range(self._n)]

    def run(self, _outputs, feeds):
        return [self._fn(*feeds.values())]


def test_dreamsim_model_wraps_pair_and_embedding_exports_and_prepares_inputs():
    seen = {}

    def pair_fn(x0, x1):
        seen["shape"], seen["dtype"], seen["max"] = x0.shape, x0.dtype, float(x0.max())
        return np.array([float(np.abs(x0 - x1).mean())])

    pair = S.DreamSimModel(FakeSession(2, pair_fn))
    assert pair.kind == "pair"
    d = pair.distance(pattern(1), pattern(2))
    assert d > 0 and seen["shape"] == (1, 3, 224, 224) and seen["dtype"] == np.float32 and seen["max"] <= 1.0
    assert pair.distance(pattern(1), pattern(1)) == 0.0
    with pytest.raises(S.DreamSimUnavailable):
        pair.embed(pattern(1))
    emb = S.DreamSimModel(FakeSession(1, lambda x: x.reshape(1, -1)[:, :64]))
    assert emb.kind == "embedding" and emb.embed(pattern(1)).shape == (64,)
    assert emb.distance(pattern(1), pattern(1)) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(S.DreamSimUnavailable):
        S.DreamSimModel(FakeSession(3, lambda *a: a[0]))
    assert S.DreamSimModel.cosine_distance(np.zeros(3), np.ones(3)) == 1.0


def test_load_dreamsim_never_raises_and_doctor_probe_is_soft(tmp_path):
    assert S.load_dreamsim(None) is None and S.load_dreamsim(tmp_path / "missing.onnx") is None
    (tmp_path / "bad.onnx").write_bytes(b"not a model")
    assert S.load_dreamsim(tmp_path / "bad.onnx") is None
    r = S.verify_fixture(None, pattern(1), pattern(2), 0.5)
    assert r.check_id == "CHK-S14" and r.kind == "soft" and not r.passed and "degraded" in r.evidence
    m = S.DreamSimModel(FakeSession(2, lambda a, b: np.array([0.42])))
    assert S.verify_fixture(m, pattern(1), pattern(2), 0.425).passed
    assert not S.verify_fixture(m, pattern(1), pattern(2), 0.5).passed
    boom = S.DreamSimModel(FakeSession(2, lambda a, b: 1 / 0))
    assert not S.verify_fixture(boom, pattern(1), pattern(2), 0.5).ran


# ---------------------------------------------------------------- cross-duo memory
def test_memory_vectors_and_nearest_past_duo():
    v1 = S.memory_vector(views((31, 138, 138), (34, 51, 92)))
    v2 = S.memory_vector(views((31, 138, 138), (34, 51, 92)))
    v3 = S.memory_vector(views((242, 115, 94), (244, 233, 210), shape="wide"))
    assert v1["mode"] == "degraded" and S.memory_distance(v1, v2) == 0.0 and S.memory_distance(v1, v3) > 0.05
    nearest = S.nearest_past(v1, [("duo_far", v3), ("duo_twin", v2)])
    assert nearest == ("duo_twin", 0.0)
    m = FakeEmbeddingModel()
    emb = S.memory_vector(views((31, 138, 138), (34, 51, 92)), m)
    assert emb["mode"] == "dreamsim" and S.memory_distance(emb, v1) is None          # modes are never mixed
    assert S.nearest_past(emb, [("d", v1)]) is None
    assert S.memory_distance({"mode": "degraded", "sides": ["front"], "phash": [1]}, {"mode": "degraded", "sides": ["back"], "phash": [1]}) is None
