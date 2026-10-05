"""The code-built blocky R15-like mannequin and its dressing (APP_SPEC 10.11, S11, FAILURE_MODES X26).

* Geometry is built from code: studs, Y up, front +Z, the character's own left is +X (so the character's right arm is at -X).
  Part sizes come from the classic template (64 px per stud): torso 2 x 1.5 + 2 x 0.5, limbs 1.0 + 0.758 + 0.25 studs, a 1.2 stud
  cube head, 5.2 studs tall.
* Attachment positions live in the ``Mannequin`` object (``mannequin_blocky.json`` may override them); the defaults here are
  approximations taken from the BlockyCharacter offsets quoted in FAILURE_MODES 4.2 (X26) and are never hard-coded elsewhere.
* ``dress`` bakes the Shirt and Pants templates onto the parts through the REAL UV regions of the 585x559 classic template
  (``TEMPLATE_PART_FACES``, taken from the R15 analysis): layering body colour -> modesty -> Pants -> Shirt on the torso, Shirt
  only on the arms, Pants only on the legs. Label canvases (fabric, block, print, trim, shoes ...) are rendered through the
  same UVs so every beauty pixel has a garment label.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from duoskin.mesh.types import MeshData
from duoskin.render.raster import RenderMesh

TEMPLATE_SIZE = (585, 559)

# part -> face -> [x0, y0, x1, y1) pixel rectangle in the 585x559 template (edges). Faces are named by the axis they face.
TEMPLATE_PART_FACES: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "RightUpperArm": {"+X": (19, 355, 83, 418.5), "-X": (151, 355, 215, 418.5), "+Z": (217, 355, 281, 418.5), "-Z": (85, 355, 149, 418.5), "+Y": (217, 289, 281, 353)},
    "RightLowerArm": {"+X": (19, 418.5, 83, 467), "-X": (151, 418.5, 215, 467), "+Z": (217, 418.5, 281, 467), "-Z": (85, 418.5, 149, 467)},
    "RightHand": {"+X": (19, 467, 83, 483), "-X": (151, 467, 215, 483), "+Z": (217, 467, 281, 483), "-Z": (85, 467, 149, 483), "-Y": (217, 485, 281, 549)},
    "LeftUpperArm": {"+X": (374, 355, 438, 418.5), "-X": (506, 355, 570, 418.5), "+Z": (308, 355, 372, 418.5), "-Z": (440, 355, 504, 418.5), "+Y": (308, 289, 372, 353)},
    "LeftLowerArm": {"+X": (374, 418.5, 438, 467), "-X": (506, 418.5, 570, 467), "+Z": (308, 418.5, 372, 467), "-Z": (440, 418.5, 504, 467)},
    "LeftHand": {"+X": (374, 467, 438, 483), "-X": (506, 467, 570, 483), "+Z": (308, 467, 372, 483), "-Z": (440, 467, 504, 483), "-Y": (308, 485, 372, 549)},
    "RightUpperLeg": {"+X": (19, 355, 83, 418.5), "-X": (151, 355, 215, 418.5), "+Z": (217, 355, 281, 418.5), "-Z": (85, 355, 149, 418.5), "+Y": (217, 289, 281, 353)},
    "RightLowerLeg": {"+X": (19, 418.5, 83, 467), "-X": (151, 418.5, 215, 467), "+Z": (217, 418.5, 281, 467), "-Z": (85, 418.5, 149, 467)},
    "RightFoot": {"+X": (19, 467, 83, 483), "-X": (151, 467, 215, 483), "+Z": (217, 467, 281, 483), "-Z": (85, 467, 149, 483), "-Y": (217, 485, 281, 549)},
    "LeftUpperLeg": {"+X": (374, 355, 438, 418.5), "-X": (506, 355, 570, 418.5), "+Z": (308, 355, 372, 418.5), "-Z": (440, 355, 504, 418.5), "+Y": (308, 289, 372, 353)},
    "LeftLowerLeg": {"+X": (374, 418.5, 438, 467), "-X": (506, 418.5, 570, 467), "+Z": (308, 418.5, 372, 467), "-Z": (440, 418.5, 504, 467)},
    "LeftFoot": {"+X": (374, 467, 438, 483), "-X": (506, 467, 570, 483), "+Z": (308, 467, 372, 483), "-Z": (440, 467, 504, 483), "-Y": (308, 485, 372, 549)},
    "UpperTorso": {"+X": (361, 74, 425, 170), "-X": (165, 74, 229, 170), "+Z": (231, 74, 359, 170), "-Z": (427, 74, 555, 170), "+Y": (231, 8, 359, 72)},
    "LowerTorso": {"+X": (361, 170, 425, 202), "-X": (165, 170, 229, 202), "+Z": (231, 170, 359, 202), "-Z": (427, 170, 555, 202), "-Y": (231, 204, 359, 268)},
}

# which template layers each part receives
PART_LAYERS = {
    "UpperTorso": ("pants", "shirt"), "LowerTorso": ("pants", "shirt"),
    "RightUpperArm": ("shirt",), "RightLowerArm": ("shirt",), "RightHand": ("shirt",),
    "LeftUpperArm": ("shirt",), "LeftLowerArm": ("shirt",), "LeftHand": ("shirt",),
    "RightUpperLeg": ("pants",), "RightLowerLeg": ("pants",), "RightFoot": ("pants",),
    "LeftUpperLeg": ("pants",), "LeftLowerLeg": ("pants",), "LeftFoot": ("pants",),
}

# label ids of the render label pass when the compositor supplies no label canvas
LABEL_NONE, LABEL_SKIN, LABEL_MODESTY, LABEL_PANTS, LABEL_SHIRT = 0, 1, 2, 3, 4

# object ids of the ID pass
OBJECT_IDS: dict[str, int] = {
    "Head": 1, "UpperTorso": 2, "LowerTorso": 3, "RightUpperArm": 4, "RightLowerArm": 5, "RightHand": 6, "LeftUpperArm": 7,
    "LeftLowerArm": 8, "LeftHand": 9, "RightUpperLeg": 10, "RightLowerLeg": 11, "RightFoot": 12, "LeftUpperLeg": 13,
    "LeftLowerLeg": 14, "LeftFoot": 15, "hair": 40,
}
ACCESSORY_ID_BASE = 50
STICKER_ID_BASE = 70


def object_id(name: str) -> int:
    """ID-pass value of a mesh name: body parts, ``hair``, ``acc.<n>`` (50+n) and ``sticker.<n>`` (70+n)."""
    if name in OBJECT_IDS:
        return OBJECT_IDS[name]
    for prefix, base in (("acc.", ACCESSORY_ID_BASE), ("sticker.", STICKER_ID_BASE)):
        if name.startswith(prefix):
            return base + int(name[len(prefix):].split(".")[0])
    raise KeyError(f"no object id for {name!r}")


@dataclass
class BodyPart:
    name: str
    centre: tuple[float, float, float]
    size: tuple[float, float, float]

    @property
    def lo(self) -> np.ndarray:
        return np.asarray(self.centre) - np.asarray(self.size) / 2

    @property
    def hi(self) -> np.ndarray:
        return np.asarray(self.centre) + np.asarray(self.size) / 2


@dataclass
class Mannequin:
    """Blocky body: parts (axis-aligned boxes) and attachment points, in studs, Y up, front +Z."""

    parts: list[BodyPart]
    attachments: dict[str, tuple[float, float, float]]
    name: str = "blocky_r15_code"
    source: str = "builtin code mannequin (attachment offsets approximate BlockyCharacter, FAILURE_MODES X26; confirm with T7)"
    meta: dict[str, Any] = field(default_factory=dict)

    def part(self, name: str) -> BodyPart:
        for p in self.parts:
            if p.name == name:
                return p
        raise KeyError(name)

    @property
    def head(self) -> BodyPart:
        return self.part("Head")

    def attachment(self, name: str) -> np.ndarray:
        from duoskin.roblox.limits import normalise_attachment

        key = normalise_attachment(name)
        if key in self.attachments:
            return np.asarray(self.attachments[key], float)
        raise KeyError(f"mannequin has no attachment {name!r}")

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        lo = np.min([p.lo for p in self.parts], axis=0)
        hi = np.max([p.hi for p in self.parts], axis=0)
        return lo, hi

    def to_json(self) -> dict[str, Any]:
        return {"schema": "duoskin.mannequin/1", "name": self.name, "source": self.source, "units": "studs", "frame": "y_up_z_front",
                "parts": [{"name": p.name, "centre": list(p.centre), "size": list(p.size)} for p in self.parts],
                "attachments": {k: list(v) for k, v in self.attachments.items()}, "meta": self.meta}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Mannequin:
        parts = [BodyPart(p["name"], tuple(p["centre"]), tuple(p["size"])) for p in data["parts"]]
        return cls(parts, {k: tuple(v) for k, v in data["attachments"].items()}, data.get("name", "mannequin"), data.get("source", "json"),
                   data.get("meta", {}))

    @classmethod
    def load(cls, path: str | Path) -> Mannequin:
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))


def default_mannequin() -> Mannequin:
    """The code-built blocky R15-like mannequin (5.2 studs tall, 4 studs wide including the arms)."""
    px = 64.0
    upper_arm, lower_arm, hand = 63.5 / px, 48.5 / px, 16.0 / px      # 0.992, 0.758, 0.25 (template rows 355/418.5/467/483)
    parts: list[BodyPart] = []
    y = 0.0
    for side, x in (("Right", -0.5), ("Left", 0.5)):          # legs: the character's right is -X
        yy = 0.0
        for nm, h in (("Foot", hand), ("LowerLeg", lower_arm), ("UpperLeg", upper_arm)):
            parts.append(BodyPart(f"{side}{nm}", (x, yy + h / 2, 0.0), (1.0, h, 1.0)))
            yy += h
    y = 2.0
    parts.append(BodyPart("LowerTorso", (0.0, y + 0.25, 0.0), (2.0, 0.5, 1.0)))
    parts.append(BodyPart("UpperTorso", (0.0, y + 0.5 + 0.75, 0.0), (2.0, 1.5, 1.0)))
    for side, x in (("Right", -1.5), ("Left", 1.5)):
        yy = 4.0
        for nm, h in (("UpperArm", upper_arm), ("LowerArm", lower_arm), ("Hand", hand)):
            parts.append(BodyPart(f"{side}{nm}", (x, yy - h / 2, 0.0), (1.0, h, 1.0)))
            yy -= h
    parts.append(BodyPart("Head", (0.0, 4.0 + 0.6, 0.0), (1.2, 1.2, 1.2)))
    att = {
        "HatAttachment": (0.0, 5.193, 0.0), "HairAttachment": (0.0, 5.193, 0.0),
        "FaceFrontAttachment": (0.0, 4.586, 0.6), "FaceCenterAttachment": (0.0, 4.6, 0.0),
        "NeckAttachment": (0.0, 3.718, 0.0),
        "RightCollarAttachment": (-0.784, 3.718, 0.0), "LeftCollarAttachment": (0.784, 3.718, 0.0),
        "RightShoulderAttachment": (-1.5, 3.571, 0.0), "LeftShoulderAttachment": (1.5, 3.571, 0.0),
        "BodyFrontAttachment": (0.0, 3.25, 0.513), "BodyBackAttachment": (0.0, 3.25, -0.513),
        "WaistFrontAttachment": (0.0, 2.25, 0.513), "WaistCenterAttachment": (0.0, 2.25, 0.0), "WaistBackAttachment": (0.0, 2.25, -0.513),
    }
    return Mannequin(parts, att, meta={"height_studs": 5.2, "px_per_stud_template": px})


def load_mannequin(path: str | Path | None) -> Mannequin:
    """``mannequin_blocky.json`` when a path is given and exists, else the code-built default."""
    if path:
        p = Path(path)
        if p.is_file():
            return Mannequin.load(p)
    return default_mannequin()


# --------------------------------------------------------------------------------------------------------------------
# dressing
# --------------------------------------------------------------------------------------------------------------------
def _face_corners(face: str, c: np.ndarray, s: np.ndarray) -> list[tuple[float, float, float]]:
    """Four corners (counter-clockwise seen from outside) ordered to match UV corners (x0,y1) (x1,y1) (x1,y0) (x0,y0)
    for the side faces and the template's own orientation for UP/DOWN faces (APP_SPEC template map notes)."""
    x, y, z = (s / 2).tolist()
    cx, cy, cz = c.tolist()
    table = {
        "+Z": [(-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)],
        "-Z": [(x, -y, -z), (-x, -y, -z), (-x, y, -z), (x, y, -z)],
        "+X": [(x, -y, z), (x, -y, -z), (x, y, -z), (x, y, z)],
        "-X": [(-x, -y, -z), (-x, -y, z), (-x, y, z), (-x, y, -z)],
        "+Y": [(-x, y, z), (x, y, z), (x, y, -z), (-x, y, -z)],      # front edge at the image bottom
        "-Y": [(-x, -y, -z), (x, -y, -z), (x, -y, z), (-x, -y, z)],  # front edge at the image top
    }
    return [(cx + a, cy + b, cz + d) for a, b, d in table[face]]


