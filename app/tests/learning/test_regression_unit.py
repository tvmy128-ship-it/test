"""APP_SPEC §3.9, §17.2: the fixed brief set, the variety metrics, the quality score and the variety-guard arithmetic."""
from __future__ import annotations

import json
from collections import Counter

import pytest
from lfix import figure, spec_dict

from duoskin.checks import thresholds as TH
from duoskin.pipeline import brief as BR
from duoskin.pipeline import regression as R


# ------------------------------------------------------------------------------------------------------------------ the brief set
def test_the_brief_set_is_deterministic_and_has_exactly_40_briefs():
    a, b = R.generate_briefs(), R.generate_briefs()
    assert len(a) == 40 == TH.get("calib.regression_briefs")
    assert [x.model_dump() for x in a] == [x.model_dump() for x in b]
    assert R.briefs_sha(a) == R.briefs_sha(b) and len(R.briefs_sha(a)) == 64
    assert [x.id for x in a] == [f"rb{i:02d}" for i in range(1, 41)]


def test_the_brief_set_covers_combinations_themes_moods_structures_and_accessories():
    briefs = R.generate_briefs()
    assert Counter(b.combo for b in briefs) == {"bb": 10, "gg": 10, "bg": 10, "gb": 10}
    assert len({b.tags["theme"] for b in briefs}) == 40, "every brief has its own theme"
    assert len({b.tags["mood"] for b in briefs}) == 10
    structures = Counter(b.structure_request for b in briefs)
    assert structures["auto"] == 20 and set(structures) == {"auto", "complement", "leader_chaotic", "same_club", "mirror", "seasonal_twins",
                                                            "object_mascot", "other"}
    assert min(v for k, v in structures.items() if k != "auto") >= 2
    kinds = Counter(k for b in briefs for k in b.tags["accessory_kinds"].split(",") if k != "none")
    assert set(kinds) == {"plush_pet", "keychain_charm", "bag", "small_hat", "hair_clip_slab", "sticker_slab", "prop"} and min(kinds.values()) >= 3
    assert sum(1 for b in briefs if not b.must_include) == 8 and sum(1 for b in briefs if len(b.must_include) == 2) == 8
    assert all(len(b.brief) < 200 and len(line.split()) <= 12 for b in briefs for line in b.must_include)
    # combinations cross the other axes (no combination is tied to one structure or one kind of accessory)
    assert all(len({b.structure_request for b in briefs if b.combo == c}) >= 4 for c in R.COMBOS)


def test_an_open_brief_stays_open_and_none_of_them_names_a_structure_by_accident():
    for b in R.generate_briefs():
        assert BR.brief_structure(" ".join([b.brief, *b.must_include])) is None, b.id


def test_the_brief_set_is_stored_as_data_and_a_person_may_edit_it(rt):
    first = R.ensure_briefs(rt)
    path = rt.paths.regression_dir / "briefs.json"
    assert first.path == path and path.exists() and not first.edited and len(first.briefs) == 40
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["count"] == 40 and doc["sha256"] == first.sha256 and doc["briefs"][0]["id"] == "rb01"
    doc["briefs"][0]["brief"] = "Two friends mending a kite in the rain. The mood is hopeful."
    path.write_text(json.dumps(doc), encoding="utf-8")
    again = R.ensure_briefs(rt)
    assert again.edited and again.sha256 != first.sha256 and again.briefs[0].brief.startswith("Two friends mending")


def test_a_broken_brief_file_is_moved_aside_never_silently_used(rt):
    path = rt.paths.regression_dir / "briefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"briefs": [{"id": "x", "combo": "zz", "brief": "bad"}]}', encoding="utf-8")
    s = R.ensure_briefs(rt)
    assert len(s.briefs) == 40 and not s.edited and (path.parent / "briefs.json.bad").exists()


def test_a_sample_keeps_the_mix_and_is_deterministic():
    briefs = R.generate_briefs()
    s = R.sample_briefs(briefs, 10)
    assert s == R.sample_briefs(briefs, 10) and len(s) == 10
    assert sorted(Counter(b.combo for b in s).values()) == [2, 2, 3, 3]
    assert len({b.structure_request for b in s}) >= 3
    assert R.sample_briefs(briefs, None) == briefs and R.sample_briefs(briefs, 99) == briefs and len(R.sample_briefs(briefs, 1)) == 1


