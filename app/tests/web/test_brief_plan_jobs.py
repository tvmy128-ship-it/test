"""New duo form, the plan page and the jobs page, against the real API with fake plan steps."""
from __future__ import annotations

import httpx
from playwright.sync_api import expect

from duoskin.engine.testkit import png_bytes, wait_for
from duoskin.models.project import Stage

from . import seed


def api(live, method: str, path: str, **kw):
    return httpx.request(method, live.url + path, headers={"X-DuoSkin-Token": live.token}, timeout=10, **kw)


# ------------------------------------------------------------------------------------------------------------- brief
def test_brief_form_submits_everything_and_starts_the_plan(ui, live, fake_plan, tmp_path):
    ui.goto("/new")
    page = ui.page
    # defaults: both reference switches are OFF, budget comes from settings
    assert not page.get_by_role("switch", name="Check similarity to my reference").is_checked()
    assert not page.get_by_role("switch", name="Use my picture as a mood image").is_checked()
    expect(page.get_by_label("Budget cap for this duo ($)")).to_have_value("15")
    ui.shot("brief_form")
    page.get_by_label("Name this duo").fill("Lantern Pair")
    page.locator('input[name="combo"][value="gg"]').check()
    page.get_by_label("What is the idea?").fill("Two friends at a lantern festival, one in soft knitwear.")
    page.get_by_label("How should the two relate?").select_option("mirror")
    page.get_by_label("Must-include line 1").fill("a paper lantern")
    page.get_by_role("button", name="Add a line").click()
    page.get_by_label("Must-include line 2").fill("matching sleeves")
    # a reference picture, shown as a thumbnail before upload; turning on the mood image shows the recommendation
    ref = tmp_path / "ref.png"
    ref.write_bytes(png_bytes(64, 64))
    page.locator('input[type="file"]').set_input_files(str(ref))
    expect(page.locator(".ref-grid img")).to_have_count(1)
    page.get_by_role("switch", name="Use my picture as a mood image").check()
    expect(page.get_by_text("We recommend turning on")).to_be_visible()
    page.get_by_label("Budget cap for this duo ($)").fill("9")
    page.get_by_label("Ask me before any step over ($)").fill("1.5")
    page.get_by_label("I will make them myself on Tripo's website").check()
    page.get_by_role("button", name="Start the plan").click()
    expect(page).to_have_url(__import__("re").compile(r"#/p/prj_[^/]+/plan$"))
    projects = api(live, "GET", "/api/projects").json()
    assert len(projects) == 1
    got = api(live, "GET", f"/api/projects/{projects[0]['id']}").json()["project"]
    assert got["name"] == "Lantern Pair" and got["combo"] == "gg" and got["structure_request"] == "mirror"
    assert got["must_include"] == ["a paper lantern", "matching sleeves"]
    assert got["settings"]["budget_usd"] == 9 and got["settings"]["ask_above_usd"] == 1.5 and got["settings"]["mesh_mode"] == "manual"
    assert got["settings"]["use_reference_as_mood"] is True and got["settings"]["reference_similarity_check"] is False
    assert len(got["references"]) == 1 and got["stage"] == "planning" and got["plan_job_id"]
    ui.no_errors()


def test_brief_validation_is_friendly_and_sends_nothing(ui, live):
    ui.goto("/new")
    ui.page.get_by_role("button", name="Start the plan").click()
    expect(ui.page.get_by_role("alert")).to_contain_text("give the duo a name")
    ui.page.get_by_label("Name this duo").fill("Wordy")
    ui.page.get_by_label("Must-include line 1").fill("one two three four five six seven eight nine ten eleven twelve thirteen")
    ui.page.get_by_role("button", name="Start the plan").click()
    expect(ui.page.get_by_role("alert")).to_contain_text("at most 12 words")
    assert api(live, "GET", "/api/projects").json() == []


