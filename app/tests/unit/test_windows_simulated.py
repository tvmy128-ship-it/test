"""The Windows-only branches of ``winplat`` and the app, run on Linux against fakes (SYS-06, SYS-09, SYS-12, SYS-16).

Nothing here proves that the real Win32 calls behave: it proves that OUR code around them is wired correctly (right flags,
right order, nothing raised, cleanup done) and that the whole package still imports and serves requests when
``sys.platform == "win32"``. The list of what only a real Windows PC can confirm is in README.txt / the review report.
"""
from __future__ import annotations

import ctypes
import socket
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from duoskin import config, keystore, winplat

ROOT = config.APP_ROOT


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(winplat, "IS_WINDOWS", True)


class FakeKernel32:
    """Records the calls our code makes; ``GetConsoleMode`` writes ``self.console_mode`` into the ``DWORD`` it is handed."""

    def __init__(self) -> None:
        self.console_mode = 0x01F7                      # QuickEdit (0x40) and Insert (0x20) on
        self.set_console_mode: list[int] = []
        self.execution_state: list[int] = []
        self.ctrl_handlers: list[tuple[object, int]] = []
        self.freed: list[object] = []
        self.std_handle_requests: list[int] = []

    def GetStdHandle(self, which):
        self.std_handle_requests.append(which)
        return 7

    def GetConsoleMode(self, handle, ref):
        ref._obj.value = self.console_mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_console_mode.append(mode)
        return 1

    def SetThreadExecutionState(self, flags):
        self.execution_state.append(flags)
        return 0x80000000

    def SetConsoleCtrlHandler(self, handler, add):
        self.ctrl_handlers.append((handler, add))
        return 1

    def LocalFree(self, ptr):
        self.freed.append(ptr)
        return 0


@pytest.fixture
def kernel32(monkeypatch, as_windows):
    fake = FakeKernel32()
    windll = types.SimpleNamespace(kernel32=fake)
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)     # same shape, callable on Linux
    return fake


def test_quickedit_is_cleared_and_extended_flags_are_set(kernel32):
    assert winplat.quickedit_off() is True
    assert kernel32.std_handle_requests == [-10]                                   # STD_INPUT_HANDLE
    (mode,) = kernel32.set_console_mode
    assert not mode & 0x0040 and mode & 0x0080 and mode & 0x0020                    # QuickEdit off, EXTENDED_FLAGS on, rest kept


def test_quickedit_does_nothing_when_there_is_no_console(kernel32):
    kernel32.GetConsoleMode = lambda handle, ref: 0                                 # redirected stdin: the call fails
    assert winplat.quickedit_off() is False
    assert kernel32.set_console_mode == []


def test_keep_awake_sets_and_clears_the_execution_state(kernel32):
    assert winplat.keep_awake(True) is True
    assert winplat.keep_awake(False) is True
    assert kernel32.execution_state == [0x80000001, 0x80000000]                     # CONTINUOUS|SYSTEM_REQUIRED, then CONTINUOUS


def test_console_handler_runs_the_callback_for_close_logoff_shutdown_and_ctrl_c(kernel32):
    fired: list[int] = []
    handle = winplat.console_ctrl_handler(lambda: fired.append(1))
    (callback, add), = kernel32.ctrl_handlers
    assert add == 1
    for event in (0, 1, 2, 5, 6):                                                   # C, BREAK, CLOSE, LOGOFF, SHUTDOWN
        assert callback(event) == 1                                                 # handled
    assert len(fired) == 5
    assert callback(3) == 0 and len(fired) == 5                                     # an unknown event is passed on
    handle.remove()
    assert kernel32.ctrl_handlers[-1][1] == 0                                       # unregistered with the same callback
    assert kernel32.ctrl_handlers[-1][0] is callback


def test_console_handler_survives_a_callback_that_raises(kernel32):
    def boom() -> None:
        raise RuntimeError("shutdown failed")

    winplat.console_ctrl_handler(boom)
    (callback, _add), = kernel32.ctrl_handlers
    assert callback(2) == 1                                                         # never lets the exception reach Windows


