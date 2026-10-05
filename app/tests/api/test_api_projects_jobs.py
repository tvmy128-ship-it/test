"""Projects, references, plan start, pause/resume/archive, specs, jobs, steps and the queue."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from duoskin.engine import scheduler as scheduler_mod
from duoskin.engine.registry import StepResult, register_handler
from duoskin.engine.testkit import png_bytes, wait_for
from duoskin.models.common import new_id, sha256_of
from duoskin.models.spec_record import SpecRecord


def mk(client, **kw):
    body = {"name": "Plush Koi", "combo": "bg", "brief": "koi duo", **kw}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------- create / read / patch
def test_create_copies_global_defaults_and_picks_a_unique_slug(client):
    p = mk(client)
    assert p["slug"] == "plush-koi" and p["stage"] == "brief" and p["version"] == 0 and p["id"].startswith("prj_")
    assert p["settings"]["budget_usd"] == 15.0 and p["settings"]["ask_above_usd"] == 2.0 and p["settings"]["reference_similarity_check"] is False
    assert p["settings"]["mesh_mode"] == "ask" and p["pins"] is None and p["spent_usd"] == 0.0
    assert mk(client)["slug"] == "plush-koi-2"
    client.put("/api/settings", json={"budgets": {"per_duo_usd": 9}, "three_d": {"default_mesh_mode": "manual"}})
    q = mk(client, name="Other")
    assert q["settings"]["budget_usd"] == 9 and q["settings"]["mesh_mode"] == "manual"                  # settings copied at creation
    r = mk(client, name="Third", settings={"budget_usd": 3.5, "reference_similarity_check": True})
    assert r["settings"]["budget_usd"] == 3.5 and r["settings"]["reference_similarity_check"] is True


@pytest.mark.parametrize("body", [{"name": "", "combo": "bg"}, {"name": "x", "combo": "mm"}, {"name": "x", "combo": "bg", "brief": "y" * 2001},
                                  {"name": "x" * 61, "combo": "bg"}, {"name": "x", "combo": "bg", "must_include": ["a"] * 6},
                                  {"name": "x", "combo": "bg", "must_include": [" ".join(["w"] * 13)]},
                                  {"name": "x", "combo": "bg", "structure_request": "bad value!"},
                                  {"name": "x", "combo": "bg", "settings": {"budget_usd": -1}},
                                  {"name": "x", "combo": "bg", "settings": {"typo": 1}}, {"name": "x", "combo": "bg", "extra": 1}])
def test_create_validation(client, body):
    assert client.post("/api/projects", json=body).status_code == 422


def test_get_project_bundle_and_list(client, rt):
    p = mk(client, must_include=["koi on the back", ""], structure_request="same_club")
    assert p["must_include"] == ["koi on the back"] and p["structure_request"] == "same_club"
    b = client.get(f"/api/projects/{p['id']}").json()
    assert b["project"]["id"] == p["id"] and b["parts"] == [] and b["spec"] is None and b["dna_card"] is None and b["open_gates"] == []
    lst = client.get("/api/projects").json()
    assert [x["id"] for x in lst] == [p["id"]] and lst[0]["waiting_on_user"] == 0 and "brief" not in lst[0]
    assert client.get("/api/projects/prj_missing").status_code == 404


def test_patch_name_settings_and_optimistic_lock(client):
    p = mk(client)
    r = client.patch(f"/api/projects/{p['id']}", json={"expected_version": 0, "name": "Renamed", "settings": {"budget_usd": 7}})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["settings"]["budget_usd"] == 7 and r.json()["version"] == 1
    stale = client.patch(f"/api/projects/{p['id']}", json={"expected_version": 0, "name": "Again"})
    assert stale.status_code == 409 and stale.json()["current"]["name"] == "Renamed"
    bad = client.patch(f"/api/projects/{p['id']}", json={"expected_version": 1, "settings": {"mesh_mode": "weird"}})
    assert bad.status_code == 422


def test_settings_lock_once_the_plan_started(client, rt):
    p = mk(client)
    from duoskin.models.project import Stage

    rt.repo.mutate_project(p["id"], lambda x: setattr(x, "stage", Stage.PLANNING))
    version = client.get(f"/api/projects/{p['id']}").json()["project"]["version"]
    r = client.patch(f"/api/projects/{p['id']}", json={"expected_version": version, "settings": {"budget_usd": 5}})
    assert r.status_code == 409 and r.json()["error"] == "settings_locked"
    assert client.patch(f"/api/projects/{p['id']}", json={"expected_version": version, "name": "ok"}).status_code == 200


def test_project_bundle_includes_current_spec_and_dna_card(client, rt):
    p = mk(client)
    spec = {"combo": "bg", "a": {"top": {"recipe_id": "tee"}}}
    rec = SpecRecord(id=new_id("spc"), project_id=p["id"], plan_set_id="ps", plan_index=0, spec=spec, sha256=sha256_of(spec))
    rt.repo.add_spec(rec)
    rt.repo.save_dna_card(rec.id, 1, p["id"], {"spec_id": rec.id, "version": 1, "palette_family": "warm"})
    rt.repo.mutate_project(p["id"], lambda x: setattr(x, "current_spec_id", rec.id))
    b = client.get(f"/api/projects/{p['id']}").json()
    assert b["spec"]["spec"] == spec and b["dna_card"]["palette_family"] == "warm"
    specs = client.get(f"/api/projects/{p['id']}/specs").json()
    assert len(specs) == 1 and specs[0]["spec"]["id"] == rec.id and specs[0]["dna_card"]["version"] == 1 and specs[0]["lint"] == []


def test_spec_diff(client, rt):
    p = mk(client)
    s1 = {"a": {"top": {"recipe_id": "tee", "fabric": "x"}}, "palette": [{"id": "p1", "hex": "#000000"}]}
    s2 = {"a": {"top": {"recipe_id": "polo", "fabric": "x"}}, "palette": [{"id": "p1", "hex": "#ffffff"}]}
    ids = []
    for s in (s1, s2):
        rec = SpecRecord(id=new_id("spc"), project_id=p["id"], plan_set_id="ps", plan_index=0, spec=s, sha256=sha256_of(s))
        rt.repo.add_spec(rec)
        ids.append(rec.id)
    rt.repo.save_dna_card(ids[0], 1, p["id"], {"palette_family": "cool"})
    rt.repo.save_dna_card(ids[1], 2, p["id"], {"palette_family": "warm"})
    d = client.get(f"/api/specs/{ids[0]}/diff/{ids[1]}").json()
    assert {c["path"]: (c["old"], c["new"]) for c in d["spec_changes"]} == {"/a/top/recipe_id": ("tee", "polo"), "/palette/0/hex": ("#000000", "#ffffff")}
    assert d["dna_changes"] == [{"path": "/palette_family", "old": "cool", "new": "warm"}]
    assert client.get(f"/api/specs/{ids[0]}/diff/spc_missing").status_code == 404


# ------------------------------------------------------------------------------------------------- references
def upload(client, pid, data, name="ref.png", role="reference", ctype="image/png"):
    return client.post(f"/api/projects/{pid}/references", files={"file": (name, data, ctype)}, data={"role": role, "note": "mood"})


def test_reference_upload_stores_a_png_in_the_cas(client, rt):
    p = mk(client)
    r = upload(client, p["id"], png_bytes(16, 16))
    assert r.status_code == 200 and r.json()["role"] == "reference" and r.json()["note"] == "mood"
    sha = r.json()["asset_sha"]
    assert rt.cas.get(sha) == png_bytes(16, 16)
    assert client.get(f"/api/projects/{p['id']}").json()["project"]["references"][0]["asset_sha"] == sha
    assert client.get(f"/cas/{sha}.png").status_code == 200


def test_reference_type_is_sniffed_from_the_content_not_the_name(client):
    p = mk(client)
    jpg = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(jpg, "JPEG")
    assert upload(client, p["id"], jpg.getvalue(), name="photo.png", ctype="image/png").status_code == 200       # a jpeg named .png is fine
    assert upload(client, p["id"], b"MZ\x90\x00 not an image", name="evil.png").status_code == 415
    assert upload(client, p["id"], b"<svg xmlns='http://www.w3.org/2000/svg'/>", name="x.svg", ctype="image/svg+xml").status_code == 415
    gif = io.BytesIO()
    Image.new("P", (4, 4)).save(gif, "GIF")
    assert upload(client, p["id"], gif.getvalue(), name="a.gif").status_code == 415


def test_reference_limits(client, monkeypatch):
    p = mk(client)
    assert upload(client, p["id"], png_bytes(), role="mystery").status_code == 422
    for i in range(4):
        assert upload(client, p["id"], png_bytes(8 + i, 8)).status_code == 200
    r = upload(client, p["id"], png_bytes(40, 40))
    assert r.status_code == 409 and r.json()["error"] == "too_many_references"
    from duoskin.api import projects as proj

    q = mk(client, name="Big")
    monkeypatch.setattr(proj, "MAX_UPLOAD_BYTES", 1000)
    r = upload(client, q["id"], b"\x89PNG" + b"0" * 5000)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    monkeypatch.setattr(proj, "MAX_PIXELS", 50)
    assert upload(client, q["id"], png_bytes(20, 20)).status_code == 413
    assert upload(client, "prj_missing", png_bytes()).status_code == 404


# ------------------------------------------------------------------------------------------------- plan, pause, archive
def test_plan_is_501_until_the_pipeline_registers_a_job_factory(client):
    p = mk(client)
    r = client.post(f"/api/projects/{p['id']}/plan")
    assert r.status_code == 501 and r.json()["error"] == "not_implemented"


def test_plan_runs_the_registered_job_factory_and_moves_the_stage(client, rt, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "_factories", dict(scheduler_mod._factories))
    release = []
    register_handler("t.plan", lambda ctx, a, b: StepResult() if release else (_ for _ in ()).throw(RuntimeError("held")), cacheable=False)

    def factory(rt_, job, project):
        return [rt_.ops.new_step("t.plan", job_id=job.id, project_id=project.id)]

    scheduler_mod.register_job_factory("plan", factory)
    release.append(1)
    p = mk(client)
    r = client.post(f"/api/projects/{p['id']}/plan")
    assert r.status_code == 200 and r.json()["kind"] == "plan" and r.json()["project_id"] == p["id"]
    job_id = r.json()["id"]
    got = client.get(f"/api/projects/{p['id']}").json()["project"]
    assert got["stage"] == "planning" and got["plan_job_id"] == job_id
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").json()["job"]["state"] == "succeeded", 5, message="the plan job")
    assert [e for e in rt.bus.events_after(0) if e.type == "project.stage" and e.payload.get("stage") == "planning"]
    again = client.post(f"/api/projects/{p['id']}/plan")                                   # finished job, stage is PLANNING: allowed again
    assert again.status_code == 200
    scheduler_mod._factories.clear()


def test_plan_refuses_a_second_running_plan_and_a_later_stage(client, rt, monkeypatch):
    import threading

    monkeypatch.setattr(scheduler_mod, "_factories", dict(scheduler_mod._factories))
    hold = threading.Event()
    register_handler("t.plan", lambda ctx, a, b: hold.wait(5) and StepResult(), cacheable=False)
    scheduler_mod.register_job_factory("plan", lambda r, job, project: [r.ops.new_step("t.plan", job_id=job.id, project_id=project.id)])
    p = mk(client)
    assert client.post(f"/api/projects/{p['id']}/plan").status_code == 200
    r = client.post(f"/api/projects/{p['id']}/plan")
    assert r.status_code == 409 and r.json()["error"] == "plan_running"
    hold.set()
    from duoskin.models.project import Stage

    q = mk(client, name="Later")
    rt.repo.mutate_project(q["id"], lambda x: setattr(x, "stage", Stage.GATE2))
    assert client.post(f"/api/projects/{q['id']}/plan").json()["error"] == "wrong_stage"


def test_pause_resume_archive(client, rt):
    p = mk(client)
    assert client.post(f"/api/projects/{p['id']}/pause").json()["paused"] is True
    assert client.post(f"/api/projects/{p['id']}/resume").json()["paused"] is False
    assert client.post(f"/api/projects/{p['id']}/archive").json()["archived"] is True
    assert client.get("/api/projects").json() == []
    assert [x["id"] for x in client.get("/api/projects?include_archived=true").json()] == [p["id"]]
    assert client.get(f"/api/projects/{p['id']}").status_code == 200                                    # archived, not deleted
    for action in ("pause", "resume", "archive"):
        assert client.post(f"/api/projects/prj_missing/{action}").status_code == 404


def test_parts_endpoint_returns_links_and_checks(client, rt):
    from duoskin.checks.model import CheckResult
    from duoskin.engine.cas import make_prov
    from duoskin.engine.deps import default_dep_rules
    from duoskin.models.asset import AssetLink
    from duoskin.models.part import Part, PartKind

    p = mk(client)
    asset = rt.cas.put(png_bytes(), "png", prov=make_prov("openai", model="gpt-image-2.5-flare-2026-09-08", prompt_id="I2"),
                       link=AssetLink(id="", asset_sha="0" * 64, project_id=p["id"], part_id="a.shirt", role="draft",
                                      provenance=make_prov("openai", model="gpt-image-2.5-flare-2026-09-08", prompt_id="I2")))
    rt.repo.save_part(Part(id="a.shirt", project_id=p["id"], character="a", kind=PartKind.SHIRT, label="A shirt",
                           deps=default_dep_rules("shirt", "a"), board_assets={"flat": asset.sha256}))
    rt.repo.insert_checks([CheckResult(check_id="A_ALPHA", kind="hard", passed=True, subject_sha=asset.sha256)], project_id=p["id"], step_id=None)
    r = client.get(f"/api/projects/{p['id']}/parts/a.shirt").json()
    assert r["part"]["id"] == "a.shirt" and r["links"][0]["role"] == "draft" and r["checks"][0]["check_id"] == "A_ALPHA"
    assert r["provenance"] == [{"asset_sha": asset.sha256, "role": "draft", "status": "candidate", "source": "openai",
                                "model": "gpt-image-2.5-flare-2026-09-08", "prompt_id": "I2", "cost_usd": None}]
    assert client.get(f"/api/projects/{p['id']}/parts/a.hair").status_code == 404
    assert client.get(f"/api/projects/{p['id']}").json()["parts"][0]["id"] == "a.shirt"


# ------------------------------------------------------------------------------------------------- jobs, steps, queue
def test_jobs_listing_filters_and_step_detail(client, rt):
    p = mk(client)
    register_handler("t.ok", lambda ctx, a, b: StepResult(result={"fine": 1}))
    j1 = rt.scheduler.submit_job("parts", p["id"], {}, steps=[rt.ops.new_step("t.ok", job_id="x", project_id=p["id"], part_id="a.face")])
    j2 = rt.scheduler.submit_job("library", None, {}, steps=[rt.ops.new_step("t.ok", job_id="x", params={"n": 2})])
    wait_for(lambda: all(rt.repo.get_job(j.id).state.value == "succeeded" for j in (j1, j2)), 5)
    allj = client.get("/api/jobs").json()
    assert {j["job"]["id"] for j in allj} == {j1.id, j2.id} and all(j["steps"] == [] for j in allj)
    assert [j["job"]["id"] for j in client.get(f"/api/jobs?project_id={p['id']}").json()] == [j1.id]
    assert [j["job"]["id"] for j in client.get("/api/jobs?state=succeeded&limit=1").json()] == [j2.id]
    with_steps = client.get("/api/jobs?steps=true").json()
    assert with_steps[0]["steps"][0]["kind"] == "t.ok"
    one = client.get(f"/api/jobs/{j1.id}").json()
    assert one["steps_by_state"] == {"succeeded": 1} and one["steps"][0]["part_id"] == "a.face" and one["steps"][0]["cached"] is False
    assert client.get("/api/jobs/job_missing").status_code == 404


def test_retry_and_cancel_through_the_api(client, rt):
    from duoskin.engine.errors import StepFailure

    state = {"fail": True}
    register_handler("t.maybe", lambda ctx, a, b: (_ for _ in ()).throw(StepFailure("no", kind="bad_request")) if state["fail"] else StepResult(), cacheable=False)
    job = rt.scheduler.submit_job("parts", None, {}, steps=[rt.ops.new_step("t.maybe", job_id="x")])
    step_id = rt.repo.list_steps(job_id=job.id)[0].id
    wait_for(lambda: rt.repo.get_step(step_id).state.value == "failed", 5)
    failed = client.get(f"/api/jobs/{job.id}").json()["steps"][0]
    assert failed["error"]["kind"] == "bad_request" and failed["error"]["user_hint"]
    state["fail"] = False
    r = client.post(f"/api/steps/{step_id}/retry")
    assert r.status_code == 200 and r.json()["state"] in ("ready", "running", "succeeded") and r.json()["attempt"] in (0, 1)
    wait_for(lambda: rt.repo.get_step(step_id).state.value == "succeeded", 5)
    assert client.post(f"/api/steps/{step_id}/retry").status_code == 409                         # only a FAILED step can be retried
    assert client.post("/api/steps/stp_missing/retry").status_code == 404
    assert client.post("/api/jobs/job_missing/cancel").status_code == 404


def test_cancel_a_waiting_job(client, rt):
    register_handler("t.ok", lambda ctx, a, b: StepResult())
    job = rt.scheduler.submit_job("parts", None, {})
    first = rt.ops.new_step("t.ok", job_id=job.id)
    rt.scheduler.spawn(job.id, [first, rt.ops.new_step("t.ok", job_id=job.id, deps=[first.id], params={"n": 1})])
    r = client.post(f"/api/jobs/{job.id}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled" and r.json()["finished_at"]
    states = {s.state.value for s in rt.repo.list_steps(job_id=job.id)}
    assert states <= {"cancelled", "succeeded"}


def test_queue_status_and_resume(client, rt):
    rt.scheduler.pause_queue("billing problem")
    rt.scheduler.pause_provider("recraft", "bad key")
    q = client.get("/api/queue").json()
    assert q["paused"] == "billing problem" and q["paused_providers"] == {"recraft": "bad key"} and q["scheduler_running"] is True
    r = client.post("/api/queue/resume").json()
    assert r["paused"] is None and r["paused_providers"] == {}


def test_focus_sets_priority_and_returns_204(client, rt):
    p = mk(client)
    register_handler("t.ok", lambda ctx, a, b: StepResult())
    rt.scheduler.pause(p["id"])                       # keep the steps unclaimed so their priority can be observed
    job = rt.scheduler.submit_job("parts", p["id"], {})
    s = rt.ops.new_step("t.ok", job_id=job.id, project_id=p["id"], part_id="a.hair")
    rt.scheduler.spawn(job.id, [s])
    r = client.post("/api/focus", json={"project_id": p["id"], "part_id": "a.hair"})
    assert r.status_code == 204 and r.content == b""
    assert rt.repo.get_step(s.id).priority == 10
    assert client.post("/api/focus", json={"project_id": p["id"], "extra": 1}).status_code == 422
