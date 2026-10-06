"""Export DreamSim to ``dreamsim.onnx`` + ``dreamsim.onnx.json`` (APP_SPEC 4.4, CHK-S14, FAILURE_MODES X19).

RUN ONCE, in CI or on a machine that has torch.  It never runs on the user's Windows install and torch is not in the lock.
The runtime side is ``duoskin/imaging/similarity.py::DreamSimModel``: two ``(1, 3, 224, 224)`` float32 tensors in [0, 1]
(RGB, composited on white, bicubic resize) go in, one distance comes out.  Anything the model needs beyond that (mean/std)
is baked into the graph here, so the app does no preprocessing of its own.

    python tools/export_dreamsim_onnx.py --out DATA/models --fixtures tests/fixtures/dreamsim
    python tools/export_dreamsim_onnx.py --out build --fixtures tests/fixtures/dreamsim --expect-sha256 <published hash>

Steps: load the pinned DreamSim -> export one pair graph -> run every fixture pair through torch AND onnxruntime and refuse
if they differ by more than ``--tol`` (0.01 = ``dreamsim.fixture_tol``) -> write the model and the sidecar.  The sidecar is
what ``doctor`` reads: {dreamsim_version, opset, input_size, preprocessing, sha256, fixture_expectations: [{pair, distance}]}.
Put the printed sha256 into ``duoskin/data/optional_components.json``.  CI passes ``--expect-sha256`` so that a changed
hash without a version bump fails the job.

The fixture folder holds the images and a ``pairs.json``: ``[["a.png", "b.png"], ...]`` (a pair may also be written
``{"pair": ["a.png", "b.png"]}``).  Exit code 0 = exported and verified, 1 = refused (message on stderr), 2 = bad arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- pins [versions UNVERIFIED until the first export]
DREAMSIM_VERSION = "0.2.1"       # PyPI ``dreamsim``; the model the clone band was calibrated on
DREAMSIM_TYPE = "ensemble"       # DreamSim's default (CLIP + OpenCLIP + DINO ensemble)
OPSET = 17
INPUT_SIZE = 224
FIXTURE_TOL = 0.01               # dreamsim.fixture_tol in checks/thresholds.py
MODEL_NAME = "dreamsim.onnx"
MANIFEST_NAME = MODEL_NAME + ".json"
PAIRS_NAME = "pairs.json"
PREPROCESSING = {
    "colour": "RGB, RGBA composited on white",
    "resize": f"{INPUT_SIZE}x{INPUT_SIZE} bicubic (PIL)",
    "range": "float32 in [0, 1], NCHW",
    "mean_std": "none outside the graph (the graph normalises)",
}


class ExportError(RuntimeError):
    """The export is refused; ``main`` prints the message and exits 1."""


# ---------------------------------------------------------------- arguments
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="export_dreamsim_onnx", description="Export DreamSim to ONNX with a hash manifest (run once, needs torch).")
    p.add_argument("--out", type=Path, required=True, help="folder that receives dreamsim.onnx and dreamsim.onnx.json")
    p.add_argument("--fixtures", type=Path, required=True, help="folder with the fixture images and pairs.json")
    p.add_argument("--opset", type=int, default=OPSET, help=f"ONNX opset (default {OPSET})")
    p.add_argument("--tol", type=float, default=FIXTURE_TOL, help=f"largest torch-vs-onnx difference allowed (default {FIXTURE_TOL})")
    p.add_argument("--expect-sha256", default="", help="fail unless the exported file has this sha256 (CI re-export check)")
    p.add_argument("--cache-dir", type=Path, default=None, help="where DreamSim keeps its downloaded weights")
    p.add_argument("--allow-version-mismatch", action="store_true", help="export even if the installed dreamsim differs from the pin")
    a = p.parse_args(argv)
    if a.opset < 11:
        p.error("--opset must be at least 11")
    if not 0 < a.tol <= 0.1:
        p.error("--tol must be in (0, 0.1]")
    a.expect_sha256 = a.expect_sha256.strip().lower()
    if a.expect_sha256 and len(a.expect_sha256) != 64:
        p.error("--expect-sha256 must be 64 hex characters")
    return a


# ---------------------------------------------------------------- manifest (pure, unit-tested)
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_pairs(fixtures: Path) -> list[tuple[str, str]]:
    """The pairs of ``pairs.json`` whose two images exist; refuses an empty or broken list."""
    try:
        raw = json.loads((fixtures / PAIRS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ExportError(f"cannot read {fixtures / PAIRS_NAME}: {e}") from e
    out: list[tuple[str, str]] = []
    for row in raw.get("pairs", []) if isinstance(raw, dict) else raw:
        pair = row.get("pair") if isinstance(row, dict) else row
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            raise ExportError(f"bad pair in {PAIRS_NAME}: {row!r}")
        for name in pair:
            if not (fixtures / name).is_file():
                raise ExportError(f"fixture image {name} is missing from {fixtures}")
        out.append((pair[0], pair[1]))
    if not out:
        raise ExportError(f"{PAIRS_NAME} lists no pairs")
    return out


def build_manifest(*, sha256: str, expectations: list[dict[str, Any]], opset: int = OPSET, version: str = DREAMSIM_VERSION) -> dict[str, Any]:
    """The sidecar document ``doctor`` (CHK-S14) reads.  ``expectations`` rows are ``{"pair": [a, b], "distance": float}``."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ExportError("sha256 must be 64 lowercase hex characters")
    if not expectations:
        raise ExportError("a manifest needs at least one fixture expectation")
    rows = [{"pair": [str(r["pair"][0]), str(r["pair"][1])], "distance": round(float(r["distance"]), 6)} for r in expectations]
    return {"dreamsim_version": version, "dreamsim_type": DREAMSIM_TYPE, "opset": int(opset), "input_size": INPUT_SIZE,
            "preprocessing": dict(PREPROCESSING), "sha256": sha256, "fixture_tolerance": FIXTURE_TOL, "fixture_expectations": rows}


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    """Write the sidecar atomically (temp file, then replace) as UTF-8 JSON with sorted keys, so a re-export is byte-identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)  # win-ok: runs once in CI or on a torch machine, never on the user's Windows install
    return path


def check_agreement(torch_d: list[float], onnx_d: list[float], tol: float) -> float:
    """The largest |torch - onnx|; raises ``ExportError`` when it exceeds ``tol`` (or the lists differ in length)."""
    if len(torch_d) != len(onnx_d) or not torch_d:
        raise ExportError("torch and onnxruntime gave a different number of fixture distances")
    worst = max(abs(a - b) for a, b in zip(torch_d, onnx_d, strict=True))
    if worst > tol:
        raise ExportError(f"torch and onnxruntime differ by {worst:.4f} on a fixture pair (limit {tol}); the export is not trusted")
    return worst


# ---------------------------------------------------------------- the torch part (cannot run in the app's test environment)
def _load_array(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        rgba = im.convert("RGBA")
        flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba).convert("RGB")
        flat = flat.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BICUBIC)
    return (np.asarray(flat, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None, ...]


def export(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import importlib.metadata as md

        import numpy as np
        import onnxruntime as ort
        import torch
        from dreamsim import dreamsim
    except ImportError as e:
        raise ExportError(f"needs torch, dreamsim, onnx and onnxruntime installed ({e})") from e
    installed = md.version("dreamsim")
    if installed != DREAMSIM_VERSION and not args.allow_version_mismatch:
        raise ExportError(f"dreamsim {installed} is installed but this tool pins {DREAMSIM_VERSION}; bump the pin with the hash")
    pairs = read_pairs(args.fixtures)

    model, _preprocess = dreamsim(pretrained=True, dreamsim_type=DREAMSIM_TYPE, device="cpu",
                                  **({"cache_dir": str(args.cache_dir)} if args.cache_dir else {}))
    model.eval()

    class PairDistance(torch.nn.Module):          # (a, b) -> one distance; DreamSim's own forward does the normalising
        def __init__(self, inner: Any) -> None:
            super().__init__()
            self.inner = inner

        def forward(self, image_a: Any, image_b: Any) -> Any:
            return self.inner(image_a, image_b).reshape(1)

    wrapped = PairDistance(model).eval()
    example = torch.zeros(1, 3, INPUT_SIZE, INPUT_SIZE)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / MODEL_NAME
    with torch.no_grad():
        torch.onnx.export(wrapped, (example, example), str(target), input_names=["image_a", "image_b"], output_names=["distance"],
                          opset_version=args.opset, do_constant_folding=True)

    sess = ort.InferenceSession(str(target), providers=["CPUExecutionProvider"])
    torch_d: list[float] = []
    onnx_d: list[float] = []
    for a, b in pairs:
        xa, xb = _load_array(args.fixtures / a), _load_array(args.fixtures / b)
        with torch.no_grad():
            torch_d.append(float(wrapped(torch.from_numpy(xa), torch.from_numpy(xb)).reshape(-1)[0]))
        onnx_d.append(float(np.asarray(sess.run(None, {"image_a": xa, "image_b": xb})[0]).reshape(-1)[0]))
    worst = check_agreement(torch_d, onnx_d, args.tol)

    sha = sha256_file(target)
    if args.expect_sha256 and sha != args.expect_sha256:
        raise ExportError(f"sha256 {sha} differs from the expected {args.expect_sha256}: bump the DreamSim pin or the opset first")
    manifest = build_manifest(sha256=sha, expectations=[{"pair": [a, b], "distance": d} for (a, b), d in zip(pairs, onnx_d, strict=True)],
                              opset=args.opset, version=installed)
    write_manifest(args.out / MANIFEST_NAME, manifest)
    print(f"wrote {target} (sha256 {sha}); torch vs onnxruntime worst difference {worst:.5f} over {len(pairs)} pairs")
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        export(args)
    except ExportError as e:
        print(f"export refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
