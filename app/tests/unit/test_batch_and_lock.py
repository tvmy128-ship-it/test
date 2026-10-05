"""setup.bat / start.bat / doctor.bat and the lock files, checked without a Windows PC.

* A linter for the batch syntax rules that bite (labels, parentheses, specials in ``rem`` and ``echo``, unquoted ``%~dp0``).
* A tiny interpreter for the subset of cmd.exe the scripts use, so that the control flow (errorlevels, ``goto``, ``call``,
  fall-through, exit codes) is executed for many "what if" worlds: no Python, only 3.12, pip fails, doctor returns 2 ...
  It models our reading of cmd semantics, not cmd itself: the first run on a real PC is still the real test.
* ``requirements/win-x64.lock`` against ``pyproject.toml`` and ``tools/check_lock.py``.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from duoskin import config

ROOT = config.APP_ROOT
BATS = ["setup.bat", "start.bat", "doctor.bat"]


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ============================================================================================================== linter
def _lines(name: str) -> list[str]:
    return (ROOT / name).read_bytes().decode("ascii").split("\r\n")


@pytest.mark.parametrize("name", BATS)
def test_every_goto_and_call_target_exists_and_labels_are_unique(name):
    lines = _lines(name)
    labels = [ln[1:].strip().lower() for ln in lines if ln.startswith(":") and not ln.startswith("::")]
    assert len(labels) == len(set(labels)), f"duplicate labels in {name}"
    for ln in lines:
        for m in re.finditer(r"\b(?:goto|call)\s+:?([A-Za-z_][\w]*)", ln, re.IGNORECASE):
            target = m.group(1).lower()
            if target in ("eof",) or ln.lstrip().lower().startswith(("rem", "echo")):
                continue
            if re.search(r'call\s+"', ln, re.IGNORECASE):
                continue
            assert target in labels, f"{name}: '{ln.strip()}' jumps to a label that does not exist"


@pytest.mark.parametrize("name", BATS)
def test_subroutines_cannot_be_entered_by_falling_through(name):
    lines = _lines(name)
    called = {m.group(1).lower() for ln in lines for m in re.finditer(r"\bcall\s+:(\w+)", ln, re.IGNORECASE)}
    for i, ln in enumerate(lines):
        if ln.startswith(":") and ln[1:].strip().lower() in called:
            prev = next((p.strip().lower() for p in reversed(lines[:i]) if p.strip()), "")
            assert prev.startswith(("exit /b", "goto ")), f"{name}: control falls into subroutine {ln} from '{prev}'"


@pytest.mark.parametrize("name", BATS)
def test_parentheses_balance_outside_rem_and_echo(name):
    depth = 0
    for ln in _lines(name):
        low = ln.strip().lower()
        if low.startswith(("rem", "echo", ":")):
            continue
        for ch in re.sub(r'"[^"]*"', "", ln):
            depth += ch == "("
            depth -= ch == ")"
            assert depth >= 0, f"{name}: ')' without '(' in: {ln}"
    assert depth == 0, f"{name}: unbalanced parentheses"


@pytest.mark.parametrize("name", BATS)
def test_rem_and_echo_lines_contain_no_cmd_special_characters(name):
    for ln in _lines(name):
        low = ln.strip().lower()
        if low.startswith("rem"):
            assert not re.search(r"[<>|&^]", ln) and "%" not in ln, f"{name}: special character in a rem line: {ln}"
        if low.startswith("echo ") and " | findstr " in ln:
            continue                                                             # a deliberate pipe, not text
        if low.startswith("echo ") or low == "echo.":
            assert not re.search(r"[<>|&^]", ln), f"{name}: unescaped special character in: {ln}"
            bare = re.sub(r"%\w+%", "", ln)
            assert "%" not in bare, f"{name}: stray % in: {ln}"


@pytest.mark.parametrize("name", BATS)
def test_dp0_is_always_quoted_and_cd_uses_d(name):
    for ln in _lines(name):
        for m in re.finditer(r"%~dp0", ln):
            assert ln[:m.start()].count('"') % 2 == 1, f"{name}: %~dp0 outside quotes (breaks on a path with spaces): {ln}"
    assert 'cd /d "%~dp0"' in "\r\n".join(_lines(name))


@pytest.mark.parametrize("name", BATS)
def test_no_parentheses_inside_echo_in_blocks(name):
    inside = 0
    for ln in _lines(name):
        low = ln.strip().lower()
        if low.startswith("echo") and inside:
            assert "(" not in ln and ")" not in ln, f"{name}: parenthesis in an echo inside a block: {ln}"
        if not low.startswith(("rem", "echo")):
            inside += ln.count("(") - ln.count(")")


# ================================================================================================= cmd subset interpreter
@dataclass
class World:
    """What the PC looks like. ``commands`` maps the start of a command line to its exit code (9009 = not recognised)."""

    files: set[str] = field(default_factory=set)
    commands: dict[str, int] = field(default_factory=dict)
    effects: dict[str, Callable[[World], None]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=lambda: {"LOCALAPPDATA": "C:\\Users\\Jane Doe\\AppData\\Local"})
    lock_changed: bool = False
    log: list[str] = field(default_factory=list)
    out: list[str] = field(default_factory=list)

    def exists(self, path: str) -> bool:
        p = path.strip('"').replace("/", "\\").lower().rstrip("\\")
        return any(f.lower() == p or f.lower().startswith(p + "\\") for f in self.files)

    @staticmethod
    def norm(cmdline: str) -> str:
        """cmd treats ``"py" -V:3.14`` and ``py -V:3.14`` alike: drop the quotes around a first word without spaces."""
        return re.sub(r'^"([^" ]+)"', r"\1", cmdline.strip())

    def run(self, cmdline: str) -> int:
        cmdline = re.sub(r"\s+", " ", self.norm(cmdline))
        self.log.append(cmdline)
        if cmdline.lower().startswith("fc /b"):
            return 0 if self.exists(".venv\\win-x64.lock.installed") and not self.lock_changed else 1
        for prefix in sorted(self.commands, key=len, reverse=True):
            if cmdline.lower().startswith(self.norm(prefix).lower()):
                if prefix in self.effects:
                    self.effects[prefix](self)
                return self.commands[prefix]
        return 9009


class BatError(AssertionError):
    pass


NEXT = ("next", None)


class Bat:
    """Runs one .bat file in a ``World`` and returns its exit code (``exit /b N``; 0 at the end of the file)."""

    def __init__(self, name: str, world: World, env: dict[str, str] | None = None) -> None:
        self.name, self.world = name, world
        self.lines = _lines(name)
        self.labels = {ln[1:].strip().lower(): i for i, ln in enumerate(self.lines) if ln.startswith(":") and not ln.startswith("::")}
        self.env = dict(world.env)
        self.env.update(env or {})
        self.errorlevel = 0
        self.stack: list[tuple[int, list[str]]] = []
        self.args: list[str] = []
        self.steps = 0

    def expand(self, text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            key = m.group(1) or m.group(2)
            if key == "~dp0":
                return "C:\\DuoSkin\\app\\"
            if re.fullmatch(r"~?\d", key):
                idx = int(key[-1]) - 1
                val = self.args[idx] if idx < len(self.args) else ""
                return val.strip('"') if key.startswith("~") else val
            if key.lower() == "errorlevel":
                return str(self.errorlevel)
            return next((v for k, v in self.env.items() if k.lower() == key.lower()), "")

        return re.sub(r"%(~dp0|~\d|\d)|%(\w+)%", sub, text)

    def run(self) -> int:
        pc = 0
        while pc < len(self.lines):
            self.steps += 1
            if self.steps > 5000:
                raise BatError(f"{self.name}: runaway loop")
            text = self.lines[pc].strip()
            if not text or text.startswith((":", "@")) or text.lower().startswith("rem"):
                pc += 1
                continue
            if text.lower().startswith("if ") and text.endswith("("):
                flow = self.conditional(text, pc, block=True)
                pc = self.block_end(pc) + 1 if flow == NEXT else pc
                if flow != NEXT:
                    kind, arg = flow
                    if kind == "exit":
                        return arg
                    pc = arg
                continue
            kind, arg = self.statement(text, pc)
            if kind == "exit":
                return arg
            pc = arg if kind == "jump" else pc + 1
        return 0

    def block_end(self, pc: int) -> int:
        depth = 0
        for i in range(pc, len(self.lines)):
            text = self.lines[i].strip()
            if text.lower().startswith(("rem", "echo")) and i > pc:
                continue
            depth += text.count("(") - text.count(")")
            if depth <= 0 and i > pc:
                return i
        raise BatError("block not closed")

    def condition(self, text: str) -> tuple[bool, str]:
        m = re.match(r"if\s+(not\s+)?(exist|defined|errorlevel)\s+(\"[^\"]*\"|\S+)\s*(.*)$", text, re.IGNORECASE)
        m2 = re.match(r'if\s+(not\s+)?"([^"]*)"=="([^"]*)"\s*(.*)$', text, re.IGNORECASE)
        if m:
            neg, kind, arg, rest = bool(m.group(1)), m.group(2).lower(), self.expand(m.group(3)), m.group(4)
            if kind == "exist":
                cond = self.world.exists(arg)
            elif kind == "defined":
                cond = any(k.lower() == arg.strip('"').lower() and v for k, v in self.env.items())
            else:
                cond = self.errorlevel >= int(arg)
        elif m2:
            neg, rest = bool(m2.group(1)), m2.group(4)
            cond = self.expand(m2.group(2)) == self.expand(m2.group(3))
        else:
            raise BatError(f"unsupported if: {text}")
        return (not cond if neg else cond), rest

    def conditional(self, text: str, pc: int, block: bool = False):
        cond, rest = self.condition(text)
        if block:
            if not cond:
                return NEXT
            for inner in self.lines[pc + 1:self.block_end(pc)]:
                flow = self.statement(inner.strip(), pc)
                if flow != NEXT:
                    return flow
            return NEXT
        return self.statement(rest, pc) if cond else NEXT

    def statement(self, text: str, pc: int):
        low = text.lower()
        if not text or low.startswith("rem"):
            return NEXT
        if low.startswith("if "):
            return self.conditional(text, pc)
        if not low.startswith("echo") and "||" in text:
            left, right = (x.strip() for x in text.split("||", 1))
            flow = self.statement(left, pc)
            if flow != NEXT:
                return flow
            return self.statement(right, pc) if self.errorlevel != 0 else NEXT
        if low.startswith("goto"):
            label = text.split(None, 1)[1].lstrip(":").lower()
            if label == "eof":
                return self.leave(0)
            if label not in self.labels:
                raise BatError(f"{self.name}: goto {label}: no such label")
            return ("jump", self.labels[label] + 1)
        if low.startswith("call "):
            rest = text[5:].strip()
            if rest.startswith(":"):
                parts = re.findall(r'"[^"]*"|\S+', rest)
                label = parts[0][1:].lower()
                if label not in self.labels:
                    raise BatError(f"{self.name}: call :{label}: no such label")
                self.stack.append((pc + 1, self.args))
                self.args = [self.expand(a) for a in parts[1:]]
                return ("jump", self.labels[label] + 1)
            target = self.expand(rest).strip('"').replace("\\", "/").rsplit("/", 1)[-1]
            self.errorlevel = Bat(target, self.world, self.env).run()
            return NEXT
        if low.startswith("exit"):
            last = text.split()[-1]
            code = int(self.expand(last)) if self.expand(last).lstrip("-").isdigit() else 0
            return self.leave(code)
        if low.startswith("set "):
            m = re.match(r'set\s+"([^=]+)=(.*)"$', text, re.IGNORECASE)
            if not m:
                raise BatError(f"unsupported set: {text}")
            key, val = m.group(1), self.expand(m.group(2))
            for k in [k for k in self.env if k.lower() == key.lower()]:
                del self.env[k]
            if val:
                self.env[key] = val
            return NEXT
        if low.startswith(("setlocal", "chcp", "cd ", "title", "endlocal")):
            return NEXT
        if low.startswith("pause"):
            self.world.log.append("PAUSE")
            return NEXT
        if low.startswith("rmdir"):
            self.world.files = {f for f in self.world.files if not f.lower().startswith(".venv")}
            self.world.log.append("RMDIR .venv")
            return NEXT
        if low.startswith("copy "):
            self.world.files.add(".venv\\win-x64.lock.installed")
            return NEXT
        if low.startswith("echo") and "|" not in text:
            self.world.out.append(self.expand(text[4:].lstrip(". ")))
            return NEXT
        cmd = re.sub(r"\s*(<nul|>nul|2>&1)", "", self.expand(text)).strip()
        self.errorlevel = self.world.run(cmd)
        return NEXT

    def leave(self, code: int):
        self.errorlevel = code
        if self.stack:
            pc, args = self.stack.pop()
            self.args = args
            return ("jump", pc)
        return ("exit", code)


# ---- worlds
VENV_PY = ".venv\\Scripts\\python.exe"
LOCK = "requirements\\win-x64.lock"


def good_world(**over) -> World:
    w = World(files={"setup.bat", "tools\\probe_python.py", "requirements\\win-x64.lock", "tools\\install_deps.py", "wheelhouse"})
    w.commands.update({
        'py -V:3.14 "tools\\probe_python.py"': 0, '"py" -V:3.14 --version': 0, 'py -V:3.14 --version': 0,
        'py -V:3.14 -m venv': 0,
        f"{VENV_PY} -m pip install --require-hashes": 0, f"{VENV_PY} -m duoskin selfcheck": 0,
        f"{VENV_PY} -m duoskin doctor": 0, f"{VENV_PY} -m duoskin run": 0, f'{VENV_PY} "tools\\install_deps.py"': 0,
        f'{VENV_PY} "tools\\probe_python.py"': 0,
    })

    def make_venv(world: World) -> None:
        world.files.add(".venv\\Scripts\\python.exe")

    w.effects["py -V:3.14 -m venv"] = make_venv
    for k, v in over.items():
        w.commands[k] = v
    return w


def run_bat(name: str, world: World, env: dict[str, str] | None = None) -> int:
    return Bat(name, world, env).run()


def test_setup_happy_path_with_python_3_14():
    w = good_world()
    assert run_bat("setup.bat", w) == 0
    assert any(c.startswith(f"{VENV_PY} -m pip install --require-hashes --no-deps --only-binary=:all: --find-links wheelhouse -r {LOCK}") for c in w.log)
    assert w.exists(".venv\\win-x64.lock.installed") and "Setup complete." in w.out
    assert "PAUSE" in w.log                                                       # double-clicked: the window stays open


def test_setup_falls_back_to_3_13_then_3_12():
    for tag, label in (("3.13", "py -V:3.13"), ("3.12", "py -V:3.12")):
        w = good_world()
        w.commands.pop('py -V:3.14 "tools\\probe_python.py"')
        for older in ("3.13", "3.12"):
            if older < tag:
                w.commands[f'py -V:{older} "tools\\probe_python.py"'] = 1
        w.commands[f'py -V:{tag} "tools\\probe_python.py"'] = 0
        w.commands[f"py -V:{tag} --version"] = 0
        w.commands[f"py -V:{tag} -m venv"] = 0
        w.effects[f"py -V:{tag} -m venv"] = lambda world: world.files.add(".venv\\Scripts\\python.exe")
        assert run_bat("setup.bat", w) == 0, label
        assert any(f"py -V:{tag} -m venv" in c for c in w.log)


def test_setup_understands_an_old_py_launcher_that_only_knows_the_dash_syntax():
    w = good_world()
    for k in [k for k in w.commands if k.startswith("py ")]:
        del w.commands[k]
    w.commands['py -3.13 "tools\\probe_python.py"'] = 0
    w.commands["py -3.13 --version"] = 0
    w.commands["py -3.13 -m venv"] = 0
    w.effects["py -3.13 -m venv"] = lambda world: world.files.add(".venv\\Scripts\\python.exe")
    assert run_bat("setup.bat", w) == 0
    assert any("py -3.13 -m venv" in c for c in w.log)


def test_setup_finds_python_exe_without_the_py_launcher():
    w = good_world()
    for k in [k for k in w.commands if k.startswith("py ")]:
        del w.commands[k]
    w.commands['python "tools\\probe_python.py"'] = 0
    w.commands["python --version"] = 0
    w.commands["python -m venv"] = 0
    w.effects["python -m venv"] = lambda world: world.files.add(".venv\\Scripts\\python.exe")
    assert run_bat("setup.bat", w) == 0


def test_setup_with_no_suitable_python_explains_and_exits_1():
    w = good_world()
    for k in [k for k in w.commands if k.startswith("py ")]:
        del w.commands[k]
    assert run_bat("setup.bat", w) == 1
    text = " ".join(w.out)
    assert "python.org/downloads/windows" in text and "winget install" in text
    assert not any("venv" in c for c in w.log)


def test_setup_installs_python_with_the_install_manager_when_it_is_the_only_thing_present():
    w = good_world()
    for k in [k for k in w.commands if k.startswith("py ")]:
        del w.commands[k]
    w.commands["where pymanager"] = 0
    w.commands["pymanager install 3.14"] = 0

    def installed(world: World) -> None:
        world.commands['py -V:3.14 "tools\\probe_python.py"'] = 0
        world.commands["py -V:3.14 --version"] = 0
        world.commands["py -V:3.14 -m venv"] = 0
        world.effects["py -V:3.14 -m venv"] = lambda x: x.files.add(".venv\\Scripts\\python.exe")

    w.effects["pymanager install 3.14"] = installed
    assert run_bat("setup.bat", w) == 0
    assert "pymanager install 3.14" in w.log


def test_setup_rebuilds_a_venv_that_is_broken_or_from_linux():
    w = good_world()
    w.files.add(".venv\\bin\\python")                                              # a Linux venv copied into the zip
    assert run_bat("setup.bat", w) == 0
    assert "RMDIR .venv" in w.log
    w2 = good_world()
    w2.files.add(".venv\\Scripts\\python.exe")
    w2.commands[f'{VENV_PY} "tools\\probe_python.py"'] = 1                           # its base Python was uninstalled
    assert run_bat("setup.bat", w2) == 0
    assert "RMDIR .venv" in w2.log and any(c.startswith("py -V:3.14 -m venv") for c in w2.log)


def test_setup_reports_a_venv_failure():
    w = good_world(**{"py -V:3.14 -m venv": 1})
    assert run_bat("setup.bat", w) == 1
    assert any("virtual environment" in o for o in w.out) and any("Setup failed" in o for o in w.out)


def test_setup_uses_the_fallback_installer_when_the_hashed_install_fails():
    w = good_world(**{f"{VENV_PY} -m pip install --require-hashes": 1})
    assert run_bat("setup.bat", w) == 0
    assert any(c.startswith(f'{VENV_PY} "tools\\install_deps.py"') for c in w.log)
    assert any("exact install did not work" in o for o in w.out)


def test_setup_fails_clearly_when_every_install_attempt_fails():
    w = good_world(**{f"{VENV_PY} -m pip install --require-hashes": 1, f'{VENV_PY} "tools\\install_deps.py"': 1})
    assert run_bat("setup.bat", w) == 1
    text = " ".join(w.out)
    assert "HTTPS_PROXY" in text and "Setup failed" in text
    assert not any("doctor" in c for c in w.log)


@pytest.mark.parametrize("doctor_rc,expected,message", [(0, 0, "Setup complete."), (2, 0, "blocks paid features"), (3, 1, "Setup failed"),
                                                       (1, 1, "Setup failed"), (-1073741819, 1, "Setup failed")])
def test_setup_reads_the_doctor_exit_code_explicitly(doctor_rc, expected, message):
    w = good_world(**{f"{VENV_PY} -m duoskin doctor": doctor_rc})
    assert run_bat("setup.bat", w) == expected
    assert any(message in o for o in w.out), w.out


def test_setup_stops_when_selfcheck_finds_a_missing_module():
    w = good_world(**{f"{VENV_PY} -m duoskin selfcheck": 1})
    assert run_bat("setup.bat", w) == 1
    assert not any("doctor" in c for c in w.log)


def test_setup_run_from_inside_a_zip_or_without_the_tools_folder_says_so():
    w = good_world()
    w.files.discard("tools\\probe_python.py")
    assert run_bat("setup.bat", w) == 1
    assert any("extracted" in o for o in w.out)


def test_setup_warns_but_continues_inside_onedrive():
    w = good_world(**{"echo \"C:\\DuoSkin\\app\\\" | findstr": 0})
    assert run_bat("setup.bat", w) == 0
    assert any("OneDrive" in o for o in w.out)
    quiet = good_world()
    assert run_bat("setup.bat", quiet) == 0 and not any("OneDrive" in o for o in quiet.out)


def test_setup_does_not_pause_when_called_by_start_bat():
    w = good_world()
    assert run_bat("setup.bat", w, env={"DUOSKIN_NOPAUSE": "1"}) == 0
    assert "PAUSE" not in w.log


def installed_world() -> World:
    w = good_world()
    w.files |= {".venv\\Scripts\\python.exe", ".venv\\win-x64.lock.installed"}
    return w


def test_start_run_from_inside_a_zip_says_to_extract_first():
    w = World()                                                                   # only start.bat was unpacked to a temp folder
    assert run_bat("start.bat", w) == 1
    assert any("Extract All" in o for o in w.out) and "PAUSE" in w.log


def test_start_runs_the_app_when_the_install_is_current():
    w = installed_world()
    assert run_bat("start.bat", w) == 0
    assert any(c.startswith(f"{VENV_PY} -m duoskin run --open-browser") for c in w.log)
    assert not any("pip install" in c for c in w.log)


def test_start_runs_setup_first_when_there_is_no_venv():
    w = good_world()
    assert run_bat("start.bat", w) == 0
    assert any("pip install --require-hashes" in c for c in w.log) and any("duoskin run" in c for c in w.log)
    assert "PAUSE" not in w.log                                                   # setup must not pause inside start


def test_start_runs_setup_again_after_an_update_changed_the_lock():
    w = installed_world()
    w.lock_changed = True
    assert run_bat("start.bat", w) == 0
    assert any("pip install --require-hashes" in c for c in w.log)


def test_start_runs_setup_when_selfcheck_fails_and_stops_if_setup_fails():
    w = installed_world()
    w.commands[f"{VENV_PY} -m duoskin selfcheck"] = 1
    w.commands[f"{VENV_PY} -m duoskin selfcheck"] = 1
    w.commands[f"{VENV_PY} -m pip install --require-hashes"] = 1
    w.commands[f'{VENV_PY} "tools\\install_deps.py"'] = 1
    assert run_bat("start.bat", w) == 1
    assert "PAUSE" in w.log and not any("duoskin run" in c for c in w.log)


def test_start_pauses_with_the_log_folder_when_the_app_crashes():
    w = installed_world()
    w.commands[f"{VENV_PY} -m duoskin run"] = 1
    assert run_bat("start.bat", w) == 1
    assert "PAUSE" in w.log and any("DuoSkin\\logs" in o for o in w.out)


def test_doctor_bat_without_an_install_explains_instead_of_failing_with_path_not_found():
    w = good_world()
    assert run_bat("doctor.bat", w) == 1
    assert any("setup.bat" in o for o in w.out)
    w2 = installed_world()
    w2.commands[f"{VENV_PY} -m duoskin doctor"] = 2
    assert run_bat("doctor.bat", w2) == 2
    assert "PAUSE" in w2.log


# ================================================================================================================ lock
def test_the_lock_is_complete_hashed_and_consistent_with_pyproject_and_the_wheelhouse():
    check = _load_tool("check_lock")
    assert check.offline_problems() == []


def test_the_lock_pins_exactly_what_the_spec_requires():
    check = _load_tool("check_lock")
    pins, problems = check.parse_lock(ROOT / "requirements" / "win-x64.lock")
    assert not problems
    assert "opencv-python" in pins and "opencv-python-headless" not in pins           # both own cv2 (SYS-08)
    assert pins["omegaconf"][0].startswith("2.3.")                                    # 2.0.6 crashes rapidocr
    assert pins["antlr4-python3-runtime"][0] == "4.9.3"
    for name in ("pywin32-ctypes", "msvc-runtime", "onnxruntime", "rapidocr", "pymeshlab", "keyring", "truststore", "psutil"):
        assert name in pins, name
    wheel = next((ROOT / "wheelhouse").glob("antlr4_python3_runtime-4.9.3-*.whl"))
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() in pins["antlr4-python3-runtime"][1]


def test_check_lock_rejects_a_broken_lock(tmp_path):
    check = _load_tool("check_lock")
    (tmp_path / "wheelhouse").mkdir()
    good = (ROOT / "requirements" / "win-x64.lock").read_text(encoding="utf-8")
    bad_cases = {
        "headless opencv": good + "opencv-python-headless==5.0.0.93 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        "old omegaconf": re.sub(r"^omegaconf==2\.3\.\d+", "omegaconf==2.0.6", good, flags=re.MULTILINE),
        "missing hash": re.sub(r"^(numpy==\S+) \\\n(?:    --hash=\S+(?: \\)?\n)+", r"\1\n", good, flags=re.MULTILINE),
        "unpinned": good + "requests>=2\n",
        "duplicate": good + "numpy==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n",
    }
    for label, text in bad_cases.items():
        lock = tmp_path / "requirements" / "win-x64.lock"
        lock.parent.mkdir(exist_ok=True)
        lock.write_text(text, encoding="utf-8")
        problems = check.offline_problems(lock, ROOT / "pyproject.toml", tmp_path / "wheelhouse")
        assert problems, f"{label} was not caught"


def test_every_third_party_import_is_declared_in_pyproject():
    import tomllib

    declared = {re.split(r"[<>=!~;\s\[]", d, maxsplit=1)[0].lower().replace("_", "-")
                for d in tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]}
    dist_of = {"PIL": "pillow", "cv2": "opencv-python", "yaml": "pyyaml", "skimage": "scikit-image", "multipart": "python-multipart",
               "sse_starlette": "sse-starlette", "resvg_py": "resvg-py", "pydantic_core": "pydantic-core", "rapidocr": "rapidocr"}
    optional = {"bpy", "DracoPy", "rapidocr_onnxruntime", "google", "fast_simplification", "xatlas", "ufbx", "assimp_py"}
    optional |= {"pydantic_core"}                                                     # always installed with pydantic, pinned by it
    std = set(sys.stdlib_module_names)
    missing: dict[str, str] = {}
    for path in (ROOT / "duoskin").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for name in names:
                top = name.split(".")[0]
                dist = dist_of.get(top, top.lower().replace("_", "-"))
                if top in std or top == "duoskin" or top in optional or dist in declared:
                    continue
                missing[top] = str(path.relative_to(ROOT))
    assert not missing, f"imported but not in pyproject.toml dependencies: {missing}"


def test_install_deps_fallback_has_the_exact_install_then_the_ranges():
    mod = _load_tool("install_deps")
    attempts = mod.commands("python")
    assert len(attempts) == 2
    assert "--require-hashes" in attempts[0][1] and "--use-feature=truststore" in attempts[0][1] and "--only-binary=:all:" in attempts[0][1]
    ranges = attempts[1][1]
    assert "--prefer-binary" in ranges and any(a.startswith("fastapi") for a in ranges)
    assert any("pymeshlab" in a and "win32" in a for a in ranges)                      # the markers are passed to pip as written
    assert mod.main(["--dry-run"]) == 0


def test_gitattributes_keeps_lf_for_hashed_text_and_marks_binaries():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for needle in ("*.bat text eol=crlf", "*.json text eol=lf", "*.lock text eol=lf", "*.png binary", "*.whl binary", "requirements/*.txt text eol=lf"):
        assert needle in attrs
