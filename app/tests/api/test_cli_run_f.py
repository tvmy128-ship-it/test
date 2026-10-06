"""`python -m duoskin ...` end to end: run, second instance, shutdown within seconds, doctor, gc, reset-leases, diagnostics."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import zipfile

import httpx

from duoskin import config
from duoskin.engine.testkit import wait_for
from duoskin.winplat import bind_socket

ENV = {**os.environ, "PYTHONPATH": str(config.APP_ROOT), "PYTHONUTF8": "1", "DUOSKIN_PROVIDERS": ""}


def cli(*args, timeout=90, **kw):
    return subprocess.run([sys.executable, "-m", *("duoskin", *args)], check=False, capture_output=True, encoding="utf-8", timeout=timeout,
                          cwd=str(config.APP_ROOT), env=ENV, **kw)


def free_port():
    s = bind_socket(0)
    port = s.getsockname()[1]
    s.close()
    return port


def test_selfcheck_and_unknown_commands():
    assert cli("selfcheck").returncode == 0
    assert cli("nonsense").returncode == 2
    head = cli("build-head-base", "somewhere.glb", "--variant", "round")
    assert head.returncode == 1 and "later milestone" in head.stdout


def test_doctor_cli_prints_a_plain_report_and_exits_by_the_rules(tmp_path):
    r = cli("--home", str(tmp_path / "h"), "--providers", "mock", "doctor", "--quick")
    assert r.returncode == 0 and "CHK-S01" in r.stdout and "CHK-S15" in r.stdout and "What to do:" in r.stdout   # warnings alone exit 0
    j = cli("--home", str(tmp_path / "h"), "--providers", "mock", "doctor", "--quick", "--json")
    report = json.loads(j.stdout)
    assert report["blocks_paid_features"] is False and j.returncode == report["exit_code"] == 0
    assert report["summary"]["warnings"] >= 2 and report["flags"]["dreamsim_present"] is False
    # the report was saved to the DB so the app can show it on its first start
    from duoskin.engine.testkit import make_runtime

    rt = make_runtime(tmp_path / "h")
    try:
        assert rt.doctor_report["exit_code"] == 0
    finally:
        rt.shutdown()


def test_gc_reset_leases_and_diagnostics_commands(tmp_path):
    home = str(tmp_path / "h")
    assert "would delete 0 unreferenced assets" in cli("--home", home, "gc", "--dry-run").stdout
    assert cli("--home", home, "gc", "--yes").returncode == 0
    r = cli("--home", home, "reset-leases")
    assert r.returncode == 0 and "re-queued" in r.stdout
    out = cli("--home", home, "export-diagnostics")
    assert out.returncode == 0
    path = out.stdout.strip().splitlines()[-1]
    assert path.endswith(".zip") and "info.json" in zipfile.ZipFile(path).namelist()


def test_run_serves_shuts_down_cleanly_and_a_second_start_reuses_it(tmp_path):
    home = tmp_path / "h"
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "duoskin", "--home", str(home), "--providers", "mock", "run", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", cwd=str(config.APP_ROOT), env=ENV)
    base = f"http://127.0.0.1:{port}"
    try:
        def healthy():
            try:
                return httpx.get(base + "/api/health", timeout=2).json()["ok"]
            except httpx.HTTPError:
                return False

        wait_for(healthy, 60, 0.2, message="the server to answer /api/health")
        assert httpx.get(base + "/api/health").json()["demo"] is True
        info = json.loads((home / "run" / "server.json").read_text(encoding="utf-8"))
        assert info["port"] == port and info["url"] == base + "/" and info["pid"] == proc.pid
        assert config.load_settings(home).port == port                                            # sticky port saved
        page = httpx.get(base + "/")
        token = re.search(r'name="duoskin-token" content="([^"]+)"', page.text).group(1)
        assert page.headers["cache-control"] == "no-store"
        # a second start does not start a second server: it finds the first one and exits 0
        second = cli("--home", str(home), "--providers", "mock", "run", "--port", str(port))
        assert second.returncode == 0 and base in second.stdout
        # security on the live socket: no token -> 403, wrong Host -> 400
        assert httpx.post(base + "/api/shutdown").status_code == 403
        assert httpx.get(base + "/api/health", headers={"Host": "evil.example"}).status_code == 400
        started = time.monotonic()
        assert httpx.post(base + "/api/shutdown", headers={"X-DuoSkin-Token": token}).status_code == 202
        proc.wait(timeout=15)
        assert time.monotonic() - started < 8                                                       # CHK-X05: exits within seconds
        assert proc.returncode == 0
        assert not (home / "run" / "server.json").exists()
        # the lock is free again and the WAL was checkpointed (no big -wal file left behind)
        wal = home / "duoskin.sqlite3-wal"
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


def test_run_falls_back_to_another_port_when_the_sticky_port_is_taken(tmp_path):
    import socket

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    home = tmp_path / "h"
    config.save_settings(config.load_settings(home).model_copy(update={"port": taken}), home)
    proc = subprocess.Popen([sys.executable, "-m", "duoskin", "--home", str(home), "--providers", "mock", "run"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding="utf-8", cwd=str(config.APP_ROOT), env=ENV)
    try:
        def info():
            try:
                return json.loads((home / "run" / "server.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        found = wait_for(info, 60, 0.2, message="server.json")
        assert found["port"] != taken and 8765 <= found["port"] <= 8799
        def healthy():
            try:
                return httpx.get(f"http://127.0.0.1:{found['port']}/api/health", timeout=2).json()["ok"]
            except httpx.HTTPError:          # server.json is written before uvicorn listens: keep polling
                return False

        wait_for(healthy, 60, 0.2, message="the health endpoint")
    finally:
        proc.kill()
        proc.wait(10)
        proc.stdout.close()
        blocker.close()
