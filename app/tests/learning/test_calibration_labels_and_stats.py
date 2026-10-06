"""APP_SPEC §3.1, §3.8, §6.13: labels, the 25% drill cap, check statistics, the auto-hide of an overridden warning, the weekly report."""
from __future__ import annotations

import math
from datetime import timedelta

import pytest
from lfix import add_check, approved_duo, make_project, seed_labels, spec_dict

from duoskin.engine import calibration as cal
from duoskin.models.common import utcnow
from duoskin.models.project import Stage


# ------------------------------------------------------------------------------------------------------------------ labels
def test_a_label_is_stored_with_its_kind_source_and_subjects(rt):
    lid = cal.log_label(rt, "like_dislike", "gate", ["prj1", "gat1"], {"liked": True})
    (lab,) = cal.list_labels(rt)
    assert lab.id == lid and lab.kind == "like_dislike" and lab.source == "gate" and lab.subject_ids == ["prj1", "gat1"] and lab.value == {"liked": True}


@pytest.mark.parametrize(("kind", "source"), [("nonsense", "gate"), ("like_dislike", "robot")])
def test_unknown_label_kinds_and_sources_are_refused(rt, kind, source):
    with pytest.raises(ValueError, match="unknown label"):
        cal.log_label(rt, kind, source, [], None)
    assert cal.list_labels(rt) == []


def test_labels_can_be_filtered_by_kind_and_source(rt):
    seed_labels(rt, gate=3, drill=2, calibration=1)
    assert len(cal.list_labels(rt, source="drill")) == 2
    assert len(cal.list_labels(rt, kind="rule_verdict")) == 1
    assert len(cal.list_labels(rt, limit=2)) == 2


# ------------------------------------------------------------------------------------------------------------------ the 25% cap
@pytest.mark.parametrize(("drill", "other"), [(0, 0), (0, 40), (10, 30), (15, 45), (30, 60), (50, 10), (7, 0), (100, 100)])
def test_drill_labels_never_exceed_a_quarter_of_the_effective_calibration_set(rt, drill, other):
    seed_labels(rt, gate=other, drill=drill)
    c = cal.label_counts(rt)
    assert c.total == drill + other and c.drill == drill
    assert c.effective_drill_share <= 0.25 + 1e-9
    if drill and other and drill / (drill + other) <= 0.25:
        assert c.drill_weight == 1.0, "under the cap nothing is down-weighted"
    if drill > other / 3 and drill:
        assert c.drill_weight < 1.0
        assert math.isclose(c.effective_drill_share, 0.25, abs_tol=1e-9) if other else c.effective_drill_share == 0.0


def test_drill_room_is_what_still_fits_under_the_cap():
    assert cal.drill_room(0, 60) == 20                 # 20 of 80 is 25%
    assert cal.drill_room(20, 60) == 0
    assert cal.drill_room(25, 60) == 0
    assert cal.drill_room(0, 0) == 0                   # drills alone can never be the calibration set
    assert cal.drill_weight(25, 60) == pytest.approx(20 / 25)
    assert cal.drill_weight(0, 0) == 1.0


def test_label_counts_report_the_set_the_tuner_will_see(rt):
    seed_labels(rt, gate=120, drill=60, calibration=20)         # 140 real + 60 drill: cap allows 46 drills
    c = cal.label_counts(rt)
    assert c.by_source == {"gate": 120, "drill": 60, "calibration": 20}
    assert c.drill_weight == pytest.approx((140 / 3) / 60)
    assert c.effective_total == pytest.approx(140 + 140 / 3)
    d = c.as_dict()
    assert d["cap_reached"] is True and d["target"] == 200 and 0 < d["progress"] < 1


# ------------------------------------------------------------------------------------------------------------------ check ids
@pytest.mark.parametrize(("warning_id", "check_id"), [
    ("a.face:F_ZONES", "F_ZONES"), ("CHK-G1-11:a", "CHK-G1-11"), ("duo:DUO-04", "DUO-04"), ("change:TASTE_LAYOUT", "TASTE_LAYOUT"),
    ("DUO-10", "DUO-10"), ("b.acc.0:ac_no_thin_parts", "ac_no_thin_parts"), ("w1", "w1")])
def test_a_warning_id_names_its_check(warning_id, check_id):
    assert cal.check_id_of(warning_id) == check_id


