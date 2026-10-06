"""Browser test of the Calibration and Learning pages against the REAL API (tests/web conventions: Chromium on a loopback server, mock providers,
strict CSP). Screenshots go to tests/web/screenshots/."""
from __future__ import annotations

import re

from lfix import add_check, approved_duo, figure, make_project, seed_labels, spec_dict, store_png, store_renders
from playwright.sync_api import expect

from duoskin.engine import calibration as cal
from duoskin.models.common import utcnow
from duoskin.pipeline import regression as R


def seed_duos(rt, n=6, gate_labels=120, *, wildcard_last=2):
    ids = []
    for i in range(n):
        spec = spec_dict(pair_structure=("complement", "mirror", "same_club", "complement", "other", "mirror", "complement")[i % 7])
        spec["is_wildcard"] = i >= n - wildcard_last
        p = approved_duo(rt, f"Base {i}", spec=spec, minutes=i)
        store_renders(rt, p, (200 - 25 * i, 70 + 20 * i, 80 + 10 * i), (40 + 30 * i, 160 - 15 * i, 200 - 20 * i), a_shape=i % 3, b_shape=(i + 2) % 5)
        ids.append(p.id)
    seed_labels(rt, gate=gate_labels)
    return ids


# ------------------------------------------------------------------------------------------------------------------ Calibration
def test_calibration_is_locked_until_five_approved_duos(ui, live):
    for i in range(2):
        approved_duo(live.rt, f"A{i}")
    ui.goto("/calibration")
    main = ui.page.locator("main")
    expect(main).to_contain_text("Not unlocked yet")
    expect(main).to_contain_text("Calibration needs at least 5 approved duos")
    expect(main).to_contain_text("You have 2")
    expect(ui.page.get_by_role("link", name="Make a duo")).to_be_visible()
    ui.shot("calibration_locked")
    ui.no_errors()


def test_a_blind_round_one_picture_one_question_answers_become_drill_labels(ui, live):
    rt = live.rt
    seed_duos(rt)
    ui.goto("/calibration")
    main = ui.page.locator("main")
    expect(main).to_contain_text("Picture 1 of 20")
    expect(main).to_contain_text("What is this?")
    img = ui.page.locator(".drill-pic img")
    expect(img).to_be_visible()
    ui.wait_until(lambda: img.evaluate("i => i.complete && i.naturalWidth > 0"), 10, "the picture to load")
    text = main.inner_text().lower()
    for hint in ("phash", "dreamsim", "metric", "verdict", "score", "expected", "recipe", "swap", "recolour", "original", "anchor", "clone check"):
        assert hint not in text, f"the blind screen shows {hint!r}"
    alt = img.get_attribute("alt") or ""
    assert "clone" not in alt.lower() and "real" not in alt.lower()
    assert ui.page.get_by_role("button", name="Save and next").is_disabled(), "nothing is saved before an answer is chosen"
    ui.shot("calibration_round")
    # answer with the buttons: a label and a like, saved together
    ui.page.get_by_role("button", name="A real duo").click()
    expect(ui.page.get_by_role("button", name="A real duo")).to_have_attribute("aria-pressed", "true")
    ui.page.get_by_role("button", name="Like", exact=True).click()
    ui.page.get_by_role("button", name="Save and next").click()
    expect(main).to_contain_text("Picture 2 of 20")
    labels = cal.list_labels(rt, source="drill")
    assert [(x.kind, x.value.get("label") if x.kind == "clone_real_stranger" else x.value) for x in labels] == [("clone_real_stranger", "real_duo"), ("like_dislike", {"liked": True})]
    assert labels[0].value["metrics"] and labels[0].value["kind"], "the hidden numbers are stored beside the answer, never shown"
    # keyboard: 3 = strangers, D = dislike, Enter = save
    ui.page.locator("main").click(position={"x": 5, "y": 5})
    ui.page.keyboard.press("3")
    ui.page.keyboard.press("d")
    ui.page.keyboard.press("Enter")
    expect(main).to_contain_text("Picture 3 of 20")
    second = [x for x in cal.list_labels(rt, source="drill")][2:]
    assert second[0].value["label"] == "strangers" and second[1].value == {"liked": False}
    # skipping stores nothing
    n = len(cal.list_labels(rt, source="drill"))
    ui.page.get_by_role("button", name="Skip this one").click()
    expect(main).to_contain_text("Picture 4 of 20")
    assert len(cal.list_labels(rt, source="drill")) == n
    expect(ui.page.get_by_role("heading", name="How many answers there are")).to_be_visible()
    expect(main).to_contain_text("capped at a quarter")
    ui.no_errors()


