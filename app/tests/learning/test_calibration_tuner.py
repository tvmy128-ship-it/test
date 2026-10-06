"""APP_SPEC §3.1, §3.8, FAILURE_MODES T10: the threshold tuner proposes bad-tail bounds only and never applies anything."""
from __future__ import annotations

import numpy as np
import pytest
from lfix import add_check, approved_duo, seed_labels, store_renders

from duoskin.checks import thresholds as TH
from duoskin.engine import calibration as cal


def seed_values(rt, check_id, metric, values, *, kind="soft"):
    for i, v in enumerate(values):
        p = approved_duo(rt, f"{check_id} {i}", minutes=i)
        add_check(rt, p.id, check_id, passed=True, kind=kind, metric=metric, value=float(v))


def proposal(report, key):
    return next(p for p in report.proposals if p.key == key)


def test_every_tunable_is_a_des_or_unv_threshold_and_none_is_a_target():
    assert cal.TUNABLES and len({t.key for t in cal.TUNABLES}) == len(cal.TUNABLES)
    for t in cal.TUNABLES:
        assert TH.is_tunable(t.key), f"{t.key} is {TH.status_of(t.key)}"
        assert "target" not in t.key.split(".")[-1]
        assert t.tail in ("low", "high")


def test_the_tuner_waits_for_about_200_labels(rt):
    seed_values(rt, "DUO-04", "hair_accessory_silhouette_iou", np.linspace(0.3, 0.8, 30))
    seed_labels(rt, gate=120)
    rep = cal.tune_thresholds(rt)
    assert rep.ready is False and rep.labels == 120 and rep.target == 200
    assert {p.status for p in rep.proposals} == {"not_ready"} and all(p.proposed is None for p in rep.proposals)
    assert "120 of 200" in proposal(rep, "duo.silhouette_iou_cold").note


def test_an_upper_bound_goes_to_the_95th_percentile_of_the_approved_duos(rt):
    values = np.linspace(0.30, 0.80, 40)
    seed_values(rt, "DUO-04", "hair_accessory_silhouette_iou", values)
    seed_labels(rt, gate=200)
    p = proposal(cal.tune_thresholds(rt), "duo.silhouette_iou_cold")
    assert p.status == "proposed" and p.tail == "high" and p.percentile == 95.0 and p.n_approved == 40
    assert p.proposed == pytest.approx(round(float(np.percentile(values, 95)), 3))
    assert p.current == 0.85


def test_a_lower_edge_goes_to_the_5th_percentile(rt):
    values = np.linspace(16.0, 46.0, 30)
    seed_values(rt, "DUO-03", "main_colour_contrast", values)
    seed_labels(rt, gate=210)
    p = proposal(cal.tune_thresholds(rt), "pln.contrast_colour_de")
    assert p.tail == "low" and p.percentile == 5.0
    assert p.proposed == pytest.approx(round(float(np.percentile(values, 5)), 3)) and p.proposed < float(np.median(values))


def test_a_bound_needs_enough_approved_values(rt):
    seed_values(rt, "DUO-03", "main_colour_contrast", [20.0] * 7)
    seed_labels(rt, gate=250)
    p = proposal(cal.tune_thresholds(rt), "pln.contrast_colour_de")
    assert p.status == "insufficient" and p.proposed is None and "7 of the 20" in p.note
    p2 = proposal(cal.tune_thresholds(rt), "taste.layout_ari_warn")
    assert p2.status == "insufficient" and p2.n_approved == 0