# ------------------------------------------------------------------------------------------------------------------ statistics
def test_flag_and_catch_rates_use_the_decisions_of_the_gates_the_check_applies_to(rt):
    # DUO-04 is a gate-3 check: its denominators are the final-pick decisions only
    for i in range(10):
        cal.record_gate_outcome(rt, "final_pick", "approved", ["DUO-04"] if i < 3 else [])
    for i in range(4):
        cal.record_gate_outcome(rt, "final_pick", "rejected", ["DUO-04"] if i < 3 else [])
    for _ in range(50):                                            # busy part-board decisions must not dilute a gate-3 check
        cal.record_gate_outcome(rt, "part_board", "approved", [])
    assert cal.flag_rate(rt, "DUO-04") == pytest.approx(3 / 10)
    assert cal.catch_rate(rt, "DUO-04") == pytest.approx(3 / 4)
    assert cal.flag_rate(rt, "never_seen") == 0.0 and cal.catch_rate(rt, "never_seen") == 0.0


def test_stats_are_kept_per_iso_week(rt):
    last_week = utcnow() - timedelta(days=8)
    cal.record_gate_outcome(rt, "final_pick", "approved", ["DUO-04"], when=last_week)
    cal.record_gate_outcome(rt, "final_pick", "approved", [])
    this = cal.week_start()
    assert cal.flag_rate(rt, "DUO-04", window=this) == 0.0
    assert cal.flag_rate(rt, "DUO-04", window=cal.week_start(last_week)) == 1.0
    assert cal.flag_rate(rt, "DUO-04") == pytest.approx(0.5)
    assert cal.parse_week(cal.iso_week_label(this)) == this and cal.parse_week("all") is None


def test_outcomes_of_actions():
    assert cal.outcome_of("approve") == cal.outcome_of("pick") == cal.outcome_of("approve_all") == "approved"
    assert cal.outcome_of("reimagine") == cal.outcome_of("new_plan") == "rejected"
    assert cal.outcome_of("change") is None and cal.outcome_of("select_alternative") is None


# ------------------------------------------------------------------------------------------------------------------ auto-hide
def shows(rt, check, overridden_flags):
    cal.record_showings(rt, [(check, bool(f)) for f in overridden_flags])


def test_a_warning_overridden_in_more_than_25_percent_of_its_last_20_showings_hides_itself(rt):
    shows(rt, "DUO-04", [0] * 15 + [1] * 5)                       # 5 of 20 = 25%: not MORE than 25%
    v = cal.warning_visibility("DUO-04", rt)
    assert v.visible and v.showings == 20 and v.overridden == 5 and v.override_rate == 0.25
    shows(rt, "DUO-04", [1])                                       # the window slides: 5 overrides + 1 in the last 20 -> the oldest heeded one drops out
    v = cal.warning_visibility("DUO-04", rt)
    assert not v.visible and v.showings == 20 and v.overridden == 6 and "6 of its last 20" in v.reason


def test_only_the_last_20_showings_count(rt):
    shows(rt, "DUO-03", [1] * 12)                                  # a long-ago habit of overriding ...
    assert not cal.warning_visibility("DUO-03", rt).visible
    shows(rt, "DUO-03", [0] * 20)                                  # ... is forgotten after 20 heeded showings
    v = cal.warning_visibility("DUO-03", rt)
    assert v.visible and v.overridden == 0 and v.showings == 20


def test_a_warning_is_never_hidden_on_a_handful_of_showings(rt):
    shows(rt, "DUO-04", [1, 1])                                    # 100% of 2 showings is not evidence
    assert cal.warning_visibility("DUO-04", rt).visible
    shows(rt, "DUO-04", [1] * 6)                                   # 8 showings, all overridden
    assert not cal.warning_visibility("DUO-04", rt).visible


def test_a_hidden_warning_comes_back_when_its_check_is_retuned(rt):
    shows(rt, "DUO-04", [1] * 10)
    assert [v.check_id for v in cal.hidden_warnings(rt)] == ["DUO-04"]
    assert cal.reset_recent(rt, ["DUO-04", "DUO-03"]) == 1
    assert cal.warning_visibility("DUO-04", rt).visible and cal.hidden_warnings(rt) == []


