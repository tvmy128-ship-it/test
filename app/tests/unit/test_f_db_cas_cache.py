"""SQLite layer (§6.14, ENG-13), content store (§6.7) and step cache (§8.5, ENG-04, CHK-P07)."""
from __future__ import annotations

import io
import sqlite3
import threading

import pytest
from PIL import Image

from duoskin.db import ConflictError, Database, NotFound, SchemaTooNew
from duoskin.engine import cache as cache_mod
from duoskin.engine.cas import CasError, kind_for_ext, make_prov
from duoskin.engine.registry import FunctionHandler
from duoskin.engine.testkit import png_bytes
from duoskin.models.asset import Asset, AssetLink
from duoskin.models.common import sha256_of, utcnow
from duoskin.models.job import Step


# ------------------------------------------------------------------------------------------------- database
@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    d.migrate()
    yield d
    d.close_all()


def test_migrations_apply_in_order_and_set_user_version(tmp_path):
    d = Database(tmp_path / "x.sqlite3")
    assert d.schema_version() == 0
    latest = d.migrations()[-1][0]
    assert d.migrate() == latest == d.schema_version()
    assert d.migrate() == latest                                            # idempotent
    tables = {r[0] for r in d.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"projects", "specs", "dna_cards", "parts", "assets", "asset_links", "jobs", "steps", "step_deps", "cache", "checks",
                "gates", "decisions", "approvals", "changes", "cost_ledger", "events", "registry_face", "registry_print",
                "duo_memory", "labels", "check_stats", "inbox", "child_procs", "kv"}
    assert expected <= tables
    d.close_all()


def test_connection_pragmas(db):
    c = db.conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 1               # NORMAL
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert c.isolation_level is None


def test_one_connection_per_thread(db):
    conns = {}
    main = db.conn()
    assert db.conn() is main

    def grab():
        conns["t"] = db.conn()
        conns["again"] = db.conn()

    t = threading.Thread(target=grab)
    t.start()
    t.join()
    assert conns["t"] is conns["again"] and conns["t"] is not main


def test_tx_commits_and_rolls_back(db):
    with db.tx() as c:
        c.execute("INSERT INTO kv VALUES ('a','1','t')")
    with pytest.raises(RuntimeError), db.tx() as c:
        c.execute("INSERT INTO kv VALUES ('b','2','t')")
        raise RuntimeError("boom")
    assert [r["key"] for r in db.conn().execute("SELECT key FROM kv")] == ["a"]
    assert not db.in_tx() and db.conn().in_transaction is False


def test_write_transactions_begin_immediate(db):
    """A second writer waits for the first instead of failing halfway (BEGIN IMMEDIATE)."""
    order = []
    started, release = threading.Event(), threading.Event()

    def first():
        with db.tx() as c:
            c.execute("INSERT INTO kv VALUES ('w1','x','t')")
            started.set()
            release.wait(2)
            order.append("first-commit")

    t = threading.Thread(target=first)
    t.start()
    started.wait(2)
    threading.Timer(0.2, release.set).start()
    with db.tx() as c:                                                       # blocks until the first transaction commits
        order.append("second-enters")
        c.execute("INSERT INTO kv VALUES ('w2','x','t')")
    t.join()
    assert order == ["first-commit", "second-enters"]


def test_nested_tx_is_a_savepoint(db):
    with db.tx() as c:
        c.execute("INSERT INTO kv VALUES ('outer','1','t')")
        with pytest.raises(ValueError), db.tx() as c2:
            c2.execute("INSERT INTO kv VALUES ('inner','2','t')")
            raise ValueError("inner failed")
        c.execute("INSERT INTO kv VALUES ('after','3','t')")
    assert sorted(r["key"] for r in db.conn().execute("SELECT key FROM kv")) == ["after", "outer"]


def test_on_commit_runs_after_the_outermost_commit_and_never_after_rollback(db):
    seen = []
    with db.tx():
        db.on_commit(lambda: seen.append("a"))
        with db.tx():
            db.on_commit(lambda: seen.append("b"))
        assert seen == []
    assert seen == ["a", "b"]
    with pytest.raises(RuntimeError), db.tx():
        db.on_commit(lambda: seen.append("never"))
        raise RuntimeError
    with db.tx(), pytest.raises(RuntimeError), db.tx():
        db.on_commit(lambda: seen.append("inner-rolled-back"))
        raise RuntimeError
    assert seen == ["a", "b"]
    db.on_commit(lambda: seen.append("now"))                                 # outside a transaction: immediately
    assert seen[-1] == "now"


