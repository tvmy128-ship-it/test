"""Health, state, settings, keys, costs, doctor, shutdown, static files and the 501 stubs."""
from __future__ import annotations

import time
import zipfile

import pytest

from duoskin import __version__
from duoskin.engine.registry import StepResult, register_handler
from duoskin.engine.testkit import wait_for


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["version"] == __version__ and len(body["instance_id"]) == 32 and body["demo"] is True
    assert set(body) == {"ok", "version", "instance_id", "demo"}


def test_health_is_fast(client):
    t = time.monotonic()
    for _ in range(20):
        client.get("/api/health")
    assert (time.monotonic() - t) / 20 < 0.25                                             # CHK-X05: p95 < 1 s


def test_state_snapshot_shape_and_content(client, rt):
    p = client.post("/api/projects", json={"name": "Snap", "combo": "gb"}).json()
    register_handler("t.ok", lambda ctx, a, b: StepResult())
    job = rt.scheduler.submit_job("parts", p["id"], {}, steps=[rt.ops.new_step("t.ok", job_id="x", project_id=p["id"])])
    wait_for(lambda: rt.repo.get_job(job.id).state.value == "succeeded", 5)
    st = client.get("/api/state").json()
    assert set(st) >= {"version", "instance_id", "demo", "max_event_id", "projects", "jobs", "steps", "open_gates", "queue", "doctor", "today_usd"}
    assert [x["name"] for x in st["projects"]] == ["Snap"] and st["projects"][0]["waiting_on_user"] == 0
    assert st["jobs"][0]["job"]["id"] == job.id and st["jobs"][0]["steps_by_state"] == {"succeeded": 1}
    assert st["steps"] == {"succeeded": 1} and st["max_event_id"] == rt.bus.max_event_id()
    assert st["queue"]["scheduler_running"] is True and st["queue"]["paused"] is None and st["doctor"] is None


# ------------------------------------------------------------------------------------------------- settings
def test_settings_roundtrip(client, rt):
    s = client.get("/api/settings").json()
    assert s["port"] == 8765 and s["budgets"]["per_duo_usd"] == 15.0 and s["telemetry"] == "off"
    r = client.put("/api/settings", json={"budgets": {"per_duo_usd": 22.5}, "providers": {"modes": {"tripo": "mock"}}})
    assert r.status_code == 200 and r.json()["budgets"]["per_duo_usd"] == 22.5 and r.json()["budgets"]["ask_above_usd"] == 2.0
    assert client.get("/api/settings").json()["providers"]["modes"]["tripo"] == "mock"
    from duoskin import config

    assert config.load_settings(rt.paths.home).budgets.per_duo_usd == 22.5                     # persisted atomically


@pytest.mark.parametrize("patch", [{"telemetry": "on"}, {"nonsense": 1}, {"budgets": {"per_duo_usd": -3}},
                                   {"models": {"planner": "claude-opus-latest"}}, {"port": 99999}])
def test_bad_settings_are_422_and_change_nothing(client, patch):
    before = client.get("/api/settings").json()
    r = client.put("/api/settings", json=patch)
    assert r.status_code == 422 and r.json()["error"] == "bad_settings"
    assert client.get("/api/settings").json() == before


def test_demo_mode_toggle_shows_in_health(client):
    client.put("/api/settings", json={"demo_mode": False, "providers": {"modes": {"anthropic": "real"}}})
    assert client.get("/api/settings").json()["demo_mode"] is False


# ------------------------------------------------------------------------------------------------- keys
SECRET = "sk-ant-api03-" + "Sup3rS3cretKeyValue" * 3


def test_keys_are_masked_and_never_echoed(client, rt, tmp_path):
    r = client.put("/api/keys/anthropic", json={"value": SECRET})
    assert r.status_code == 200 and r.json()["set"] is True and r.json()["masked"].startswith("sk-…")
    assert SECRET not in r.text
    listing = client.get("/api/keys")
    assert SECRET not in listing.text and listing.json()["anthropic"]["set"] and not listing.json()["openai"]["set"]
    assert set(listing.json()) == {"anthropic", "openai", "tripo", "recraft", "gemini", "fal"}
    assert listing.json()["recraft"]["test_cost_note"] and "$0.08" in listing.json()["recraft"]["test_cost_note"]
    assert SECRET not in client.get("/api/settings").text and SECRET not in client.get("/api/state").text
    assert rt.keys.get_key("anthropic") == SECRET
    assert client.delete("/api/keys/anthropic").json()["set"] is False