def test_stop_here_closes_the_round_and_the_next_one_starts_fresh(ui, live):
    seed_duos(live.rt)
    ui.goto("/calibration")
    expect(ui.page.locator("main")).to_contain_text("Picture 1 of 20")
    expect(ui.page.locator("main")).to_contain_text("Time so far: 00:0")
    expect(ui.page.locator("main")).to_contain_text("a round takes 10 to 15 minutes")
    first = live.rt.repo.kv_get(cal.__name__ and "drill:current")
    ui.page.get_by_role("button", name="Stop here").click()
    expect(ui.page.locator("main")).to_contain_text("Picture 1 of 20")
    assert live.rt.repo.kv_get("drill:current") != first


def test_the_cap_ends_practice_with_a_kind_message(ui, live):
    seed_duos(live.rt, gate_labels=8)               # 8 real answers allow 2 practice answers
    ui.goto("/calibration")
    for _ in range(2):
        ui.page.get_by_role("button", name="A copy of something").click()
        ui.page.get_by_role("button", name="Save and next").click()
        ui.page.wait_for_timeout(300)
    expect(ui.page.locator("main")).to_contain_text("That is enough practice for now")
    expect(ui.page.locator("main")).to_contain_text("capped at a quarter of all your answers")
    assert cal.label_counts(live.rt).drill == 2
    ui.shot("calibration_cap")
    ui.no_errors()


def test_the_judge_tab_shows_a_picture_and_never_the_judges_verdict(ui, live):
    rt = live.rt
    p = make_project(rt, "Judged")
    sha = store_png(rt, figure((200, 60, 60)))
    add_check(rt, p.id, "cn_back_view", passed=True, kind="hard", subject_sha=sha)
    ui.goto("/calibration")
    ui.page.get_by_role("tab", name="Judge calibration").click()
    main = ui.page.locator("main")
    expect(main.locator(".drill-question")).to_contain_text("Does this picture pass")
    assert "judge_passed" not in main.inner_text() and "passed:" not in main.inner_text().lower()
    ui.page.get_by_role("button", name="Something is wrong").click()
    expect(main).to_contain_text("Nothing to look at")
    (lab,) = cal.list_labels(rt, kind="rule_verdict", source="calibration")
    assert lab.value["label"] == "fail" and lab.value["judge_passed"] is True
    ui.shot("calibration_judge")
    ui.no_errors()


# ------------------------------------------------------------------------------------------------------------------ Learning
def seed_learning(rt):
    seed_duos(rt, 10, 150)
    for i in range(10):
        cal.record_gate_outcome(rt, "part_board", "approved", ["TASTE_LAYOUT"] if i < 2 else [])
    for i in range(4):
        cal.record_gate_outcome(rt, "part_board", "rejected", ["TASTE_LAYOUT"] if i < 3 else ["F_BLUSH"])
    cal.record_showings(rt, [("TASTE_LAYOUT", False)] * 5 + [("TASTE_LAYOUT", True)] * 2)
    cal.record_showings(rt, [("F_BLUSH", True)] * 9)                         # hidden: set aside too often
    for i in range(12):                                                      # a hard check that fires on 5 of 12 duos
        p = make_project(rt, f"Fire {i}")
        add_check(rt, p.id, "A_ALPHA", passed=i >= 5, kind="hard", fm_ids=["IMG-01"])
    cal.apply_demotions(rt)
    for i, pid in enumerate(cal.approved_duo_ids(rt)):
        add_check(rt, pid, "DUO-04", passed=True, metric="hair_accessory_silhouette_iou", value=0.30 + 0.025 * i)


