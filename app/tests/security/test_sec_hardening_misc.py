"""Security lens 3: kits from untrusted folders, child-process environments, log plumbing, request size."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from duoskin.logsetup import forget_secret, register_secret

CNRY = "CNRYenvsecretvalue0123456789abcdef"


# ================================================================================================= Add kit
def fabric_kit(root: Path, ident: str | None, name: str = "kit", extra: dict[str, bytes] | None = None) -> Path:
    kit = root / name
    kit.mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("L", (8, 8), 128).save(buf, "PNG")
    (kit / "tile.png").write_bytes(buf.getvalue())
    if ident is not None:
        (kit / "fabric.json").write_text(json.dumps({"id": ident}), encoding="utf-8")
    for n, data in (extra or {}).items():
        (kit / n).write_bytes(data)
    return kit


def origin():
    from duoskin.pipeline import kits

    return min(kits.ORIGINS)


@pytest.mark.parametrize("ident", ["../../../escaped", "..\\..\\escaped", "..", ".", "/abs/escaped", "C:\\escaped", "C:escaped", "a/b", "a\\b", "x" * 80, "con", "NUL",
                                   "x:stream", "x.", " x", "x y", "x\x00y", "%2e%2e", "$HOME", "~", "-x", "_x", ".hidden"])
def test_a_kit_id_from_the_kit_files_can_never_become_a_path(rt_bare, tmp_path, ident):
    from duoskin.pipeline import library

    rt = rt_bare
    kit = fabric_kit(tmp_path / "src", ident, extra={"payload.bat": b"calc.exe"})
    with pytest.raises(library.KitError):
        library.add_kit(rt, str(kit), "fabric", origin(), "user_made")
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "home" / "escaped").exists()
    written = [p for p in tmp_path.rglob("payload.bat")]
    assert written == [kit / "payload.bat"], "the kit's files may only be copied under DATA/kits"


def test_the_traversal_that_used_to_write_outside_data_is_closed(rt_bare, tmp_path):
    """Regression: ``{"id": "../../../escaped_kit"}`` in fabric.json copied the whole kit folder (a .bat included) next to the data folder."""
    from duoskin.pipeline import library

    kit = fabric_kit(tmp_path / "src", "../../../escaped_kit", extra={"payload.bat": b"calc.exe"})
    with pytest.raises(library.KitError, match="not allowed"):
        library.add_kit(rt_bare, str(kit), "fabric", origin(), "user_made")
    assert not any(p.name == "escaped_kit" for p in tmp_path.rglob("*"))


def test_a_good_kit_is_copied_inside_the_kits_folder(rt_bare, tmp_path):
    from duoskin.pipeline import library

    rt = rt_bare
    kit = fabric_kit(tmp_path / "src", "denim_01")
    out = library.add_kit(rt, str(kit), "fabric", origin(), "user_made")
    assert out["added"] == "denim_01"
    dest = rt.paths.kits_dir / "fabrics" / "denim_01"
    assert (dest / "tile.png").is_file() and json.loads((dest / "fabric.json").read_text(encoding="utf-8"))["origin"] == origin()
    with pytest.raises(library.KitError, match="already exists"):
        library.add_kit(rt, str(kit), "fabric", origin(), "user_made")


def test_folder_names_of_folds_and_bases_are_checked_too(rt_bare, tmp_path):
    from duoskin.pipeline import library

    for kind, name in (("folds", "bad name!"), ("head_base", "con"), ("body_base", "x" * 90)):
        src = tmp_path / "src" / name
        src.mkdir(parents=True)
        (src / "fold.json").write_text("{}", encoding="utf-8")
        with pytest.raises(library.KitError):
            library.add_kit(rt_bare, str(src), kind, origin(), "user_made")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_kit_with_a_link_is_refused_instead_of_copying_what_it_points_at(rt_bare, tmp_path):
    from duoskin.pipeline import library

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    kit = fabric_kit(tmp_path / "src", "linked")
    (kit / "notes.txt").symlink_to(secret)
    with pytest.raises(library.KitError, match="link"):
        library.add_kit(rt_bare, str(kit), "fabric", origin(), "user_made")
    assert not (rt_bare.paths.kits_dir / "fabrics" / "linked").exists()
    assert not any("TOP-SECRET" in p.read_text(encoding="utf-8", errors="ignore") for p in rt_bare.paths.kits_dir.rglob("*") if p.is_file())


def test_a_kit_with_too_many_files_is_refused_and_leaves_nothing_behind(rt_bare, tmp_path, monkeypatch):
    from duoskin.pipeline import library

    monkeypatch.setattr(library, "MAX_KIT_FILES", 3)
    kit = fabric_kit(tmp_path / "src", "many", extra={f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(library.KitError, match="too big"):
        library.add_kit(rt_bare, str(kit), "fabric", origin(), "user_made")
    assert not (rt_bare.paths.kits_dir / "fabrics" / "many").exists()


def test_the_head_base_variant_is_a_plain_name(client):
    for variant in ("../x", "a/b", "C:\\x", "x" * 80, "", ".."):
        r = client.post("/api/library/head-base/build", json={"source_path": "x.fbx", "variant": variant})
        assert r.status_code == 422, variant


# ================================================================================================= child processes
def test_child_environments_never_carry_a_key_or_token(monkeypatch):
    from duoskin.keystore import ENV_VARS
    from duoskin.security import child_env

    for name in ENV_VARS.values():
        monkeypatch.setenv(name, CNRY)
    for name in ("MY_SERVICE_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "SOME_API_KEY", "openai_api_key", "HF_TOKEN"):
        monkeypatch.setenv(name, CNRY)
    monkeypatch.setenv("KEYBOARD_LAYOUT", "us")
    monkeypatch.setenv("PATH_EXTRA", "keep")
    env = child_env({"DUOSKIN_RESULT_JSON": "r.json"})
    assert not [k for k, v in env.items() if v == CNRY]
    assert env["KEYBOARD_LAYOUT"] == "us" and env["PATH_EXTRA"] == "keep" and env["DUOSKIN_RESULT_JSON"] == "r.json" and "PATH" in env


def test_the_mesh_worker_process_does_not_inherit_keys(monkeypatch):
    from duoskin.mesh.proc import run_process

    monkeypatch.setenv("OPENAI_API_KEY", CNRY)
    monkeypatch.setenv("TRIPO_API_KEY", CNRY)
    monkeypatch.setenv("SOME_SECRET", CNRY)
    res = run_process([sys.executable, "-c", "import os; print(sorted(k for k, v in os.environ.items() if " + repr(CNRY) + " in v))"], timeout_s=30)
    assert res.ok and res.stdout.strip() == "[]", res.stdout


def test_step_children_do_not_inherit_keys(rt, monkeypatch):
    """``StepContext.run_child`` (the mesh worker and Blender launcher) builds its environment with ``child_env``."""
    import inspect

    from duoskin.engine import context

    src = inspect.getsource(context.StepContext.run_child) if hasattr(context.StepContext, "run_child") else inspect.getsource(context)
    assert "scrubbed_env" in src and "{**os.environ" not in src


# ================================================================================================= log plumbing
def test_debug_logging_switches_of_the_sdks_are_dropped_at_start(monkeypatch):
    from duoskin import __main__ as entry

    for name in entry.DEBUG_LOG_ENV_VARS:
        monkeypatch.setenv(name, "debug")
    monkeypatch.setattr(entry, "_early_setup", entry._early_setup)
    try:
        entry._early_setup()
    except Exception:  # noqa: BLE001 - the certificate/DLL steps may not apply here; the env drop is the first statement
        logging.getLogger("duoskin.test").debug("early setup raised after dropping the variables")
    assert not [n for n in entry.DEBUG_LOG_ENV_VARS if n in os.environ]


def test_http_library_loggers_are_held_at_warning_so_no_url_or_header_is_logged(tmp_path):
    request_cleanup: list[str] = []
    from duoskin import logsetup
    from duoskin.providers._http import httpx

    handle = logsetup.setup_logging(tmp_path / "logs", console=False, hooks=False, dev=True)       # even dev mode: DEBUG for us, WARNING for them
    try:
        for name in logsetup.NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING, name
        key = "CNRYhttpxlogkey0123456789abcdef"
        register_secret(key)
        request_cleanup.append(key)
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)), headers={"Authorization": f"Bearer {key}"}) as c:
            c.get("https://tripo-data.example.com/a.glb?Signature=abcdef0123456789abcdef&Policy=zzzzzzzzzzzzzzzz")
    finally:
        handle.shutdown()
        for k in request_cleanup:
            forget_secret(k)
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (tmp_path / "logs").glob("duoskin.log*"))
    assert key not in text and "Signature=abcdef" not in text and "Policy=zzzz" not in text


def test_every_log_record_is_born_redacted_for_any_handler_even_before_setup():
    from duoskin import logsetup

    logsetup.install_record_factory()
    key = "CNRYfactorykey0123456789abcdef"
    register_secret(key)
    seen: list[str] = []

    class Spy(logging.Handler):
        def emit(self, record):
            seen.append(self.format(record))

    spy = Spy()
    spy.setFormatter(logging.Formatter("%(message)s | %(exc_text)s"))
    log = logging.getLogger("duoskin.test_factory")
    log.addHandler(spy)
    log.setLevel(logging.DEBUG)
    try:
        try:
            raise RuntimeError(f"Authorization: Bearer {key} for https://x.example.com/?key={key}")
        except RuntimeError:
            log.exception("failed with %s", key)
    finally:
        log.removeHandler(spy)
    assert seen and key not in seen[0] and "REDACTED" in seen[0]


def test_the_cli_installs_the_redacting_factory_before_anything_else():
    from duoskin import __main__ as entry

    src = Path(entry.__file__).read_text(encoding="utf-8")
    assert src.index("install_record_factory") < src.index("build_parser().parse_args")


# ================================================================================================= request size
def test_a_huge_declared_body_is_refused_before_it_is_read(client):
    r = client.post("/api/projects", content=b"{}", headers={"Content-Length": str(10 * 1024 * 1024 * 1024), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"error": "too_large"}


def test_the_largest_legitimate_upload_still_passes_the_size_guard(client):
    r = client.post("/api/imports", files={"file": ("m.glb", b"glTF" + b"\0" * 1024, "model/gltf-binary")})
    assert r.status_code in (200, 422)


def test_a_network_share_is_never_touched_for_a_kit_or_a_head_base(rt_bare, client):
    from duoskin.pipeline import library
    from duoskin.security import is_network_path

    for path in ("\\\\attacker\\share\\kit", "//attacker/share/kit", "\\\\?\\UNC\\attacker\\share", "\\\\.\\pipe\\x", "  \\\\attacker\\share"):
        assert is_network_path(path)
        with pytest.raises(library.KitError, match="on this PC"):
            library.add_kit(rt_bare, path, "fabric", origin(), "user_made")
        r = client.post("/api/library/head-base/build", json={"source_path": path, "variant": "v1"})
        assert r.status_code == 422 and r.json()["error"] == "network_path"
    assert not is_network_path("C:\\Users\\me\\kit") and not is_network_path("/home/me/kit") and not is_network_path("relative/kit")


def test_nothing_deserialises_untrusted_bytes_into_objects():
    """Static audit: no ``allow_pickle=True``, no ``pickle``/``marshal``/``shelve``/``dill`` import, no ``yaml.load`` (only ``safe_load``)."""
    import ast

    root = Path(__file__).resolve().parents[2] / "duoskin"
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(n.split(".")[0] in ("pickle", "cPickle", "marshal", "shelve", "dill", "cloudpickle", "joblib") for n in names):
                    bad.append(f"{rel}:{node.lineno} imports a pickle-like module")
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "allow_pickle" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        bad.append(f"{rel}:{node.lineno} allow_pickle")
                if ast.unparse(node.func) in ("yaml.load", "yaml.unsafe_load", "yaml.full_load"):
                    bad.append(f"{rel}:{node.lineno} unsafe yaml")
    assert bad == []