def test_integrity_check_checkpoint_ping_and_backup_rotation(db, tmp_path):
    assert db.integrity_check() == [] and db.ping()
    db.checkpoint("TRUNCATE")
    with pytest.raises(ValueError):
        db.checkpoint("DROP")
    made = []
    for i in range(7):
        with db.tx() as c:
            c.execute("INSERT INTO kv VALUES (?,?,?)", (f"k{i}", "v", "t"))
        made.append(db.backup(tmp_path / "bk", keep=5))
    kept = sorted((tmp_path / "bk").glob("duoskin-*.sqlite3"))
    assert len(kept) == 5 and made[-1] in kept
    restored = sqlite3.connect(str(made[-1]))
    assert restored.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 7        # a backup is a full, consistent copy
    restored.close()


def test_backup_of_an_empty_database_is_skipped(tmp_path):
    assert Database(tmp_path / "e.sqlite3").backup(tmp_path / "bk") is None


def test_a_database_from_a_newer_app_is_refused(tmp_path):
    d = Database(tmp_path / "new.sqlite3")
    d.migrate()
    d.conn().execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNew):
        d.migrate()
    d.close_all()


def test_close_all_never_touches_other_live_threads_connections(tmp_path):
    d = Database(tmp_path / "c.sqlite3")
    d.migrate()
    hold, done = threading.Event(), threading.Event()

    def worker():
        d.conn().execute("SELECT 1").fetchone()
        done.set()
        hold.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    done.wait(2)
    d.close_all()                                                           # must not crash the process or close the worker's handle
    with pytest.raises(RuntimeError):
        d.conn()
    hold.set()
    t.join()


