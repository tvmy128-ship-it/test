"""Fabric tiles, fold overlays and their library admission checks (CLO-04, CLO-13, CLO-14)."""
from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from duoskin.imaging import compositor as C
from duoskin.imaging import fabric as F
from duoskin.imaging import folds as FO
from duoskin.imaging.palette import srgb_to_lab
from duoskin.roblox import template as T
from duoskin.roblox import validators as V


def test_ten_builtin_fabrics_are_deterministic_seamless_and_admitted():
    ids = F.builtin_fabric_ids()
    assert len(ids) >= 10 and {"denim_classic", "jersey_plain", "nylon_smooth", "wool_felt", "canvas_plain", "leather_pebble"} <= set(ids)
    shas = set()
    for fid in ids:
        a = F.generate_tile(F.builtin_fabric_info(fid)["kind"], 256, F.builtin_fabric_info(fid)["seed"], F.builtin_fabric_info(fid)["repeats"])
        ft = F.procedural_fabric(fid)
        assert np.array_equal(a, ft.tile) and ft.tile.dtype == np.uint8 and ft.tile.shape == (256, 256)
        assert abs(float(ft.tile.mean()) - 128) < 3 and ft.amplitude_dL <= F.MAX_AMPLITUDE_DL
        res = F.check_fabric_tile(ft.tile, fid)
        assert res and all(r.passed and r.kind == "hard" for r in res), [(r.metric, r.value) for r in res if not r.passed]
        shas.add(ft.sha256)
    assert len(shas) == len(ids)                                           # every tile is distinct
    with pytest.raises(KeyError):
        F.procedural_fabric("nope")


def test_generators_validate_their_arguments_and_seeds_change_the_tile():
    a = F.generate_tile("wool", 128, 1, 8)
    b = F.generate_tile("wool", 128, 2, 8)
    assert not np.array_equal(a, b) and np.array_equal(a, F.generate_tile("wool", 128, 1, 8))
    with pytest.raises(ValueError):
        F.generate_tile("plaid", 128, 1, 8)
    with pytest.raises(ValueError):
        F.generate_tile("wool", 100, 1, 8)


def test_admission_checks_reject_seams_moire_baked_lighting_and_colour():
    good = F.procedural_fabric("twill_fine").tile
    assert all(r.passed for r in F.check_fabric_tile(good))
    # a non-periodic tile: a diagonal ramp has a hard seam after the 50% roll
    ramp = np.tile(np.linspace(40, 215, 256), (256, 1)).astype(np.uint8)
    r = {x.metric: x for x in F.check_fabric_tile(ramp)}
    assert not r["seam_energy_ratio"].passed or not r["block_lum_std"].passed
    # moire: a checkerboard at the pixel pitch is all energy above 0.35 cycles/px
    yy, xx = np.mgrid[0:256, 0:256]
    checker = (128 + 100 * (((xx + yy) % 2) * 2 - 1)).astype(np.uint8)
    assert not {x.metric: x for x in F.check_fabric_tile(checker)}["alias_energy_fraction"].passed
    # baked lighting: a vignette makes the 64-px block means differ
    vign = np.clip(good.astype(float) - 70 * ((xx - 128) ** 2 + (yy - 128) ** 2) / 128 ** 2, 0, 255).astype(np.uint8)
    assert not {x.metric: x for x in F.check_fabric_tile(vign)}["block_lum_std"].passed
    # colour: chroma must stay below fabric.chroma_max
    rgb = np.dstack([good, good, np.clip(good.astype(int) + 60, 0, 255).astype(np.uint8)])
    assert not {x.metric: x for x in F.check_fabric_tile(rgb)}["chroma_max"].passed
    assert F.max_chroma(good) == 0.0