@pytest.mark.parametrize("seed", range(5))
def test_proposals_are_always_a_tail_of_what_the_approved_duos_span(rt, seed):
    rng = np.random.default_rng(seed)
    low_vals = rng.uniform(10, 50, 35)
    high_vals = rng.uniform(0.2, 0.9, 35)
    seed_values(rt, "DUO-03", "main_colour_contrast", low_vals)
    seed_values(rt, "DUO-04", "hair_accessory_silhouette_iou", high_vals)
    seed_labels(rt, gate=300)
    rep = cal.tune_thresholds(rt)
    low, high = proposal(rep, "pln.contrast_colour_de"), proposal(rep, "duo.silhouette_iou_cold")
    assert low_vals.min() <= low.proposed <= float(np.median(low_vals)), "a lower edge never sits above the middle of the approved values"
    assert float(np.median(high_vals)) <= high.proposed <= high_vals.max(), "an upper bound never sits below the middle"
    # about 5% of the approved duos fall on the wrong side of each proposed bound, never a target for the rest
    assert (low_vals < low.proposed).mean() <= 0.10 and (high_vals > high.proposed).mean() <= 0.10


def test_tuning_never_applies_anything(rt):
    seed_values(rt, "DUO-04", "hair_accessory_silhouette_iou", np.linspace(0.3, 0.7, 30))
    seed_labels(rt, gate=200)
    before = TH.get("duo.silhouette_iou_cold")
    rep = cal.tune_thresholds(rt)
    assert proposal(rep, "duo.silhouette_iou_cold").status == "proposed"
    assert TH.get("duo.silhouette_iou_cold") == before and TH.calibrated() == {}
    assert cal.active_thresholds(rt) == {}, "proposals are stored for the person to look at, not activated"
    assert rt.repo.kv_get(cal.PROPOSALS_KEY)["proposals"]


def test_the_values_of_drill_answers_count_at_their_down_weighted_size_and_pairs_called_clones_only_add_a_note(rt):
    seed_values(rt, "DUO-04", "hair_accessory_silhouette_iou", [0.5] * 5)           # unrelated
    seed_labels(rt, gate=90)
    for i in range(30):                                                             # 30 pairs the person called real duos, each with a phash distance
        cal.log_label(rt, "clone_real_stranger", "drill", [f"it{i}"], {"label": "real_duo", "metrics": {"phash_mean": 8.0 + i * 0.5}})
    for i in range(6):
        cal.log_label(rt, "clone_real_stranger", "drill", [f"cl{i}"], {"label": "clone", "metrics": {"phash_mean": 3.0 + i * 3.0}})
    counts = cal.label_counts(rt)
    assert counts.drill == 36 and counts.drill_weight == pytest.approx(30 / 36)
    seed_labels(rt, gate=130)                                                       # 220 real labels: ready; the drill cap is 73
    counts = cal.label_counts(rt)
    assert counts.drill_weight == 1.0 and counts.effective_total >= counts.target
    rep = cal.tune_thresholds(rt, keys=["duo.degraded_phash_clone_max"])
    p = rep.proposals[0]
    assert p.n_drill == 30 and p.effective_n == pytest.approx(30.0) and p.status == "proposed"
    assert isinstance(p.proposed, float) and p.proposed == float(round(p.proposed)), "an integer threshold stays an integer"
    assert p.proposed == pytest.approx(round(float(np.percentile([8.0 + i * 0.5 for i in range(30)], 5))))
    assert p.negatives["clone"]["pairs"] == 6 and p.negatives["clone"]["still_pass"] >= 1
    assert "called clone would still pass" in p.note


def test_a_disliked_drill_pair_is_not_a_positive_example(rt):
    seed_labels(rt, gate=400)
    for i in range(30):
        cal.log_label(rt, "clone_real_stranger", "drill", [f"it{i}"], {"label": "real_duo", "metrics": {"phash_mean": 20.0 + i}})
        if i < 10:
            cal.log_label(rt, "like_dislike", "drill", [f"it{i}"], {"liked": False})
    p = cal.tune_thresholds(rt, keys=["duo.degraded_phash_clone_max"]).proposals[0]
    assert p.n_drill == 20, "the ten disliked pairs are left out"