# ---------------------------------------------------------------------------------------------------------------- DPAPI
class FakeCrypt32:
    """A reversible stand-in for CryptProtectData / CryptUnprotectData that marshals ``DATA_BLOB`` exactly like the real ones."""

    def __init__(self) -> None:
        self._keep: list[object] = []
        self.calls: list[tuple[str, int]] = []

    def _transform(self, name, blob_in, blob_out, fn):
        src = blob_in._obj
        data = ctypes.string_at(src.pbData, src.cbData)
        out = fn(data)
        buf = ctypes.create_string_buffer(out, len(out))
        self._keep.append(buf)
        blob_out._obj.cbData = len(out)
        blob_out._obj.pbData = ctypes.cast(buf, type(src.pbData))
        self.calls.append((name, len(data)))
        return 1

    def CryptProtectData(self, blob_in, descr, entropy, reserved, prompt, flags, blob_out):
        assert descr is None and entropy is None and reserved is None and prompt is None and flags == 0
        return self._transform("protect", blob_in, blob_out, lambda d: b"DPAPI" + d[::-1])

    def CryptUnprotectData(self, blob_in, descr, entropy, reserved, prompt, flags, blob_out):
        assert descr is None and entropy is None and reserved is None and prompt is None and flags == 0
        return self._transform("unprotect", blob_in, blob_out, lambda d: d[len(b"DPAPI"):][::-1])


@pytest.fixture
def dpapi(monkeypatch, kernel32):
    fake = FakeCrypt32()
    monkeypatch.setattr(ctypes.windll, "crypt32", fake, raising=False)
    return fake


def test_dpapi_round_trip_and_frees_the_output_buffer(dpapi, kernel32):
    secret = "sk-ant-api03-\u00e9\u4e2d-test".encode()
    blob = winplat.dpapi_protect(secret)
    assert blob != secret and secret not in blob
    assert winplat.dpapi_unprotect(blob) == secret
    assert [c[0] for c in dpapi.calls] == ["protect", "unprotect"]
    assert len(kernel32.freed) == 2                                                 # LocalFree for each output blob


def test_dpapi_failure_raises_an_oserror(dpapi):
    dpapi.CryptProtectData = lambda *a: 0
    with pytest.raises(OSError):
        winplat.dpapi_protect(b"x")


def test_dpapi_is_unavailable_off_windows():
    if not winplat.IS_WINDOWS:
        with pytest.raises(winplat.DpapiUnavailable):
            winplat.dpapi_protect(b"x")


def test_keystore_on_windows_falls_back_to_a_dpapi_file_that_never_holds_plain_text(dpapi, tmp_path):
    paths = config.DataPaths(tmp_path / "home").ensure()
    store = keystore.KeyStore(paths, keyring_module=False, environ={})
    assert store.set_key("openai", "sk-test-1234567890abcdef") == "dpapi_file"
    raw = paths.secrets_dpapi.read_bytes()
    assert b"sk-test-1234567890abcdef" not in raw and paths.secrets_dev.exists() is False
    assert keystore.KeyStore(paths, keyring_module=False, environ={}).get_key("openai") == "sk-test-1234567890abcdef"
    assert store.status_of("openai").source == "dpapi_file"


