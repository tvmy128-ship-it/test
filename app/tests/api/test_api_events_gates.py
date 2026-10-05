"""SSE and polling (§8.7) over a real loopback server, and the gate routes (§9.1, §9.9, §13)."""
from __future__ import annotations

import json
import threading

import httpx
import pytest

from duoskin.engine import deps
from duoskin.engine.gates import allowed_actions_for
from duoskin.engine.registry import StepResult, register_handler
from duoskin.engine.testkit import wait_for
from duoskin.models.common import new_id, utcnow
from duoskin.models.gate import Gate, GateKind, GateTile, TileState
from duoskin.models.part import Part, PartKind, PartState


# ------------------------------------------------------------------------------------------------- events
def parse_sse(lines):
    """Yield (id, data) pairs from an iterator of SSE lines."""
    ev_id, data = None, []
    for line in lines:
        if line == "":
            if data:
                yield ev_id, json.loads("\n".join(data))
            ev_id, data = None, []
        elif line.startswith("id:"):
            ev_id = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())


def test_poll_returns_events_after_an_id(client, rt):
    ids = [rt.bus.emit("toast", {"n": i}) for i in range(5)]
    r = client.get(f"/api/events/poll?after={ids[1]}").json()
    assert [e["id"] for e in r["events"]] == ids[2:] and r["max_event_id"] == ids[-1]
    assert r["events"][0]["type"] == "toast" and r["events"][0]["payload"] == {"n": 2} and r["events"][0]["ts"].endswith("Z")
    assert client.get("/api/events/poll?after=999999").json()["events"] == []
    assert len(client.get("/api/events/poll?limit=2").json()["events"]) == 2
    assert client.get("/api/events/poll?after=-1").status_code == 422


def test_sse_replays_the_backlog_then_closes_with_once(live_server):
    base, rt, _token = live_server
    ids = [rt.bus.emit("toast", {"n": i}) for i in range(3)]
    with httpx.Client(base_url=base, timeout=10) as c, c.stream("GET", "/api/events?after=0&once=1") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        got = list(parse_sse(r.iter_lines()))
    assert [int(i) for i, _ in got][-3:] == ids and all(d["id"] == int(i) for i, d in got)
    assert [d["payload"]["n"] for _, d in got if d["type"] == "toast"] == [0, 1, 2]


def test_sse_tails_live_events_and_honours_last_event_id(live_server):
    base, rt, _token = live_server
    first = rt.bus.emit("toast", {"n": "old"})
    received = []
    ready = threading.Event()

    def reader():
        with httpx.Client(base_url=base, timeout=20) as c, c.stream("GET", "/api/events", headers={"Last-Event-ID": str(first)}) as r:
            ready.set()
            for ev_id, data in parse_sse(r.iter_lines()):
                received.append((int(ev_id), data))
                if data["payload"].get("n") == "last":
                    return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait(10)
    wait_for(lambda: rt.bus.subscriber_count() == 1, 5, message="the subscription")
    a = rt.bus.emit("toast", {"n": "new1"})
    b = rt.bus.emit("toast", {"n": "last"})
    t.join(15)
    assert [i for i, _ in received] == [a, b]                                                    # Last-Event-ID skipped the old one, nothing doubled
    wait_for(lambda: rt.bus.subscriber_count() == 0, 5, message="the subscriber to be cleaned up")   # disconnect frees it


def test_sse_after_wins_when_there_is_no_header_and_header_wins_over_query(live_server):
    base, rt, _token = live_server
    ids = [rt.bus.emit("toast", {"n": i}) for i in range(4)]
    with httpx.Client(base_url=base, timeout=10) as c:
        with c.stream("GET", f"/api/events?after={ids[1]}&once=1") as r:
            assert [int(i) for i, _ in parse_sse(r.iter_lines())] == ids[2:]
        with c.stream("GET", "/api/events?after=0&once=1", headers={"Last-Event-ID": str(ids[2])}) as r:
            assert [int(i) for i, _ in parse_sse(r.iter_lines())] == ids[3:]


def test_shutting_down_the_bus_ends_open_streams(live_server):
    base, rt, _token = live_server
    done = threading.Event()

    def reader():
        with httpx.Client(base_url=base, timeout=20) as c, c.stream("GET", "/api/events") as r:
            for _ in r.iter_lines():
                pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    wait_for(lambda: rt.bus.subscriber_count() == 1, 5)
    rt.bus.close()
    assert done.wait(10)


def test_a_new_page_load_does_snapshot_plus_tail_without_gaps(live_server):
    """GET /api/state first, then the stream from max_event_id: events fired in between are not lost."""
    base, rt, _token = live_server
    rt.bus.emit("toast", {"n": 1})
    with httpx.Client(base_url=base, timeout=10) as c:
        snap = c.get("/api/state").json()
        mid = rt.bus.emit("toast", {"n": 2})                                                      # happens after the snapshot
        with c.stream("GET", f"/api/events?after={snap['max_event_id']}&once=1") as r:
            ids = [int(i) for i, _ in parse_sse(r.iter_lines())]
    assert ids == [mid]