def test_an_annotated_warning_carries_the_catch_rate_and_the_hide_flag(rt):
    cal.record_gate_outcome(rt, "final_pick", "rejected", ["DUO-04"])
    cal.record_gate_outcome(rt, "final_pick", "rejected", [])
    w = cal.annotate_warning(rt, {"id": "duo:DUO-04", "text": "x", "catch_rate": 0.0, "visible": True})
    assert w["catch_rate"] == pytest.approx(0.5) and w["visible"] is True and w["check_id"] == "DUO-04"
    shows(rt, "DUO-04", [1] * 9)
    assert cal.annotate_warning(rt, {"id": "duo:DUO-04", "visible": True})["visible"] is False


# ------------------------------------------------------------------------------------------------------------------ demotion
def seed_fire(rt, check_id, *, kind, fired, ran):
    for i in range(ran):
        p = make_project(rt, f"Fire {check_id} {i}")
        add_check(rt, p.id, check_id, passed=i >= fired, kind=kind)


def test_a_demotable_hard_check_that_fires_on_more_than_30_percent_of_duos_goes_back_to_soft(rt):
    seed_fire(rt, "A_ALPHA", kind="hard", fired=5, ran=12)       # 42%
    seed_fire(rt, "A_HALO", kind="hard", fired=3, ran=12)        # 25%: fine
    due = cal.demotions_due(rt)
    assert [d.check_id for d in due] == ["A_ALPHA"] and due[0].fire_rate == pytest.approx(5 / 12)
    done = cal.apply_demotions(rt)
    assert [d.check_id for d in done] == ["A_ALPHA"]
    assert rt.settings.checks.check_overrides == {"A_ALPHA": "soft"}
    from duoskin.checks import policy

    assert policy.effective_kind("A_ALPHA", rt.settings.checks.check_overrides) == "soft"
    assert cal.apply_demotions(rt) == [], "already demoted"
    banner = cal.active_demotions(rt)[0]
    assert banner["check_id"] == "A_ALPHA" and "warning" in banner["banner"]
    cal.restore_check(rt, "A_ALPHA")
    assert rt.settings.checks.check_overrides == {}


def test_roblox_ip_stray_text_security_and_integrity_checks_are_never_demoted(rt):
    from duoskin.checks import policy

    never = [cid for cid, m in policy.registry().items() if m.kind in ("hard", "assert") and m.policy_class in policy.NEVER_DEMOTABLE]
    assert len(never) > 20
    for cid in never[:6]:
        seed_fire(rt, cid, kind="hard", fired=12, ran=12)        # fires on every duo
    assert cal.demotions_due(rt) == []
    assert cal.apply_demotions(rt) == [] and rt.settings.checks.check_overrides == {}


def test_a_check_needs_enough_duos_before_it_can_be_demoted(rt):
    seed_fire(rt, "A_ALPHA", kind="hard", fired=4, ran=5)         # 80% of 5 duos
    assert cal.demotions_due(rt) == []


def test_regression_projects_and_checks_that_did_not_run_do_not_count_toward_fire_rates(rt):
    for i in range(12):
        p = make_project(rt, f"{cal.REGRESSION_PREFIX}{i}")
        add_check(rt, p.id, "A_ALPHA", passed=False, kind="hard")
    p = make_project(rt, "real")
    add_check(rt, p.id, "A_HALO", passed=False, kind="hard", ran=False)
    assert cal.fire_rates(rt) == {}


def test_the_hard_reject_rate_on_approved_duos_alerts_above_10_percent(rt):
    for i in range(10):
        p = approved_duo(rt, f"A{i}")
        add_check(rt, p.id, "A_ALPHA", passed=i != 0, kind="hard")          # one duo of ten was stopped
    r = cal.hard_reject_rate(rt)
    assert r["rate"] == pytest.approx(0.1) and r["alert"] is False and r["by_class"] == {"buildability": 1}
    p = approved_duo(rt, "A11")
    add_check(rt, p.id, "CHK-D02", passed=False, kind="hard")
    r = cal.hard_reject_rate(rt)
    assert r["alert"] is True and r["rejected_duos"] == 2 and r["by_class"]["clone_lower_edge"] == 1
    # a demoted check is soft now and no longer rejects anything
    rt.update_settings({"checks": {"check_overrides": {"A_ALPHA": "soft", "CHK-D02": "soft"}}})
    assert cal.hard_reject_rate(rt)["rejected_duos"] == 0