def run_record(rid, role, *, accepted, state="done", cand=None, quality=0.71, image=0.42, index=0.62, minutes=0):
    base = {"id": rid, "stage": "plan", "role": role, "label": role, "state": state, "mode": "mock", "created_at": utcnow().isoformat(), "n_briefs": 10, "ok_briefs": 10,
            "failed_briefs": [], "brief_set_sha": "abc", "image_mode": "degraded", "template_kinds": [], "quality": quality, "cost_usd": 0.0,
            "metrics": {"image_distance": image, "variety_index": index}, "candidate_versions": {"models": cand} if cand else {}, "served_models": list((cand or {}).values())}
    if role == "candidate":
        baseline = {**base, "id": "reg_base", "quality": 0.70, "metrics": {"image_distance": 0.42, "variety_index": 0.62}, "candidate_versions": {}}
        base["guard"] = R.variety_guard(baseline, base).as_dict()
    return base


def test_the_learning_page_shows_the_weekly_report_from_real_data(ui, live):
    rt = live.rt
    seed_learning(rt)
    ui.goto("/learning")
    main = ui.page.locator("main")
    expect(ui.page.get_by_role("heading", name="Which checks help")).to_be_visible()
    cards = ui.page.locator(".stat-card")
    expect(cards.nth(0)).to_contain_text("10")                                # approved duos
    expect(cards.nth(3)).to_contain_text("20%")                               # the wildcard was picked in 2 of the 10 last duos
    expect(main).to_contain_text("Required checks stopped")
    row = ui.page.locator("tr", has_text="Garment layout")
    expect(row).to_contain_text("20%")                                        # flagged 2 of 10 approved duos
    expect(row).to_contain_text("75%")                                        # and 3 of 4 rejected ones
    expect(ui.page.locator("tr", has_text="blush").first).to_contain_text("Hidden")
    expect(ui.page.locator(".alert-list")).to_contain_text("flagged")         # the demotion banner
    expect(ui.page.get_by_role("button", name="Turn it back on")).to_be_visible()
    expect(ui.page.get_by_role("heading", name="How often each pair style was used")).to_be_visible()
    assert "{" not in main.inner_text() and "undefined" not in main.inner_text() and "NaN" not in main.inner_text()
    ui.shot("learning_report", full=True)
    # a demoted check can be turned back on
    assert rt.settings.checks.check_overrides == {"A_ALPHA": "soft"}
    ui.page.get_by_role("button", name="Turn it back on").click()
    ui.wait_until(lambda: rt.settings.checks.check_overrides == {}, 5, "the check to be turned back on")
    ui.no_errors()


def test_the_tuner_only_suggests_and_the_variety_guard_decides(ui, live):
    rt = live.rt
    seed_learning(rt)
    seed_labels(rt, gate=60)
    for i in range(14):                                       # 24 approved duos in all: enough values for a bound (the tuner wants 20)
        add_check(rt, approved_duo(rt, f"More {i}", minutes=50 + i).id, "DUO-04", passed=True, metric="hair_accessory_silhouette_iou", value=0.30 + 0.01 * i)
    ui.goto("/learning")
    panel = ui.page.locator("section.panel", has=ui.page.get_by_role("heading", name="Quality limits"))
    expect(panel).to_contain_text("never changes a limit by itself")
    expect(panel.get_by_role("button", name="Test and accept the ticked limits")).to_be_disabled()
    panel.get_by_role("button", name="Look for suggestions").click()
    row = panel.locator("tr", has_text="hair and accessory shapes")
    expect(row).to_contain_text("0.85")
    box = row.get_by_role("checkbox")
    expect(box).to_be_enabled()
    box.check()
    panel.get_by_role("button", name="Test and accept the ticked limits").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Test and accept").click()
    expect(panel.locator(".msg")).to_contain_text("Not accepted")             # no regression run to judge it on yet: the guard says so
    expect(panel.locator(".msg")).to_contain_text("regression test once")
    from duoskin.checks import thresholds as TH

    assert TH.get("duo.silhouette_iou_cold") == 0.85 and cal.active_thresholds(rt) == {}
    ui.shot("learning_tuner")
    ui.no_errors()