# ------------------------------------------------------------------------------------------------- gates
def make_board(rt, project_id, warnings=None, with_spec=True):
    pids = ("a.shirt", "a.hair")
    for pid, kind in zip(pids, (PartKind.SHIRT, PartKind.HAIR), strict=True):
        from duoskin.engine.cas import make_prov

        asset = rt.cas.put(f"board-{pid}".encode(), "txt", prov=make_prov("code"))
        rt.repo.save_part(Part(id=pid, project_id=project_id, character="a", kind=kind, label=pid, state=PartState.READY,
                               deps=deps.default_dep_rules(kind.value, "a"), board_assets={"flat": asset.sha256}))
    tiles = [GateTile(tile_id=pid, part_id=pid, label=pid, state=TileState.READY,
                      facts={"warnings": warnings} if warnings and pid == pids[0] else {},
                      allowed_actions=allowed_actions_for(GateKind.PART_BOARD, kind.value, can_manual=True))
             for pid, kind in zip(pids, (PartKind.SHIRT, PartKind.HAIR), strict=True)]
    return rt.gates.open_gate(Gate(id="", project_id=project_id, job_id="j", kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow()))


@pytest.fixture
def project(client):
    return client.post("/api/projects", json={"name": "G", "combo": "bg"}).json()


def decision(tile="a.shirt", action="approve", version=0, **kw):
    return {"tile_id": tile, "action": action, "expected_version": version, "client_decision_id": new_id("cid"), **kw}


def test_get_gate_and_list(client, rt, project):
    gate = make_board(rt, project["id"])
    g = client.get(f"/api/gates/{gate.id}").json()
    assert g["kind"] == "part_board" and [t["tile_id"] for t in g["tiles"]] == ["a.shirt", "a.hair"] and g["state"] == "open"
    assert [x["id"] for x in client.get(f"/api/gates?project_id={project['id']}").json()] == [gate.id]
    assert client.get("/api/gates?state=decided").json() == []
    assert client.get("/api/gates/gat_missing").status_code == 404
    assert client.get(f"/api/projects/{project['id']}").json()["open_gates"][0]["id"] == gate.id
    assert client.get("/api/projects").json()[0]["waiting_on_user"] == 1
    assert client.get("/api/state").json()["open_gates"][0]["id"] == gate.id


def test_approve_through_the_api_stamps_the_part(client, rt, project):
    gate = make_board(rt, project["id"])
    r = client.post(f"/api/gates/{gate.id}/decisions", json=decision())
    assert r.status_code == 200
    body = r.json()
    assert body["provisional"] is False and body["replay"] is False and body["released_warnings"] == []
    d = body["decision"]
    assert d["action"] == "approve" and d["approval"]["part_id"] == "a.shirt" and len(d["approval"]["approval_hash"]) == 64
    assert d["approval"]["build_hash"] is None
    part = client.get(f"/api/projects/{project['id']}/parts/a.shirt").json()["part"]
    assert part["state"] == "approved" and part["approval"]["approval_hash"] == d["approval"]["approval_hash"]
    tile = client.get(f"/api/gates/{gate.id}").json()["tiles"][0]
    assert tile["state"] == "approved" and tile["version"] == 1


def test_double_click_and_conflicts(client, rt, project):
    gate = make_board(rt, project["id"])
    body = decision("a.hair", "change", text="rounder")
    first = client.post(f"/api/gates/{gate.id}/decisions", json=body).json()
    again = client.post(f"/api/gates/{gate.id}/decisions", json=body).json()
    assert again["replay"] is True and again["decision"]["id"] == first["decision"]["id"]
    stale = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.hair", "reimagine", version=0))
    assert stale.status_code == 409 and stale.json()["error"] == "version_conflict"
    assert stale.json()["current"]["tiles"][1]["version"] == 1                                    # the UI refreshes from this
    assert client.post(f"/api/gates/{gate.id}/decisions", json=decision("nope")).status_code == 404
    bad = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "pick"))
    assert bad.status_code == 422 and bad.json()["error"] == "action_not_allowed"
    assert client.post(f"/api/gates/{gate.id}/decisions", json={"tile_id": "a.shirt", "action": "approve"}).status_code == 422
    assert client.post(f"/api/gates/{gate.id}/decisions", json=decision(action="reject")).status_code == 422     # S8: there is no reject
    rt.gates.close_gate(gate.id)
    closed = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "change"))
    assert closed.status_code == 409 and closed.json()["error"] == "gate_closed"


