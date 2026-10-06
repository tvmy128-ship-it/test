"""Fallback installer used by setup.bat when ``pip install --require-hashes -r requirements/win-x64.lock`` failed.

It first retries the exact pins (still binary-only, but through the Windows certificate store, which fixes antivirus or
proxy TLS interception), then installs the version RANGES from pyproject.toml (``--prefer-binary``; pip picks the newest
wheels that satisfy them). The duoskin package itself is never installed: ``python -m duoskin`` runs from the app folder.

``--dry-run`` prints the commands without running them. Exit code 0 = installed, 1 = every attempt failed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements" / "win-x64.lock"
WHEELHOUSE = ROOT / "wheelhouse"


def dependencies(pyproject: Path = ROOT / "pyproject.toml") -> list[str]:
    """The ``[project].dependencies`` strings (pip evaluates the ``sys_platform`` markers itself)."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return [str(d) for d in data["project"]["dependencies"]]


def commands(python: str = sys.executable) -> list[tuple[str, list[str]]]:
    """The attempts, in order, as ``(label, argv)``."""
    base = [python, "-m", "pip", "install", "--find-links", str(WHEELHOUSE), "--use-feature=truststore"]
    return [
        ("exact pins from requirements/win-x64.lock (Windows certificate store)",
         [*base, "--require-hashes", "--no-deps", "--only-binary=:all:", "-r", str(LOCK)]),
        ("version ranges from pyproject.toml (newest compatible wheels)",
         [*base, "--prefer-binary", *dependencies()]),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    for label, argv_ in commands():
        print(f"Trying: {label}", flush=True)
        if args.dry_run:
            print("  " + " ".join(argv_))
            continue
        if subprocess.run(argv_, check=False, cwd=str(ROOT)).returncode == 0:
            return 0
        print("That attempt failed.", flush=True)
    return 1 if not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
