"""Security middleware (§13, SYS-02), CAS-serving helpers, and storage GC (§8.5, ENG-12, CHK-X03)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from helpers_unit import make_part, make_project
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from duoskin import security
from duoskin.engine import gc as gc_mod
from duoskin.engine.cas import make_prov
from duoskin.models.common import iso_utc, new_id, sha256_of, utcnow
from duoskin.models.part import ApprovalRecord

TOKEN = "t0k3n-" + "x" * 30


def make_app(port=8765, importmap=None):
    async def echo(request):
        return JSONResponse({"method": request.method})

    async def html(request):
        return HTMLResponse("<p>hi</p>")

    async def cas(request):
        return PlainTextResponse("x", headers={"Content-Security-Policy": "sandbox", "Cache-Control": "public, immutable"})

    async def api(request):
        return JSONResponse({})

    async def stream(request):
        async def gen():
            yield b"one"
            yield b"two"
        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/echo", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]), Route("/page", html),
                            Route("/cas/x", cas), Route("/api/x", api), Route("/stream", stream)])
    security.install(app, TOKEN, importmap, get_port=lambda: port)
    return app


@pytest.fixture
def client():
    return TestClient(make_app(), base_url="http://127.0.0.1:8765")


# ------------------------------------------------------------------------------------------------- token and origin
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_needs_the_token(client, method):
    r = client.request(method, "/echo")
    assert r.status_code == 403 and r.json() == {"error": "bad_token"}
    r = client.request(method, "/echo", headers={"X-DuoSkin-Token": "wrong"})
    assert r.status_code == 403 and r.json() == {"error": "bad_token"}
    assert client.request(method, "/echo", headers={"X-DuoSkin-Token": TOKEN}).status_code == 200


def test_safe_methods_need_no_token(client):
    assert client.get("/echo").status_code == 200 and client.head("/echo").status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "http://localhost:8765", "https://127.0.0.1:8765",
                                    "null", "http://127.0.0.1:8765.evil.com"])
def test_a_wrong_origin_is_refused_even_with_a_token(client, origin):
    r = client.post("/echo", headers={"X-DuoSkin-Token": TOKEN, "Origin": origin})
    assert r.status_code == 403 and r.json() == {"error": "bad_origin"}


def test_the_right_origin_or_no_origin_passes(client):
    assert client.post("/echo", headers={"X-DuoSkin-Token": TOKEN, "Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert client.post("/echo", headers={"X-DuoSkin-Token": TOKEN}).status_code == 200


def test_origin_follows_the_live_port():
    port = {"n": 8765}
    app = Starlette(routes=[Route("/echo", lambda r: JSONResponse({}), methods=["POST"])])
    security.install(app, TOKEN, None, get_port=lambda: port["n"])
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    h = {"X-DuoSkin-Token": TOKEN, "Origin": "http://127.0.0.1:8777"}
    assert c.post("/echo", headers=h).status_code == 403
    port["n"] = 8777
    assert c.post("/echo", headers=h).status_code == 200


def test_the_token_comparison_is_constant_time_and_exact(client):
    assert client.post("/echo", headers={"X-DuoSkin-Token": TOKEN + " "}).status_code == 403
    assert client.post("/echo", headers={"X-DuoSkin-Token": TOKEN[:-1]}).status_code == 403
    assert client.post("/echo", headers={"X-DuoSkin-Token": ""}).status_code == 403


# ------------------------------------------------------------------------------------------------- host check
@pytest.mark.parametrize("host,status", [("127.0.0.1", 200), ("127.0.0.1:8765", 200), ("localhost", 200), ("localhost:8765", 200),
                                         ("evil.example", 400), ("192.168.1.5:8765", 400), ("127.0.0.1.evil.com", 400), ("[::1]:8765", 400),
                                         ("testserver", 400)])
def test_host_header_allowlist(client, host, status):
    assert client.get("/echo", headers={"Host": host}).status_code == status


# ------------------------------------------------------------------------------------------------- response headers
def test_html_gets_the_csp_and_every_response_gets_nosniff(client):
    h = client.get("/page").headers
    csp = h["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "style-src 'self'" in csp and "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp and "img-src 'self' blob: data:" in csp and "unsafe-inline" not in csp
    assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
    j = client.get("/echo").headers
    assert "content-security-policy" not in j and j["x-content-type-options"] == "nosniff"


def test_api_responses_are_no_store_and_there_is_no_cors_or_cookie(client):
    h = client.get("/api/x", headers={"Origin": "http://evil.example"}).headers
    assert h["cache-control"] == "no-store"
    assert not any(k.startswith("access-control-") for k in h) and "set-cookie" not in h


def test_a_routes_own_headers_are_not_overwritten(client):
    h = client.get("/cas/x").headers
    assert h["content-security-policy"] == "sandbox" and h["cache-control"] == "public, immutable"


def test_importmap_hash_goes_into_the_csp():
    html = '<head><script type="importmap">{"imports":{"three":"/vendor/three.js"}}</script></head>'
    src = security.importmap_csp_source(html)
    assert src.startswith("sha256-") and security.importmap_csp_source("<p>none</p>") is None
    c = TestClient(make_app(importmap=src), base_url="http://127.0.0.1:8765")
    assert f"script-src 'self' '{src}'" in c.get("/page").headers["content-security-policy"]
    import base64
    import hashlib

    body = '{"imports":{"three":"/vendor/three.js"}}'
    assert src == "sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()


def test_streaming_responses_pass_through_unbuffered():
    app = make_app()
    messages = []

    async def run():
        sent = []

        calls = []

        async def receive():
            if not calls:
                calls.append(1)
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)          # the client stays connected until the response ends

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "method": "GET", "path": "/stream", "headers": [(b"host", b"127.0.0.1:8765")], "server": ("127.0.0.1", 8765),
                 "query_string": b"", "scheme": "http", "http_version": "1.1", "client": ("127.0.0.1", 1), "root_path": "", "raw_path": b"/stream",
                 "extensions": {}}
        await app(scope, receive, send)
        return sent

    messages = asyncio.run(run())
    bodies = [m["body"] for m in messages if m["type"] == "http.response.body" and m.get("body")]
    assert bodies == [b"one", b"two"]                                         # chunks arrive separately, not merged


def test_inject_token_variants():
    t = security.inject_token
    assert 'content="abc"' in t('<head><meta name="duoskin-token" content="__DUOSKIN_TOKEN__"></head>', "abc")
    assert t('<head><meta name="duoskin-token" content="old"></head>', "abc").count("duoskin-token") == 1
    assert '<meta name="duoskin-token" content="abc">' in t("<html><head></head></html>", "abc")
    assert t("<p>no head</p>", "abc").startswith('<meta name="duoskin-token" content="abc">')


# ------------------------------------------------------------------------------------------------- GC
def _asset(rt, text, *, days_old=60, ext="txt"):
    a = rt.cas.put(text.encode(), ext, prov=make_prov("code"))
    old = iso_utc(utcnow() - timedelta(days=days_old))
    with rt.db.tx() as c:
        c.execute("UPDATE assets SET created_at=? WHERE sha256=?", (old, a.sha256))
    return a.sha256


def _step(rt, sha_in_json, days_old):
    from duoskin.models.job import Step

    job = rt.scheduler.submit_job("parts", None, {})
    s = Step(id=new_id("stp"), job_id=job.id, kind="k", pool="cpu", created_at=utcnow() - timedelta(days=days_old), outputs=[sha_in_json])
    rt.repo.insert_steps([s])


def test_gc_dry_run_lists_only_unreferenced_old_assets(rt):
    p = make_project(rt)
    orphan = _asset(rt, "orphan")
    _asset(rt, "young", days_old=2)                                       # recent and unreferenced: not a candidate
    in_part, in_approval, in_decision, in_gate = (_asset(rt, f"ref-{n}") for n in ("part", "approval", "decision", "gate"))
    in_registry, in_project, link_final, link_cand, new_step_ref, old_step_ref = (_asset(rt, f"ref2-{n}") for n in
                                                                                  ("registry", "project", "final", "cand", "newstep", "oldstep"))
    rt.repo.save_part(make_part(rt, p, board={"flat": in_part}))
    rt.repo.upsert_approval(p.id, ApprovalRecord(part_id="a.shirt", approval_hash="a" * 64, spec_id="s", spec_version=1, spec_slice_sha="b" * 64,
                                                 output_shas=[in_approval], pins_sha="c" * 64, approved_at=utcnow(), decision_id="d"))
    gate_id = new_id("gat")
    with rt.db.tx() as c:
        c.execute("INSERT INTO gates (id, project_id, job_id, kind, state, json, opened_at) VALUES (?,?,?,?,?,?,?)",
                  (gate_id, p.id, "j", "part_board", "decided", f'{{"tiles":[{{"assets":{{"x":"{in_gate}"}}}}]}}', "t"))
        c.execute("INSERT INTO decisions (id, gate_id, tile_id, action, client_decision_id, json, decided_at) VALUES (?,?,?,?,?,?,?)",
                  ("dec1", gate_id, "t", "approve", "cid", f'{{"mask_sha":"{in_decision}"}}', "t"))
        c.execute("INSERT INTO registry_face (id, project_id, asset_sha, pixel_sha, phash, json, duo_seq, registered_at) VALUES (?,?,?,?,?,?,?,?)",
                  ("r1", p.id, in_registry, "p" * 64, "0f", "{}", 1, "t"))
        c.execute("UPDATE projects SET json=json_set(json,'$.references',json(?)) WHERE id=?",
                  (f'[{{"asset_sha":"{in_project}","role":"reference","note":""}}]', p.id))
        for sha, status in ((link_final, "final"), (link_cand, "candidate")):
            c.execute("INSERT INTO asset_links (id, asset_sha, project_id, role, status, stream, json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (new_id("lnk"), sha, p.id, "x", status, "pipeline", "{}", "t"))
    _step(rt, new_step_ref, days_old=3)
    _step(rt, old_step_ref, days_old=90)
    report = gc_mod.gc(rt, dry_run=True, older_than_days=30)
    flagged = {c.sha for c in report.candidates}
    assert flagged == {orphan, link_cand, old_step_ref}                      # everything else is protected by some record
    assert report.dry_run and report.deleted == 0 and rt.cas.exists(orphan)
    assert report.assets_total == 12 and report.referenced == 8 and "would delete 3" in report.summary()


def test_gc_purge_deletes_files_rows_links_and_cache(rt):
    sha = _asset(rt, "to be deleted")
    keep = _asset(rt, "kept", days_old=1)
    with rt.db.tx() as c:
        c.execute("INSERT INTO asset_links (id, asset_sha, role, status, stream, json, created_at) VALUES (?,?,?,?,?,?,?)",
                  ("lnk1", sha, "draft", "rejected", "pipeline", "{}", "t"))
    from duoskin.engine.cache import CachedResult

    rt.cache.store(sha256_of("k"), CachedResult("k", [sha], {}))
    path = rt.cas.path(sha)
    report = gc_mod.gc(rt, dry_run=False, older_than_days=30)
    assert report.deleted == 1 and report.freed_bytes == len(b"to be deleted") and not path.exists()
    assert rt.db.conn().execute("SELECT COUNT(*) FROM assets WHERE sha256=?", (sha,)).fetchone()[0] == 0
    assert rt.db.conn().execute("SELECT COUNT(*) FROM asset_links").fetchone()[0] == 0
    assert report.cache_rows_dropped == 1 and rt.cas.exists(keep)


def test_gc_removes_stray_cas_files_without_a_row_but_not_fresh_ones(rt):
    import os
    import time

    stray = rt.cas.root / "ab" / ("c" * 64 + ".png")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")
    fresh = rt.cas.root / "ab" / ("d" * 64 + ".png")
    fresh.write_bytes(b"fresh")
    os.utime(stray, (time.time() - 7200, time.time() - 7200))
    dry = gc_mod.gc(rt, dry_run=True)
    assert dry.orphan_files == [str(stray)] and stray.exists()
    gc_mod.gc(rt, dry_run=False)
    assert not stray.exists() and fresh.exists()


def test_gc_prunes_events_and_old_tmp_files(rt):
    import os
    import time

    with rt.db.tx() as c:
        c.execute("INSERT INTO events (ts, project_id, type, payload) VALUES (?,?,?,?)", (iso_utc(utcnow() - timedelta(days=40)), None, "toast", "{}"))
    old, new = rt.paths.tmp_dir / "old.tmp", rt.paths.tmp_dir / "new.tmp"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    r = gc_mod.gc(rt, dry_run=False)
    assert r.events_pruned == 1 and r.tmp_files_removed == 1 and not old.exists() and new.exists()


def test_gc_never_touches_referenced_assets_even_when_old(rt):
    p = make_project(rt)
    sha = _asset(rt, "approved board", days_old=400)
    rt.repo.save_part(make_part(rt, p, board={"flat": sha}))
    gc_mod.gc(rt, dry_run=False, older_than_days=1)
    assert rt.cas.exists(sha)
    assert gc_mod.referenced_shas(rt, step_cutoff_iso="9") >= {sha}
