"""Fold and shading overlays for the clothing compositor (APP_SPEC §10.5 step 3, S23, bible §11.8, CLO-14).

A fold overlay is one greyscale panel per template region at 4x (neutral grey 128). ``g < 128`` darkens (multiply),
``g > 128`` lightens (screen); the compositor applies it clamped to the garment mask. Two sources:

* ``procedural_folds(set_id)``: code-drawn soft creases and elbow bands (``builtin_kits/folds_procedural.json``), used
  whenever the recipe's library fold set is missing, so the app works with no user kit. Pure and deterministic.
* ``load_fold_set(dir)``: a curated library set ``folds/<set>/<region>.png`` (+ ``fold.json``) after admission checks.

Every panel keeps its outer 8% exactly neutral so neighbouring panels join without a lighting jump (CLO-14).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.imaging.fabric import DetRng
from duoskin.roblox import template as T

KIT_JSON = Path(__file__).resolve().parent.parent / "builtin_kits" / "folds_procedural.json"
OUTLINE_IOU_MIN = 0.98              # CLO-14 [DES]: panel outline vs recipe mask
NEUTRAL = 128
_SCALE = 4


@dataclass(frozen=True, eq=False)
class FoldSet:
    """A named set of fold panels keyed by template region (``torso_f`` ...). ``panels[region]`` is uint8 (h*4, w*4)
    with 128 = neutral; a region without a panel is flat. ``origin``: ``procedural`` or ``library``."""

    set_id: str
    origin: str
    panels: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return "procedural folds" if self.origin == "procedural" else f"fold set {self.set_id}"

    @property
    def sha256(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.set_id}|{self.origin}".encode())
        for k in sorted(self.panels):
            h.update(k.encode())
            h.update(np.ascontiguousarray(self.panels[k]).tobytes())
        return h.hexdigest()

    def delta(self, region: str, shape: tuple[int, int]) -> np.ndarray:
        """float32 (shape) overlay offsets in [-0.5, 0.5] (0 = neutral); resized (bilinear) if the stored panel differs."""
        p = self.panels.get(region)
        if p is None:
            return np.zeros(shape, dtype=np.float32)
        if p.shape != shape:
            from PIL import Image

            p = np.asarray(Image.fromarray(p).resize((shape[1], shape[0]), Image.Resampling.BILINEAR), dtype=np.uint8)
        return ((p.astype(np.float32) - NEUTRAL) / 255.0).astype(np.float32)


def flat_folds() -> FoldSet:
    """No folds at all."""
    return FoldSet("flat", "flat", {})


# --------------------------------------------------------------------------------------------------------------------
# procedural generation
# --------------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _kit() -> dict[str, Any]:
    return json.loads(KIT_JSON.read_text(encoding="utf-8"))


def procedural_set_ids() -> list[str]:
    """Ids of the procedural sets (``default`` is the fallback for any unknown recipe fold-set name)."""
    return sorted(_kit()["sets"])


def _rng(seed: int) -> DetRng:
    return DetRng(seed)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _profile(d: np.ndarray, sigma: float) -> np.ndarray:
    """Zero-mean crease profile: a dark trough flanked by two soft highlights (integral 0 across the line)."""
    q = d / sigma
    return -np.exp(-q * q) + 0.5 * (np.exp(-((q - 1.6) ** 2)) + np.exp(-((q + 1.6) ** 2)))


def _polyline_distance(px: np.ndarray, py: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance from every (px, py) to the polyline ``pts`` (S+1, 2) and the position (0-1) of the nearest point."""
    best = np.full(px.shape, np.inf)
    best_t = np.zeros(px.shape)
    n = len(pts) - 1
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0) if l2 > 0 else np.zeros_like(px)
        qx, qy = ax + t * dx, ay + t * dy
        d2 = (px - qx) ** 2 + (py - qy) ** 2
        better = d2 < best
        best = np.where(better, d2, best)
        best_t = np.where(better, (i + t) / n, best_t)
    return np.sqrt(best), best_t


def _bezier(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray, n: int = 18) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n + 1)[:, None]
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t * t * p2


def _panel_kind(region: str) -> str:
    face = T.FACE_OF[region]
    part = T.PART_OF[region]
    if face in ("u", "d"):
        return "cap"
    if part == "torso":
        return "torso_fb" if face in ("f", "b") else "torso_side"
    return "limb_side"


