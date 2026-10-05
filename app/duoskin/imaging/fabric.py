"""Fabric tiles for the clothing compositor (APP_SPEC §10.5 step 2, bible §11.7, FAILURE_MODES CLO-04 / CLO-13).

* ``FabricTile``: a greyscale, seamless, lighting-free tile (mean 128) plus the template-pixel scale of one weave repeat.
* ``procedural_fabric(fabric_id)``: ten deterministic procedural tiles (jersey knit, ribbed knit, twill, denim, fleece,
  nylon ripstop, wool felt, canvas, pebbled leather, corduroy) so the app works with no user kit and no image model. They
  are pure functions of ``(id, seed, size)``: float64 maths and a PCG64 stream, quantised once to uint8.
* ``check_fabric_tile``: library admission (CLO-13, HARD): seam energy, aliasing energy, flat lighting, chroma.
* ``fabric_values(tile, region)``: the fabric layout of one region at 4x. Fabric is one **continuous strip per body part**
  (CLO-04): the four side faces share a perimeter coordinate and a vertical coordinate, and the cap faces continue the side
  faces across every cap edge (inverse-distance blend), so every seam in ``ADJACENCY`` matches.
* Colour helpers (``rgb_to_lab`` ...) come from ``imaging/palette.py``; here are only the pieces the compositor needs.

Nothing here touches the network; ``kit`` tiles are read from ``fabrics/<id>/{tile.png,fabric.json}`` by
``load_fabric_kit_dir``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.roblox import template as T

KIT_JSON = Path(__file__).resolve().parent.parent / "builtin_kits" / "fabrics.json"
MAX_AMPLITUDE_DL = 6.0            # fabric_amplitude_max_dL (APP_SPEC §10.5): peak-to-peak L* swing of the weave
_MIN_TILE = 64


# --------------------------------------------------------------------------------------------------------------------
# the tile object
# --------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class FabricTile:
    """A seamless greyscale fabric tile.

    ``tile``: uint8 (N, N), mean about 128, no baked lighting. ``px_per_repeat``: template pixels per weave repeat and
    ``repeats``: weave repeats across the tile width, so the tile covers ``repeats * px_per_repeat`` template pixels.
    ``amplitude_dL``: peak-to-peak lightness swing in L* applied when the tile is gradient-mapped to a block colour."""

    fabric_id: str
    material_family: str
    tile: np.ndarray
    px_per_repeat: float
    repeats: int
    amplitude_dL: float
    origin: str = "procedural"            # procedural | library | flat
    prompt_phrase: str = ""

    def __post_init__(self) -> None:
        t = self.tile
        if t.ndim != 2 or t.shape[0] != t.shape[1] or t.dtype != np.uint8:
            raise ValueError("fabric tile must be a square uint8 greyscale array")
        if not 0.0 <= self.amplitude_dL <= MAX_AMPLITUDE_DL:
            raise ValueError(f"amplitude_dL must be 0..{MAX_AMPLITUDE_DL} L*, got {self.amplitude_dL}")
        if self.px_per_repeat <= 0 or self.repeats <= 0:
            raise ValueError("px_per_repeat and repeats must be positive")

    @property
    def size(self) -> int:
        return int(self.tile.shape[0])

    @property
    def template_px(self) -> float:
        """Template pixels covered by one tile width."""
        return self.repeats * self.px_per_repeat

    @property
    def sha256(self) -> str:
        h = hashlib.sha256()
        h.update(self.tile.tobytes())
        h.update(json.dumps([self.fabric_id, self.px_per_repeat, self.repeats, round(self.amplitude_dL, 3)]).encode())
        return h.hexdigest()

    def signed(self) -> np.ndarray:
        """float64 tile in [-1, 1] (0 = mean grey)."""
        return np.clip((self.tile.astype(np.float64) - 128.0) / 127.0, -1.0, 1.0)


def flat_fabric() -> FabricTile:
    """No texture at all (amplitude 0): the plain-colour fallback."""
    return FabricTile("flat", "jersey", np.full((_MIN_TILE, _MIN_TILE), 128, np.uint8), 4.0, 4, 0.0, origin="flat")


# --------------------------------------------------------------------------------------------------------------------
# procedural generators (float64, periodic by construction)
# --------------------------------------------------------------------------------------------------------------------
class DetRng:
    """A random stream whose every conversion is specified here, on top of the PCG64 raw 64-bit output (``random_raw``), so the
    same seed gives the same numbers on every platform and NumPy version (``Generator.normal``/``uniform`` make no such promise).
    Used for the procedural fabrics and folds, whose pixels are hashed by the golden tests."""

    def __init__(self, seed: int):
        self._bg = np.random.PCG64(int(seed))

    def random(self, shape: int | tuple[int, ...] = ()) -> np.ndarray | float:
        """Uniform doubles in [0, 1) with 53 random bits."""
        shp = (shape,) if isinstance(shape, int) else tuple(shape)
        n = int(np.prod(shp)) if shp else 1
        u = (self._bg.random_raw(n) >> np.uint64(11)).astype(np.float64) * (2.0 ** -53)
        return float(u[0]) if not shp else u.reshape(shp)

    def uniform(self, lo: float, hi: float) -> float:
        return float(lo + (hi - lo) * self.random())  # type: ignore[arg-type]

    def integers(self, lo: int, hi: int) -> int:
        """An integer in [lo, hi)."""
        return int(lo + int(np.floor(float(self.random()) * (hi - lo))))  # type: ignore[arg-type]

    def normal(self, shape: tuple[int, ...]) -> np.ndarray:
        """Standard normals by Box-Muller from two uniform streams."""
        n = int(np.prod(shape))
        u1 = 1.0 - np.asarray(self.random(n))          # (0, 1]
        u2 = np.asarray(self.random(n))
        return (np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)).reshape(shape)


def _rng(seed: int) -> DetRng:
    return DetRng(seed)


def _periodic_noise(n: int, seed: int, sigma_px: float, aniso: tuple[float, float] = (1.0, 1.0)) -> np.ndarray:
    """Zero-mean unit-std periodic noise: white noise low-passed with a Gaussian of ``sigma_px`` (x, y scaled by ``aniso``)."""
    w = _rng(seed).normal((n, n))
    fy = np.fft.fftfreq(n)[:, None]
    fx = np.fft.rfftfreq(n)[None, :]
    k = np.exp(-2.0 * np.pi ** 2 * ((sigma_px * aniso[0] * fx) ** 2 + (sigma_px * aniso[1] * fy) ** 2))
    out = np.fft.irfft2(np.fft.rfft2(w) * k, s=(n, n))
    out -= out.mean()
    sd = out.std()
    return out / sd if sd > 0 else out


def _lowpass(a: np.ndarray, sigma_px: float) -> np.ndarray:
    """Periodic Gaussian low-pass (FFT) of a square array."""
    n = a.shape[0]
    fy = np.fft.fftfreq(n)[:, None]
    fx = np.fft.rfftfreq(n)[None, :]
    k = np.exp(-2.0 * np.pi ** 2 * (sigma_px ** 2) * (fx ** 2 + fy ** 2))
    return np.fft.irfft2(np.fft.rfft2(a) * k, s=(n, n))


def _tri(x: np.ndarray) -> np.ndarray:
    return 2.0 * np.abs(x - np.floor(x) - 0.5)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _grid(n: int) -> tuple[np.ndarray, np.ndarray]:
    v, u = np.meshgrid(np.arange(n) / n, np.arange(n) / n, indexing="ij")
    return u, v


def _worley(n: int, seed: int, points: int) -> np.ndarray:
    """Periodic Worley F1 distance (toroidal) normalised by the mean nearest-neighbour spacing."""
    rng = _rng(seed)
    pts = np.asarray(rng.random((points, 2)))
    u, v = _grid(n)
    best = np.full((n, n), np.inf)
    for px, py in pts:
        du = np.abs(u - px)
        dv = np.abs(v - py)
        du = np.minimum(du, 1.0 - du)
        dv = np.minimum(dv, 1.0 - dv)
        best = np.minimum(best, du * du + dv * dv)
    return np.sqrt(best) * np.sqrt(points)


def _knit_v(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    a, b = r * u, r * v
    zig = 0.5 + 0.5 * np.cos(2 * np.pi * (a + 0.38 * _tri(b)))
    row = 0.82 + 0.18 * np.cos(2 * np.pi * b)
    return zig * row + 0.10 * _periodic_noise(n, seed, 1.6)


def _knit_rib(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    rib = 0.5 + 0.5 * np.cos(2 * np.pi * r * u)
    fine = 0.12 * np.cos(2 * np.pi * 3 * r * v + 3.0 * np.cos(2 * np.pi * r * u))
    return rib + fine + 0.10 * _periodic_noise(n, seed, 1.8, (1.0, 0.5))


def _twill(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    return 0.5 + 0.5 * np.cos(2 * np.pi * r * (u + v)) + 0.06 * _periodic_noise(n, seed, 1.5)


def _denim(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    diag = 0.5 + 0.5 * np.cos(2 * np.pi * r * (u + v))
    weft = 0.5 + 0.5 * np.cos(2 * np.pi * r * 1.0 * v)
    slub = _periodic_noise(n, seed, 3.6, (0.4, 1.0))           # horizontal streaks (anisotropic)
    return 0.55 * diag + 0.25 * weft + 0.22 * slub


def _fleece(n: int, seed: int, r: int) -> np.ndarray:
    return 0.7 * _periodic_noise(n, seed, 2.6) + 0.35 * _periodic_noise(n, seed + 1, 1.3)


def _nylon(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    grid = np.maximum(np.cos(np.pi * r * u) ** 24, np.cos(np.pi * r * v) ** 24)   # ripstop lines every n/r px
    return 0.25 * grid + 0.12 * _periodic_noise(n, seed, 1.4)


def _wool(n: int, seed: int, r: int) -> np.ndarray:
    return 0.8 * _periodic_noise(n, seed, 3.2) + 0.45 * _periodic_noise(n, seed + 1, 1.5)


def _canvas(n: int, seed: int, r: int) -> np.ndarray:
    u, v = _grid(n)
    weave = np.cos(2 * np.pi * r * u) * np.cos(2 * np.pi * r * v)
    streak = _periodic_noise(n, seed, 3.2, (0.5, 1.0))
    return 0.5 * weave + 0.2 * streak + 0.1 * _periodic_noise(n, seed + 1, 2.2)


def _leather(n: int, seed: int, r: int) -> np.ndarray:
    f1 = _worley(n, seed, 36 * r)
    bump = 1.0 - _smoothstep(0.15, 0.95, f1)
    return bump + 0.08 * _periodic_noise(n, seed + 1, 1.2)


def _corduroy(n: int, seed: int, r: int) -> np.ndarray:
    u, _v = _grid(n)
    wale = 0.5 + 0.5 * np.cos(2 * np.pi * r * u)
    return wale ** 1.5 + 0.08 * _periodic_noise(n, seed, 1.6, (0.6, 1.0))


_KINDS = {"knit_v": _knit_v, "knit_rib": _knit_rib, "twill": _twill, "denim": _denim, "fleece": _fleece, "nylon": _nylon,
          "wool": _wool, "canvas": _canvas, "leather": _leather, "corduroy": _corduroy}


def generate_tile(kind: str, size: int = 256, seed: int = 0, repeats: int = 16) -> np.ndarray:
    """Procedural seamless greyscale tile as uint8 (mean 128, flattened to +-114 levels). Pure and deterministic."""
    if kind not in _KINDS:
        raise ValueError(f"unknown fabric kind {kind!r}; known: {sorted(_KINDS)}")
    if size < _MIN_TILE or size & (size - 1):
        raise ValueError("tile size must be a power of two >= 64")
    g = _KINDS[kind](size, seed, repeats)
    g = g - _lowpass(g, size / 12.0)                    # flatten lighting (bible I7 step 2): no baked-in shading
    peak = np.percentile(np.abs(g), 99.5)
    g = np.clip(g / (peak if peak > 0 else 1.0), -1.0, 1.0)
    return np.rint(128.0 + 114.0 * g).astype(np.uint8)


@lru_cache(maxsize=1)
def _specs() -> dict[str, dict[str, Any]]:
    return json.loads(KIT_JSON.read_text(encoding="utf-8"))["fabrics"]


def builtin_fabric_ids() -> list[str]:
    """Sorted ids of the built-in procedural fabrics (the manifest builder lists these as ``fabric_ids``)."""
    return sorted(_specs())


def builtin_fabric_info(fabric_id: str) -> dict[str, Any]:
    """The static description (material family, prompt phrase, scale, amplitude) of a built-in fabric."""
    if fabric_id not in _specs():
        raise KeyError(f"no built-in fabric {fabric_id!r}")
    return dict(_specs()[fabric_id])


@lru_cache(maxsize=32)
def procedural_fabric(fabric_id: str) -> FabricTile:
    """The deterministic built-in tile for ``fabric_id`` (cached). Unknown ids raise ``KeyError``."""
    spec = builtin_fabric_info(fabric_id)
    tile = generate_tile(spec["kind"], spec["size"], spec["seed"], spec["repeats"])
    return FabricTile(fabric_id, spec["material_family"], tile, float(spec["px_per_repeat"]), int(spec["repeats"]),
                      float(spec["amplitude_dL"]), "procedural", spec.get("prompt_phrase", ""))


def resolve_fabric(fabric_id: str | None, kit_dirs: list[Path] | None = None) -> FabricTile:
    """A user kit tile (``<dir>/<id>/tile.png``) wins over the built-in one; unknown ids fall back to the closest
    built-in family by name, then to ``jersey_plain``. Never raises for a missing tile, so the app works without a kit."""
    fid = (fabric_id or "").strip()
    for d in kit_dirs or []:
        p = Path(d) / fid
        if fid and (p / "tile.png").exists():
            return load_fabric_kit_dir(p)
    if fid in _specs():
        return procedural_fabric(fid)
    low = fid.lower()
    for key in sorted(_specs()):
        if low and (low in key or key.split("_")[0] in low):
            return procedural_fabric(key)
    return procedural_fabric("jersey_plain")


def load_fabric_kit_dir(path: Path) -> FabricTile:
    """Load ``tile.png`` (greyscale, any size, square) and ``fabric.json`` (material_family, prompt_phrase, px_per_repeat,
    optional repeats and amplitude_dL) from a user kit folder and run the admission checks (CLO-13); raises ValueError
    listing the failed checks."""
    from PIL import Image

    meta = json.loads((path / "fabric.json").read_text(encoding="utf-8"))
    with Image.open(path / "tile.png") as im:
        arr = np.array(im.convert("RGB"), dtype=np.uint8)
    results = check_fabric_tile(arr, fabric_id=path.name)
    bad = [r for r in results if not r.passed]
    if bad:
        raise ValueError("; ".join(f"{r.metric}: {r.evidence}" for r in bad))
    grey = np.rint(arr.astype(np.float64).mean(axis=2)).astype(np.uint8)
    if grey.shape[0] > 1024:
        raise ValueError("fabric tile larger than 1024")
    grey = _fit_pow2(grey)
    return FabricTile(path.name, meta.get("material_family", "jersey"), grey, float(meta.get("px_per_repeat", 4.0)),
                      int(meta.get("repeats", 8)), min(float(meta.get("amplitude_dL", 4.0)), MAX_AMPLITUDE_DL), "library",
                      meta.get("prompt_phrase", ""))


def _fit_pow2(g: np.ndarray) -> np.ndarray:
    """Square tiles only; a non power-of-two tile is box-resized to 256 (the sampler does not need powers of two, the
    generators do)."""
    from PIL import Image

    n = g.shape[0]
    if g.shape[0] != g.shape[1]:
        raise ValueError("fabric tile must be square")
    if (n & (n - 1)) == 0:
        return g
    return np.asarray(Image.fromarray(g).resize((256, 256), Image.Resampling.BOX), dtype=np.uint8)


# --------------------------------------------------------------------------------------------------------------------
# admission checks (CLO-13)
# --------------------------------------------------------------------------------------------------------------------
def _grey_of(tile: np.ndarray) -> np.ndarray:
    return tile.astype(np.float64) if tile.ndim == 2 else tile[..., :3].astype(np.float64).mean(axis=2)


def seam_energy_ratio(tile: np.ndarray) -> float:
    """Gradient energy on the seam cross after a 50% roll, relative to the median interior gradient energy."""
    g = _grey_of(tile)
    n = g.shape[0]
    r = np.roll(np.roll(g, n // 2, axis=0), n // 2, axis=1)
    gy = np.abs(np.diff(r, axis=0))          # (n-1, n): gradient between rows k and k+1
    gx = np.abs(np.diff(r, axis=1))          # (n, n-1)
    c = n // 2
    cross = np.concatenate([gy[c - 1], gx[:, c - 1]])      # rows c-1|c and columns c-1|c: the old tile edges
    interior = np.concatenate([gy[:c - 4, :].mean(axis=1), gy[c + 4:, :].mean(axis=1),
                               gx[:, :c - 4].mean(axis=0), gx[:, c + 4:].mean(axis=0)])
    med = float(np.median(interior))
    return float(cross.mean() / med) if med > 1e-9 else 1.0


def alias_energy_fraction(tile: np.ndarray, to: int = 128, cutoff: float = 0.35) -> float:
    """Share of the (non-DC) spectral energy that would sit above ``cutoff`` cycles/px once the tile is resampled to ``to`` px
    (CLO-13 moire test). Measured on the original tile: a frequency f cycles/px there becomes f * n / to at ``to`` px, so the
    cut-off is ``cutoff * to / n``. (Box-downscaling first would hide exactly the content that aliases.)"""
    g = _grey_of(tile)
    n = g.shape[0]
    g = g - g.mean()
    spec = np.abs(np.fft.fft2(g)) ** 2
    fy = np.fft.fftfreq(g.shape[0])[:, None]
    fx = np.fft.fftfreq(g.shape[1])[None, :]
    rad = np.hypot(fx, fy)
    lim = cutoff * min(to, n) / n
    tot = spec.sum()
    return float(spec[rad > lim].sum() / tot) if tot > 0 else 0.0


def block_luminance_std(tile: np.ndarray, block: int = 64) -> float:
    """Std of the 64-px block means as a fraction of full scale (0-1): baked-in lighting shows up here."""
    g = _grey_of(tile)
    n = g.shape[0]
    b = min(block, n)
    m = g[:n // b * b, :n // b * b].reshape(n // b, b, n // b, b).mean(axis=(1, 3))
    return float(m.std() / 255.0)


def max_chroma(tile: np.ndarray) -> float:
    """Maximum CIELAB chroma over all pixels (0 for a greyscale tile)."""
    if tile.ndim == 2:
        return 0.0
    from duoskin.imaging.palette import srgb_to_lab

    lab = srgb_to_lab(tile[..., :3].reshape(-1, 3))
    return float(np.hypot(lab[:, 1], lab[:, 2]).max())


def check_fabric_tile(tile: np.ndarray, fabric_id: str = "") -> list[CheckResult]:
    """CLO-13 library admission (HARD): seams, moire, baked lighting, greyscale. ``tile`` is (N, N) or (N, N, 3|4) uint8."""
    sha = hashlib.sha256(np.ascontiguousarray(tile).tobytes()).hexdigest()
    out: list[CheckResult] = []

    def add(metric: str, value: float, key: str, ok: bool, op: str) -> None:
        out.append(CheckResult(check_id="A_FABRIC_TILE", fm_ids=TH.fm_ids_of(key), subject_sha=sha, kind="hard", passed=ok,
                               metric=metric, value=float(value), threshold=TH.describe(key, op),
                               evidence=f"{fabric_id or 'tile'}: {metric}={value:.4f}",
                               fix_hint="none" if ok else "regenerate", thresholds_version=TH.THRESHOLDS_VERSION))

    r = seam_energy_ratio(tile)
    add("seam_energy_ratio", r, "fabric.seam_energy_ratio", r <= TH.get("fabric.seam_energy_ratio"), "<=")
    a = alias_energy_fraction(tile)
    add("alias_energy_fraction", a, "fabric.alias_energy_max", a <= TH.get("fabric.alias_energy_max"), "<=")
    s = block_luminance_std(tile)
    add("block_lum_std", s, "fabric.flat_lum_std_max", s <= TH.get("fabric.flat_lum_std_max"), "<=")
    c = max_chroma(tile)
    add("chroma_max", c, "fabric.chroma_max", c <= TH.get("fabric.chroma_max"), "<=")
    return out


# --------------------------------------------------------------------------------------------------------------------
# strip layout and sampling (CLO-04)
# --------------------------------------------------------------------------------------------------------------------
def strip_layout(part: str) -> tuple[dict[str, float], float]:
    """Perimeter offsets (template px) of the four side faces of ``part`` in wrap order and the total strip length."""
    return T.strip_layout(part)


def _bilinear(tile: np.ndarray, tu: np.ndarray, tv: np.ndarray) -> np.ndarray:
    """Periodic bilinear sample of a float64 (N, N) tile at tile-pixel coordinates (pixel centres at +0.5)."""
    n = tile.shape[0]
    x = np.mod(tu - 0.5, n)
    y = np.mod(tv - 0.5, n)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = x - x0
    fy = y - y0
    x1 = (x0 + 1) % n
    y1 = (y0 + 1) % n
    top = tile[y0, x0] * (1 - fx) + tile[y0, x1] * fx
    bot = tile[y1, x0] * (1 - fx) + tile[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def _part_scale(ft: FabricTile, part: str) -> float:
    """Tile pixels per template pixel so that a whole number of tiles wraps the part's side strip exactly."""
    _, length = strip_layout(part)
    n_tiles = max(1, round(length / ft.template_px))
    return ft.size * n_tiles / length