def test_when_planning_is_not_built_the_duo_is_saved_and_the_message_is_plain(ui, live, monkeypatch):
    from duoskin.engine import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_factories", {k: v for k, v in scheduler_mod._factories.items() if k != "plan"})   # the 501 path
    ui.goto("/new")
    ui.page.get_by_label("Name this duo").fill("Saved Anyway")
    ui.page.get_by_role("button", name="Start the plan").click()
    expect(ui.page).to_have_url(__import__("re").compile(r"#/p/prj_[^/]+$"))                  # the project hub, not an error page
    expect(ui.page.locator("main h1")).to_have_text("Saved Anyway")
    ui.page.get_by_role("button", name="Start the plan").click()
    expect(ui.page.locator(".toast")).to_contain_text("not available yet")
    assert "501" not in ui.page.locator("main").inner_text() and "not_implemented" not in ui.page.content()
    ui.shot("project_hub_brief")


# ------------------------------------------------------------------------------------------------------------- plan
def test_plan_page_shows_steps_and_updates_live(ui, live, fake_plan):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    assert api(live, "POST", f"/api/projects/{p.id}/plan").status_code == 200
    ui.goto(f"/p/{p.id}/plan")
    steps = ui.page.locator(".timeline .step")
    expect(steps).to_have_count(2)
    expect(steps.nth(0)).to_contain_text("Writing three plans")
    expect(steps.nth(0)).to_contain_text("Working")                                           # running, in words
    expect(steps.nth(1)).to_contain_text("Checking the plans against the rules")
    expect(steps.nth(1)).to_contain_text("Waiting")
    live.rt.bus.emit("llm.thinking", {"text": "Pairing a calm koi look with a bright lantern look."}, p.id)
    expect(ui.page.locator(".thinking")).to_contain_text("calm koi look")                      # the planner's notes arrive by SSE
    ui.shot("plan_running")
    fake_plan.release.set()
    expect(steps.nth(0)).to_contain_text("Done", timeout=15000)
    expect(steps.nth(1)).to_contain_text("Done", timeout=15000)                                # no reload happened
    ui.no_errors()


def test_plan_page_asks_before_extra_cost_with_the_budget_gate(ui, live):
    from duoskin.engine.gates import allowed_actions_for
    from duoskin.models.common import utcnow
    from duoskin.models.gate import Gate, GateKind, GateTile

    p = seed.project(live.rt, "Costly", "bg", Stage.PLANNING, spent=4.0)
    j = seed.job(live.rt, p.id)
    from duoskin.models.job import Step, StepState

    step = Step(id="stp_budget1", job_id=j.id, project_id=p.id, kind="img.draft", pool="api", state=StepState.WAITING_USER, paid=True, created_at=utcnow())
    live.rt.repo.insert_steps([step])
    tile = GateTile(tile_id="stp_budget1", label="img.draft (about $3.20)", facts={"step_kind": "img.draft", "estimate_usd": 3.2, "spent_usd": 4.0, "cap_usd": 15.0, "reason": "ask_above"},
                    badges=["Budget"], allowed_actions=allowed_actions_for(GateKind.BUDGET))
    gate = live.rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=GateKind.BUDGET, tiles=[tile], opened_at=utcnow()))
    ui.goto(f"/p/{p.id}/plan")
    panel = ui.page.locator(".gate-panel.budget")
    expect(panel).to_contain_text("$3.20")
    expect(panel).to_contain_text("Nothing has been charged for it yet")
    expect(panel).to_contain_text("$4.00 of the $15.00 cap")
    ui.shot("budget_gate")
    panel.get_by_label("New cap in dollars").fill("20")
    panel.get_by_role("button", name="Raise the cap").click()
    expect(ui.page.locator(".gate-panel.budget")).to_have_count(0)
    assert live.rt.repo.get_project(p.id).settings.budget_usd == 20
    assert live.rt.repo.get_gate(gate.id).state == "decided"