def test_warnings_are_withheld_released_after_the_first_choice_and_confirmed(client, rt, project):
    warns = [{"id": "w1", "text": "The sleeves look alike.", "severity": "low", "catch_rate": 0.4},
             {"id": "w2", "text": "Prints are busy.", "severity": "high", "catch_rate": 0.8},
             {"id": "w3", "text": "Third", "severity": "low", "catch_rate": 0.1}]
    gate = make_board(rt, project["id"], warnings=warns)
    assert "warnings" not in client.get(f"/api/gates/{gate.id}").json()["tiles"][0]["facts"]       # nothing before the first choice
    r = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "approve")).json()
    assert r["provisional"] is True and [w["id"] for w in r["released_warnings"]] == ["w2", "w1"]    # at most 2, by catch rate
    dec_id = r["decision"]["id"]
    shown = client.get(f"/api/gates/{gate.id}").json()["tiles"][0]["facts"]["warnings"]
    assert {w["id"] for w in shown} == {"w2", "w1"} and "w3" not in json.dumps(client.get(f"/api/gates/{gate.id}").json())
    assert client.get(f"/api/projects/{project['id']}/parts/a.shirt").json()["part"]["state"] == "ready"     # nothing started yet
    bad = client.post(f"/api/gates/{gate.id}/decisions/{dec_id}/confirm", json={"override_warnings": ["w3"]})
    assert bad.status_code == 422
    ok = client.post(f"/api/gates/{gate.id}/decisions/{dec_id}/confirm", json={"override_warnings": ["w2"]})
    assert ok.status_code == 200 and ok.json()["provisional"] is False and ok.json()["warnings_overridden"] == ["w2"] and ok.json()["approval"]
    assert client.get(f"/api/projects/{project['id']}/parts/a.shirt").json()["part"]["state"] == "approved"
    labels = rt.db.conn().execute("SELECT kind, source FROM labels").fetchall()
    assert [(x["kind"], x["source"]) for x in labels] == [("warning_override", "gate")]
    assert client.post(f"/api/gates/{gate.id}/decisions/{dec_id}/confirm", json={}).status_code == 409


def test_go_back_deletes_the_provisional_decision(client, rt, project):
    gate = make_board(rt, project["id"], warnings=[{"id": "w1", "text": "x"}])
    r = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "approve")).json()
    assert r["provisional"] is True
    assert client.delete(f"/api/gates/{gate.id}/decisions/{r['decision']['id']}").status_code == 204
    assert client.get(f"/api/gates/{gate.id}").json()["tiles"][0]["state"] == "ready"
    assert client.delete(f"/api/gates/{gate.id}/decisions/{r['decision']['id']}").status_code == 404
    final = client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "change", text="x")).json()["decision"]
    assert client.delete(f"/api/gates/{gate.id}/decisions/{final['id']}").status_code == 409        # only provisional decisions can go back


def test_budget_gate_through_the_api(client, rt, project):
    register_handler("t.big", lambda ctx, a, b: StepResult(result={"ran": True}), paid=True, provider="anthropic", estimate=lambda p: 3.0, pool="api")
    job = rt.scheduler.submit_job("parts", project["id"], {})
    s = rt.ops.new_step("t.big", job_id=job.id, project_id=project["id"])
    rt.scheduler.spawn(job.id, [s])
    gate = wait_for(lambda: next(iter(client.get(f"/api/gates?project_id={project['id']}").json()), None), 5, message="the BUDGET gate")
    assert gate["kind"] == "budget" and gate["tiles"][0]["facts"]["estimate_usd"] == 3.0
    assert client.get("/api/state").json()["jobs"][0]["steps_by_state"] == {"waiting_user": 1}
    assert client.get("/api/projects").json()[0]["waiting_on_user"] == 1
    bad = client.post(f"/api/gates/{gate['id']}/decisions", json=decision(s.id, "raise_cap"))
    assert bad.status_code == 422                                                                    # needs the new cap
    ok = client.post(f"/api/gates/{gate['id']}/decisions", json=decision(s.id, "continue"))
    assert ok.status_code == 200
    wait_for(lambda: rt.repo.get_step(s.id).state.value == "succeeded", 5)
    assert client.get(f"/api/gates/{gate['id']}").json()["state"] == "decided"
    assert client.get("/api/projects").json()[0]["waiting_on_user"] == 0


def test_gate_events_reach_the_ui(client, rt, project):
    gate = make_board(rt, project["id"])
    client.post(f"/api/gates/{gate.id}/decisions", json=decision("a.shirt", "change", text="x"))
    types = [e.type for e in rt.bus.events_after(0)]
    assert "gate.opened" in types and "tile.updated" in types and "gate.updated" in types
    tile_event = next(e for e in rt.bus.events_after(0) if e.type == "tile.updated")
    assert tile_event.payload["tile_id"] == "a.shirt" and tile_event.payload["version"] == 1 and tile_event.project_id == project["id"]