def test_regression_runs_show_their_numbers_and_the_guards_reasons_and_promotion_follows_the_guard(ui, live):
    rt = live.rt
    cand_a, cand_b = "claude-opus-5-20261001", "claude-opus-5-20261002"
    rt.update_settings({"models": {"candidates": {"planner": cand_a, "critic": cand_b}}})
    R.save_run(rt, {**run_record("reg_base", "baseline", accepted=True), "id": "reg_base"})
    rt.repo.kv_set(R.BASELINE_PREFIX + "plan", "reg_base")
    R.save_run(rt, run_record("reg_ok", "candidate", accepted=True, cand={"planner": cand_a}))
    R.save_run(rt, run_record("reg_bad", "candidate", accepted=False, cand={"critic": cand_b}, quality=0.80, image=0.30, index=0.45))
    ui.goto("/learning")
    expect(ui.page.get_by_role("heading", name="Regression test")).to_be_visible()
    expect(ui.page.locator(".estimate-line").first).to_contain_text("Mock providers: nothing is charged.")
    runs = ui.page.locator(".run")
    expect(runs).to_have_count(3)
    bad = runs.filter(has_text="Critic claude-opus-5-20261002")
    expect(bad).to_contain_text("Did not pass")
    bad.locator("summary").click()
    expect(bad).to_contain_text("fell")
    expect(bad.locator(".guard-checks")).to_contain_text("Failed")
    ok = runs.filter(has_text="Planner claude-opus-5-20261001")
    expect(ok).to_contain_text("Passed the guard")
    ui.shot("learning_regression", full=True)
    # promotion: only the version whose test passed can be promoted
    vp = ui.page.locator("section.panel", has=ui.page.get_by_role("heading", name="New versions"))
    expect(vp.locator("tr", has_text=cand_b).get_by_role("button", name="Promote this version")).to_be_disabled()
    vp.locator("tr", has_text=cand_a).get_by_role("button", name="Promote this version").click()
    ui.wait_until(lambda: rt.settings.models.planner == cand_a, 8, "the promotion")
    assert rt.settings.models.critic == "claude-opus-5" and rt.settings.models.candidates == {"critic": cand_b}
    ui.no_errors()


def test_starting_the_regression_asks_first_and_starts_a_real_job(ui, live):
    rt = live.rt
    ui.goto("/learning")
    ui.page.get_by_role("button", name="Start the regression test").click()
    dlg = ui.page.get_by_role("dialog")
    expect(dlg).to_contain_text("Run the regression test?")
    expect(dlg).to_contain_text("Mock providers: nothing is charged.")
    dlg.get_by_role("button", name="Cancel").click()
    assert R.list_runs(rt) == [], "cancelling starts nothing"
    ui.page.get_by_role("button", name="Start the regression test").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Start the test").click()
    ui.wait_until(lambda: R.list_runs(rt), 10, "the run record")
    run = R.list_runs(rt)[0]
    assert run["stage"] == "plan" and run["role"] == "baseline" and run["n_briefs"] == 10
    expect(ui.page.locator(".run").first).to_be_visible()
    job = rt.repo.get_job(run["job_id"])
    rt.scheduler.cancel_job(job.id)
    ui.shot("learning_regression_started")


def test_an_empty_install_shows_friendly_empty_states(ui, live):
    ui.goto("/learning")
    main = ui.page.locator("main")
    expect(main).to_contain_text("Nothing to show yet")
    expect(main).to_contain_text("No candidate versions are waiting")
    expect(main).to_contain_text("No test has been run yet")
    assert re.search(r"\bundefined\b|\bNaN\b|\[object", main.inner_text()) is None
    ui.shot("learning_empty", full=True)
    ui.no_errors()
