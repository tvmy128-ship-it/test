"""The product with faults injected into the mock providers (``DUOSKIN_MOCK_FAULTS``, APP_SPEC 16): every fault must end in a friendly state a person
can recover from, and the duo must still reach the export preview.

The real app runs (scheduler, every pipeline step, mock providers that raise the very errors the real adapters raise). A small "pilot" does what a
person does, brief to Gate 3 and Export, using the same API the pages call. Whenever a step fails the pilot does what a person would do: it opens
the page for the stage, checks that the page says what happened in plain words (the step's own hint, no stack trace, no check code, no raw id),
saves a screenshot, and presses "Try again" in the browser. Faults the app absorbs by itself (an automatic retry, the next provider) need nothing.
"""
from __future__ import annotations

import json
import re
import time
import uuid

import pytest
from e2e_helpers import Api, clean
from playwright.sync_api import expect

from duoskin.providers import faults

# the page a person is looking at in each stage of the project, where a failed step has to be visible
PAGE_OF_STAGE = {"planning": "plan", "gate1": "gate1", "parts": "board", "gate2": "board", "building": "build", "duo": "gate3", "gate3": "gate3"}

# (fault set, the steps that a person has to retry in the browser: every other fault is absorbed by the app itself)
SCENARIOS = {
    "the planner and the IP screen": ("anthropic:truncated@L3,anthropic:schema_invalid@L3,anthropic:refusal@L13", {"plan.planner", "duo.ip"}),
    "the pictures": ("openai:moderation_blocked@first,openai:size_drift@I2,openai:timeout@I4,recraft:429@first", {"concept.char"}),
    "the 3D models": ("tripo:2000@first,tripo:read_timeout_after_send@T3", set()),
}


class Pilot:
    def __init__(self, live, ui) -> None:
        self.live, self.ui, self.api = live, ui, Api(live)
        self.pid = ""
        self.retried_in_browser: set[str] = set()
        self.seen: set[str] = set()
        self.shots = 0

    # ---------------------------------------------------------------------------------------------------------- what the app says
    def failed_steps(self) -> list[dict]:
        jobs = self.api.get("/api/jobs", project_id=self.pid, steps="true", limit=50)
        live_jobs = [j for j in jobs if j["job"]["state"] in ("running", "failed", "waiting_user", "paused")]
        return [s for j in live_jobs for s in j["steps"] if s["state"] == "failed" and not s["kind"].startswith("export.")]

    def gates(self, kind: str | None = None) -> list[dict]:
        return self.api.gates(self.pid, kind)

    def stage(self) -> str:
        return self.api.project(self.pid)["project"]["stage"]

    # ---------------------------------------------------------------------------------------------------------- what a person does
    def retry_in_browser(self, step: dict, tag: str) -> None:
        ui = self.ui
        stage = self.stage()
        ui.goto(f"/p/{self.pid}/{PAGE_OF_STAGE.get(stage, '')}".rstrip("/"))
        main = ui.page.locator("main")
        again = main.get_by_role("button", name="Try again")
        ui.wait_until(lambda: again.count() > 0, 30, f"a 'Try again' button on the {stage} page for {step['kind']}")
        hint = step["error"]["user_hint"]
        assert hint, f"{step['kind']} failed without a hint for the user"
        expect(main).to_contain_text(hint[:60])                                                     # the page says what happened, in the step's own plain words
        clean(ui, f"{stage} page after {step['kind']} failed", "main")
        assert not re.search(r"mock-[a-z]+-[0-9a-f]{6,}|stp_[0-9a-z]{10,}", main.inner_text()), "a request or step id leaks to the user"
        self.shots += 1
        ui.shot(f"faults_{tag}_{self.shots}_{step['kind'].replace('.', '_')}_failed")
        again.first.click()
        ui.wait_until(lambda: not any(s["id"] == step["id"] and s["state"] == "failed" for s in self.failed_steps()), 30, f"{step['kind']} to be tried again")
        self.retried_in_browser.add(step["kind"])
        ui.no_errors()

    def decide(self, gate: dict, tile: dict, action: str) -> None:
        r = self.api.c.post(f"/api/gates/{gate['id']}/decisions", json={"tile_id": tile["tile_id"], "action": action, "expected_version": tile["version"],
                                                                         "client_decision_id": "cli_" + uuid.uuid4().hex})
        assert r.status_code == 200, f"{action} {tile['tile_id']}: {r.status_code} {r.text[:300]}"
        body = r.json()
        if body.get("provisional"):                           # the heads-up ("Approve anyway"): a person reads it and confirms
            shown = body["decision"]["warnings_shown"]
            assert len(shown) <= 2, "at most two heads-ups"
            r = self.api.c.post(f"/api/gates/{gate['id']}/decisions/{body['decision']['id']}/confirm", json={"override_warnings": shown})
            assert r.status_code == 200, r.text[:300]

    # ---------------------------------------------------------------------------------------------------------- the flow
    def run(self, tag: str, deadline_s: float = 1500) -> dict:
        api, ui = self.api, self.ui
        ui.goto("/")
        ui.page.get_by_role("link", name="Start your first duo").click()
        ui.page.wait_for_selector(".brief-form")
        ui.page.get_by_label("Name this duo").fill("Faulty Picnic")
        ui.page.get_by_label("What is the idea?").fill("two friends sharing a rainy-day picnic")
        ui.page.get_by_label("Let Tripo make them automatically").check()
        ui.page.get_by_role("button", name="Start the plan").click()
        ui.page.wait_for_url(re.compile(r"#/p/prj_[^/]+/plan$"))
        self.pid = re.search(r"/p/(prj_[^/]+)/plan", ui.page.url).group(1)
        ui.wait_until(lambda: self.pid and self.stage() != "", 10, "the project")

        approved_concept = approved_all = picked = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline_s:
            for step in self.failed_steps():
                if step["id"] not in self.seen:
                    self.seen.add(step["id"])
                    self.retry_in_browser(step, tag)
            for gate in self.gates():
                kind = gate["kind"]
                tiles = gate["tiles"]
                if kind == "concept" and not approved_concept and tiles and not any(t["state"] in ("generating", "planned") for t in tiles):
                    ready = [t for t in tiles if t["state"] == "ready"]
                    assert ready, f"a plan to approve: {[t['state'] for t in tiles]}"
                    self.decide(gate, ready[0], "approve")
                    approved_concept = True
                elif kind == "part_board" and not approved_all and not any(t["state"] in ("generating", "planned") for t in tiles):
                    stuck = [t for t in tiles if t["state"] in ("needs_human", "failed")]
                    assert not stuck, f"tiles that a person has to look at: {[(t['tile_id'], t['state'], t['facts']) for t in stuck]}"
                    self.decide(gate, tiles[0], "approve_all")
                    approved_all = True
                elif kind == "final_pick" and not picked:
                    self.decide(gate, tiles[0], "pick")
                    api.post(f"/api/projects/{self.pid}/export")
                    picked = True
                elif kind not in ("concept", "part_board", "final_pick"):
                    raise AssertionError(f"a {kind} gate opened that this flow does not expect: {json.dumps(tiles)[:400]}")
            state = api.get(f"/api/exports/{self.pid}")
            if state["status"] in ("blocked", "done", "failed"):
                return state
            ui.page.wait_for_timeout(1500)
        raise AssertionError(f"the flow did not reach the export in {deadline_s:g}s: stage {self.stage()}, failed steps {[s['kind'] for s in self.failed_steps()]}")