def part_mesh(part: BodyPart, rects: dict[str, tuple[float, float, float, float]], texture: np.ndarray | None, *, color=(200, 160, 130),
              label_map: np.ndarray | None = None, object_id_: int = 1, tex_size=TEMPLATE_SIZE) -> RenderMesh:
    """A box whose faces sample the given template rectangles (faces without a rectangle use the part's flat colour)."""
    w, h = tex_size
    c, s = np.asarray(part.centre, float), np.asarray(part.size, float)
    verts, faces, uvs = [], [], []
    for face in ("+Z", "-Z", "+X", "-X", "+Y", "-Y"):
        corners = _face_corners(face, c, s)
        rect = rects.get(face)
        if rect is None:
            uv = [(0.0, 0.0)] * 4
        else:
            x0, y0, x1, y1 = rect
            uv = [(x0 / w, y1 / h), (x1 / w, y1 / h), (x1 / w, y0 / h), (x0 / w, y0 / h)]
        base = len(verts)
        verts += corners
        uvs += uv
        faces += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    return RenderMesh(part.name, np.array(verts), np.array(faces), np.array(uvs), texture, color, object_id_, label_map)


def _alpha_over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    a = top[..., 3:4].astype(np.float32) / 255.0
    out = base.astype(np.float32).copy()
    out[..., :3] = top[..., :3] * a + out[..., :3] * (1 - a)
    out[..., 3] = np.maximum(base[..., 3], top[..., 3])
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _as_canvas(img: Any, name: str) -> np.ndarray | None:
    if img is None:
        return None
    if isinstance(img, (str, Path)):
        with Image.open(img) as im:
            im.load()
            img = im.copy()
    if isinstance(img, Image.Image):
        arr = np.asarray(img.convert("RGBA"))
    else:
        arr = np.asarray(img)
        if arr.ndim == 3 and arr.shape[2] == 3:
            arr = np.concatenate([arr, np.full(arr.shape[:2] + (1,), 255, np.uint8)], axis=2)
    if arr.shape[1] != TEMPLATE_SIZE[0] or arr.shape[0] != TEMPLATE_SIZE[1]:
        raise ValueError(f"{name} must be a {TEMPLATE_SIZE[0]}x{TEMPLATE_SIZE[1]} classic template, got {arr.shape[1]}x{arr.shape[0]}")
    return arr.astype(np.uint8)


