"""Check ``requirements/win-x64.lock`` (APP_SPEC 4.2, FAILURE_MODES SYS-08, SYS-18, X22).

Offline checks (always; no network, run in the unit tests):
  * every pin is ``name==version`` with at least one ``sha256`` hash, no duplicates, no unpinned or URL lines;
  * every dependency of ``pyproject.toml`` that applies on win32 is pinned in the lock;
  * ``opencv-python`` is pinned and ``opencv-python-headless`` is not (both own ``cv2``);
  * ``pywin32-ctypes`` (keyring's Windows backend), ``msvc-runtime``, ``onnxruntime``, ``rapidocr`` are pinned, ``omegaconf``
    is 2.3.x (2.0.6 crashes rapidocr) and ``antlr4-python3-runtime`` 4.9.3 carries the hash of the wheel in ``wheelhouse/``;
  * every wheel in ``wheelhouse/`` matches a hash in the lock.

Online checks (``--online``; needs PyPI, run by CI and by hand): for every pin and every Python in ``--pythons`` (default
3.12, 3.13, 3.14) a ``win_amd64`` (or abi3, or pure ``py3-none-any``) wheel exists on PyPI and its sha256 is in the lock.
That is what lets ONE hashed file install on all three interpreters.

Exit code 0 = fine, 1 = problems (printed one per line).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = ROOT / "requirements" / "win-x64.lock"
DEFAULT_PYTHONS = ("3.12", "3.13", "3.14")
REQUIRED_PINS = ("opencv-python", "pywin32-ctypes", "msvc-runtime", "onnxruntime", "rapidocr", "omegaconf", "antlr4-python3-runtime",
                 "keyring", "pymeshlab", "numpy", "pillow", "uvicorn", "fastapi")
FORBIDDEN_PINS = ("opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless", "torch", "tensorflow")
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.!+_-]+)\s*(\\)?\s*(?:;.*)?$")
_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})\s*(\\)?$")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> tuple[dict[str, tuple[str, set[str]]], list[str]]:
    """Return ``({normalised name: (version, {sha256})}, problems)``. Understands uv/pip-compile output."""
    pins: dict[str, tuple[str, set[str]]] = {}
    problems: list[str] = []
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash"):
            m = _HASH_RE.match(line)
            if not m or current is None:
                problems.append(f"malformed hash line: {line[:80]}")
            else:
                pins[current][1].add(m.group(1))
            continue
        if line.startswith("-"):
            continue                                            # --find-links and similar options
        m = _PIN_RE.match(line)
        if not m:
            problems.append(f"not an exact pin (name==version): {line[:80]}")
            current = None
            continue
        current = norm(m.group(1))
        if current in pins:
            problems.append(f"{current} is pinned twice")
        pins[current] = (m.group(2), set())
    for name, (version, hashes) in pins.items():
        if not hashes:
            problems.append(f"{name}=={version} has no --hash (the install uses --require-hashes)")
    return pins, problems


def _pyproject_requirements(pyproject: Path) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _applies_on_windows(req_text: str) -> bool:
    from packaging.requirements import Requirement

    req = Requirement(req_text)
    if req.marker is None:
        return True
    return bool(req.marker.evaluate({"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
                                     "platform_machine": "AMD64", "python_version": "3.13"}))


def offline_problems(lock: Path = DEFAULT_LOCK, pyproject: Path | None = None, wheelhouse: Path | None = None) -> list[str]:
    """Every problem that can be found without the network (empty list = fine)."""
    from packaging.requirements import Requirement

    pyproject = pyproject or lock.parent.parent / "pyproject.toml"
    wheelhouse = wheelhouse or lock.parent.parent / "wheelhouse"
    if not lock.exists():
        return [f"{lock} is missing"]
    pins, problems = parse_lock(lock)
    for name in REQUIRED_PINS:
        if name not in pins:
            problems.append(f"{name} is not pinned in the lock")
    for name in FORBIDDEN_PINS:
        if name in pins:
            problems.append(f"{name} must not be in the lock (opencv-python and opencv-python-headless both own cv2; no torch)")
    if "omegaconf" in pins and not pins["omegaconf"][0].startswith("2.3."):
        problems.append(f"omegaconf is {pins['omegaconf'][0]}; rapidocr crashes on anything below 2.3 (SYS-08)")
    if pyproject.exists():
        for text in _pyproject_requirements(pyproject):
            if not _applies_on_windows(text):
                continue
            name = norm(Requirement(text).name)
            if name not in pins:
                problems.append(f"pyproject dependency '{text}' is not in the lock (re-run tools/make_lock.py)")
    wheels = sorted(wheelhouse.glob("*.whl")) if wheelhouse.exists() else []
    all_hashes = {h for _v, hs in pins.values() for h in hs}
    for wheel in wheels:
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest not in all_hashes:
            problems.append(f"{wheel.name} (sha256 {digest[:12]}...) is not in the lock")
    if "antlr4-python3-runtime" in pins:
        version, hashes = pins["antlr4-python3-runtime"]
        local = {hashlib.sha256(w.read_bytes()).hexdigest() for w in wheels if w.name.lower().startswith("antlr4_python3_runtime-")}
        if version != "4.9.3":
            problems.append(f"antlr4-python3-runtime is {version}; omegaconf 2.3.x needs 4.9.3")
        if not local & hashes:
            problems.append("the antlr4-python3-runtime wheel in wheelhouse/ is missing or its hash is not in the lock")
    return problems


# ------------------------------------------------------------------------------------------------------------ online
def _target_tags(py: str) -> set[object]:
    from packaging import tags

    major, minor = (int(x) for x in py.split("."))
    version = (major, minor)
    out: set[object] = set(tags.cpython_tags(python_version=version, abis=[f"cp{major}{minor}"], platforms=["win_amd64"]))
    out |= set(tags.compatible_tags(python_version=version, interpreter=f"cp{major}{minor}", platforms=["win_amd64"]))
    return out


def _wheel_tags(filename: str) -> set[object]:
    from packaging.utils import InvalidWheelFilename, parse_wheel_filename

    try:
        return set(parse_wheel_filename(filename)[3])
    except InvalidWheelFilename:
        return set()


def _fetch_release(name: str, version: str, timeout: float) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    last: Exception | None = None
    for _attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"could not read {url}: {last}")


def online_problems(lock: Path = DEFAULT_LOCK, pythons: tuple[str, ...] = DEFAULT_PYTHONS, timeout: float = 30.0,
                    wheelhouse: Path | None = None, report_sizes: bool = False) -> list[str]:
    """For each pin and Python: a compatible win_amd64 wheel exists on PyPI and its sha256 is in the lock."""
    wheelhouse = wheelhouse or lock.parent.parent / "wheelhouse"
    pins, problems = parse_lock(lock)
    local_hashes = {hashlib.sha256(w.read_bytes()).hexdigest() for w in wheelhouse.glob("*.whl")} if wheelhouse.exists() else set()
    targets = {py: _target_tags(py) for py in pythons}
    total = {py: 0 for py in pythons}
    for name, (version, hashes) in sorted(pins.items()):
        if hashes & local_hashes:
            continue                                                  # the wheel ships in wheelhouse/ (antlr4)
        try:
            release = _fetch_release(name, version, timeout)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        files = [f for f in release.get("urls", []) if f.get("packagetype") == "bdist_wheel" and not f.get("yanked")]
        for py, wanted in targets.items():
            match = [f for f in files if _wheel_tags(f["filename"]) & wanted]
            if not match:
                problems.append(f"{name}=={version}: no win_amd64 / abi3 / pure wheel for Python {py} on PyPI")
                continue
            missing = [f["filename"] for f in match if f["digests"]["sha256"] not in hashes]
            if missing:
                problems.append(f"{name}=={version}: Python {py} wheel hash not in the lock: {missing[0]}")
            total[py] += max(f.get("size", 0) for f in match)
    if report_sizes:
        for py in pythons:
            print(f"download size for Python {py}: about {total[py] / 1e6:.0f} MB")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--online", action="store_true", help="also check PyPI for wheels for each Python (needs the network)")
    ap.add_argument("--pythons", nargs="+", default=list(DEFAULT_PYTHONS), metavar="X.Y")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    problems = offline_problems(args.lock)
    if args.online and not problems:
        problems += online_problems(args.lock, tuple(args.pythons), args.timeout, report_sizes=True)
    for p in problems:
        print("PROBLEM:", p)
    if not problems:
        pins, _ = parse_lock(args.lock)
        print(f"OK: {len(pins)} pins" + (f", wheels found for Python {', '.join(args.pythons)} on win_amd64" if args.online else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