@pytest.mark.timeout(2400)
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_a_flow_with_faults_still_ends_in_the_export_preview_and_every_failure_was_said_in_plain_words(scenario, start_app, open_page):
    spec, must_retry = SCENARIOS[scenario]
    live = start_app(faults=spec)
    ui = open_page(live)
    pilot = Pilot(live, ui)
    tag = re.sub(r"[^a-z0-9]+", "_", scenario.lower()).strip("_")

    export = pilot.run(tag)

    # every fault that was asked for really fired, so the run above is the run of the faults and not of a clean app
    fired = {(f["provider"], f["fault"]) for f in faults.default_injector().fired}
    wanted = {tuple(entry.split("@")[0].split(":")) for entry in spec.split(",")}
    assert wanted <= fired, f"faults that never fired: {wanted - fired}"
    assert must_retry <= pilot.retried_in_browser, f"steps that had to be retried in the browser: {must_retry - pilot.retried_in_browser}"

    # the end of the flow is the demo export preview with the checklist; nothing was written; the page is clean
    assert export["status"] == "blocked" and export["mock"] is True and export["preview"]["checklist"]["items"]
    ui.goto(f"/p/{pilot.pid}/export")
    expect(ui.page.get_by_role("heading", name="Export preview: nothing was written")).to_be_visible(timeout=30000)
    clean(ui, "export preview")
    ui.shot(f"faults_{tag}_export_preview", full=False)
    states = {p["id"]: p["state"] for p in pilot.api.project(pilot.pid)["parts"]}
    assert {s for k, s in states.items() if k != "duo"} == {"built"}, states

    # no step is charged twice for one call, and the cost bar equals the ledger
    rows = [r for r in pilot.api.get("/api/costs", project_id=pilot.pid, limit=5000)["rows"] if r["state"] in ("committed", "orphan") and r["step_id"]]
    keys = [(r["step_id"], r["attempt"], r["operation"], r.get("request_id")) for r in rows]
    assert len(keys) == len(set(keys)), "a call was charged twice"
    totals = pilot.api.get("/api/costs", project_id=pilot.pid)["totals"]
    assert abs(totals["spent_usd"] - pilot.api.project(pilot.pid)["project"]["spent_usd"]) < 1e-6
    ui.no_errors()
