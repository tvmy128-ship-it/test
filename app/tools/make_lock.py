"""Regenerate ``requirements/win-x64.lock`` from ``pyproject.toml`` (APP_SPEC 4.2; replaces the spec's make_lock.ps1).

Runs on any OS that has ``uv`` (https://docs.astral.sh/uv/): it compiles the dependencies for Windows x64 once per
supported Python (3.12, 3.13, 3.14), refuses to continue unless all three resolve to the SAME pins (that is what lets one
hashed file install on every supported interpreter), writes the 3.13 result, then runs ``tools/check_lock.py --online``.

    python tools/make_lock.py            # needs the network (PyPI)
    python tools/make_lock.py --check    # compile only, compare with the committed lock, write nothing

``wheelhouse/antlr4_python3_runtime-4.9.3-py3-none-any.whl`` is a pure-Python wheel built once from the PyPI sdist
(``pip wheel antlr4-python3-runtime==4.9.3 --no-deps -w wheelhouse``); omegaconf 2.3.x needs it and PyPI has no wheel for it.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements" / "win-x64.lock"
PYTHONS = ("3.12", "3.13", "3.14")
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*==[^\s\\]+)", re.MULTILINE)


def compile_for(python_version: str, out: Path) -> str:
    cmd = ["uv", "pip", "compile", "pyproject.toml", "--python-platform", "x86_64-pc-windows-msvc", "--python-version", python_version,
           "--generate-hashes", "--only-binary", ":all:", "--find-links", "wheelhouse", "-q", "-o", str(out)]
    subprocess.run(cmd, check=True, cwd=str(ROOT), encoding="utf-8")
    return out.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compile and compare only; do not write the lock")
    args = ap.parse_args(argv)
    import tempfile

    with tempfile.TemporaryDirectory(prefix="duoskin-lock-", ignore_cleanup_errors=True) as tmp:
        results = {v: compile_for(v, Path(tmp) / f"lock-{v}.txt") for v in PYTHONS}
        pins = {v: sorted(_PIN.findall(text)) for v, text in results.items()}
        if len({tuple(p) for p in pins.values()}) != 1:
            for v in PYTHONS[1:]:
                for only in sorted(set(pins[v]) ^ set(pins[PYTHONS[0]])):
                    print(f"differs between {PYTHONS[0]} and {v}: {only}")
            print("The pins differ between Python versions: one shared lock is not possible. Fix the constraint that differs.")
            return 1
        text = results["3.13"]
    # the header names the temporary output file: replace it with the command a person would run
    text = re.sub(r"^#    uv pip compile .*$", "#    python tools/make_lock.py   (uv pip compile pyproject.toml --python-platform x86_64-pc-windows-msvc "
                  "--python-version 3.13 --generate-hashes --only-binary :all: --find-links wheelhouse; 3.12 and 3.14 resolve identically)",
                  text, count=1, flags=re.MULTILINE)
    if args.check:
        same = LOCK.exists() and sorted(_PIN.findall(LOCK.read_text(encoding="utf-8"))) == pins["3.13"]
        print("lock is up to date" if same else "lock differs from what pyproject.toml resolves to now")
        return 0 if same else 1
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    print(f"wrote {LOCK.relative_to(ROOT)} ({len(pins['3.13'])} pins, identical for {', '.join(PYTHONS)})")
    return subprocess.run([sys.executable, str(ROOT / "tools" / "check_lock.py"), "--online"], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
