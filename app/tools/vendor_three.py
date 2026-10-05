"""Vendor three.js into ``duoskin/web/vendor/three`` (APP_SPEC section 12: no build step, no CDN).

Usage::

    python tools/vendor_three.py --tarball path/to/three-0.186.1.tgz     # offline: the npm tarball
    python tools/vendor_three.py                                         # downloads it from the npm registry first
    python tools/vendor_three.py --check                                 # verify the vendored files against VENDOR.json

What is copied, unchanged and byte for byte:

* ``build/three.module.js`` and ``build/three.core.js`` (the ES module build; ``three.module.js`` imports ``three.core.js``);
* the add-ons the UI uses: ``GLTFLoader``, ``OrbitControls``, ``RoomEnvironment``, ``FBXLoader``;
* every file those add-ons import (found by following the relative ``import ... from './x.js'`` statements, so the list
  can never drift from what the add-ons really need) and the MIT ``LICENSE``.

The layout under ``vendor/three`` mirrors the npm package (``build/`` and ``addons/`` for ``examples/jsm/``) so the page's
import map is just ``"three": "/vendor/three/build/three.module.js"`` and ``"three/addons/": "/vendor/three/addons/"``.
``VENDOR.json`` records the version, the tarball sha-256 and the sha-256 of every file; the run is reproducible and
``--check`` (also used by ``tests/web/test_vendor_three.py``) fails when a vendored file was edited.

Only the standard library is used. Paths are ``pathlib`` based and every file is read and written as bytes or UTF-8.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.186.1"
TARBALL_SHA256 = "8cd068708ea44f2c73c944b1cead2ba2f0d5c15c8fc194e5700f4e4f4a033fe7"
NPM_URL = f"https://registry.npmjs.org/three/-/three-{VERSION}.tgz"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "duoskin" / "web" / "vendor" / "three"

# (path inside the npm package, path under vendor/three)
CORE_FILES = (("build/three.module.js", "build/three.module.js"), ("build/three.core.js", "build/three.core.js"))
ADDON_ROOTS = (
    "examples/jsm/loaders/GLTFLoader.js",
    "examples/jsm/controls/OrbitControls.js",
    "examples/jsm/environments/RoomEnvironment.js",
    "examples/jsm/loaders/FBXLoader.js",
)
ADDON_PREFIX = "examples/jsm/"
_IMPORT_RE = re.compile(r"""(?:^|\n)\s*(?:import|export)\b[^;'"]*?\bfrom\s*['"]([^'"]+)['"]|(?:^|\n)\s*import\s*['"]([^'"]+)['"]""")


class VendorError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_tarball(path: Path | None) -> tuple[bytes, dict[str, bytes]]:
    """The tarball bytes and ``{name inside package/: bytes}`` for every regular file."""
    if path is None:
        with urllib.request.urlopen(NPM_URL, timeout=120) as resp:
            raw = resp.read()
    else:
        raw = Path(path).read_bytes()
    digest = sha256_bytes(raw)
    if digest != TARBALL_SHA256:
        raise VendorError(f"unexpected tarball (sha-256 {digest}); this tool is pinned to three@{VERSION} ({TARBALL_SHA256})")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if name.startswith(("..", "/")) or ".." in name.split("/"):
                raise VendorError(f"unsafe path in tarball: {member.name}")
            extracted = tar.extractfile(member)
            if extracted is not None:
                files[name] = extracted.read()
    return raw, files


def relative_imports(source: str) -> list[str]:
    """Relative module specifiers (``./x.js``, ``../y.js``) imported by ``source``; bare ones such as ``three`` are skipped."""
    out: list[str] = []
    for m in _IMPORT_RE.finditer(source):
        spec = m.group(1) or m.group(2)
        if spec and spec.startswith("."):
            out.append(spec)
    return out


def _normalise(base: str, spec: str) -> str:
    parts = base.split("/")[:-1]
    for seg in spec.split("/"):
        if seg == "..":
            if not parts:
                raise VendorError(f"{spec} escapes the package from {base}")
            parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/".join(parts)


def addon_closure(files: dict[str, bytes], roots: tuple[str, ...] = ADDON_ROOTS) -> list[str]:
    """``roots`` plus every file they (transitively) import with a relative path, sorted."""
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        if name not in files:
            raise VendorError(f"{name} is not in the tarball")
        seen.add(name)
        for spec in relative_imports(files[name].decode("utf-8")):
            todo.append(_normalise(name, spec))
    return sorted(seen)


def plan(files: dict[str, bytes]) -> dict[str, str]:
    """``{vendored path: package path}`` for everything to copy."""
    mapping = {dst: src for src, dst in CORE_FILES}
    for name in addon_closure(files):
        mapping["addons/" + name[len(ADDON_PREFIX):]] = name   # win-ok: tarball member paths, not file system paths
    mapping["LICENSE"] = "LICENSE"
    return dict(sorted(mapping.items()))


def vendor(tarball: Path | None, out: Path = DEFAULT_OUT) -> dict[str, object]:
    raw, files = read_tarball(tarball)
    mapping = plan(files)
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
        if out.exists():
            raise OSError(f"could not remove {out}: a file in it is read-only or locked (close the editor or antivirus scan)")
    manifest_files: dict[str, str] = {}
    for dst, src in mapping.items():
        target = out / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[src])
        manifest_files[dst] = sha256_bytes(files[src])
    manifest: dict[str, object] = {
        "package": "three", "version": VERSION, "license": "MIT", "source": NPM_URL, "tarball_sha256": sha256_bytes(raw),
        "generated_by": "tools/vendor_three.py", "files": manifest_files,
    }
    (out / "VENDOR.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def check(out: Path = DEFAULT_OUT) -> list[str]:
    """Problems found in an existing vendor folder (empty list = all files match ``VENDOR.json``)."""
    problems: list[str] = []
    manifest_path = out / "VENDOR.json"
    if not manifest_path.exists():
        return [f"{manifest_path} is missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        p = out / name
        if not p.exists():
            problems.append(f"missing {name}")
        elif sha256_bytes(p.read_bytes()) != digest:
            problems.append(f"changed {name}")
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} - {"VENDOR.json"}
    problems += [f"unexpected {n}" for n in sorted(on_disk - set(manifest["files"]))]
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tarball", type=Path, help=f"three-{VERSION}.tgz from npm (downloaded when omitted)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="verify the vendored files instead of writing them")
    args = ap.parse_args(argv)
    if args.check:
        problems = check(args.out)
        for p in problems:
            print(p, file=sys.stderr)
        print("vendor/three is intact" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    try:
        manifest = vendor(args.tarball, args.out)
    except VendorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"vendored three@{manifest['version']}: {len(manifest['files'])} files -> {args.out}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
