"""Windows platform rules (§4.3) that also run on Linux: sockets, MIME fix, file replace, path linter, instance lock."""
from __future__ import annotations

import errno
import mimetypes
import socket

import pytest

from duoskin import winplat


def test_bind_socket_binds_loopback_only():
    s = winplat.bind_socket(0)
    try:
        host, port = s.getsockname()
        assert host == "127.0.0.1" and port > 0
    finally:
        s.close()
    with pytest.raises(ValueError):
        winplat.bind_socket(8765, host="0.0.0.0")


def test_sticky_port_then_falls_back_when_taken():
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    try:
        got = winplat.bind_socket(taken, ports=[taken, taken + 1, taken + 2, taken + 3])
        try:
            assert got.getsockname()[1] != taken and got.getsockname()[1] in (taken + 1, taken + 2, taken + 3)
        finally:
            got.close()
    finally:
        blocker.close()
    free = winplat.bind_socket(taken, ports=[])        # the sticky port is used again once it is free
    try:
        assert free.getsockname()[1] == taken
    finally:
        free.close()


def test_bind_failure_codes_mean_try_the_next_port(monkeypatch):
    calls = []
    real_socket = socket.socket

    class Flaky(real_socket):
        def bind(self, addr):
            calls.append(addr[1])
            if len(calls) == 1:
                raise OSError(10013, "WinError 10013 reserved by Hyper-V")   # WSAEACCES
            if len(calls) == 2:
                raise OSError(10048, "WinError 10048 in use")                # WSAEADDRINUSE
            return super().bind(("127.0.0.1", 0))

    monkeypatch.setattr(winplat.socket, "socket", Flaky)
    s = winplat.bind_socket(8765, ports=[8765, 8766, 8767])
    s.close()
    assert calls == [8765, 8766, 8767]


def test_all_ports_failing_raises_a_clear_error(monkeypatch):
    class Dead(socket.socket):
        def bind(self, addr):
            raise OSError(errno.EADDRINUSE, "in use")

    monkeypatch.setattr(winplat.socket, "socket", Dead)
    with pytest.raises(winplat.PortBindError):
        winplat.bind_socket(8765, ports=[8765, 8766])


def test_other_bind_errors_are_not_swallowed(monkeypatch):
    class Odd(socket.socket):
        def bind(self, addr):
            raise OSError(errno.ENOTSOCK, "weird")

    monkeypatch.setattr(winplat.socket, "socket", Odd)
    with pytest.raises(OSError) as ei:
        winplat.bind_socket(8765)
    assert not isinstance(ei.value, winplat.PortBindError)


def test_exclusive_address_option_is_only_used_on_windows():
    s = winplat.bind_socket(0)
    try:
        if winplat.IS_WINDOWS:
            assert hasattr(socket, "SO_EXCLUSIVEADDRUSE")
        else:
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
            assert not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    finally:
        s.close()


def test_fix_mimetypes_makes_js_text_javascript(monkeypatch):
    mimetypes.add_type("text/plain", ".js")                 # what a bad Windows registry entry does
    winplat.fix_mimetypes()
    assert mimetypes.guess_type("app.js")[0] == "text/javascript"
    assert mimetypes.guess_type("m.mjs")[0] == "text/javascript"
    for name, mime in (("a.css", "text/css"), ("a.svg", "image/svg+xml"), ("a.webp", "image/webp"), ("m.glb", "model/gltf-binary"),
                       ("m.gltf", "model/gltf+json"), ("a.wasm", "application/wasm")):
        assert mimetypes.guess_type(name)[0] == mime


def test_replace_with_retry_retries_permission_errors(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    real = winplat.os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "WinError 5 locked by antivirus")
        return real(a, b)

    monkeypatch.setattr(winplat.os, "replace", flaky)
    sleeps = []
    winplat.replace_with_retry(src, dst, sleep=sleeps.append)
    assert calls["n"] == 3 and dst.read_text(encoding="utf-8") == "x" and len(sleeps) == 2


def test_replace_with_retry_gives_up_after_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(winplat.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "locked")))
    with pytest.raises(PermissionError):
        winplat.replace_with_retry(tmp_path / "a", tmp_path / "b", timeout_s=0.05, sleep=lambda s: None)


def test_atomic_write_leaves_no_temp_files_and_cleans_up_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "deep" / "f.bin"
    winplat.atomic_write(target, b"\x00\x01")
    assert target.read_bytes() == b"\x00\x01" and [p.name for p in target.parent.iterdir()] == ["f.bin"]
    monkeypatch.setattr(winplat, "replace_with_retry", lambda s, d, **k: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        winplat.atomic_write(target, b"new")
    assert target.read_bytes() == b"\x00\x01" and [p.name for p in target.parent.iterdir()] == ["f.bin"]


@pytest.mark.parametrize("path,problem", [("C:\\DuoSkin\\" + "a" * 250, "characters long"), ("C:\\x\\CON.txt", "reserved"),
                                          ("C:\\x\\nul", "reserved"), ("C:\\x\\LPT1\\f", "reserved"), ("/tmp/bad.", "dot or space"),
                                          ("/tmp/bad /f", "dot or space")])
def test_path_linter_flags_problems(path, problem):
    assert any(problem in p for p in winplat.lint_path(path))


@pytest.mark.parametrize("path", ["C:\\DuoSkin\\app", "/home/user/ok/file.png", "cas/ab/" + "a" * 64 + ".png"])
def test_path_linter_accepts_normal_paths(path):
    assert winplat.lint_path(path) == []


def test_slug_rule_and_onedrive_detection():
    assert winplat.is_slug("plush-koi_2") and not winplat.is_slug("Plush") and not winplat.is_slug("con") and not winplat.is_slug("-x")
    assert winplat.is_onedrive_path("C:\\Users\\a\\OneDrive\\Docs") and not winplat.is_onedrive_path("C:\\DuoSkin")


def test_single_instance_lock(tmp_path):
    path = tmp_path / "run" / "instance.lock"
    first = winplat.single_instance(path)
    assert first is not None
    assert winplat.single_instance(path) is None            # a second copy cannot take it
    first.release()
    second = winplat.single_instance(path)
    assert second is not None
    second.release()
    second.release()                                         # releasing twice is harmless


def test_keep_awake_tracks_state_and_is_a_noop_off_windows():
    assert winplat.keep_awake(True) is winplat.IS_WINDOWS
    assert winplat.keep_awake(False) is winplat.IS_WINDOWS


def test_console_ctrl_handler_returns_a_removable_handle():
    fired = []
    handle = winplat.console_ctrl_handler(lambda: fired.append(1))
    handle.remove()
    handle.remove()


def test_quickedit_off_is_a_noop_off_windows():
    if not winplat.IS_WINDOWS:
        assert winplat.quickedit_off() is False


def test_fix_dll_directories_is_a_noop_off_windows():
    if not winplat.IS_WINDOWS:
        assert winplat.fix_dll_directories() == []
