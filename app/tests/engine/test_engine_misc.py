"""Heartbeat (§8.4), keep-awake, restartability, startup maintenance and plug-in loading."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from helpers_f import add_step, new_job, ok_handler, state_of, wait_state

from duoskin import winplat
from duoskin.engine import registry
from duoskin.engine.heartbeat import Heartbeat
from duoskin.engine.registry import StepResult, register_handler
from duoskin.engine.testkit import make_app, make_client, wait_for
from duoskin.models.common import utcnow
from duoskin.models.job import StepState


def test_heartbeat_extends_leases_of_running_steps_it_owns(rt):
    register_handler("t.ok", ok_handler)
    s = add_step(rt, new_job(rt), "t.ok")
    claimed = rt.ops.claim_one("cpu", rt.instance_id)
    other = add_step(rt, new_job(rt), "t.ok", params={"n": 2})
    foreign = rt.ops.claim_one("cpu", "someone-else")
    before = rt.repo.get_step(s.id).lease_until
    time.sleep(0.01)
    assert rt.heartbeat.beat() == 1                                                      # only this instance's step
    after = rt.repo.get_step(s.id).lease_until
    assert after > before and rt.repo.get_step(foreign.id).lease_until < after
    assert claimed.lease_until - utcnow() > timedelta(seconds=100) and other.id == foreign.id


def test_heartbeat_thread_beats_on_its_interval_and_stops(rt):
    hb = Heartbeat(rt, interval_s=0.05)
    hb.start()
    wait_for(lambda: hb.beats >= 3, 3, message="three beats")
    hb.stop()
    n = hb.beats
    time.sleep(0.2)
    assert hb.beats == n


def test_a_long_blocking_call_keeps_its_lease_through_the_heartbeat(rt):
    rt.heartbeat = Heartbeat(rt, interval_s=0.05)
    release = threading.Event()
    seen = {}

    def slow(ctx, p, i):
        first = rt.repo.get_step(ctx.step.id).lease_until
        release.wait(0.4)                                                               # no progress() calls at all
        seen["grew"] = rt.repo.get_step(ctx.step.id).lease_until > first
        return StepResult()

    register_handler("t.slow", slow)
    rt.heartbeat.start()
    rt.scheduler.start()
    wait_state(rt, add_step(rt, new_job(rt), "t.slow"), StepState.SUCCEEDED)
    rt.heartbeat.stop()
    assert seen["grew"] is True


def test_keep_awake_is_on_while_work_is_pending_and_off_when_idle(rt, monkeypatch):
    calls = []
    monkeypatch.setattr(winplat, "keep_awake", lambda on: calls.append(on))
    release = threading.Event()
    register_handler("t.hold", lambda ctx, p, i: release.wait(5) and StepResult())
    s = add_step(rt, new_job(rt), "t.hold")
    rt.scheduler.tick()
    assert calls[-1] is True
    release.set()
    wait_state(rt, s, StepState.SUCCEEDED)
    rt.scheduler.tick()
    assert calls[-1] is False and calls.count(True) == 1


def test_tick_is_deterministic_and_dispatches_up_to_pool_capacity(rt):
    release = threading.Event()
    running = []
    register_handler("t.hold", lambda ctx, p, i: (running.append(1), release.wait(5))[1] and StepResult(), pool="proc")
    job = new_job(rt)
    steps = [add_step(rt, job, "t.hold", params={"n": n}) for n in range(3)]
    assert rt.scheduler.tick() == 1                                                      # the proc pool has one slot
    wait_for(lambda: running, 3)
    assert rt.scheduler.tick() == 0
    release.set()
    for s in steps:
        wait_for(lambda s=s: (rt.scheduler.tick(), state_of(rt, s) == StepState.SUCCEEDED)[1], 5, message="all three to finish")


def test_the_scheduler_can_be_stopped_and_started_again(rt):
    register_handler("t.ok", ok_handler)
    rt.scheduler.start()
    wait_state(rt, add_step(rt, new_job(rt), "t.ok", params={"n": 1}), StepState.SUCCEEDED)
    rt.scheduler.stop()
    assert not rt.scheduler.running
    rt.scheduler.start()
    wait_state(rt, add_step(rt, new_job(rt), "t.ok", params={"n": 2}), StepState.SUCCEEDED)


def test_the_app_can_be_started_twice_with_the_same_runtime(tmp_path):
    app = make_app(tmp_path / "h")
    for _ in range(2):
        with make_client(app) as c:
            assert c.get("/api/health").json()["ok"] is True
            assert c.get("/api/state").json()["queue"]["scheduler_running"] is True


def test_startup_takes_a_backup_and_prunes_events(tmp_path):
    from duoskin.engine.testkit import make_runtime

    rt = make_runtime(tmp_path / "h")
    rt.repo.kv_set("x", 1)
    rt.startup(start_threads=False, backup=True)
    assert len(list(rt.paths.backups_dir.glob("duoskin-*.sqlite3"))) == 1
    rt.shutdown()


def test_startup_recovers_steps_left_running_by_a_previous_process(tmp_path):
    from duoskin.engine.testkit import make_runtime

    first = make_runtime(tmp_path / "h")
    register_handler("t.ok", ok_handler)
    s = add_step(first, new_job(first), "t.ok")
    first.ops.claim_one("cpu", "dead-process")                                             # the previous process died mid-step
    first.shutdown()
    second = make_runtime(tmp_path / "h")
    second.startup(start_threads=True, backup=False)
    try:
        done = wait_state(second, s, StepState.SUCCEEDED)
        assert done.attempt == 2
    finally:
        second.shutdown()


def test_plugins_are_loaded_and_their_register_hook_called(tmp_path, monkeypatch):
    import sys
    import types

    from duoskin import app as app_mod

    called = []
    plugin = types.ModuleType("duoskin_fake_pipeline")
    plugin.register = lambda rt: (called.append(rt), register_handler("plug.step", ok_handler))
    monkeypatch.setitem(sys.modules, "duoskin_fake_pipeline", plugin)
    monkeypatch.setattr(app_mod, "PLUGIN_MODULES", ("duoskin_fake_pipeline", "duoskin_missing_module"))
    app = make_app(tmp_path / "h")
    assert app.state.plugins == ["duoskin_fake_pipeline"] and called == [app.state.rt] and registry.find("plug.step") is not None


def test_a_broken_plugin_does_not_stop_the_app(tmp_path, monkeypatch):
    import sys
    import types

    from duoskin import app as app_mod

    bad = types.ModuleType("duoskin_bad_plugin")
    bad.register = lambda rt: 1 / 0
    monkeypatch.setitem(sys.modules, "duoskin_bad_plugin", bad)
    monkeypatch.setattr(app_mod, "PLUGIN_MODULES", ("duoskin_bad_plugin",))
    app = make_app(tmp_path / "h")
    assert app.state.plugins == [] and make_client(app).get("/api/health").status_code == 200


def test_optional_routers_are_picked_up_when_a_module_appears(tmp_path, monkeypatch):
    import sys
    import types

    from fastapi import APIRouter

    router = APIRouter(prefix="/api")
    router.add_api_route("/library", lambda: {"kits": []}, methods=["GET"])
    mod = types.ModuleType("duoskin.api.library")
    mod.router = router
    monkeypatch.setitem(sys.modules, "duoskin.api.library", mod)
    app = make_app(tmp_path / "h")
    assert make_client(app).get("/api/library").json() == {"kits": []}


def test_register_handler_contract_helpers():
    h = register_handler("t.contract", ok_handler, version=3, pool="api", paid=True, provider="openai", estimate=lambda p: 0.5)
    assert registry.get("t.contract") is h and h.version == 3 and h.pool == "api" and h.paid and h.provider == "openai"
    assert h.estimate(None) == 0.5 and h.cache_fields({}, []) == {}
    with pytest.raises(NotImplementedError):
        h.poll(None, {}, "ref")
    with pytest.raises(ValueError):
        register_handler("t.bad_pool", ok_handler, pool="gpu")
    with pytest.raises(ValueError):
        registry.register(h, replace=False)
    with pytest.raises(registry.UnknownStepKind):
        registry.get("nope")
    assert "t.contract" in registry.registered_kinds()
    registry.unregister("t.contract")
    assert registry.find("t.contract") is None
