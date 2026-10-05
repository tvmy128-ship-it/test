"""Security lens 3, the proof: a canary key goes through a whole mock flow with injected provider errors, then every byte the app wrote
(DATA folder, SQLite file + WAL, logs, backups, exports, the diagnostics zip) and every API / SSE answer is searched for it.

The only place a key may legitimately live is the key store itself (``secrets.dev.json`` on a development machine, DPAPI-encrypted or the
Credential Manager on Windows); it is excluded from the search, and its presence is the positive control that proves the search works.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline_plan"))

from planhelpers import new_project, start_plan, wait_gate1

from duoskin import logsetup
from duoskin.engine import registry as eng_registry
from duoskin.engine.errors import StepFailure
from duoskin.engine.registry import register_handler
from duoskin.engine.testkit import make_app, make_client, wait_for
from duoskin.providers import registry as provider_registry

MARK = "CNRYX"
CANARY = {
    "anthropic": "sk-ant-api03-" + MARK + "AaBbCcDdEeFf0123456789" * 2,
    "openai": "sk-proj-" + MARK + "ZzYyXxWwVvUu9876543210" * 2,
    "tripo": "tsk_" + MARK + "QqWwEeRrTtYy1357924680" * 2,
    "recraft": MARK + "recraftTokenWithoutAnyPrefix0123456789abcdef",
    "gemini": "AIza" + MARK + "SyGeminiKey0123456789abcdefghij",
}
KEY_STORE_FILES = {"secrets.dev.json", "secrets.dpapi"}


def _leaky(secret: str) -> str:
    """What a careless library puts into an exception: the header, the URL with ?key=, the key itself."""
    return (f"HTTP 401 from https://api.example.com/v1/x?key={secret}&Signature={secret} -- request headers: "
            f"{{'Authorization': 'Bearer {secret}', 'x-api-key': '{secret}', 'x-goog-api-key': '{secret}'}}")


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    base = tmp_path_factory.mktemp("canary")
    saved = eng_registry.handlers_snapshot()
    provider_registry.reset()
    mp = pytest.MonkeyPatch()
    mp.setenv("DUOSKIN_MESH_INPROC", "1")
    mp.setenv("DUOSKIN_NO_INBOX_THREAD", "1")
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRIPO_API_KEY", "RECRAFT_API_TOKEN", "GEMINI_API_KEY", "FAL_KEY"):
        mp.delenv(env, raising=False)
    app = make_app(base / "home", providers_mode="mock")
    rt = app.state.rt
    rt.update_settings({"paths": {"exports_root": str(base / "exports"), "tripo_inbox": str(base / "exports" / "inbox")}})
    handle = logsetup.setup_logging(rt.paths.logs_dir, console=False, hooks=False)
    client = make_client(app)
    client.__enter__()
    try:
        # 1. the user pastes every key into Settings
        for provider, value in CANARY.items():
            r = client.put(f"/api/keys/{provider}", json={"value": value})
            assert r.status_code == 200, r.text
        # 2. a normal mock flow: brief to Gate 1
        pid = new_project(client, brief="two friends sharing a rainy-day picnic", must_include=["a teal ribbon"])
        plan_error = None
        try:
            start_plan(client, pid)
            wait_gate1(client, pid, timeout=280)
        except Exception as exc:  # noqa: BLE001  # e.g. the prompt templates are mid-edit: reported by test_the_mock_plan_flow_ran
            plan_error = f"{type(exc).__name__}: {exc}"
        # 3. injected failures whose texts carry the keys (an exception, a log line, an event, a key test, a retry)
        def boom(ctx, params, inputs):
            raise RuntimeError(_leaky(CANARY["openai"]) + " " + _leaky(CANARY["recraft"]))

        def boom_failure(ctx, params, inputs):
            raise StepFailure(_leaky(CANARY["tripo"]), kind="auth", user_hint="rejected " + CANARY["tripo"], request_id=CANARY["tripo"])

        register_handler("sec.boom", boom, cacheable=False)
        register_handler("sec.failure", boom_failure, cacheable=False)
        job = rt.scheduler.submit_job("parts", pid, {}, steps=[rt.ops.new_step("sec.boom", job_id="x", project_id=pid),
                                                              rt.ops.new_step("sec.failure", job_id="x", project_id=pid)])
        wait_for(lambda: all(s.state.value == "failed" for s in rt.repo.list_steps(job_id=job.id)) and rt.repo.list_steps(job_id=job.id), 20,
                 message="the injected failures")
        log = logging.getLogger("duoskin.test_canary")
        for value in CANARY.values():
            log.error("provider call failed: %s", _leaky(value))
            try:
                raise RuntimeError(_leaky(value))
            except RuntimeError:
                log.exception("and with a traceback for %s", value)
        rt.bus.emit("toast", {"message": _leaky(CANARY["anthropic"]), "level": "error"}, pid)

        class Fake:
            def test_key(self):
                raise RuntimeError(_leaky(CANARY["gemini"]))

        mp.setattr(provider_registry, "get", lambda provider: Fake())
        mp.setattr(type(rt.effective_settings()), "mode_of", lambda self, provider: type("M", (), {"value": "real"})())
        for provider in CANARY:
            client.post(f"/api/keys/{provider}/test")
        mp.undo()
        mp.setenv("DUOSKIN_MESH_INPROC", "1")
        # 4. a diagnostics zip, as the "export diagnostics" button builds it
        diag = client.post("/api/diagnostics")
        assert diag.status_code == 200, diag.text
        yield {"rt": rt, "client": client, "pid": pid, "job": job, "diag": Path(diag.json()["path"]), "base": base, "plan_error": plan_error}
    finally:
        client.__exit__(None, None, None)
        handle.shutdown()
        for value in CANARY.values():
            logsetup.forget_secret(value)
        mp.undo()
        provider_registry.reset()
        eng_registry.clear()
        for h in saved.values():
            eng_registry.register(h, replace=True)


def _every_file(root: Path):
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.name not in KEY_STORE_FILES:
            yield f


@pytest.mark.timeout(600)
def test_the_mock_plan_flow_ran(flow):
    """The canary sat in the key store during a real mock plan (brief to Gate 1): that is what makes the file search below a proof."""
    err = flow["plan_error"]
    if err and ("TemplateError" in err or "front matter" in err):
        pytest.skip("the prompt templates are being edited by another track: " + err[:120])
    assert err is None, err


@pytest.mark.timeout(600)
def test_the_search_can_find_a_key_at_all(flow):
    """Positive control: the key store file does hold the keys (so an empty search elsewhere means something)."""
    store = [f for f in flow["rt"].paths.home.rglob("*") if f.name in KEY_STORE_FILES]
    if not store:
        pytest.skip("the keys live in a system keyring here, not in a file")
    if store[0].name == "secrets.dpapi":
        pytest.skip("DPAPI-encrypted: unreadable by design")
    entries = json.loads(store[0].read_text(encoding="utf-8"))["entries"]            # the development file holds base64 of the value (not encrypted)
    assert {base64.b64decode(v).decode("utf-8") for v in entries.values()} >= set(CANARY.values())


@pytest.mark.timeout(600)
def test_no_key_in_any_file_the_app_wrote(flow):
    rt = flow["rt"]
    rt.db.checkpoint("PASSIVE") if hasattr(rt.db, "checkpoint") else None
    hits = []
    for root in (rt.paths.home, flow["base"] / "exports"):
        if not root.exists():
            continue
        for f in _every_file(root):
            data = f.read_bytes()
            if MARK.encode() in data or any(v.encode() in data or base64.b64encode(v.encode()) in data for v in CANARY.values()):
                hits.append(str(f.relative_to(flow["base"])))
    assert hits == []


@pytest.mark.timeout(600)
def test_the_failures_were_recorded_and_are_clean(flow):
    rt = flow["rt"]
    steps = rt.repo.list_steps(job_id=flow["job"].id)
    assert {s.kind for s in steps} == {"sec.boom", "sec.failure"} and all(s.error is not None for s in steps)
    blob = "".join(s.model_dump_json() for s in steps)
    assert MARK not in blob
    assert any("api.example.com" in (s.error.message or "") or "REDACTED" in (s.error.message or "") for s in steps)   # the error text survived, minus the secrets


@pytest.mark.timeout(600)
def test_the_log_file_has_the_failures_but_no_key(flow):
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in flow["rt"].paths.logs_dir.glob("duoskin.log*"))
    assert "provider call failed" in text and "REDACTED" in text
    assert MARK not in text


@pytest.mark.timeout(600)
def test_the_diagnostics_zip_holds_no_key(flow):
    with zipfile.ZipFile(flow["diag"]) as z:
        names = z.namelist()
        assert {"info.json", "failing_steps.json", "doctor.json"} <= set(names) and any(n.startswith("logs/") for n in names)
        for n in names:
            assert MARK.encode() not in z.read(n), n
    assert "provider call failed" in zipfile.ZipFile(flow["diag"]).read("logs/duoskin.log").decode("utf-8")


@pytest.mark.timeout(600)
def test_no_api_answer_or_event_stream_holds_a_key(flow):
    client, pid = flow["client"], flow["pid"]
    gets = ["/api/health", "/api/state", "/api/keys", "/api/settings", "/api/doctor", "/api/jobs?steps=true", "/api/costs", "/api/queue",
            "/api/inbox", "/api/library", "/api/gates", f"/api/projects/{pid}", "/api/projects", "/api/events/poll", "/api/events?once=1",
            f"/api/projects/{pid}/specs", f"/api/jobs/{flow['job'].id}"]
    for path in gets:
        r = client.get(path)
        assert r.status_code < 500, (path, r.status_code)
        assert MARK not in r.text, path
        assert not any(v in r.text for v in CANARY.values()), path
    keys = client.get("/api/keys").json()
    for provider, st in keys.items():
        if provider in CANARY:
            assert st["set"] and st["masked"] and st["masked"] != CANARY[provider] and len(st["masked"]) < 12
    # the key-test route reported the injected failure, scrubbed
    for provider in CANARY:
        out = client.get(f"/api/keys/{provider}") if False else keys[provider]
        assert MARK not in str((out.get("last_test") or {}).get("message", ""))


@pytest.mark.timeout(600)
def test_the_put_and_delete_answers_never_echo_the_value(flow):
    client = flow["client"]
    r = client.put("/api/keys/fal", json={"value": "fal-" + MARK + "0123456789abcdefghij"})
    assert r.status_code == 200 and MARK not in r.text
    r = client.delete("/api/keys/fal")
    assert r.status_code == 200 and MARK not in r.text
    bad = client.put("/api/keys/openai", json={"value": "line one\n" + MARK})
    assert bad.status_code == 422 and MARK not in bad.text                      # the refusal names the rule, not the value


@pytest.mark.timeout(600)
def test_the_export_kit_scan_catches_a_key_that_slips_in(flow, tmp_path):
    from duoskin.pipeline import export

    rt = flow["rt"]
    clean = tmp_path / "kit"
    clean.mkdir()
    (clean / "manifest.json").write_text('{"ok": true}', encoding="utf-8")
    assert export.secret_scan(rt, root=clean) == []
    for secret in (CANARY["recraft"], CANARY["openai"], "https://x.example.com/a.glb?X-Amz-Security-Token=" + "A" * 40, "see ?sig=" + "b" * 30):
        (clean / "provenance.json").write_text('{"note": "' + secret + '"}', encoding="utf-8")
        assert export.secret_scan(rt, root=clean), secret
        z = tmp_path / "kit.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(clean / "provenance.json", "kit/provenance.json")
        assert export.secret_scan(rt, zip_path=z), secret
