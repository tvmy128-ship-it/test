"""Job/result contracts of the mesh worker (APP_SPEC 10.9) and the in-memory mesh used by every mesh module.

Conventions used everywhere in ``duoskin.mesh`` (one place, so MESH-13 stays testable):

* units are studs, Y up, the front of the object faces +Z (APP_SPEC S6), the character's own left is +X;
* ``MeshData.vertices`` are *seam-split* (one UV per vertex, exactly like glTF), so topology tests always run on a
  position-welded copy (``geometry.weld``);
* ``MeshData.uv`` uses the glTF/image convention: ``(0, 0)`` is the TOP-LEFT of the texture image and ``v`` grows
  downwards. FBX and OBJ use ``v`` up; ``uv_gltf_to_gl`` / ``uv_gl_to_gltf`` are the only conversions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from pydantic import Field

from duoskin.checks.model import CheckResult
from duoskin.models.common import Strict

AssetType = Literal["Hat", "Hair", "Face", "Neck", "Shoulder", "Front", "Back", "Waist"]
MeshOp = Literal["import", "repair", "validate", "render_views", "slab", "primitive", "fit_hair", "register_hair", "flip_lr"]

Licence = Literal[
    "n/a", "tripo_api_private_commercial", "tripo_paid_private_commercial", "tripo_free_public_ccby_noncommercial",
    "user_made", "unknown",
]


class MeshJob(Strict):
    """What ``python -m duoskin.mesh.worker job.json`` reads (APP_SPEC 10.9; the ops ``register_hair`` and ``flip_lr`` and the
    trailing optional fields are additive)."""

    op: MeshOp
    input_path: str
    out_dir: str
    asset_type: AssetType
    attachment: str                          # e.g. "RightCollarAttachment"
    target_studs: tuple[float, float, float]
    tris_target: int                         # 3800 accessories, 3600 hair
    texture_px: int                          # 1024 (512 for props of about 2 studs)
    approved_views: dict[str, str] = Field(default_factory=dict)   # view -> PNG path (orientation and judging)
    mannequin: str = ""                      # path of mannequin_blocky.json ("" = the built-in code mannequin)
    forward_axis: str = "+Z"                 # from the Studio calibration (settings.capabilities.studio.forward_axis)
    # ---- additive fields ----
    params: dict[str, Any] = Field(default_factory=dict)   # op specific options (see duoskin.mesh.worker)
    asset_id: str = ""
    licence: Licence = "unknown"
    blender_path: str = ""                   # "" = auto-detect
    result_path: str = ""                    # "" = <out_dir>/result.json


class MeshResult(Strict):
    """What the worker writes back (and what ``run_job`` returns)."""

    ok: bool
    files: dict[str, str] = Field(default_factory=dict)     # "gltf", "bin", "png", "fbx", "glb_archive", "renders/*"
    facts: dict[str, Any] = Field(default_factory=dict)     # tris, shells, bbox_studs, box_margins, surface_area, ...
    checks: list[CheckResult] = Field(default_factory=list)
    # ---- additive fields ----
    messages: list[str] = Field(default_factory=list)       # plain-language guidance for the user
    degraded: list[str] = Field(default_factory=list)       # optional tools that were missing (e.g. pymeshlab) and what ran instead
    error: str = ""                                         # machine-readable reason when the job did not finish


class MeshError(Exception):
    """A mesh problem that has a user-facing explanation. ``code`` is stable, ``message`` is plain language."""

    def __init__(self, code: str, message: str, *, fix_hint: str = "human") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix_hint = fix_hint


def uv_gltf_to_gl(uv: np.ndarray) -> np.ndarray:
    """glTF/image UV (v down) to OpenGL/FBX/OBJ UV (v up). The conversion is its own inverse."""
    out = np.asarray(uv, float).copy()
    out[:, 1] = 1.0 - out[:, 1]
    return out


uv_gl_to_gltf = uv_gltf_to_gl


@dataclass
class MeshData:
    """One mesh, one material, one UV set (or none), in the conventions above."""

    vertices: np.ndarray                      # (N, 3) float64, seam-split
    faces: np.ndarray                         # (M, 3) int64
    uv: np.ndarray | None = None              # (N, 2) float64, glTF convention
    texture: Any = None                       # PIL.Image.Image (any mode) or None
    meta: dict[str, Any] = field(default_factory=dict)   # loader facts, provenance of edits, attachment_offset ...

    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = np.asarray(self.faces, dtype=np.int64).reshape(-1, 3)
        if self.uv is not None:
            self.uv = np.asarray(self.uv, dtype=np.float64).reshape(-1, 2)
            if len(self.uv) != len(self.vertices):
                raise ValueError("uv and vertices must have the same length (seam-split vertices)")

    @property
    def n_tris(self) -> int:
        return len(self.faces)

    @property
    def bounds(self) -> np.ndarray:
        if len(self.vertices) == 0:
            return np.zeros((2, 3))
        used = np.unique(self.faces) if len(self.faces) else np.arange(len(self.vertices))
        v = self.vertices[used]
        return np.stack([v.min(axis=0), v.max(axis=0)])

    @property
    def extents(self) -> np.ndarray:
        b = self.bounds
        return b[1] - b[0]

    def copy(self) -> MeshData:
        return MeshData(self.vertices.copy(), self.faces.copy(), None if self.uv is None else self.uv.copy(),
                        None if self.texture is None else self.texture.copy(), dict(self.meta))

    def compact(self) -> MeshData:
        """Drop vertices no face uses (indices renumbered)."""
        if len(self.faces) == 0:
            return self
        used = np.unique(self.faces)
        remap = -np.ones(len(self.vertices), dtype=np.int64)
        remap[used] = np.arange(len(used))
        return MeshData(self.vertices[used], remap[self.faces], None if self.uv is None else self.uv[used],
                        self.texture, dict(self.meta))
