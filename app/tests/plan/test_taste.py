"""checks/taste.py: SOFT warnings only, pure functions over supplied pixels and masks (APP_SPEC §3.4, §3.6)."""
from __future__ import annotations

import numpy as np
import pytest

from duoskin.checks import policy, taste

H, W = 600, 300
SKIN, SHIRT, PANTS, HAIR = 0, 1, 2, 3


def figure(shirt=(214, 130, 43), pants=(43, 43, 51), hair=(138, 75, 43)):
    """A flat figure: head with hair and skin, a shirt block, a pants block. Returns (render, labels)."""
    img = np.full((H, W, 3), 250, np.uint8)
    lab = np.full((H, W), SKIN, np.int32)
    img[0:150, 90:210] = (240, 200, 170)
    img[0:60, 90:210] = hair
    lab[0:60, 90:210] = HAIR
    img[150:400, 60:240] = shirt
    lab[150:400, 60:240] = SHIRT
    img[400:H, 90:210] = pants
    lab[400:H, 90:210] = PANTS
    return img, lab


@pytest.mark.parametrize("fn", ["phone_top_colours", "main_colour_contrast", "silhouette_overlap", "accessory_phone_size",
                                "garment_layout_similarity", "colour_plan_ratio"])
def test_every_taste_check_is_soft_in_the_registry(fn):
    ids = {"phone_top_colours": "DUO-03", "main_colour_contrast": "DUO-03", "silhouette_overlap": "DUO-04", "accessory_phone_size": "DUO-04",
           "garment_layout_similarity": "TASTE_LAYOUT", "colour_plan_ratio": "TASTE_RATIO"}
    assert policy.meta(ids[fn]).kind == "soft"


# ---------------------------------------------------------------------------------------------- DUO-03
def test_planned_main_colour_among_the_top_colours_passes():
    img, lab = figure()
    r = taste.phone_top_colours(img, lab, [SHIRT, PANTS, HAIR], "#D6822B")
    assert r.passed and r.kind == "soft" and r.check_id == "DUO-03" and r.ran
    assert r.value is not None and r.value < 5
    assert "top colours at phone size" in r.evidence


def test_a_planned_main_that_is_not_on_screen_warns_and_never_blocks():
    img, lab = figure()
    r = taste.phone_top_colours(img, lab, [SHIRT, PANTS, HAIR], "#1F8A8A")
    assert not r.passed and r.kind == "soft" and not policy.is_blocking(r)


def test_skin_is_not_a_clothes_colour():
    img, lab = figure()
    tops_all = taste.top_colours(img, lab, [SHIRT, PANTS, HAIR])
    assert [h.lower() for h, _ in tops_all[:3]] == ["#d6822b", "#2b2b33", "#8a4b2b"]
    assert not any(abs(int(h[1:3], 16) - 240) < 3 and abs(int(h[3:5], 16) - 200) < 3 for h, _ in tops_all)       # the skin tone is absent
    tops = taste.top_colours(img, lab, [SHIRT])
    assert len(tops) >= 1 and tops[0][1] > 0.9


def test_phone_view_is_the_registry_height():
    img, _ = figure()
    small = taste.area_downscale(img, 150)
    assert small.shape[0] == 150 and small.shape[1] == 75


def test_mismatched_label_map_fails_closed_with_ran_false():
    img, lab = figure()
    r = taste.phone_top_colours(img, lab[:-1], [SHIRT], "#D6822B")
    assert not r.passed and not r.ran and r.kind == "soft" and not policy.is_blocking(r)


def test_empty_clothes_selection_fails_closed():
    img, lab = figure()
    r = taste.phone_top_colours(img, lab, [99], "#D6822B")
    assert not r.ran and not r.passed


def test_a_render_with_the_wrong_shape_fails_closed():
    img, lab = figure()
    r = taste.phone_top_colours(img[..., 0], lab, [SHIRT], "#D6822B")
    assert not r.ran


def test_main_colour_contrast_only_under_structures_that_want_different_mains():
    far = taste.main_colour_contrast(["#D6822B"], ["#2F6B4F"], "complement")
    assert far.passed and far.value >= 15
    near = taste.main_colour_contrast(["#D6822B"], ["#D88A33"], "complement")
    assert not near.passed and near.kind == "soft"
    club = taste.main_colour_contrast(["#D6822B"], ["#D88A33"], "same_club")
    assert club.passed and "no different-mains rule" in club.evidence
    assert taste.main_colour_contrast([], ["#D88A33"], "complement").passed
    assert not taste.main_colour_contrast(["#D6822B"], ["#D88A33"], "triplets").ran


# ---------------------------------------------------------------------------------------------- DUO-04
def _box(y0, y1, x0, x1):
    m = np.zeros((H, W), bool)
    m[y0:y1, x0:x1] = True
    return m


def test_silhouette_overlap_warns_above_the_cold_start_value():
    a, b = _box(0, 100, 0, 100), _box(0, 100, 0, 100)
    r = taste.silhouette_overlap(a, b)
    assert not r.passed and r.value == 1.0 and r.kind == "soft" and "duo.silhouette_iou_cold" in r.threshold
    far = taste.silhouette_overlap(a, _box(200, 300, 150, 250))
    assert far.passed and far.value == 0.0


