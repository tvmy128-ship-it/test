"""Orientation search (APP_SPEC 10.9 step 8; FM MESH-12, ACC-01, CHK-M08).

The best of the 24 axis rotations is picked by silhouette IoU against the approved views (the front alone, or the mean over
every view supplied; ``left`` is the subject's left, the object's front pointing to the image's left edge). The mirrored mesh
is searched the same way. A model whose mirrored match beats the true match by ``acc.mirror_margin`` is FLAGGED, never
flipped automatically; ``flip_lr`` is the explicit user action ("Flip left/right (I checked)").

Roblox's forward axis in a mesh file is an open question (FM T5): ``forward_axis`` lets the Studio calibration pick the file
axis the object's front must face; the search itself always works in the canonical frame (front +Z).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from duoskin.mesh import views as V
from duoskin.mesh.geometry import all_axis_rotations
from duoskin.mesh.types import MeshData
from duoskin.roblox import limits

FORWARD_AXES = ("+Z", "-Z", "+X", "-X")


def forward_rotation(axis: str) -> np.ndarray:
    """Rotation about Y that carries the canonical front (+Z) onto ``axis`` ("+Z" is the identity)."""
    axis = (axis or "+Z").strip().upper().replace(" ", "")
    if axis in ("Z", "+Z"):
        return np.eye(3)
    table = {"-Z": 180.0, "+X": 90.0, "X": 90.0, "-X": -90.0}
    if axis not in table:
        raise ValueError(f"unknown forward axis {axis!r}; expected one of {', '.join(FORWARD_AXES)}")
    t = np.radians(table[axis])
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def to_export_frame(vertices: np.ndarray, axis: str) -> np.ndarray:
    return vertices @ forward_rotation(axis).T


def from_export_frame(vertices: np.ndarray, axis: str) -> np.ndarray:
    return vertices @ forward_rotation(axis)


@dataclass
class OrientResult:
    rotation_index: int
    rotation: np.ndarray
    iou: float                        # best true match (mean over the views used)
    mirror_iou: float                 # best mirrored match
    mirrored: bool                    # mirror beats the true match by the margin: FLAGGED
    ambiguous: bool                   # true and mirrored match within the margin (a left-right symmetric object)
    per_view: dict[str, float] = field(default_factory=dict)
    views_used: list[str] = field(default_factory=list)
    identity_iou: float = 0.0
    margin: float = 0.02
    min_iou: float = 0.80

    @property
    def passed(self) -> bool:
        return self.iou >= self.min_iou and not self.mirrored

    def facts(self) -> dict[str, Any]:
        return {"rotation_index": self.rotation_index, "rotation": self.rotation.round(6).tolist(), "iou": round(self.iou, 4),
                "mirror_iou": round(self.mirror_iou, 4), "mirrored": self.mirrored, "ambiguous": self.ambiguous,
                "per_view": {k: round(v, 4) for k, v in self.per_view.items()}, "views_used": self.views_used,
                "identity_iou": round(self.identity_iou, 4)}


def _scores(vertices: np.ndarray, faces: np.ndarray, masks: dict[str, np.ndarray], size: int) -> tuple[float, dict[str, float]]:
    per = {name: V.silhouette_iou_normed(vertices, faces, name, m, size=size) for name, m in masks.items()}
    return float(np.mean(list(per.values()))), per


def search_orientation(mesh: MeshData, approved_views: dict[str, Any], *, margin: float | None = None, min_iou: float | None = None,
                       views: tuple[str, ...] | None = None, size: int = 96) -> OrientResult:
    """Best of the 24 axis rotations (and of the 24 mirrored ones) against the approved views.

    ``approved_views`` maps view name or file name to a PNG path / PIL image / array. At least the front must be present.
    """
    margin = limits.threshold("acc.mirror_margin") if margin is None else margin
    min_iou = limits.threshold("mesh.orient_iou_min") if min_iou is None else min_iou
    masks = V.approved_masks(approved_views)
    if views:
        masks = {k: m for k, m in masks.items() if k in views}
    if "front" not in masks:
        raise ValueError("an approved front view is required for the orientation search")
    masks = {k: V.normalise_mask(m, max(48, size - 16)) for k, m in masks.items()}
    verts = mesh.vertices - mesh.bounds.mean(axis=0)
    faces = mesh.faces
    rots = all_axis_rotations()
    mirror = np.diag([-1.0, 1.0, 1.0])
    best, best_m = (-1.0, 0, {}), (-1.0, 0, {})
    identity_iou = 0.0
    for i, r in enumerate(rots):
        s, per = _scores(verts @ r.T, faces, masks, size)
        if i == 0:
            identity_iou = s
        if s > best[0]:
            best = (s, i, per)
        sm, perm = _scores((verts @ mirror.T) @ r.T, faces, masks, size)
        if sm > best_m[0]:
            best_m = (sm, i, perm)
    mirrored = (best_m[0] - best[0]) >= margin
    ambiguous = abs(best_m[0] - best[0]) < margin
    return OrientResult(best[1], rots[best[1]], best[0], best_m[0], mirrored, ambiguous, best[2], sorted(masks), identity_iou, margin, min_iou)


def apply_rotation(mesh: MeshData, rotation: np.ndarray) -> MeshData:
    """A copy rotated by ``rotation`` (a proper rotation; reflections also flip the winding)."""
    out = mesh.copy()
    out.vertices = mesh.vertices @ np.asarray(rotation, float).T
    if np.linalg.det(rotation) < 0:
        out.faces = out.faces[:, ::-1].copy()
    return out


def flip_lr(mesh: MeshData) -> MeshData:
    """The user's "Flip left/right (I checked)" action: negate X, repair the winding. UVs and texture stay as they are, so
    the model is the exact mirror image of what was imported. Logged by the caller."""
    out = mesh.copy()
    out.vertices = out.vertices * np.array([-1.0, 1.0, 1.0])
    out.faces = out.faces[:, ::-1].copy()      # mirroring reverses the handedness; reversing the faces restores outward normals
    out.meta["flipped_lr"] = True
    if "attachment_offset" in out.meta:
        a = np.asarray(out.meta["attachment_offset"], float) * np.array([-1.0, 1.0, 1.0])
        out.meta["attachment_offset"] = a.tolist()
    return out