# ---------------------------------------------------------------------------------------------------- instance lock
class FakeMsvcrt(types.ModuleType):
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.held = False
        self.calls: list[tuple[int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK:
            if self.held:
                raise OSError(13, "Permission denied")                              # what Windows says for a locked region
            self.held = True
        else:
            self.held = False


def test_single_instance_uses_msvcrt_locking_on_windows(monkeypatch, as_windows, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    path = tmp_path / "run" / "instance.lock"
    first = winplat.single_instance(path)
    assert first is not None and fake.held
    assert winplat.single_instance(path) is None                                    # the second copy is refused, not crashed
    first.release()
    assert not fake.held and fake.calls[-1] == (FakeMsvcrt.LK_UNLCK, 1)
    again = winplat.single_instance(path)
    assert again is not None
    again.release()


# ------------------------------------------------------------------------------------------------------------ DLL dirs
def test_dll_directories_are_added_only_where_msvcp140_is(monkeypatch, as_windows, tmp_path):
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "msvcp140.dll").write_bytes(b"MZ")
    added: list[str] = []
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(winplat.os, "add_dll_directory", lambda p: added.append(p), raising=False)
    assert winplat.fix_dll_directories() == [str(tmp_path / "Scripts")]
    assert added == [str(tmp_path / "Scripts")]


# ---------------------------------------------------------------------------------------------------------------- sockets
def test_windows_sockets_get_exclusive_address_use(monkeypatch, as_windows):
    options: list[tuple[int, int, int]] = []

    class Recording(socket.socket):
        def setsockopt(self, level, optname, value, *rest):
            options.append((level, optname, value))
            if optname == 0x7FFFFFFC:                                               # SO_EXCLUSIVEADDRUSE: unknown to Linux
                return None
            return super().setsockopt(level, optname, value, *rest)

    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", 0x7FFFFFFC, raising=False)
    monkeypatch.setattr(winplat.socket, "socket", Recording)
    sock = winplat.bind_socket(0)
    try:
        assert (socket.SOL_SOCKET, 0x7FFFFFFC, 1) in options
        assert not any(o[1] == socket.SO_REUSEADDR for o in options)                # REUSEADDR would let another process share it
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


# ------------------------------------------------------------------------------------------------------------ processes
def test_run_process_builds_windows_creation_flags_without_posix_options(monkeypatch):
    from duoskin.mesh import proc

    seen: dict = {}
    real_popen = subprocess.Popen

    class Spy(real_popen):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            kwargs.pop("creationflags", None)                                      # Linux accepts only 0; keep the test honest
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc.subprocess, "Popen", Spy)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    result = proc.run_process([sys.executable, "-c", "print('ok')"], timeout_s=60)
    assert result.ok and "ok" in result.stdout
    assert seen["creationflags"] == 0x08000000 | 0x00000200
    assert "start_new_session" not in seen                                          # POSIX-only


# ------------------------------------------------------------------------------------------------ the whole package
SMOKE = textwrap.dedent('''
    import importlib, os, pkgutil, sys, tempfile, types
    from pathlib import Path

    # Third-party modules look at sys.platform while they import: load them first, then pretend to be Windows.
    for name in ("numpy", "PIL.Image", "cv2", "scipy.ndimage", "scipy.spatial", "scipy.signal", "skimage", "imagehash", "trimesh",
                 "networkx", "manifold3d", "pydantic", "fastapi", "starlette", "uvicorn", "sse_starlette", "httpx", "keyring",
                 "platformdirs", "psutil", "yaml", "truststore", "defusedxml", "resvg_py", "pygltflib", "multipart", "anthropic",
                 "openai"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass

    # asyncio (3.14 creates its event-loop policy lazily) and anyio (the TestClient) also branch on sys.platform when they
    # first start a loop: start one now, while the platform is still the real one.
    import asyncio
    import anyio
    asyncio.run(asyncio.sleep(0))
    anyio.run(anyio.sleep, 0)

    class Win32Api:
        """ctypes.windll stand-in: every Win32 call succeeds and returns 1."""
        def __getattr__(self, name):
            return lambda *a, **k: 1

    import ctypes
    ctypes.windll = Win32Api()
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    msvcrt = types.ModuleType("msvcrt")
    msvcrt.LK_NBLCK, msvcrt.LK_UNLCK, msvcrt.locking = 2, 0, lambda *a: None
    sys.modules["msvcrt"] = msvcrt
    sys.platform = "win32"

    import duoskin
    failed = []
    for mod in pkgutil.walk_packages(duoskin.__path__, "duoskin."):
        if "blender_scripts" in mod.name:
            continue                                   # these run inside Blender
        try:
            importlib.import_module(mod.name)
        except Exception as exc:                       # noqa: BLE001
            failed.append(f"{mod.name}: {type(exc).__name__}: {str(exc)[:120]}")
    assert not failed, failed

    from duoskin import winplat
    assert winplat.IS_WINDOWS is True
    from duoskin.engine.testkit import make_app, make_client

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = make_app(Path(tmp) / "home", providers_mode="mock")
        with make_client(app) as client:                # runs the lifespan: scheduler thread, keep_awake, event bus
            assert client.get("/api/health").status_code == 200
            js = client.get("/web/app.js")
            assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript"), js.headers
            assert client.get("/").status_code == 200
    print("WIN32-SMOKE-OK")
''')


def test_the_whole_package_imports_and_serves_pages_when_the_platform_is_win32(tmp_path):
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", SMOKE], cwd=str(ROOT), capture_output=True, encoding="utf-8",
                          errors="replace", timeout=300, check=False, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and "WIN32-SMOKE-OK" in proc.stdout, proc.stdout[-1500:] + "\n" + proc.stderr[-3000:]


def test_the_entry_point_under_main_guard_is_importable_without_side_effects():
    # spawn (Windows) re-imports __main__: importing the module must not start the app
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", "import duoskin.__main__ as m; print(callable(m.main))"], cwd=str(ROOT),
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and proc.stdout.strip() == "True", proc.stderr[-1500:]


def test_probe_python_accepts_only_supported_windows_builds(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_python", ROOT / "tools" / "probe_python.py")
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    import sysconfig

    def patch(platform="win-amd64", gil_disabled=0, base_prefix="C:\\Users\\a\\AppData\\Local\\Programs\\Python\\Python313", version=(3, 13, 1)):
        monkeypatch.setattr(sysconfig, "get_platform", lambda: platform)
        real = sysconfig.get_config_var
        monkeypatch.setattr(sysconfig, "get_config_var", lambda name: gil_disabled if name == "Py_GIL_DISABLED" else real(name))
        monkeypatch.setattr(sys, "base_prefix", base_prefix)
        monkeypatch.setattr(sys, "version_info", version + ("final", 0) if len(version) == 3 else version, raising=False)

    for v in ((3, 14, 0), (3, 13, 5), (3, 12, 10)):
        patch(version=v)
        assert probe.is_supported(), v
    for v in ((3, 11, 9), (3, 15, 0), (3, 10, 0)):
        patch(version=v)
        assert not probe.is_supported(), v
    patch(platform="win32")
    assert not probe.is_supported()                                               # 32-bit
    patch(platform="win-arm64")
    assert not probe.is_supported()
    patch(gil_disabled=1)
    assert not probe.is_supported()                                               # 3.14t
    patch(base_prefix="C:\\Program Files\\WindowsApps\\PythonSoftwareFoundation.Python.3.13_3.13.1520.0_x64__qbz5n2kfra8p0")
    assert not probe.is_supported()                                               # the Microsoft Store build


def test_wheelhouse_path_helpers_do_not_assume_a_drive():
    assert Path(config.APP_ROOT).is_absolute()


def test_log_rollover_survives_a_locked_log_file(tmp_path, monkeypatch):
    """WinError 32 while renaming duoskin.log (antivirus, OneDrive, a second copy of the app) must not lose or break logging."""
    import logging

    from duoskin import logsetup

    handler = logsetup.SafeRotatingFileHandler(tmp_path / "duoskin.log", maxBytes=200, backupCount=2, encoding="utf-8")
    attempts: list[int] = []

    def locked(src, dst):
        attempts.append(1)
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(logging.handlers.os, "rename", locked)
    logger = logging.getLogger("duoskin.test.rollover")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for i in range(30):
            logger.info("line %03d %s", i, "x" * 40)
    finally:
        logger.removeHandler(handler)
        handler.close()
    text = (tmp_path / "duoskin.log").read_text(encoding="utf-8")
    assert "line 029" in text and len(attempts) == 1             # tried once, then backed off for a minute


def test_doctor_probe_children_register_the_vc_runtime_folders_like_the_app_does(monkeypatch, tmp_path):
    """SYS-09: without this, `import onnxruntime` in a probe child fails on a PC that lacks the VC++ redistributable."""
    from duoskin.api import doctor_checks as dc

    (tmp_path / "Scripts").mkdir()
    (tmp_path / "msvcp140.dll").write_bytes(b"MZ")
    (tmp_path / "Scripts" / "msvcp140.dll").write_bytes(b"MZ")
    added: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(dc.os, "add_dll_directory", lambda p: added.append(p), raising=False)
    exec(compile(dc._DLL_PRELUDE, "<prelude>", "exec"), {})                       # noqa: S102 - our own constant
    assert added == [str(tmp_path), str(tmp_path / "Scripts")]
    monkeypatch.undo()
    rc, out, _err = dc.run_py("print('probe child ok')")                          # and it is valid, harmless code on this host
    assert rc == 0 and "probe child ok" in out


def test_new_ids_keep_creation_order_even_when_the_clock_is_coarse_or_goes_backwards(monkeypatch):
    """Python 3.12 on Windows has a ~15.6 ms clock: ids made in one tick must still sort in creation order."""
    from duoskin.models import common

    ticks = iter([1_700_000_000.000] * 50 + [1_699_999_999.000] * 50 + [1_700_000_000.016] * 50)
    monkeypatch.setattr(common.time, "time", lambda: next(ticks))
    ids = [common.new_id("stp") for _ in range(150)]
    assert ids == sorted(ids) and len(set(ids)) == 150
    assert all(i.startswith("stp_") and len(i) == 4 + 26 and i == i.lower() for i in ids)


def test_hand_edited_json_with_a_utf8_bom_is_read(tmp_path):
    """Notepad (older versions) and PowerShell 5 save UTF-8 with a BOM; a user-edited settings or kit file must still load."""
    from duoskin.pipeline import kits

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_bytes(b"\xef\xbb\xbf" + b'{"schema_version": 1, "demo_mode": true}')
    assert config.load_settings(home).demo_mode is True
    assert not list(home.glob("settings.corrupt-*"))                       # not mistaken for a corrupt file
    kit = tmp_path / "style.json"
    kit.write_bytes(b"\xef\xbb\xbf" + '{"id": "café"}'.encode())
    assert kits._read_json(kit) == {"id": "café"}