def test_pair_metrics_of_approved_duos_come_from_their_stored_renders(rt):
    for i in range(24):
        p = approved_duo(rt, f"R{i}", minutes=i)
        store_renders(rt, p, (200 - i * 4, 60 + i * 3, 90), (40 + i * 5, 150, 200 - i * 3), a_shape=i % 3, b_shape=(i + 1) % 4)
    seed_labels(rt, gate=200)
    rep = cal.tune_thresholds(rt, keys=["duo.degraded_phash_clone_max", "duo.degraded_palette_overlap_max", "duo.dreamsim_clone_min"])
    ph, ov, ds = (proposal(rep, k) for k in ("duo.degraded_phash_clone_max", "duo.degraded_palette_overlap_max", "duo.dreamsim_clone_min"))
    assert ph.status in ("proposed", "unchanged") and ph.n_approved == 24
    assert ov.status in ("proposed", "unchanged") and ov.tail == "high"
    assert ds.status == "insufficient" and ds.n_approved == 0, "no DreamSim model: the DreamSim bound has no values and says so"
    cached = rt.repo.kv_get(next(k for k in [f"calib:pair:{pid}:degraded" for pid in cal.approved_duo_ids(rt)]))
    assert set(cached) == {"phash_mean", "palette_overlap"}


def test_accepting_needs_the_variety_guard_and_then_activates_and_unhides(rt):
    cal.record_showings(rt, [("DUO-04", True)] * 10)
    assert not cal.warning_visibility("DUO-04", rt).visible
    with pytest.raises(cal.GuardRequired):
        cal.accept_proposals(rt, {"duo.silhouette_iou_cold": 0.7}, guard_passed=False)
    assert TH.get("duo.silhouette_iou_cold") == 0.85
    with pytest.raises(ValueError, match="not a threshold the tuner may change"):
        cal.accept_proposals(rt, {"img.margin_min": 0.1}, guard_passed=True)
    with pytest.raises(ValueError, match="not a threshold the tuner may change"):
        cal.accept_proposals(rt, {"tpl.gap_px": 3}, guard_passed=True)          # a DER value is no tunable at all
    out = cal.accept_proposals(rt, {"duo.silhouette_iou_cold": 0.7}, guard_passed=True, guard_ref="run_1")
    assert TH.get("duo.silhouette_iou_cold") == 0.7 and out["active"] == {"duo.silhouette_iou_cold": 0.7}
    assert "DUO-04" in out["reset_warnings"] and cal.warning_visibility("DUO-04", rt).visible
    hist = rt.repo.kv_get(cal.HISTORY_KEY)
    assert hist[0]["old"] == 0.85 and hist[0]["new"] == 0.7 and hist[0]["guard"] == "run_1"
    TH.set_calibrated({})
    assert TH.get("duo.silhouette_iou_cold") == 0.85
    assert cal.load_calibrated(rt) == {"duo.silhouette_iou_cold": 0.7} and TH.get("duo.silhouette_iou_cold") == 0.7


def test_only_tunable_names_can_be_calibrated_in_the_threshold_registry():
    with pytest.raises(ValueError, match="cannot be calibrated"):
        TH.set_calibrated({"tpl.gap_px": 5})
    with pytest.raises(TH.UnknownThreshold):
        TH.get("nope")
    with TH.overrides({"duo.silhouette_iou_cold": 0.1}):
        TH.set_calibrated({"duo.silhouette_iou_cold": 0.2})
        assert TH.get("duo.silhouette_iou_cold") == 0.1, "a with-block still wins"
    assert TH.get("duo.silhouette_iou_cold") == 0.2


def test_weighted_percentile_matches_numpy_for_equal_weights_and_follows_the_weights_otherwise():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    for q in (5, 50, 95):
        assert cal.weighted_percentile(xs, q) == pytest.approx(float(np.percentile(xs, q)))
        assert cal.weighted_percentile(xs, q, [2.0] * 20) == pytest.approx(float(np.percentile(xs, q)))
    heavy_low = cal.weighted_percentile([1, 10], 50, [9.0, 1.0])
    heavy_high = cal.weighted_percentile([1, 10], 50, [1.0, 9.0])
    assert heavy_low < 5.5 < heavy_high
    with pytest.raises(ValueError):
        cal.weighted_percentile([], 5)