@pytest.mark.parametrize("value,status", [("", 422), ("x" * 1281, 422), ("a\nb", 422)])
def test_bad_key_values_are_refused(client, value, status):
    r = client.put("/api/keys/openai", json={"value": value})
    assert r.status_code == status and r.json()["error"] == "bad_key"
    assert client.get("/api/keys").json()["openai"]["set"] is False


def test_unknown_provider_and_extra_fields(client):
    assert client.put("/api/keys/nope", json={"value": "abc"}).status_code == 404
    assert client.delete("/api/keys/nope").status_code == 404
    assert client.put("/api/keys/openai", json={"value": "abcdefghijkl", "extra": 1}).status_code == 422
    assert client.post("/api/keys/nope/test").status_code == 404


def test_test_key_in_mock_mode_makes_no_call_and_is_remembered(client):
    r = client.post("/api/keys/openai/test")
    assert r.status_code == 200 and r.json()["ok"] is True and "no call" in r.json()["message"]
    assert client.get("/api/keys").json()["openai"]["last_test"]["ok"] is True


def test_test_key_real_mode_without_a_key_and_with_a_failing_adapter(client, rt, monkeypatch):
    client.put("/api/settings", json={"providers": {"modes": {"openai": "real"}}})
    rt.provider_override = {}
    rt._effective = None
    r = client.post("/api/keys/openai/test").json()
    assert r["ok"] is False and "No key" in r["message"]
    client.put("/api/keys/openai", json={"value": SECRET})

    import sys
    import types

    boom = types.SimpleNamespace(get=lambda name: types.SimpleNamespace(test_key=lambda: (_ for _ in ()).throw(RuntimeError(f"401 for {SECRET}"))))
    monkeypatch.setitem(sys.modules, "duoskin.providers", types.SimpleNamespace(registry=boom))
    monkeypatch.setitem(sys.modules, "duoskin.providers.registry", boom)
    r = client.post("/api/keys/openai/test")
    assert r.json()["ok"] is False and SECRET not in r.text                                # the failure message never carries the key
    fine = types.SimpleNamespace(get=lambda name: types.SimpleNamespace(test_key=lambda: {"ok": True, "message": "fine"}))
    monkeypatch.setitem(sys.modules, "duoskin.providers", types.SimpleNamespace(registry=fine))
    monkeypatch.setitem(sys.modules, "duoskin.providers.registry", fine)
    ok = client.post("/api/keys/openai/test").json()
    assert ok["ok"] is True and ok["message"] == "fine"


def test_a_new_key_lifts_a_provider_pause(client, rt):
    rt.scheduler.pause_provider("recraft", "key rejected")
    assert client.get("/api/keys").json()["recraft"]["paused"] == "key rejected"
    client.put("/api/keys/recraft", json={"value": "r" * 24})
    assert "recraft" not in rt.scheduler.paused_providers and client.get("/api/keys").json()["recraft"]["paused"] is None


# ------------------------------------------------------------------------------------------------- costs
def test_costs_listing(client, rt):
    from duoskin.models.common import utcnow
    from duoskin.models.cost import CostEntry

    p = client.post("/api/projects", json={"name": "Money", "combo": "bb"}).json()
    rt.budget.add_entry(CostEntry(ts=utcnow(), project_id=p["id"], step_id="s1", attempt=1, provider="openai", model="m", operation="op1", usd=0.25))
    rt.budget.add_entry(CostEntry(ts=utcnow(), project_id=p["id"], step_id="s2", attempt=1, provider="tripo", model="m", operation="op2", usd=1.1, credits=110))
    r = client.get(f"/api/costs?project_id={p['id']}").json()
    assert len(r["rows"]) == 2 and r["totals"]["spent_usd"] == 1.35 and r["totals"]["by_provider"] == {"openai": 0.25, "tripo": 1.1}
    assert r["tripo_available_credits"] is None and "price_table" in r
    assert client.get("/api/costs?project_id=prj_other").json()["rows"] == []
    assert client.get("/api/costs?from=2999-01-01").json()["rows"] == []
    assert client.get("/api/projects/" + p["id"]).json()["project"]["spent_usd"] == 1.35