def test_unique_ledger_rule(db):
    ins = "INSERT INTO cost_ledger (id, project_id, step_id, attempt, operation, provider, state, usd, json, ts) VALUES (?,?,?,?,?,?,?,?,?,?)"
    db.conn().execute(ins, ("c1", None, "stp1", 1, "op", "openai", "committed", 1.0, "{}", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn().execute(ins, ("c2", None, "stp1", 1, "op", "openai", "committed", 1.0, "{}", "t"))
    db.conn().execute(ins, ("c3", None, "stp1", 2, "op", "openai", "committed", 1.0, "{}", "t"))     # a new attempt is fine
    db.conn().execute(ins, ("c4", None, None, 0, "op", "openai", "committed", 1.0, "{}", "t"))
    db.conn().execute(ins, ("c5", None, None, 0, "op", "openai", "committed", 1.0, "{}", "t"))       # no step: unconstrained


def test_repo_optimistic_locks(rt):
    from helpers_unit import make_part, make_project

    p = make_project(rt)
    stale = rt.repo.get_project(p.id)
    rt.repo.save_project(rt.repo.get_project(p.id).model_copy(update={"name": "first"}), expected_version=0)
    with pytest.raises(ConflictError) as ei:
        rt.repo.save_project(stale.model_copy(update={"name": "second"}), expected_version=0)
    assert ei.value.current.name == "first" and rt.repo.get_project(p.id).version == 1
    part = make_part(rt, p)
    assert rt.repo.save_part(part, expected_version=0).version == 0            # new part: inserted
    assert rt.repo.save_part(part, expected_version=0).version == 1            # matches the stored version: updated
    with pytest.raises(ConflictError):
        rt.repo.save_part(part, expected_version=0)                              # stale now
    with pytest.raises(NotFound):
        rt.repo.get_project("prj_missing")


def test_mutate_project_without_bump_keeps_the_version(rt):
    from helpers_unit import make_project

    p = make_project(rt)
    rt.repo.mutate_project(p.id, lambda x: setattr(x, "spent_usd", 1.5), bump=False)
    q = rt.repo.get_project(p.id)
    assert q.spent_usd == 1.5 and q.version == 0
    rt.repo.mutate_project(p.id, lambda x: setattr(x, "paused", True))
    assert rt.repo.get_project(p.id).version == 1


def test_unique_slug_and_kv(rt):
    from helpers_unit import make_project

    make_project(rt, "Plush Koi")
    assert rt.repo.unique_slug("Plush Koi") == "plush-koi-2"
    rt.repo.kv_set("x", {"a": [1, 2]})
    assert rt.repo.kv_get("x") == {"a": [1, 2]} and rt.repo.kv_get("nope") is None


# ------------------------------------------------------------------------------------------------- CAS
def test_put_get_roundtrip_with_image_facts(rt):
    data = png_bytes(12, 7, (10, 20, 30, 255))
    a = rt.cas.put(data, "png", prov=make_prov("code"))
    assert a.kind == "png" and a.mime == "image/png" and (a.width, a.height) == (12, 7) and a.bytes == len(data)
    assert a.pixel_sha and len(a.sha256) == 64
    assert rt.cas.get(a.sha256) == data
    p = rt.cas.path(a.sha256)
    assert p.parent.name == a.sha256[:2] and p.name == f"{a.sha256}.png" and p.exists()


def test_same_bytes_give_one_asset_and_one_file(rt):
    data = png_bytes()
    a = rt.cas.put(data, "png", prov=make_prov("code"))
    b = rt.cas.put(data, "png", prov=make_prov("openai"))
    assert a == b and a.first_provenance.source == "code"                  # the first provenance is kept
    assert len(rt.cas.all_files()) == 1
    assert rt.db.conn().execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_links_are_recorded_per_context(rt):
    data = png_bytes()
    for role, status in (("draft", "candidate"), ("final", "chosen")):
        rt.cas.put(data, "png", prov=make_prov("code", stream="drill"),
                   link=AssetLink(id="", asset_sha="0" * 64, project_id="prj_1", part_id="a.face", step_id="stp_1", role=role,
                                  status=status, provenance=make_prov("code", stream="drill")))
    rows = rt.db.conn().execute("SELECT role, status, stream, asset_sha FROM asset_links ORDER BY role").fetchall()
    assert [(r["role"], r["status"], r["stream"]) for r in rows] == [("draft", "candidate", "drill"), ("final", "chosen", "drill")]
    assert len({r["asset_sha"] for r in rows}) == 1


@pytest.mark.parametrize("ext,kind", [("png", "png"), ("PNG", "png"), (".jpg", "jpeg"), ("jpeg", "jpeg"), ("webp", "webp"), ("svg", "svg"),
                                      ("glb", "glb"), ("gltf", "gltf"), ("bin", "bin"), ("fbx", "fbx"), ("zip", "zip"), ("json", "json"),
                                      ("npz", "npz"), ("luau", "luau"), ("html", "html")])
def test_extension_to_kind(ext, kind):
    assert kind_for_ext(ext) == kind


def test_bad_inputs_are_rejected(rt):
    with pytest.raises(CasError):
        rt.cas.put(b"x", "exe", prov=make_prov("code"))
    with pytest.raises(CasError, match="valid png"):
        rt.cas.put(b"not a png at all", "png", prov=make_prov("code"))
    with pytest.raises(CasError):
        rt.cas.put("text", "txt", prov=make_prov("code"))                     # type: ignore[arg-type]
    with pytest.raises(NotFound):
        rt.cas.get("a" * 64)
    with pytest.raises(CasError):
        rt.cas.path("../../etc/passwd")


def test_non_image_kinds_are_stored_as_is(rt):
    a = rt.cas.put(b'{"a": 1}', "json", prov=make_prov("code"))
    s = rt.cas.put(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "svg", prov=make_prov("recraft"))
    assert a.mime == "application/json" and a.width is None and a.pixel_sha is None and s.mime == "image/svg+xml"
    assert rt.cas.get(s.sha256).startswith(b"<svg")


def test_a_missing_file_is_healed_on_the_next_put_and_describe_checks_the_extension(rt):
    data = png_bytes()
    a = rt.cas.put(data, "png", prov=make_prov("code"))
    rt.cas.path(a.sha256).unlink()
    assert rt.cas.exists(a.sha256) is False
    assert rt.cas.describe(a.sha256, "png") is None
    rt.cas.put(data, "png", prov=make_prov("code"))
    assert rt.cas.exists(a.sha256)
    _path, mime, kind = rt.cas.describe(a.sha256, "png")
    assert mime == "image/png" and kind == "png" and rt.cas.describe(a.sha256, "jpg") is None and rt.cas.describe("zz", "png") is None


def test_update_meta_records_triangles(rt):
    a = rt.cas.put(b"glTF....", "glb", prov=make_prov("code"))
    assert rt.cas.update_meta(a.sha256, tris=3500).tris == 3500 and rt.cas.get_asset(a.sha256).tris == 3500


def test_cas_refuses_an_overlong_root(tmp_path):
    from duoskin.engine.cas import Cas

    cas = Cas(tmp_path / ("x" * 200), Database(tmp_path / "d.sqlite3"))
    with pytest.raises(CasError, match="characters long"):
        cas.put(b"a", "txt", prov=make_prov("code"))


# ------------------------------------------------------------------------------------------------- cache keys
def _handler(**kw):
    return FunctionHandler("k.x", lambda ctx, p, i: None, **kw)


def _asset(sha="a" * 64, px=None):
    return Asset(sha256=sha, pixel_sha=px, kind="png", mime="image/png", bytes=1, first_provenance=make_prov("code"))


def _step(nonce=""):
    return Step(id="stp_1", job_id="j", kind="k.x", created_at=utcnow(), nonce=nonce)


BASE_FIELDS = {"model": "m1", "prompt_id": "I2", "prompt_version": 1, "prompt_sha256": "p" * 64, "schema_hash": "s1",
               "mask_pixel_sha": "m" * 64, "kit_subset_sha": "k1", "house_style_version": 1, "style_guide_version": 1,
               "thresholds_version": "t1", "rules_version": 1, "capability_flags": {"mask_multi_ok": True}}


def _key(fields=None, params=None, inputs=None, nonce="", handler=None):
    h = handler or _handler(cache_fields=lambda p, i: dict(fields if fields is not None else BASE_FIELDS))
    return cache_mod.cache_key(_step(nonce), h, params if params is not None else {"n": 4, "size": "1024x1024"},
                               inputs if inputs is not None else [_asset("a" * 64, "1" * 64), _asset("b" * 64, "2" * 64)])


def test_cache_key_is_deterministic():
    assert _key() == _key() and len(_key()) == 64


@pytest.mark.parametrize("field", list(BASE_FIELDS))
def test_changing_any_cache_field_changes_the_key(field):                    # CHK-P07 / ENG-04
    changed = dict(BASE_FIELDS)
    changed[field] = {"mask_multi_ok": False} if field == "capability_flags" else (BASE_FIELDS[field] + 1 if isinstance(BASE_FIELDS[field], int) else str(BASE_FIELDS[field]) + "x")
    assert _key(fields=changed) != _key()


def test_changing_kind_version_provider_params_inputs_or_nonce_changes_the_key():
    base = _key()
    assert _key(handler=FunctionHandler("k.y", lambda *a: None, cache_fields=lambda p, i: dict(BASE_FIELDS))) != base
    assert _key(handler=FunctionHandler("k.x", lambda *a: None, version=2, cache_fields=lambda p, i: dict(BASE_FIELDS))) != base
    assert _key(handler=FunctionHandler("k.x", lambda *a: None, provider="openai", cache_fields=lambda p, i: dict(BASE_FIELDS))) != base
    assert _key(params={"n": 6, "size": "1024x1024"}) != base
    assert _key(inputs=[_asset("a" * 64, "1" * 64)]) != base
    assert _key(inputs=[_asset("b" * 64, "2" * 64), _asset("a" * 64, "1" * 64)]) != base      # order matters
    assert _key(nonce="reimagine-1") != base


def test_inputs_are_identified_by_pixels_not_file_bytes():
    same_pixels_other_file = [_asset("c" * 64, "1" * 64), _asset("d" * 64, "2" * 64)]
    assert _key(inputs=same_pixels_other_file) == _key()
    assert _key(inputs=[_asset("a" * 64, None)]) != _key(inputs=[_asset("b" * 64, None)])   # no pixel sha: the file sha is used


def test_pixel_sha_ignores_metadata_but_not_pixels():
    img = Image.new("RGBA", (6, 6), (9, 8, 7, 255))
    plain, with_meta = io.BytesIO(), io.BytesIO()
    img.save(plain, "PNG")
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    meta.add_text("c2pa", "provider metadata")
    img.save(with_meta, "PNG", pnginfo=meta, compress_level=0)
    assert plain.getvalue() != with_meta.getvalue()
    assert cache_mod.pixel_sha(plain.getvalue()) == cache_mod.pixel_sha(with_meta.getvalue())
    other = Image.new("RGBA", (6, 6), (9, 8, 8, 255))
    buf = io.BytesIO()
    other.save(buf, "PNG")
    assert cache_mod.pixel_sha(buf.getvalue()) != cache_mod.pixel_sha(plain.getvalue())
    wide = io.BytesIO()
    Image.new("RGBA", (3, 12), (9, 8, 7, 255)).save(wide, "PNG")
    assert cache_mod.pixel_sha(wide.getvalue()) != cache_mod.pixel_sha(plain.getvalue())   # the size is part of the identity


def test_step_cache_store_lookup_and_missing_outputs(rt):
    a = rt.cas.put(png_bytes(), "png", prov=make_prov("code"))
    key = sha256_of({"k": 1})
    assert rt.cache.lookup(key) is None
    rt.cache.store(key, cache_mod.CachedResult("k.x", [a.sha256], {"score": 0.9}))
    hit = rt.cache.lookup(key)
    assert hit.outputs == [a.sha256] and hit.result == {"score": 0.9}
    assert rt.db.conn().execute("SELECT last_hit_at FROM cache").fetchone()[0] is not None
    rt.cas.path(a.sha256).unlink()
    assert rt.cache.lookup(key) is None                                      # a hit needs every output in the CAS
    assert rt.cache.prune_missing() == 1 and rt.db.conn().execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