def layer_canvas(layers: tuple[str, ...], body_rgb: tuple[int, int, int], shirt: np.ndarray | None, pants: np.ndarray | None,
                 modesty: np.ndarray | None = None) -> np.ndarray:
    """Body colour -> modesty -> Pants -> Shirt, straight alpha-over, on a 585x559 canvas."""
    w, h = TEMPLATE_SIZE
    canvas = np.zeros((h, w, 4), np.uint8)
    canvas[..., :3] = body_rgb
    canvas[..., 3] = 255
    if modesty is not None:
        canvas = _alpha_over(canvas, modesty)
    if "pants" in layers and pants is not None:
        canvas = _alpha_over(canvas, pants)
    if "shirt" in layers and shirt is not None:
        canvas = _alpha_over(canvas, shirt)
    return canvas


def layer_labels(layers: tuple[str, ...], shirt: np.ndarray | None, pants: np.ndarray | None, shirt_labels: np.ndarray | None,
                 pants_labels: np.ndarray | None, modesty: np.ndarray | None = None) -> np.ndarray:
    """Per-texel label canvas: the compositor's label of the top-most opaque layer, else modesty or skin."""
    w, h = TEMPLATE_SIZE
    lab = np.full((h, w), LABEL_SKIN, np.uint16)
    if modesty is not None:
        lab[modesty[..., 3] > 127] = LABEL_MODESTY
    if "pants" in layers and pants is not None:
        m = pants[..., 3] > 127
        lab[m] = pants_labels[m] if pants_labels is not None else LABEL_PANTS
    if "shirt" in layers and shirt is not None:
        m = shirt[..., 3] > 127
        lab[m] = shirt_labels[m] if shirt_labels is not None else LABEL_SHIRT
    return lab