# ------------------------------------------------------------------------------------------------------------------ variety metrics
def test_evenness_and_entropy_arithmetic():
    d = R.distribution(["a", "b", "c", "d"], domain=4)
    assert d["entropy_bits"] == pytest.approx(2.0) and d["evenness"] == pytest.approx(1.0) and d["max_share"] == 0.25 and d["distinct"] == 4
    d = R.distribution(["a"] * 8, domain=4)
    assert d["entropy_bits"] == 0.0 and d["evenness"] == 0.0 and d["max_share"] == 1.0
    d = R.distribution(["a", "a", "a", "b"], domain=8)
    assert d["max_share"] == 0.75 and d["evenness"] == pytest.approx(0.8113 / 3, rel=1e-3)
    empty = R.distribution([], domain=3)
    assert empty["n"] == 0 and empty["max_share"] == 1.0 and empty["evenness"] == 0.0, "a category with no values at all is the least varied one"
    assert R.distribution(["x", "y"])["evenness"] == pytest.approx(1.0), "without a domain the observed values are the domain"


def vary(i: int) -> dict:
    s = spec_dict(pair_structure=("complement", "mirror", "same_club", "other", "leader_chaotic", "seasonal_twins", "object_mascot")[i % 7],
                  palette_family=("warm_pastel", "cool_pastel", "neon_night", "earthy_natural", "jewel_tones")[i % 5])
    for c, off in (("a", 0), ("b", 1)):
        s[c]["face"]["eye_shape"] = ("round", "narrow", "sleepy")[(i + off) % 3]
        s[c]["face"]["mouth_style"] = ("smile_line", "smirk_side", "open_grin", "cat_w", "flat_line")[(i * 2 + off) % 5]
        s[c]["hair"]["kit_style_id"] = ("hair_bob_03", "hair_spiky_05", "hair_short_crop_01")[(i + 2 * off) % 3]
    return s


def test_a_set_of_identical_specs_has_no_variety_and_a_varied_set_has_more():
    same = [spec_dict() for _ in range(12)]
    varied = [vary(i) for i in range(12)]
    dom = {"hair_kit_ids": 4, "eye_shapes": 3, "mouths": 7, "structures": 7, "palette_families": 10, "anchor_kinds": 8, "accessory_types": 7}
    a, b = R.spec_variety(same, domains=dom), R.spec_variety(varied, domains=dom)
    assert a["spec_distance"] == 0.0 and a["distinct"] == {"structures": 1, "hair_kits": 2, "palette_families": 1}
    assert a["grammar"]["combinations"] == 2 and a["grammar_spread"] == pytest.approx(2 / 24), "A and B of the fixture differ, so two combinations"
    assert b["distinct"]["structures"] == 7 and b["distinct"]["hair_kits"] == 3 and b["distinct"]["palette_families"] == 5
    for key in ("variety_index", "category_entropy", "category_spread", "distinct_ratio", "spec_distance", "grammar_spread"):
        assert b[key] > a[key], key
    assert b["categories"]["structures"]["max_share"] == pytest.approx(2 / 12)


def test_the_features_count_both_characters_and_every_anchor_and_accessory():
    f = R.spec_features(spec_dict())
    assert len(f["hair_kit_ids"]) == len(f["eye_shapes"]) == len(f["mouths"]) == 2 and len(f["structures"]) == 1
    assert len(f["anchor_kinds"]) in (2, 3)
    assert R.face_grammar({"face": {"eye_shape": "round", "mouth_style": "cat_w"}})[0] == "round"


def test_picture_distance_is_zero_for_identical_pictures_and_grows_with_difference():
    same = [figure((200, 60, 60)) for _ in range(4)]
    assert R.image_distance(same) == (0.0, "degraded")
    mixed = [figure((200, 60, 60)), figure((60, 200, 60), shape=4), figure((60, 60, 200), shape=8), figure((220, 200, 40), shape=2)]
    d, mode = R.image_distance(mixed)
    assert d > 0.05 and mode == "degraded"
    assert R.image_distance(mixed[:1]) == (0.0, "degraded")

    class Fake:
        kind = "pair"

        def distance(self, a, b):
            return 0.25

    assert R.image_distance(mixed, Fake()) == (0.25, "dreamsim")


# ------------------------------------------------------------------------------------------------------------------ quality
def test_the_quality_score_is_the_critic_levels_and_the_lint_pass_share():
    plan = {"critic_levels": {"a": "strong", "b": "ok", "c": "weak", "d": "fail"}, "lint": {"total": 10, "passed": 9}}
    assert R.critic_score(plan["critic_levels"]) == pytest.approx(1.5 / 3) and R.lint_score(plan["lint"]) == 0.9
    assert R.plan_quality(plan) == pytest.approx((0.5 + 0.9) / 2)
    assert R.plan_quality({"critic_levels": {}, "lint": {}}) == 0.0
    assert R.plan_quality({"critic_levels": {"a": "strong"}, "lint": {}}) == 1.0, "one measure alone still counts"
    ok = {"ok": True, "plans": [plan, {"critic_levels": {"a": "strong"}, "lint": {"total": 4, "passed": 4}}]}
    assert R.brief_quality(ok) == pytest.approx((0.7 + 1.0) / 2)
    assert R.brief_quality({"ok": False, "plans": [plan]}) == 0.0, "a brief that made no plans scores zero"


