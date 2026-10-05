"""The demo kits (APP_SPEC §16): code-made stand-ins for the user's missing kits, used in demo mode and by the e2e tests.

``build_demo_kits(dest)`` writes ``builtin_kits/demo/``:

* ``hair/<style_id>/``: three code-made hair styles (``hair_short_crop_01``, ``hair_bob_03``, ``hair_spiky_05``: the ids of the
  fixture specs' demo hair kit) with ``style.json`` (the §10.7.1 contract: ``prompt_phrase``, ``length_class``, ``silhouette_class``,
  ``symmetric``, ``parting``, ``supported_adjustments``, ``attach_frame``, ``origin=code_generated``), ``mesh.glb`` (one mesh, one
  material, one UV set, in ``HairAttachment`` space, studs, +Z front, <= 3000 triangles), ``bands.png`` (the fixed grey band values
  64, 128, 192) and ``views/{front,left,back,right}.png``;
* ``hair/modules/<id>/``: two fringe and two back modules in the same layout;
* ``hair/pair_iou.json``: the front/side silhouette IoU between the styles (the plan lint's kit-hair pair rule);
* ``fabrics/demo_weave/``: a demo fabric tile (``tile.png``, ``fabric.json``).

Every entry is marked ``origin = code_generated`` and ``license = n/a``; demo kits never enter the registries. Run
``python -m duoskin.pipeline.demo_kits`` to regenerate the shipped files (a test checks that they match this generator).
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image

from duoskin.mesh import boolean
from duoskin.mesh.types import MeshData

BAND_VALUES = (64, 128, 192)                       # shadow, base, highlight (kit convention, mesh.hair.BAND_EDGES)
BAND_U = (1 / 6, 3 / 6, 5 / 6)                     # the u coordinate of each band in bands.png (3 equal stripes)
HEAD_TOP, HEAD_HALF = 0.007, 0.6                    # the head cube in the HairAttachment frame: y in [-1.193, 0.007], x and z in +-0.6
ORIGIN = {"origin": "code_generated", "license": "n/a"}


def _bands_png() -> Image.Image:
    arr = np.zeros((32, 96, 3), np.uint8)
    for i, v in enumerate(BAND_VALUES):
        arr[:, i * 32:(i + 1) * 32] = v
    return Image.fromarray(arr, "RGB")


def _box(lo: tuple[float, float, float], hi: tuple[float, float, float], band: int) -> MeshData:
    return boolean.box_mesh(np.array(lo), np.array(hi), uv=(BAND_U[band], 0.5))


def _union(parts: list[MeshData]) -> MeshData:
    mesh = boolean.union_all(parts)
    mesh.texture = _bands_png()
    mesh.meta = {"attachment_offset": [0.0, 0.0, 0.0], "attachment": "HairAttachment", "asset_type": "Hair"}
    return mesh


def _cap(front_y: float = -0.2) -> list[MeshData]:
    """The common cap: top and sides over the crown; the front only reaches down to ``front_y`` (the bangs line)."""
    h = HEAD_HALF + 0.08
    return [_box((-h, front_y, -h), (h, HEAD_TOP + 0.14, h), 1),                          # crown + front line
            _box((-h, -0.55, -h), (h, front_y + 0.05, -HEAD_HALF + 0.02), 0)]            # back of the head, in shadow


def style_crop() -> MeshData:
    return _union(_cap(-0.18) + [_box((-0.3, HEAD_TOP + 0.1, 0.1), (0.3, HEAD_TOP + 0.24, 0.5), 2)])


def style_bob() -> MeshData:
    h = HEAD_HALF + 0.08
    return _union(_cap(-0.15) + [_box((-h, -1.05, -h), (-HEAD_HALF + 0.0, -0.15, 0.2), 0), _box((HEAD_HALF - 0.0, -1.05, -h), (h, -0.15, 0.2), 0),
                                 _box((-h, -1.05, -h), (h, -0.5, -HEAD_HALF + 0.02), 0)])


def style_spiky() -> MeshData:
    parts = _cap(-0.2)
    for i, (x, height) in enumerate(((-0.42, 0.34), (-0.21, 0.5), (0.0, 0.62), (0.21, 0.5), (0.42, 0.34))):
        parts.append(_box((x - 0.07, HEAD_TOP + 0.1, -0.12), (x + 0.07, HEAD_TOP + 0.1 + height, 0.16), 2 if i % 2 else 1))
    return _union(parts)


def module_fringe(drop: float, band: int = 1) -> MeshData:
    return _union([_box((-0.62, -0.2 - drop, 0.55), (0.62, 0.06, 0.68), band)])


def module_back(drop: float, band: int = 0) -> MeshData:
    return _union([_box((-0.62, -0.2 - drop, -0.68), (0.62, 0.06, -0.52), band)])


STYLES: dict[str, dict] = {
    "hair_short_crop_01": {"build": style_crop, "prompt_phrase": "short tousled crop with a tapered back", "length_class": "short",
                           "silhouette_class": "close", "clump_k": 6, "parting": "left", "default_fringe": "fringe_a", "symmetric": False,
                           "supported_adjustments": ["volume", "fringe_length", "part_side"]},
    "hair_bob_03": {"build": style_bob, "prompt_phrase": "chin-length bob with blunt ends", "length_class": "medium",
                    "silhouette_class": "close", "clump_k": 5, "parting": "centre", "default_fringe": "fringe_b", "symmetric": True,
                    "supported_adjustments": ["fringe_length", "side_length"]},
    "hair_spiky_05": {"build": style_spiky, "prompt_phrase": "tall swept-up spiky style", "length_class": "short",
                      "silhouette_class": "spiky", "clump_k": 7, "parting": "none", "default_fringe": "none", "symmetric": True,
                      "supported_adjustments": ["volume", "clump_size"]},
}
MODULES: dict[str, dict] = {
    "fringe_a": {"kind": "fringe", "prompt_phrase": "side-swept fringe", "build": lambda: module_fringe(0.12)},
    "fringe_b": {"kind": "fringe", "prompt_phrase": "straight blunt fringe", "build": lambda: module_fringe(0.18)},
    "back_a": {"kind": "back", "prompt_phrase": "long tapered back", "build": lambda: module_back(0.7)},
    "back_b": {"kind": "back", "prompt_phrase": "short tidy back", "build": lambda: module_back(0.3)},
}


def _write_style_folder(folder: Path, mesh: MeshData, meta: dict, views: bool = True) -> dict[str, np.ndarray]:
    from duoskin.mesh import export
    from duoskin.render import sheets

    folder.mkdir(parents=True, exist_ok=True)
    export.write_glb(mesh, folder / "mesh.glb", extras={"attachment": "HairAttachment", "asset_type": "Hair", "attachment_offset": [0.0, 0.0, 0.0]})
    _bands_png().save(folder / "bands.png")
    masks: dict[str, np.ndarray] = {}
    if views:
        vdir = folder / "views"
        vdir.mkdir(exist_ok=True)
        rendered = sheets.render_mesh_views(mesh, ("front", "left", "back", "right"), size=256, bg=(242, 242, 242))
        for name, im in rendered.items():
            im.save(vdir / f"{name}.png")
            arr = np.asarray(im.convert("RGB")).astype(int)
            masks[name] = np.abs(arr - np.array((242, 242, 242))).max(axis=2) > 12
    (folder / "style.json").write_text(json.dumps({**meta, "tris": mesh.n_tris, **ORIGIN}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return masks


def build_demo_kits(dest: Path | str) -> list[str]:
    """Write the demo kits under ``dest`` (normally ``duoskin/builtin_kits/demo``). Deterministic. Returns the written style ids."""
    root = Path(dest)
    masks: dict[str, dict[str, np.ndarray]] = {}
    for sid, d in STYLES.items():
        meta = {k: v for k, v in d.items() if k != "build"}
        meta.update({"id": sid, "attach_frame": "HairAttachment"})
        masks[sid] = _write_style_folder(root / "hair" / sid, d["build"](), meta)
    for mid, d in MODULES.items():
        meta = {k: v for k, v in d.items() if k != "build"}
        meta.update({"id": mid, "attach_frame": "HairAttachment"})
        _write_style_folder(root / "hair" / "modules" / mid, d["build"](), meta, views=False)
    iou: dict[str, dict[str, float]] = {}
    for a, b in combinations(sorted(masks), 2):
        def _iou(view: str) -> float:
            ma, mb = masks[a][view], masks[b][view]
            union = float((ma | mb).sum())
            return round(float((ma & mb).sum()) / union, 3) if union else 0.0

        iou[f"{a}|{b}"] = {"front": _iou("front"), "side": _iou("left")}
    (root / "hair" / "pair_iou.json").write_text(json.dumps(iou, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _write_fabric(root / "fabrics" / "demo_weave")
    return sorted(STYLES)


def _write_fabric(folder: Path) -> None:
    from duoskin.imaging import fabric as FAB

    folder.mkdir(parents=True, exist_ok=True)
    tile = FAB.generate_tile("canvas", 256, 777, 12)
    Image.fromarray(tile).save(folder / "tile.png")
    meta = {"id": "demo_weave", "material_family": "canvas", "prompt_phrase": "plain demo canvas weave", "pattern_phrase": "plain weave",
            "px_per_repeat": 4.0, "repeats": 12, "amplitude_dL": 4.0, "weave_k": 12, **ORIGIN}
    (folder / "fabric.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    dest = Path(__file__).resolve().parent.parent / "builtin_kits" / "demo"
    ids = build_demo_kits(dest)
    print("wrote demo kits:", ", ".join(ids), "->", dest)


if __name__ == "__main__":
    main()