def _panel_delta(region: str, params: dict[str, Any], set_cfg: dict[str, Any], seed: int, edge: float) -> np.ndarray:
    """float64 (h*4, w*4) overlay offsets for one region (0 = neutral)."""
    w, h = T.SIZE[region]
    x0, y0, _x1, _y1 = T.REGIONS[region]
    kind = _panel_kind(region)
    # the creases are smooth (sigma >= 1.8 template px), so they are computed at 1x and upsampled to 4x
    X, Y = np.meshgrid((np.arange(w) + 0.5), (np.arange(h) + 0.5))
    delta = np.zeros_like(X)
    rng = _rng(seed * 100003 + T.REGION_IDS[region] * 977)
    scale = float(set_cfg.get("scale", 1.0))
    base = params["defaults"].get(kind, {})
    n_creases = int(base.get("creases", 0)) + (int(set_cfg.get("extra_creases", 0)) if kind != "cap" else 0)
    sigma = float(base.get("sigma", 2.0)) * float(set_cfg.get("sigma_scale", 1.0))
    strength = float(base.get("strength", 0.05)) * scale
    length = float(base.get("length", 0.6))
    tilt = np.deg2rad(float(base.get("tilt_deg", 20)) * float(set_cfg.get("tilt_scale", 1.0)))
    for _ in range(n_creases):
        if kind == "limb_side":
            row = 418.5 - y0
            p0 = np.array([rng.uniform(0.1, 0.45) * w, row + rng.uniform(-9.0, 9.0)])
            ang = rng.uniform(-tilt, tilt)
            ln = length * w * rng.uniform(0.8, 1.2)
            p2 = p0 + ln * np.array([np.cos(ang), np.sin(ang)])
        else:
            p0 = np.array([rng.uniform(0.15, 0.85) * w, rng.uniform(0.06, 0.28) * h])
            ang = rng.uniform(-tilt, tilt)
            ln = length * h * rng.uniform(0.7, 1.0)
            p2 = p0 + ln * np.array([np.sin(ang), np.cos(ang)])
        mid = (p0 + p2) / 2.0
        perp = np.array([-(p2 - p0)[1], (p2 - p0)[0]])
        perp /= max(np.linalg.norm(perp), 1e-9)
        p1 = mid + perp * rng.uniform(-0.18, 0.18) * ln
        pts = _bezier(p0, p1, p2)
        d, t = _polyline_distance(X, Y, pts)
        env = np.sin(np.pi * np.clip(t, 0.0, 1.0)) ** 0.8
        delta += strength * env * _profile(d, sigma) * rng.uniform(0.7, 1.1)
    # elbow / knee bands: soft darker rings 7 px either side of the R15 split row (never on 418/419)
    if kind == "limb_side" and not set_cfg.get("no_elbow", False):
        el = params["defaults"].get("elbow", {})
        row = float(el.get("row", 418.5))
        span = float(el.get("span", 14))
        sig = float(el.get("sigma", 2.2))
        amp = float(el.get("strength", 0.05)) * scale
        ya = y0 + Y
        wob = 0.65 + 0.35 * np.cos(2 * np.pi * (X / w * rng.integers(1, 3) + float(rng.random())))
        for off in (-span / 2.0, span / 2.0):
            delta += amp * wob * _profile(ya - (row + off), sig)
    pl = set_cfg.get("pleats")
    if pl and kind in ("torso_fb", "limb_side"):
        r0, r1 = pl.get("rows", [T.TORSO_ROWS[0], T.LIMB_ROWS[1]])
        ya = y0 + Y
        stripe = np.sin(2 * np.pi * (x0 + X) / float(pl.get("period", 9)))
        taper = _smoothstep(r0, r0 + 4, ya) * (1.0 - _smoothstep(r1 - 4, r1, ya))
        delta += float(pl.get("strength", 0.05)) * stripe * taper
    from PIL import Image

    up = np.asarray(Image.fromarray(delta.astype(np.float32), "F").resize((w * _SCALE, h * _SCALE), Image.Resampling.BICUBIC),
                    dtype=np.float64)
    X4, Y4 = np.meshgrid((np.arange(w * _SCALE) + 0.5) / _SCALE, (np.arange(h * _SCALE) + 0.5) / _SCALE)
    dx = np.minimum(X4, w - X4) / w
    dy = np.minimum(Y4, h - Y4) / h
    window = _smoothstep(edge, edge * 1.75, np.minimum(dx, dy))
    return np.clip(up * window, -0.5, 0.5)


@lru_cache(maxsize=32)
def procedural_folds(set_id: str = "default") -> FoldSet:
    """Deterministic code-drawn fold set for ``set_id`` (unknown ids use ``default``). Cached; panels are read-only."""
    cfg = _kit()
    sets = cfg["sets"]
    set_cfg = sets.get(set_id, sets["default"])
    seed = int(set_cfg.get("seed", 7))
    edge = float(cfg.get("edge_band", 0.08))
    panels: dict[str, np.ndarray] = {}
    for region in T.REGION_ORDER:
        if _panel_kind(region) == "cap":
            continue
        d = _panel_delta(region, cfg, set_cfg, seed, edge)
        g = np.rint(NEUTRAL + 255.0 * d).clip(0, 255).astype(np.uint8)
        g.setflags(write=False)
        panels[region] = g
    return FoldSet(set_id if set_id in sets else "default", "procedural", panels)