def test_kit_tile_loading_applies_admission_and_user_tile_wins(tmp_path):
    good = F.procedural_fabric("canvas_plain").tile
    d = tmp_path / "myweave"
    d.mkdir()
    Image.fromarray(good).save(d / "tile.png")
    (d / "fabric.json").write_text(json.dumps({"material_family": "canvas", "prompt_phrase": "my weave", "px_per_repeat": 5, "repeats": 12,
                                               "amplitude_dL": 9}), encoding="utf-8")
    ft = F.load_fabric_kit_dir(d)
    assert ft.origin == "library" and ft.fabric_id == "myweave" and ft.amplitude_dL == F.MAX_AMPLITUDE_DL   # amplitude clamped to 6 L*
    assert F.resolve_fabric("myweave", [tmp_path]).origin == "library"
    bad = tmp_path / "bad"
    bad.mkdir()
    yy, xx = np.mgrid[0:256, 0:256]
    Image.fromarray((128 + 100 * (((xx + yy) % 2) * 2 - 1)).astype(np.uint8)).save(bad / "tile.png")
    (bad / "fabric.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        F.load_fabric_kit_dir(bad)


def test_resolve_fabric_falls_back_without_a_kit():
    assert F.resolve_fabric(None).fabric_id == "jersey_plain"
    assert F.resolve_fabric("Denim").fabric_id in F.builtin_fabric_ids()
    assert F.resolve_fabric("zzz").fabric_id == "jersey_plain"
    assert F.flat_fabric().amplitude_dL == 0.0


def test_fabric_amplitude_never_exceeds_six_L_star():
    for fid in F.builtin_fabric_ids():
        ft = F.procedural_fabric(fid)
        lut = C._fabric_lut((120, 140, 200), ft.amplitude_dL)
        lab = srgb_to_lab(lut.astype(float))
        assert lab[:, 0].max() - lab[:, 0].min() <= 6.2, fid
    with pytest.raises(ValueError):
        F.FabricTile("x", "jersey", np.full((64, 64), 128, np.uint8), 4.0, 4, 7.0)


@pytest.mark.parametrize("fid", F.builtin_fabric_ids())
def test_base_fabric_layer_is_continuous_across_every_seam(fid):
    """CLO-04: the fabric is one continuous strip per part; mean dE <= 6 and max <= 15 over all 36 seams."""
    ft = F.procedural_fabric(fid)
    layer = C.base_fabric_layer(ft, (90, 120, 190))
    stats = V.seam_stats(layer)
    assert len(stats) == 36
    assert max(s["mean"] for s in stats) <= 6 and max(s["max"] for s in stats) <= 15, max(stats, key=lambda s: s["max"])
    res = V.check_b04(layer)
    assert res.passed and res.check_id == "CHK-B04"


def test_side_strip_wrap_closes_exactly():
    ft = F.procedural_fabric("jersey_plain")
    for part, first, last in (("torso", "torso_r", "torso_b"), ("rlimb", "rlimb_l", "rlimb_f")):
        a = F.fabric_values(ft, first, 4)[:, :1]
        b = F.fabric_values(ft, last, 4)[:, -1:]
        assert float(np.abs(a - b).mean()) < 0.35          # neighbouring sub-pixels of a periodic pattern
    # and the strip has the exact whole number of tiles
    _off, length = T.strip_layout("torso")
    assert abs(F._part_scale(ft, "torso") * length / ft.size - round(length / ft.template_px)) < 1e-9


# ---------------------------------------------------------------------------------------------------------------- folds
@pytest.mark.parametrize("set_id", FO.procedural_set_ids())
def test_procedural_fold_sets_pass_admission_and_keep_a_flat_edge_band(set_id):
    fs = FO.procedural_folds(set_id)
    assert fs.origin == "procedural" and fs.label == "procedural folds"
    assert fs.panels and "torso_f" in fs.panels and "torso_u" not in fs.panels
    for region, p in fs.panels.items():
        w, h = T.SIZE[region]
        assert p.shape == (h * 4, w * 4) and p.dtype == np.uint8
        assert all(r.passed for r in FO.check_fold_panel(p, region=region)), (set_id, region)
        band = FO.edge_band_mask(p.shape, 0.08)
        assert (p[band] == 128).all()                                  # outer 8% exactly neutral: panels join without a seam


def test_procedural_folds_are_pure_and_default_for_unknown_names():
    assert FO.procedural_folds("tee_soft").sha256 == FO.procedural_folds("tee_soft").sha256
    assert FO.procedural_folds("tee_soft").sha256 != FO.procedural_folds("hoodie_heavy").sha256
    assert FO.procedural_folds("no_such_set").set_id == "default"
    assert FO.resolve_folds(None).set_id == "default" and FO.resolve_folds("denim_folds").set_id == "denim_folds"
    assert FO.flat_folds().delta("torso_f", (4, 4)).sum() == 0


def test_fold_delta_resizes_and_is_signed():
    fs = FO.procedural_folds("hoodie_heavy")
    d = fs.delta("torso_f", (512, 512))
    assert d.dtype == np.float32 and d.min() < -0.02 and d.max() > 0.005 and abs(float(d.mean())) < 0.01
    small = fs.delta("torso_f", (128, 128))
    assert small.shape == (128, 128) and float(np.abs(small).max()) > 0.01


def test_fold_admission_rejects_off_grey_noisy_edges_colour_and_bad_outline():
    flat = np.full((256, 256), 128, np.uint8)
    assert all(r.passed for r in FO.check_fold_panel(flat))
    assert not {r.metric: r for r in FO.check_fold_panel(np.full((256, 256), 160, np.uint8))}["mean_grey"].passed
    noisy = flat.copy()
    noisy[:10] = np.random.default_rng(0).integers(60, 200, (10, 256))
    assert not {r.metric: r for r in FO.check_fold_panel(noisy)}["edge_band_std"].passed
    rgb = np.dstack([flat, flat, np.full_like(flat, 190)])
    assert not {r.metric: r for r in FO.check_fold_panel(rgb)}["chroma_max"].passed
    # outline: white outside the recipe mask, mid-grey inside; IoU against the mask
    outline = np.zeros((256, 256), bool)
    outline[:, :128] = True
    panel = np.full((256, 256), 255, np.uint8)
    panel[:, :128] = 128
    assert {r.metric: r for r in FO.check_fold_panel(panel, outline=outline)}["outline_iou"].passed
    wrong = np.full((256, 256), 255, np.uint8)
    wrong[:, 64:192] = 128
    assert not {r.metric: r for r in FO.check_fold_panel(wrong, outline=outline)}["outline_iou"].passed


def test_library_fold_set_loading(tmp_path):
    d = tmp_path / "my_tee"
    d.mkdir()
    (d / "fold.json").write_text(json.dumps({"set_id": "my_tee"}), encoding="utf-8")
    p = np.full((128, 128), 128, np.uint8)
    p[40:90, 60:64] = 100
    Image.fromarray(p).save(d / "torso_f.png")                      # stored at 1x: stretched to 4x on load
    fs = FO.load_fold_set(d)
    assert fs.origin == "library" and fs.panels["torso_f"].shape == (512, 512) and FO.resolve_folds("my_tee", [tmp_path]).origin == "library"
    Image.fromarray(np.full((128, 128), 190, np.uint8)).save(d / "torso_b.png")
    with pytest.raises(ValueError):
        FO.load_fold_set(d)