def test_a_caller_percentile_replaces_the_cold_start():
    a, b = _box(0, 100, 0, 100), _box(0, 100, 0, 60)
    assert taste.silhouette_overlap(a, b).passed
    r = taste.silhouette_overlap(a, b, threshold=0.5)
    assert not r.passed and "caller's percentile" in r.threshold


def test_empty_masks_do_not_warn_and_mismatched_masks_fail_closed():
    z = np.zeros((H, W), bool)
    assert taste.silhouette_overlap(z, z).passed
    assert not taste.silhouette_overlap(z, z[:-1]).ran


def test_iou_is_a_difference_measure_not_a_target():
    a = _box(0, 100, 0, 100)
    assert taste.mask_iou(a, a) == 1.0 and taste.mask_iou(a, ~a) == 0.0


def test_tiny_accessories_at_phone_size_warn():
    masks = {"a.acc.0": _box(0, 120, 0, 120), "b.acc.0": _box(0, 24, 0, 24)}
    r = taste.accessory_phone_size(masks, H)
    assert not r.passed and "b.acc.0" in r.evidence and "a.acc.0" not in r.evidence and r.kind == "soft"
    ok = taste.accessory_phone_size({"a.acc.0": _box(0, 120, 0, 120)}, H)
    assert ok.passed and ok.value == pytest.approx(30.0)
    assert taste.accessory_phone_size({}, H).passed


def test_accessory_phone_size_fails_closed_on_a_bad_height():
    assert not taste.accessory_phone_size({"x": _box(0, 10, 0, 10)}, 0).ran


# ---------------------------------------------------------------------------------------------- TASTE_LAYOUT
def test_adjusted_rand_index_ignores_label_names():
    x = np.array([[0, 0, 1, 1], [0, 0, 1, 1]])
    y = np.array([[5, 5, 9, 9], [5, 5, 9, 9]])
    assert taste.adjusted_rand_index(x, y) == pytest.approx(1.0)
    z = np.array([[0, 1, 0, 1], [1, 0, 1, 0]])
    assert taste.adjusted_rand_index(x, z) < 0.2


def test_full_size_label_maps_do_not_overflow():
    big = np.zeros((1024, 1024), np.int32)
    big[:, 512:] = 1
    assert taste.adjusted_rand_index(big, big) == pytest.approx(1.0)
    assert -1.0 <= taste.adjusted_rand_index(big, big.T) <= 1.0


def test_same_colour_block_layout_warns_and_different_layouts_pass():
    a = np.zeros((H, W), np.int32)
    a[:, W // 2:] = 1
    renamed = np.where(a == 0, 7, 8)
    r = taste.garment_layout_similarity(a, renamed)
    assert not r.passed and r.value == pytest.approx(1.0) and r.kind == "soft" and r.check_id == "TASTE_LAYOUT"
    stripes = np.zeros((H, W), np.int32)
    stripes[H // 2:, :] = 1
    assert taste.garment_layout_similarity(a, stripes).passed


def test_ignored_labels_are_left_out_of_the_layout_comparison():
    a = np.zeros((H, W), np.int32)
    a[:, W // 2:] = 1
    b = a.copy()
    b[: H // 3] = 9
    with_ignore = taste.garment_layout_similarity(a, b, ignore=[9])
    assert with_ignore.value == pytest.approx(1.0)


def test_layout_mismatch_fails_closed():
    assert not taste.garment_layout_similarity(np.zeros((4, 4), int), np.zeros((4, 5), int)).ran


# ---------------------------------------------------------------------------------------------- TASTE_RATIO
@pytest.mark.parametrize("plan", ["ratio_60_30_10", "ratio_70_20_10", "block_50_50", "mono_accent"])
def test_a_built_ratio_close_to_the_plan_passes(plan):
    from duoskin.prompts.catalog import data_json

    target = data_json("rules.json")["plan"]["colour_plan_ratios"][plan]
    counts = [int(t * 10000) for t in target]
    r = taste.colour_plan_ratio(counts, plan)
    assert r.passed and r.kind == "soft" and r.check_id == "TASTE_RATIO"


def test_a_built_ratio_far_from_the_plan_warns_only():
    r = taste.colour_plan_ratio([5000, 5000, 5000], "ratio_60_30_10")
    assert not r.passed and r.kind == "soft" and not policy.is_blocking(r) and r.value > 15


def test_allover_pattern_has_no_ratio_and_unknown_plans_fail_closed():
    assert taste.colour_plan_ratio([1, 2, 3], "allover_pattern").passed
    assert not taste.colour_plan_ratio([1, 2, 3], "ratio_99").ran
    assert taste.colour_plan_ratio([], "ratio_60_30_10").passed


def test_there_is_no_check_that_sets_a_target_look():
    """Taste checks only flag the bad tail: none of them is hard, none is an assert."""
    for check_id in ("DUO-03", "DUO-04", "TASTE_LAYOUT", "TASTE_RATIO", "TASTE_DETAIL"):
        assert policy.meta(check_id).kind == "soft", check_id