# ------------------------------------------------------------------------------------------------------------------ the guard
def run(**kw):
    base = {"id": "base", "stage": "plan", "brief_set_sha": "abc", "n_briefs": 40, "image_mode": "degraded", "template_kinds": [],
            "metrics": {"image_distance": 0.40, "variety_index": 0.60, "category_entropy": 0.5, "category_spread": 0.6, "distinct_ratio": 0.7,
                        "spec_distance": 0.5, "grammar_spread": 0.9}, "quality": 0.70, "served_models": ["claude-opus-5"], "candidate_versions": {}}
    metrics = {**base["metrics"], **kw.pop("metrics", {})}
    return {**base, **kw, "metrics": metrics}


def test_the_numbers_the_guard_uses():
    assert TH.get("calib.variety_drop_max") == 0.05 and TH.status_of("calib.variety_drop_max") == "SPEC"
    assert TH.get("calib.quality_drop_max") == 0.0 and TH.get("calib.regression_briefs") == 40 and TH.get("calib.regression_sample") == 10


def test_an_unchanged_candidate_is_accepted():
    g = R.variety_guard(run(), run(id="cand"))
    assert g.accepted and g.decision == "accept" and g.baseline_id == "base" and g.candidate_id == "cand"
    assert all(c["ok"] for c in g.checks) and g.as_dict()["accepted"] is True


@pytest.mark.parametrize(("candidate_value", "accepted"), [(0.40 * 0.951, True), (0.40 * 0.95, True), (0.40 * 0.949, False), (0.40 * 0.50, False), (0.60, True)])
def test_a_fall_of_more_than_5_percent_in_picture_distance_is_rejected(candidate_value, accepted):
    g = R.variety_guard(run(), run(id="c", metrics={"image_distance": candidate_value}))
    assert g.accepted is accepted
    if not accepted:
        assert "fell" in g.reasons[0] and "5%" in g.reasons[0]


@pytest.mark.parametrize(("candidate_value", "accepted"), [(0.60 * 0.96, True), (0.60 * 0.949, False), (0.58, True), (0.50, False)])
def test_a_fall_of_more_than_5_percent_in_the_variety_index_is_rejected(candidate_value, accepted):
    assert R.variety_guard(run(), run(id="c", metrics={"variety_index": candidate_value})).accepted is accepted


def test_a_change_that_raises_the_rating_but_lowers_variety_is_rejected():
    """The point of the guard (PROPOSAL_DECISION safeguards): safer designs always win on pass rate."""
    g = R.variety_guard(run(), run(id="c", quality=0.95, metrics={"image_distance": 0.30, "variety_index": 0.45}))
    assert g.decision == "reject" and len(g.reasons) == 2
    assert next(c for c in g.checks if c["name"] == "quality")["ok"] is True
    assert next(c for c in g.checks if c["name"] == "image_distance")["ok"] is False


@pytest.mark.parametrize(("quality", "accepted"), [(0.70, True), (0.71, True), (0.6999, False), (0.50, False)])
def test_the_quality_score_may_not_fall_at_all(quality, accepted):
    g = R.variety_guard(run(), run(id="c", quality=quality))
    assert g.accepted is accepted
    if not accepted:
        assert "quality score fell" in g.reasons[0]


def test_a_quality_tolerance_can_be_set_explicitly():
    assert R.variety_guard(run(), run(id="c", quality=0.69), quality_drop_max=0.02).accepted
    assert not R.variety_guard(run(), run(id="c", quality=0.67), quality_drop_max=0.02).accepted


def test_relative_drop_arithmetic():
    assert R.relative_drop(0.40, 0.38) == pytest.approx(0.05) and R.relative_drop(0.40, 0.44) == pytest.approx(-0.10) and R.relative_drop(0.0, 0.0) == 0.0


def test_no_baseline_and_different_inputs_never_give_an_accept():
    g = R.variety_guard(None, run(id="c"))
    assert g.decision == "needs_baseline" and not g.accepted
    for key, value in (("brief_set_sha", "other"), ("n_briefs", 10), ("image_mode", "dreamsim"), ("stage", "parts")):
        g = R.variety_guard(run(), run(id="c", **{key: value}))
        assert g.decision == "incomparable" and not g.accepted and key.split("_")[0] in " ".join(g.reasons).lower() or g.decision == "incomparable"