def head_mesh(mannequin: Mannequin, skin_rgb: tuple[int, int, int], overlay: Image.Image | None = None) -> RenderMesh:
    """The cube head in the skin colour; ``overlay`` (RGBA, e.g. the face canvas) is alpha-composited on the front face."""
    head = mannequin.head
    cell = 128
    cols, rows = 4, 3
    tex = np.zeros((cell * rows, cell * cols, 3), np.uint8)
    tex[...] = skin_rgb
    cells = {"+Z": (1, 1), "-Z": (3, 1), "+X": (2, 1), "-X": (0, 1), "+Y": (1, 0), "-Y": (1, 2)}
    if overlay is not None:
        ov = overlay.convert("RGBA").resize((cell, cell), Image.Resampling.LANCZOS)
        cx, cy = cells["+Z"]
        region = Image.fromarray(tex[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell]).convert("RGBA")
        region = Image.alpha_composite(region, ov).convert("RGB")
        tex[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell] = np.asarray(region)
    rects = {f: (cx * cell, cy * cell, (cx + 1) * cell, (cy + 1) * cell) for f, (cx, cy) in cells.items()}
    return part_mesh(head, rects, tex, color=skin_rgb, object_id_=OBJECT_IDS["Head"], tex_size=(cell * cols, cell * rows))