# ------------------------------------------------------------------------------------------------- doctor and diagnostics
def test_doctor_run_and_report(client, rt):
    assert client.get("/api/doctor").json()["ran"] is False
    r = client.post("/api/doctor/run?quick=true")
    assert r.status_code == 202 and r.json()["started"] is True
    report = wait_for(lambda: (d := client.get("/api/doctor").json())["ran"] and not d["running"] and d, 60, message="the doctor to finish")
    assert report["checks"][0]["id"] == "CHK-S01" and "summary" in report and report["blocks_paid_features"] is False
    assert [e for e in rt.bus.events_after(0) if e.type == "doctor.result"]
    assert rt.repo.kv_get("doctor.last")["exit_code"] == report["exit_code"]                     # saved to the DB
    st = client.get("/api/state").json()
    assert st["doctor"]["summary"] == report["summary"]


def test_diagnostics_zip_is_redacted(client, rt, tmp_path, monkeypatch):
    from duoskin.engine import diagnostics

    monkeypatch.setattr(diagnostics.config, "exports_root", lambda s: tmp_path / "exports")
    client.put("/api/keys/anthropic", json={"value": SECRET})
    import logging

    logging.getLogger("duoskin.test").warning("leak? %s", SECRET)
    (rt.paths.logs_dir / "duoskin.log").write_text(f"line with {SECRET}\n", encoding="utf-8")
    r = client.post("/api/diagnostics")
    path = r.json()["path"]
    assert path.endswith(".zip")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        blob = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
    assert {"info.json", "failing_steps.json", "doctor.json"} <= set(names) and "logs/duoskin.log" in names
    assert SECRET not in blob and "[REDACTED]" in blob


# ------------------------------------------------------------------------------------------------- shutdown
def test_shutdown_returns_202_then_calls_the_host(client, rt):
    called = []
    rt.on_shutdown = lambda: called.append(1)
    r = client.post("/api/shutdown")
    assert r.status_code == 202 and r.json()["ok"] is True
    wait_for(lambda: called, 3, message="the shutdown callback")


def test_shutdown_requires_the_token(anon, client):
    assert anon.post("/api/shutdown").status_code == 403


# ------------------------------------------------------------------------------------------------- static, stubs, errors
def test_index_and_static_files(client, app):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "text/html" in r.headers["content-type"]
    assert f'content="{app.state.rt.token}"' in r.text and "__DUOSKIN_TOKEN__" not in r.text
    assert "content-security-policy" in r.headers
    js = client.get("/web/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert client.get("/web/shell.css").headers["content-type"].startswith("text/css")
    assert client.get("/web/../app.py").status_code == 404
    assert client.get("/web/nope.js").status_code == 404


def test_every_launch_gets_a_fresh_token(tmp_path):
    from duoskin.engine.testkit import make_app

    a, b = make_app(tmp_path / "a"), make_app(tmp_path / "b")
    assert a.state.rt.token != b.state.rt.token and len(a.state.rt.token) >= 40


def test_openapi_docs_exist_only_in_dev_mode(tmp_path):
    from duoskin.engine.testkit import make_app, make_client

    prod = make_client(make_app(tmp_path / "p"))
    assert prod.get("/api/docs").status_code == 404 and prod.get("/api/openapi.json").status_code == 404
    from duoskin import config

    config.save_settings(config.load_settings(tmp_path / "d").model_copy(update={"dev_mode": True}), tmp_path / "d")
    dev = make_client(make_app(tmp_path / "d"))
    assert dev.get("/api/openapi.json").status_code == 200


@pytest.mark.parametrize("method,path", [("POST", "/api/projects/prj_x/export"), ("GET", "/api/exports/prj_x"), ("PATCH", "/api/exports/prj_x/checklist"),
                                         ("POST", "/api/projects/prj_x/parts/a.hair/tripo-pack")])
def test_the_pipeline_routes_are_no_longer_stubs(client, method, path):
    """These four were 501 stubs until the pipeline track filled them in (``api/exports.py``, ``api/parts.py``): they now answer for the project they are asked about."""
    r = client.request(method, path)
    assert r.status_code in (404, 422) and r.json()["error"] != "not_implemented"


def test_unknown_routes_and_methods(client):
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/shutdown").status_code == 405


def test_validation_errors_are_json(client):
    r = client.post("/api/projects", json={"name": "", "combo": "zz"})
    assert r.status_code == 422 and r.json()["error"] == "validation" and r.json()["detail"]


def test_not_found_and_conflict_shapes(client):
    r = client.get("/api/projects/prj_missing")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    p = client.post("/api/projects", json={"name": "X", "combo": "bg"}).json()
    r = client.patch(f"/api/projects/{p['id']}", json={"expected_version": 99, "name": "Y"})
    assert r.status_code == 409 and r.json()["error"] == "conflict" and r.json()["current"]["name"] == "X"
