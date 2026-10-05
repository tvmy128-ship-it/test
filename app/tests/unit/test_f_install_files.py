"""CHK-X06 / SYS-06 / SYS-19: the install scripts are ASCII with CRLF, and say what APP_SPEC §15 says."""
from __future__ import annotations

import re
import subprocess
import sys

import pytest

from duoskin import config

ROOT = config.APP_ROOT
BATS = ["setup.bat", "start.bat", "doctor.bat"]


@pytest.mark.parametrize("name", BATS)
def test_bat_files_are_ascii_with_crlf_only(name):
    raw = (ROOT / name).read_bytes()
    assert all(b < 128 for b in raw), f"{name} has non-ASCII bytes"
    assert b"\r\n" in raw and not re.search(rb"(?<!\r)\n", raw), f"{name} must use CRLF line endings only"
    assert b"\x1a" not in raw and not raw.startswith(b"\xef\xbb\xbf")


def test_gitattributes_forces_crlf_for_bat_files():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in attrs


@pytest.mark.parametrize("name", BATS)
def test_bat_files_never_use_activate_ps1_or_a_bare_python(name):
    text = (ROOT / name).read_text(encoding="ascii")
    assert "Activate.ps1" not in text
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(("rem", "echo")):
            continue
        assert not re.match(r"^\s*python(\.exe)?\s", stripped), f"bare python in {name}: {line}"


def test_setup_bat_follows_the_spec():
    t = (ROOT / "setup.bat").read_text(encoding="ascii")
    assert 'set "PYTHONUTF8=1"' in t and "cd /d \"%~dp0\"" in t and "chcp 65001" in t
    assert t.index("py\" \"-V:3.14") < t.index("py\" \"-V:3.13") < t.index("py\" \"-V:3.12")   # 3.14 first, then the fallbacks
    assert "tools\\probe_python.py" in t and "--require-hashes --no-deps --only-binary=:all:" in t
    assert "requirements\\win-x64.lock" in t and "-m duoskin doctor --setup" in t and "tools\\install_deps.py" in t
    assert "winget install 9NQ7512CXL7T" in t and "if not defined DUOSKIN_NOPAUSE pause" in t
    # APP_SPEC 15.2 v1.3: no uv.exe and no .python folder is shipped; with no Python found, :nopython prints the instructions
    assert "uv.exe" not in t and "--install-dir" not in t and "if not defined PYEXE goto :nopython" in t
    assert "python.org/downloads/windows" in t and "Python 3.14 - Windows installer 64-bit" in t
    # the doctor exit code is read explicitly: 0 fine, 2 installed-with-blockers, anything else (also a crash = 1) is a failure
    assert 'if "%DOCTOR_RC%"=="0" goto :done' in t and 'if "%DOCTOR_RC%"=="2" goto :warn' in t and "goto :fail" in t
    # no .bat file may block on a prompt it cannot show
    assert "<nul >nul 2>&1" in t


def test_start_bat_follows_the_spec():
    t = (ROOT / "start.bat").read_text(encoding="ascii")
    assert "-m duoskin selfcheck" in t and "-m duoskin run --open-browser" in t and 'call "%~dp0setup.bat"' in t
    assert "%LOCALAPPDATA%\\DuoSkin\\logs" in t
    assert "fc /b" in t and "win-x64.lock.installed" in t               # a new lock from an update triggers setup again


def test_setup_stamps_the_installed_lock_so_start_can_compare_it():
    setup = (ROOT / "setup.bat").read_text(encoding="ascii")
    assert 'copy /y "requirements\\win-x64.lock" ".venv\\win-x64.lock.installed"' in setup


def test_doctor_bat_runs_the_doctor():
    t = (ROOT / "doctor.bat").read_text(encoding="ascii")
    assert "-m duoskin doctor" in t and "pause" in t
    assert 'if not exist ".venv\\Scripts\\python.exe"' in t                # a friendly message instead of "path not found"


def test_probe_python_matches_the_spec():
    probe = ROOT / "tools" / "probe_python.py"
    text = probe.read_text(encoding="utf-8")
    for needle in ("win-amd64", "Py_GIL_DISABLED", "windowsapps", "(3, 14), (3, 13), (3, 12)"):
        assert needle in text
    assert subprocess.run([sys.executable, str(probe)], check=False, capture_output=True).returncode == 1       # a Linux dev host is not a supported build


def test_readme_is_plain_english_and_has_the_windows_steps():
    text = (ROOT / "README.txt").read_text(encoding="utf-8")
    assert "Unblock" in text and "C:\\DuoSkin\\app" in text and "start.bat" in text and "doctor.bat" in text
    assert "http://127.0.0.1:8765/" in text and "%LOCALAPPDATA%\\DuoSkin" in text and "Demo mode" in text
    assert "OneDrive" in text and "Media Feature Pack" in text and "vc_redist" in text
    assert text.index("Unblock") < text.index("Extract everything")                           # the order of the steps matters
    assert all(ord(c) < 128 for c in text)                                                     # opens correctly in any editor


def test_the_shell_page_keeps_the_token_placeholder():
    html = (ROOT / "duoskin" / "web" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="duoskin-token" content="__DUOSKIN_TOKEN__">' in html
    assert '<script type="module" src="/web/app.js">' in html