def test_a_candidate_model_that_was_never_called_has_not_been_tested():
    cand = run(id="c", candidate_versions={"models": {"planner": "claude-opus-5-20261001"}}, served_models=["claude-opus-5"])
    g = R.variety_guard(run(), cand)
    assert g.decision == "reject" and "never used" in g.reasons[0]
    cand["served_models"] = ["claude-opus-5-20261001"]
    assert R.variety_guard(run(), cand).accepted


def test_briefs_that_made_no_plans_are_reported():
    g = R.variety_guard(run(), run(id="c", failed_briefs=[{"id": "rb01", "error": "x"}], quality=0.70))
    assert g.accepted is True, "the quality score already counts them as zero"
    g = R.variety_guard(run(), run(id="c", failed_briefs=[{"id": "rb01", "error": "x"}], quality=0.60))
    assert not g.accepted


# ------------------------------------------------------------------------------------------------------------------ the estimate and the stages
def test_mock_providers_cost_nothing_and_ask_nothing(rt):
    e = R.estimate(rt, "plan")
    assert e["live"] is False and e["usd"] == 0.0 and e["needs_confirmation"] is False and "nothing is charged" in e["text"]


def test_real_providers_quote_the_plan_loop_and_ask_above_the_regression_threshold(rt):
    rt.update_settings({"providers": {"modes": {"anthropic": "real", "openai": "real"}}})
    rt.provider_override.clear()
    e = R.estimate(rt, "plan")
    assert e["live"] and e["usd_low"] == 80.0 and e["usd_high"] == 180.0 and e["needs_confirmation"] is True and "$80 to $180" in e["text"]
    s = R.estimate(rt, "plan", briefs=10)
    assert (s["usd_low"], s["usd_high"]) == (20.0, 45.0) and s["needs_confirmation"] is True
    two = R.estimate(rt, "plan", briefs=10, arms=2)
    assert two["usd_high"] == 90.0 and "2 runs" in two["text"]
    small = R.estimate(rt, "plan", briefs=2)
    assert small["usd_high"] == 9.0 and small["needs_confirmation"] is False
    p = R.estimate(rt, "parts", specs=12, templates=2)
    assert p["usd_low"] == pytest.approx(2.4) and p["usd_high"] == pytest.approx(7.2) and not p["needs_confirmation"], "a few dollars per template"


def test_templates_map_to_part_kinds():
    assert R.kinds_for(["I2", "R2"]) == ["print"] and R.kinds_for(["face", "hair"]) == ["face", "hair"] and R.kinds_for(["r1", "I3"]) == ["face"]
    with pytest.raises(R.RegressionError, match="unknown part template"):
        R.kinds_for(["ZZ"])


def test_the_regression_stages_are_split_and_validated(rt):
    with pytest.raises(R.RegressionError, match="stage must be"):
        R.start_regression(rt, stage="all")
    with pytest.raises(R.RegressionError, match="not a model role"):
        R.start_regression(rt, candidate_versions={"nonsense": "x"})
    with pytest.raises(R.RegressionError, match="needs 10 to 20 approved duos"):
        R.start_regression(rt, stage="parts", templates=["I2"])


def test_promotion_is_refused_without_a_passing_guard(rt):
    with pytest.raises(R.RegressionError) as e:
        R.promote(rt, "planner", "claude-opus-5-20261001")
    assert e.value.code == "not_a_candidate"
    rt.update_settings({"models": {"candidates": {"planner": "claude-opus-5-20261001"}}})
    with pytest.raises(R.RegressionError) as e:
        R.promote(rt, "planner", "claude-opus-5-20261001")
    assert e.value.code == "no_regression"
    rejected = run(id="reg1", role="candidate", state="done", candidate_versions={"models": {"planner": "claude-opus-5-20261001"}},
                   guard=R.variety_guard(run(), run(id="reg1", quality=0.1)).as_dict())
    R.save_run(rt, rejected)
    with pytest.raises(R.RegressionError) as e:
        R.promote(rt, "planner", "claude-opus-5-20261001")
    assert e.value.code == "guard" and "variety guard" in str(e.value) and rt.settings.models.planner == "claude-opus-5"
    accepted = {**rejected, "id": "reg2", "guard": R.variety_guard(run(), run(id="reg2")).as_dict()}
    R.save_run(rt, accepted)
    out = R.promote(rt, "planner", "claude-opus-5-20261001")
    assert out["run_id"] == "reg2" and rt.settings.models.planner == "claude-opus-5-20261001" and rt.settings.models.candidates == {}
    assert R.baseline_of(rt, "plan")["id"] == "reg2", "the promoted run is the new baseline"