def resolve_folds(set_id: str | None, kit_dirs: list[Path] | None = None) -> FoldSet:
    """A curated library set wins over the procedural one; a missing set falls back to procedural folds (labelled so on the
    tile, APP_SPEC S23). Never raises for a missing set."""
    sid = (set_id or "").strip()
    for d in kit_dirs or []:
        p = Path(d) / sid
        if sid and (p / "fold.json").exists():
            return load_fold_set(p)
    return procedural_folds(sid or "default")


# --------------------------------------------------------------------------------------------------------------------
# library sets and admission checks (CLO-14)
# --------------------------------------------------------------------------------------------------------------------
def edge_band_mask(shape: tuple[int, int], frac: float = 0.08) -> np.ndarray:
    """bool mask of the outer ``frac`` along every panel edge."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    return (xs < frac * w) | (xs >= (1 - frac) * w) | (ys < frac * h) | (ys >= (1 - frac) * h)


def check_fold_panel(panel: np.ndarray, *, region: str = "", outline: np.ndarray | None = None) -> list[CheckResult]:
    """CLO-14 admission (HARD) for one panel: mean 128 +-8, outer 8% edge band std <= 3 grey levels, chroma <= 3,
    optional outline IoU >= 0.98 against the recipe mask (``panel`` then uses white = outside, mid-grey = inside)."""
    sha = hashlib.sha256(np.ascontiguousarray(panel).tobytes()).hexdigest()
    rgb = panel[..., :3].astype(np.float64) if panel.ndim == 3 else None
    grey = rgb.mean(axis=2) if rgb is not None else panel.astype(np.float64)
    inside = np.ones(grey.shape, bool)
    if outline is not None:
        inside = grey < 250.0
    out: list[CheckResult] = []

    def add(metric: str, value: float, key: str, ok: bool, op: str, hint: str = "regenerate") -> None:
        out.append(CheckResult(check_id="A_FOLD_PANEL", fm_ids=TH.fm_ids_of(key), subject_sha=sha, kind="hard", passed=ok,
                               metric=metric, value=float(value), threshold=TH.describe(key, op),
                               evidence=f"{region or 'panel'}: {metric}={value:.3f}",
                               fix_hint="none" if ok else hint, thresholds_version=TH.THRESHOLDS_VERSION))  # type: ignore[arg-type]

    lo, hi = TH.get("fold.mean_grey")
    mean = float(grey[inside].mean()) if inside.any() else 0.0
    add("mean_grey", mean, "fold.mean_grey", lo <= mean <= hi, "in")
    band = edge_band_mask(grey.shape) & inside
    std = float(grey[band].std()) if band.any() else 0.0
    add("edge_band_std", std, "fold.edge_band_std_max", std <= TH.get("fold.edge_band_std_max"), "<=")
    if rgb is not None:
        from duoskin.imaging.palette import srgb_to_lab

        lab = srgb_to_lab(rgb[inside].reshape(-1, 3))
        chroma = float(np.hypot(lab[:, 1], lab[:, 2]).max()) if len(lab) else 0.0
        add("chroma_max", chroma, "fabric.chroma_max", chroma <= TH.get("fabric.chroma_max"), "<=")
    if outline is not None:
        o = outline.astype(bool)
        union = float((inside | o).sum())
        iou = float((inside & o).sum() / union) if union else 0.0
        out.append(CheckResult(check_id="A_FOLD_PANEL", fm_ids=["CLO-14"], subject_sha=sha, kind="hard",
                               passed=iou >= OUTLINE_IOU_MIN, metric="outline_iou", value=iou,
                               threshold=f">= {OUTLINE_IOU_MIN} (CLO-14, DES)", evidence=f"{region or 'panel'}: iou={iou:.3f}",
                               fix_hint="none" if iou >= OUTLINE_IOU_MIN else "masked_edit",
                               thresholds_version=TH.THRESHOLDS_VERSION))
    return out


def load_fold_set(path: Path) -> FoldSet:
    """Load a curated set: ``<path>/fold.json`` (``{"set_id": ..}``) and ``<path>/<region>.png`` greyscale panels (any size,
    stretched to 4x). Every panel must pass ``check_fold_panel``; raises ValueError listing the failures."""
    from PIL import Image

    meta = json.loads((path / "fold.json").read_text(encoding="utf-8-sig"))
    panels: dict[str, np.ndarray] = {}
    bad: list[str] = []
    for region in T.REGION_ORDER:
        f = path / f"{region}.png"
        if not f.exists():
            continue
        with Image.open(f) as im:
            arr = np.array(im.convert("L"), dtype=np.uint8)
        for r in check_fold_panel(arr, region=region):
            if not r.passed:
                bad.append(f"{region}.{r.metric}")
        w, h = T.SIZE[region]
        if arr.shape != (h * _SCALE, w * _SCALE):
            arr = np.asarray(Image.fromarray(arr).resize((w * _SCALE, h * _SCALE), Image.Resampling.BILINEAR), dtype=np.uint8)
        panels[region] = arr
    if bad:
        raise ValueError("fold panels failed admission: " + ", ".join(bad))
    return FoldSet(str(meta.get("set_id", path.name)), "library", panels)