# ------------------------------------------------------------------------------------------------------------------ the report
def test_the_weekly_report_has_every_section_and_zero_values_on_an_empty_database(rt):
    rep = cal.weekly_report(rt)
    for key in ("week", "approved_duos", "per_check", "hidden_warnings", "demoted_checks", "hard_reject_rate_on_approved", "structure_use",
                "wildcard_pick_rate", "gate1_first_try_approval", "nearest_duo_distances", "registry_reject_rate", "cost_per_duo", "labels",
                "revisit_triggers", "tuner", "alerts", "top_failure_causes"):
        assert key in rep, key
    assert rep["approved_duos"] == 0 and rep["per_check"] == [] and rep["alerts"] == []
    assert rep["week"]["label"].startswith("20") and rep["labels"]["total"] == 0
    assert "weekly report" in cal.format_report(rep)


def test_the_report_lists_each_check_with_the_two_numbers(rt):
    for i in range(10):
        cal.record_gate_outcome(rt, "final_pick", "approved", ["DUO-04"] if i < 2 else [])
    for i in range(5):
        cal.record_gate_outcome(rt, "final_pick", "rejected", ["DUO-04"] if i < 4 else [])
    cal.record_showings(rt, [("DUO-04", True)] + [("DUO-04", False)] * 3)
    row = next(r for r in cal.weekly_report(rt)["per_check"] if r["check_id"] == "DUO-04")
    assert row["flag_rate_on_approved"] == pytest.approx(0.2) and row["catch_rate_on_rejected"] == pytest.approx(0.8)
    assert row["shown"] == 4 and row["overridden"] == 1 and row["hidden"] is False and row["kind"] == "soft"
    text = cal.format_report(cal.weekly_report(rt))
    assert "DUO-04" in text and "20%" in text and "80%" in text


def test_structure_use_the_collapse_hint_and_the_wildcard_pick_rate(rt):
    for i in range(10):
        spec = spec_dict(pair_structure="complement" if i < 6 else "mirror")
        spec["is_wildcard"] = i >= 7
        approved_duo(rt, f"D{i}", spec=spec, minutes=i)
    rep = cal.weekly_report(rt)
    assert rep["structure_use"]["complement"] == pytest.approx(0.6)
    assert rep["structure_collapse"] == "complement" and "least used" in rep["structure_hint"]
    assert rep["wildcard_pick_rate"] == pytest.approx(0.3) and rep["wildcard"]["suggest_novelty"] is True and "novelty" in rep["wildcard_hint"]
    assert any(t["id"] == "pair_share" for t in rep["revisit_triggers"]), "one structure above 35% after 10 duos"


def test_the_failure_causes_are_counted_by_failure_mode_id(rt):
    p = make_project(rt, "x")
    for _ in range(3):
        add_check(rt, p.id, "A_ALPHA", passed=False, kind="hard", fm_ids=["IMG-01"])
    add_check(rt, p.id, "A_HALO", passed=False, kind="hard", fm_ids=["IMG-02"])
    causes = cal.failure_causes(rt)
    assert {k: causes[0][k] for k in ("fm_id", "failures", "check_ids")} == {"fm_id": "IMG-01", "failures": 3, "check_ids": ["A_ALPHA"]}
    assert causes[1]["fm_id"] == "IMG-02"
    assert "transparency" in causes[0]["label"].lower() and "A_ALPHA" not in causes[0]["label"]  # plain words, not the code


def test_cost_per_approved_duo_counts_all_spend_but_not_regression_projects(rt):
    from duoskin.models.cost import CostEntry

    a = approved_duo(rt, "Done")
    b = make_project(rt, "Abandoned", stage=Stage.GATE1)
    r = make_project(rt, f"{cal.REGRESSION_PREFIX}1")
    for pid, usd in ((a.id, 6.0), (b.id, 3.0), (r.id, 40.0)):
        rt.budget.add_entry(CostEntry(ts=utcnow(), project_id=pid, provider="openai", model="m", operation=f"op{pid}", usd=usd, basis="usage",
                                      state="committed", step_id=f"s{pid}", price_table="t"))
    c = cal.cost_per_duo(rt)
    assert c["per_approved_duo"] == pytest.approx(6.0) and c["all_spend_per_approved_duo"] == pytest.approx(9.0) and c["duos"] == 1