# ------------------------------------------------------------------------------------------------------------- jobs
def test_jobs_page_lists_jobs_and_follows_them_live(ui, live, fake_plan):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    job = api(live, "POST", f"/api/projects/{p.id}/plan").json()
    ui.goto("/jobs")
    row = ui.page.locator(f'tr[data-job-id="{job["id"]}"]')
    expect(row).to_contain_text("Plan")
    expect(row).to_contain_text("Plush Koi")
    expect(row).to_contain_text("Running")
    row.get_by_role("button", name="Show steps").click()
    expect(ui.page.locator(".detail-row")).to_contain_text("Writing three plans")
    ui.shot("jobs_running")
    fake_plan.release.set()
    expect(row).to_have_count(0, timeout=15000)                                                 # finished, so it leaves "running and waiting": SSE, no reload
    expect(ui.page.get_by_role("heading", name="Nothing here")).to_be_visible()
    ui.page.get_by_label("Show jobs").select_option("all")
    expect(ui.page.locator(f'tr[data-job-id="{job["id"]}"]')).to_contain_text("Done")
    ui.no_errors()


def test_failed_step_shows_the_plain_hint_and_can_be_retried(ui, live, fake_plan):
    fake_plan.fail_lint = True
    fake_plan.release.set()
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    job = api(live, "POST", f"/api/projects/{p.id}/plan").json()
    wait_for(lambda: api(live, "GET", f"/api/jobs/{job['id']}").json()["job"]["state"] == "failed", 10, message="the job to fail")
    ui.goto("/jobs")
    ui.page.get_by_label("Show jobs").select_option("all")
    row = ui.page.locator(f'tr[data-job-id="{job["id"]}"]')
    expect(row).to_contain_text("Failed")
    row.get_by_role("button", name="Show 1 failed").click()
    detail = ui.page.locator(".detail-row")
    expect(detail).to_contain_text("The rules check could not run. Try again in a moment.")
    assert "Traceback" not in detail.inner_text() and "lint blew up" not in detail.inner_text()          # the hint, not the exception
    ui.shot("jobs_failed")
    fake_plan.fail_lint = False
    detail.get_by_role("button", name="Try again").click()
    expect(ui.page.locator(f'tr[data-job-id="{job["id"]}"]')).to_contain_text("Done", timeout=15000)


def test_cancel_a_job_asks_first(ui, live, fake_plan):
    p = seed.project(live.rt, "Plush Koi", "bg", Stage.BRIEF)
    job = api(live, "POST", f"/api/projects/{p.id}/plan").json()
    ui.goto("/jobs")
    row = ui.page.locator(f'tr[data-job-id="{job["id"]}"]')
    row.get_by_role("button", name="Cancel").click()
    dialog = ui.page.get_by_role("dialog")
    expect(dialog).to_contain_text("Cancel this job?")
    dialog.get_by_role("button", name="Keep going").click()
    expect(row).to_contain_text("Running")
    row.get_by_role("button", name="Cancel").click()
    ui.page.get_by_role("dialog").get_by_role("button", name="Cancel the job").click()
    ui.page.get_by_label("Show jobs").select_option("all")
    expect(ui.page.locator(f'tr[data-job-id="{job["id"]}"]')).to_contain_text("Cancelled", timeout=15000)


# ------------------------------------------------------------------------------------------------------------- real mock pipeline
def test_the_real_mock_plan_loop_shows_up_in_the_ui(ui, live):
    """End to end with the pipeline track's mock plan loop (no fake steps): brief form -> plan page -> three plans."""
    import pytest

    from duoskin.engine.scheduler import has_job_factory
    from duoskin.models.job import JobKind

    if not has_job_factory(JobKind.PLAN):
        pytest.skip("the plan loop is not installed in this build")
    ui.goto("/new")
    ui.page.get_by_label("Name this duo").fill("E2E Duo")
    ui.page.get_by_label("What is the idea?").fill("two friends at a lantern festival")
    ui.page.get_by_role("button", name="Start the plan").click()
    expect(ui.page).to_have_url(__import__("re").compile(r"#/p/prj_[^/]+/plan$"))
    expect(ui.page.get_by_text("Writing three plans")).to_be_visible(timeout=30000)
    expect(ui.page.locator(".plan-card")).to_have_count(3, timeout=60000)                       # the three plans appear while the loop runs
    expect(ui.page.locator(".plan-card .badge.wild")).to_have_count(1)                          # exactly one wildcard
    cards = ui.page.locator(".plan-card").all_inner_texts()
    assert all("Rules check passed" in c for c in cards)
    assert "Traceback" not in ui.page.locator("main").inner_text()
    ui.shot("plan_real_mock")
    ui.no_errors()
