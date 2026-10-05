"""Warp the 2D face canvas into a head base's UV layout through a lookup table (APP_SPEC §10.6 "With a head base", FACE-05).

``uv_lut.npz`` (one per head-base variant, keyed by ``head_mesh.sha256``) holds, for every texel of the head UV image at ``density`` times
the final resolution, the **source coordinate on the face canvas** to sample (``src_xy``, float32 ``(H, W, 2)`` as ``x, y`` canvas pixels,
NaN where the UV texel is not covered by the face). ``apply_lut`` samples the canvas in **premultiplied** RGBA with bilinear filtering,
box-downsamples by ``density`` and un-premultiplies, so skin stays transparent and feature edges keep their colour.

UV convention (the one helper every caller must use): the image row ``r`` of a ``(W, H)`` UV texture corresponds to ``v = 1 - (r + 0.5) / H``
(FBX / glTF-style v = 0 at the *bottom* of the image after the exporters' flip), ``u = (c + 0.5) / W``.

A real LUT is built once by ``blender_scripts/kit_build.py`` from the head mesh. ``synthetic_lut`` builds an exact rectangle-island LUT for
tests and for kits that map the face canvas onto simple UV islands.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result

LUT_VERSION = 1


class StaleLutError(AssertionError):
    """The LUT was built for another head mesh than the one in use (FACE-05 ASSERT)."""


# ------------------------------------------------------------------ UV convention
def uv_to_pixel(u: float, v: float, w: int, h: int) -> tuple[float, float]:
    """UV (v = 0 at the bottom) to continuous pixel coordinates of a ``w x h`` image (pixel centres at +0.5)."""
    return u * w - 0.5, (1.0 - v) * h - 0.5


def pixel_to_uv(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    return (x + 0.5) / w, 1.0 - (y + 0.5) / h


# ------------------------------------------------------------------ the LUT
@dataclass
class UvLut:
    src_xy: np.ndarray          # (H*density, W*density, 2) float32: canvas x, y to sample; NaN = not covered
    canvas_size: tuple[int, int]
    final_size: tuple[int, int]
    density: int
    mesh_sha256: str
    version: int = LUT_VERSION

    @property
    def uv_size(self) -> tuple[int, int]:
        return self.src_xy.shape[1], self.src_xy.shape[0]

    def covered(self) -> np.ndarray:
        """Bool mask at LUT resolution: texels that sample the canvas."""
        return np.isfinite(self.src_xy[..., 0])

    def save(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(p, src_xy=self.src_xy.astype(np.float32), canvas_size=np.array(self.canvas_size, np.int32),
                            final_size=np.array(self.final_size, np.int32), density=np.int32(self.density),
                            mesh_sha256=np.array(self.mesh_sha256), version=np.int32(self.version))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> UvLut:
        with np.load(Path(path), allow_pickle=False) as z:
            for key in ("src_xy", "canvas_size", "final_size", "density", "mesh_sha256"):
                if key not in z.files:
                    raise ValueError(f"uv_lut.npz is missing {key!r}")
            return cls(z["src_xy"].astype(np.float32), tuple(int(v) for v in z["canvas_size"]),   # type: ignore[arg-type]
                       tuple(int(v) for v in z["final_size"]), int(z["density"]), str(z["mesh_sha256"]),   # type: ignore[arg-type]
                       int(z["version"]) if "version" in z.files else LUT_VERSION)


def assert_lut_key(lut: UvLut, head_mesh_sha256: str) -> None:
    """ASSERT that the LUT belongs to this head mesh (``head_mesh.sha256``); a stale LUT raises ``StaleLutError``."""
    if lut.mesh_sha256.lower() != head_mesh_sha256.lower():
        raise StaleLutError(f"LUT is for mesh {lut.mesh_sha256[:12]}, head mesh is {head_mesh_sha256[:12]}")
    if lut.density < 1 or lut.src_xy.shape[:2] != (lut.final_size[1] * lut.density, lut.final_size[0] * lut.density):
        raise StaleLutError("LUT density or size does not match its header")


def check_lut_key(lut: UvLut, head_mesh_sha256: str, *, subject_sha: str = "") -> CheckResult:
    """F_LUT_KEY (FACE-05, ASSERT): the LUT is keyed by the current head-mesh hash."""
    try:
        assert_lut_key(lut, head_mesh_sha256)
    except StaleLutError as e:
        return build_result("F_LUT_KEY", passed=False, subject_sha=subject_sha, metric="mesh_sha", evidence=str(e), fix_hint="human")
    return build_result("F_LUT_KEY", passed=True, subject_sha=subject_sha, metric="mesh_sha", evidence="LUT matches the head mesh")


def apply_lut(canvas: Image.Image, lut: UvLut, *, head_mesh_sha256: str | None = None) -> Image.Image:
    """Warp the face canvas (RGBA) into the head UV texture of ``lut.final_size`` (premultiplied sampling, then area downsample).

    Pass ``head_mesh_sha256`` to assert the LUT is not stale. Uncovered texels are fully transparent; skin stays transparent and
    features keep their alpha.
    """
    import cv2

    if head_mesh_sha256 is not None:
        assert_lut_key(lut, head_mesh_sha256)
    if canvas.size != tuple(lut.canvas_size):
        raise ValueError(f"canvas is {canvas.size}, the LUT expects {tuple(lut.canvas_size)}")
    rgba = np.asarray(canvas.convert("RGBA"), dtype=np.float32) / 255.0
    pre = rgba.copy()
    pre[..., :3] *= pre[..., 3:4]
    src = lut.src_xy
    cov = np.isfinite(src[..., 0])
    mx = np.where(cov, src[..., 0], -1.0).astype(np.float32)
    my = np.where(cov, src[..., 1], -1.0).astype(np.float32)
    samp = cv2.remap(pre, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    samp[~cov] = 0.0
    d = lut.density
    fw, fh = lut.final_size
    down = samp.reshape(fh, d, fw, d, 4).mean(axis=(1, 3))
    a = down[..., 3:4]
    rgb = np.where(a > 1e-6, down[..., :3] / np.maximum(a, 1e-6), 0.0)
    out = np.concatenate([np.clip(rgb, 0, 1), np.clip(a, 0, 1)], axis=-1)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8), "RGBA")


# ------------------------------------------------------------------ synthetic LUT (tests, and kits with plain rectangle islands)
@dataclass(frozen=True)
class Island:
    """A rectangle of the head UV (``u0, v0, u1, v1``, v = 0 at the bottom) that shows a rectangle of the face canvas.

    ``canvas_rect`` is ``(x0, y0, x1, y1)`` in canvas pixels (y down). ``flip_x`` mirrors the island (a left-eye island that is mapped
    from the right-eye part of the canvas, for example); ``rotate90`` rotates it by 90 degrees clockwise.
    """

    uv_rect: tuple[float, float, float, float]
    canvas_rect: tuple[float, float, float, float]
    flip_x: bool = False
    rotate90: bool = False


def synthetic_lut(canvas_size: tuple[int, int], final_size: tuple[int, int], density: int, mesh_sha256: str, islands: Sequence[Island]) -> UvLut:
    """Build an exact LUT from rectangle islands (later islands overwrite earlier ones where they overlap)."""
    fw, fh = final_size
    w, h = fw * density, fh * density
    src = np.full((h, w, 2), np.nan, np.float32)
    cols = (np.arange(w) + 0.5) / w
    rows = 1.0 - (np.arange(h) + 0.5) / h
    uu, vv = np.meshgrid(cols, rows)
    for isl in islands:
        u0, v0, u1, v1 = isl.uv_rect
        cx0, cy0, cx1, cy1 = isl.canvas_rect
        inside = (uu >= u0) & (uu < u1) & (vv >= v0) & (vv < v1)
        s = (uu - u0) / (u1 - u0)                      # 0..1 left to right in UV
        t = (v1 - vv) / (v1 - v0)                      # 0..1 top to bottom (v decreases downward)
        if isl.rotate90:
            s, t = t, 1.0 - s
        if isl.flip_x:
            s = 1.0 - s
        x = cx0 + s * (cx1 - cx0) - 0.5
        y = cy0 + t * (cy1 - cy0) - 0.5
        src[inside, 0] = x[inside]
        src[inside, 1] = y[inside]
    return UvLut(src, tuple(canvas_size), tuple(final_size), density, mesh_sha256)   # type: ignore[arg-type]


def render_islands_back(texture: Image.Image, islands: Sequence[Island], canvas_size: tuple[int, int]) -> Image.Image:
    """What a renderer would show of the head front: the texture sampled back onto canvas coordinates through the (analytic) islands.

    Only for synthetic LUTs; the real renderer is ``render/avatar.py``. Lets tests close the loop for the warp-IoU check.
    """
    tex = np.asarray(texture.convert("RGBA"))
    th, tw = tex.shape[:2]
    cw, ch = canvas_size
    out = np.zeros((ch, cw, 4), np.uint8)
    ys, xs = np.mgrid[0:ch, 0:cw]
    for isl in islands:
        u0, v0, u1, v1 = isl.uv_rect
        cx0, cy0, cx1, cy1 = isl.canvas_rect
        inside = (xs + 0.5 >= cx0) & (xs + 0.5 < cx1) & (ys + 0.5 >= cy0) & (ys + 0.5 < cy1)
        s = (xs + 0.5 - cx0) / (cx1 - cx0)
        t = (ys + 0.5 - cy0) / (cy1 - cy0)
        if isl.flip_x:
            s = 1.0 - s
        if isl.rotate90:
            s, t = 1.0 - t, s
        u = u0 + s * (u1 - u0)
        v = v1 - t * (v1 - v0)
        px = np.clip((u * tw).astype(int), 0, tw - 1)
        py = np.clip(((1.0 - v) * th).astype(int), 0, th - 1)
        out[inside] = tex[py[inside], px[inside]]
    return Image.fromarray(out, "RGBA")


# ------------------------------------------------------------------ head-base checks that need renders (CHK-B09 / FACE-05, FACE-08)
def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        b_im = Image.fromarray((b * 255).astype(np.uint8)).resize((a.shape[1], a.shape[0]), Image.Resampling.NEAREST)
        b = np.asarray(b_im) > 127
    u = np.logical_or(a, b).sum()
    return 1.0 if u == 0 else float(np.logical_and(a, b).sum() / u)


def check_warp_iou(canvas_alpha: np.ndarray, render_alpha: np.ndarray, *, subject_sha: str = "") -> CheckResult:
    """F_WARP_IOU (FACE-05, HARD, head base only): the feature mask of the head-front render matches the face canvas (IoU >= 0.95)."""
    iou = mask_iou(canvas_alpha >= 128 if canvas_alpha.dtype != bool else canvas_alpha,
                   render_alpha >= 128 if render_alpha.dtype != bool else render_alpha)
    return build_result("F_WARP_IOU", passed=iou >= float(TH.get("face.warp_iou_min")), subject_sha=subject_sha, metric="warp_iou", value=iou,
                        threshold=TH.describe("face.warp_iou_min", ">="), evidence=f"feature IoU {iou:.3f}", fix_hint="human")


def check_stretch(stretch_by_pose: dict[str, np.ndarray], feature_mask_by_pose: dict[str, np.ndarray] | None = None, *,
                  subject_sha: str = "") -> CheckResult:
    """F_STRETCH (FACE-08, HARD, head base only): triangles under feature pixels stretch at most 1.5x their neutral UV-to-3D area ratio
    in every pose. ``stretch_by_pose[pose]`` is the per-texel (or per-triangle) ratio; ``feature_mask_by_pose`` selects the feature texels."""
    lim = float(TH.get("face.facs_stretch_max"))
    worst = 0.0
    worst_pose = ""
    for pose, ratio in stretch_by_pose.items():
        r = np.asarray(ratio, dtype=np.float64)
        m = None if feature_mask_by_pose is None else feature_mask_by_pose.get(pose)
        vals = r[m] if m is not None and m.shape == r.shape else r.ravel()
        vals = vals[np.isfinite(vals)]
        if vals.size:
            hi = float(np.maximum(vals, 1.0 / np.maximum(vals, 1e-9)).max())     # stretch and squash both count
            if hi > worst:
                worst, worst_pose = hi, pose
    return build_result("F_STRETCH", passed=worst <= lim, subject_sha=subject_sha, metric="stretch_max", value=worst,
                        threshold=TH.describe("face.facs_stretch_max", "<="), evidence=f"worst {worst:.2f}x in pose {worst_pose or 'n/a'}",
                        fix_hint="human")