def fabric_values(ft: FabricTile, region: str, scale: int = 4) -> np.ndarray:
    """float32 (h*scale, w*scale) fabric signal of ``region`` in [-1, 1] at ``scale`` times the template resolution.

    Side faces sample one continuous strip around the part (the wrap B -> R closes exactly); cap faces continue the side
    face across each of their four edges and blend those continuations by inverse distance, so the base fabric layer is
    continuous across every seam of ``ADJACENCY`` (CLO-04)."""
    part = T.PART_OF[region]
    face = T.FACE_OF[region]
    w, h = T.SIZE[region]
    _x0, y0, _x1, _y1 = T.REGIONS[region]
    s = _part_scale(ft, part)
    sig = ft.signed()
    offs, _length = strip_layout(part)
    y_ref = float(T.REGIONS[T.SIDE_CYCLE[part][0]][1])
    xs = (np.arange(w * scale) + 0.5) / scale
    ys = (np.arange(h * scale) + 0.5) / scale
    xl, yl = np.meshgrid(xs, ys)                      # local template-px coordinates of every sub-pixel centre
    if face in ("f", "b", "l", "r"):
        tu = (offs[region] + xl) * s
        tv = (y0 + yl - y_ref) * s
        return _bilinear(sig, tu, tv).astype(np.float32)
    # ---- caps: planar baseline plus inverse-distance blend of the four edge continuations
    front = next(r for r in T.SIDE_CYCLE[part] if T.FACE_OF[r] == "f")
    base_v = (yl - h) if face == "u" else (float(T.REGIONS[front][3] + 1) - y_ref + yl)
    base = _bilinear(sig, (offs[front] + xl) * s, base_v * s)
    num = base * (1.0 / 14.25 ** 3)
    den = np.full_like(base, 1.0 / 14.25 ** 3)
    for side in ("top", "bottom", "left", "right"):
        nb = T.neighbour(region, side)
        if nb is None:
            continue
        other, _other_side, reverse = nb
        edge_len = w if side in ("top", "bottom") else h
        tau = xl if side in ("top", "bottom") else yl
        dist = {"top": yl, "bottom": h - yl, "left": xl, "right": w - xl}[side]
        tau_n = (edge_len - tau) if reverse else tau
        _ox0, oy0, _ox1, oy1 = T.REGIONS[other]
        p = offs[other] + tau_n
        if face == "u":
            yy = oy0 - dist                       # continue upwards from the neighbour's top row
        else:
            yy = (oy1 + 1) + dist                 # continue downwards from the neighbour's bottom row
        val = _bilinear(sig, p * s, (yy - y_ref) * s)
        wgt = 1.0 / (dist + 0.25) ** 3
        num = num + val * wgt
        den = den + wgt
    return (num / den).astype(np.float32)