def dress(mannequin: Mannequin | None = None, *, shirt: Any = None, pants: Any = None, skin_rgb: tuple[int, int, int] = (226, 178, 140),
          body_rgb: tuple[int, int, int] | None = None, modesty: Any = None, head_overlay: Image.Image | None = None,
          shirt_labels: np.ndarray | None = None, pants_labels: np.ndarray | None = None) -> list[RenderMesh]:
    """The mannequin as ``RenderMesh``es dressed with a Shirt and Pants (585x559 RGBA templates) through the real UV regions.

    ``body_rgb`` defaults to ``skin_rgb`` (the layer under the clothes). Label canvases are optional uint arrays of the same
    size as the templates, as produced by the compositor.
    """
    mq = mannequin or default_mannequin()
    shirt_a, pants_a, mod_a = _as_canvas(shirt, "shirt"), _as_canvas(pants, "pants"), _as_canvas(modesty, "modesty")
    body = body_rgb or skin_rgb
    meshes: list[RenderMesh] = [head_mesh(mq, skin_rgb, head_overlay)]
    cache: dict[tuple[str, ...], tuple[np.ndarray, np.ndarray]] = {}
    for p in mq.parts:
        if p.name == "Head":
            continue
        layers = PART_LAYERS[p.name]
        if layers not in cache:
            cache[layers] = (layer_canvas(layers, body, shirt_a, pants_a, mod_a),
                             layer_labels(layers, shirt_a, pants_a, shirt_labels, pants_labels, mod_a))
        tex, lab = cache[layers]
        meshes.append(part_mesh(p, TEMPLATE_PART_FACES[p.name], tex, color=skin_rgb, label_map=lab, object_id_=OBJECT_IDS[p.name]))
    return meshes


def place_mesh(mannequin: Mannequin, mesh: MeshData, attachment: str, *, name: str, attachment_offset: Any = None,
               smooth: bool = True) -> RenderMesh:
    """A Handle-space ``MeshData`` positioned on the mannequin: the attachment point of the mesh meets the mannequin's attachment."""
    off = np.asarray(attachment_offset if attachment_offset is not None else mesh.meta.get("attachment_offset", [0.0, 0.0, 0.0]), float)
    pos = mannequin.attachment(attachment)
    v = mesh.vertices - off + pos
    tex = None
    if mesh.texture is not None:
        tex = np.asarray(mesh.texture if isinstance(mesh.texture, Image.Image) else Image.fromarray(np.asarray(mesh.texture)))
        if tex.ndim == 3 and tex.shape[2] == 4:
            tex = tex[..., :3]
    return RenderMesh(name, v, mesh.faces, mesh.uv, tex, (200, 200, 200), object_id(name), None, True, smooth)
