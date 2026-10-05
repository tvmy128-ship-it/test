"""mesh/primitives.py: closed, outward, UV-mapped, palette atlas."""
from __future__ import annotations

import numpy as np
import pytest

from duoskin.mesh import geometry as geo
from duoskin.mesh import primitives as prim
from duoskin.mesh import texture as tx
from duoskin.mesh.types import MeshError


@pytest.mark.parametrize("kind", prim.KINDS)
def test_every_kind_is_watertight_outward_uv_mapped_and_textured(kind):
    m = prim.build(kind, {}, (200, 100, 50))
    w = geo.weld(m.vertices, m.faces)
    st = geo.topology_stats(w.faces)
    assert st["watertight"] and st["winding_consistent"], kind
    assert geo.signed_volume(w.vertices, w.faces) > 0
    assert m.uv.min() >= 0 and m.uv.max() <= 1
    assert m.texture.size == (1024, 1024) and m.texture.mode == "RGB"
    assert not tx.is_flat(m.texture)                         # a gradient inside every cell: never one flat colour
    assert tx.min_alpha(m.texture) == 255
    assert m.n_tris < 3800
    assert m.meta["primitive_kind"] == kind


def test_uv_islands_do_not_overlap_between_parts():
    m = prim.build("charm", {}, (240, 160, 180))
    assert len(m.meta["atlas"]["cells"]) == 2
    # the bead (first part) owns the left atlas cell, the ring (second part) the right one
    bead_faces = m.faces[: m.meta["atlas"]["cells"][0]["faces"]]
    ring_faces = m.faces[len(bead_faces):]
    bead_u = m.uv[np.unique(bead_faces), 0]
    ring_u = m.uv[np.unique(ring_faces), 0]
    assert bead_u.max() < 0.5 <= ring_u.min() + 1e-9


def test_dimensions_follow_parameters_in_studs():
    assert np.allclose(prim.build("box", {"size": (2, 1, 0.5)}, (1, 2, 3)).extents, [2, 1, 0.5])
    ring = prim.build("ring", {"diameter": 1.6, "tube": 0.2}, (255, 255, 255))
    assert abs(ring.extents[0] - 1.6) < 0.02 and abs(ring.extents[2] - 0.2) < 0.02       # lies in XY, faces the front
    loop = prim.build("loop", {"width": 1.4, "height": 0.9, "tube": 0.16}, (200, 200, 200))
    assert abs(loop.extents[0] - 1.4) < 0.02 and abs(loop.extents[1] - 0.9) < 0.02
    cyl = prim.build("cylinder", {"diameter": 1.0, "height": 2.0, "axis": "z"}, (9, 9, 9))
    assert abs(cyl.extents[2] - 2.0) < 1e-6 and abs(cyl.extents[0] - 1.0) < 0.05
    strap = prim.build("strap", {"length": 3.0, "width": 0.5, "thickness": 0.1, "bend": 0.0}, (90, 60, 30))
    assert abs(strap.extents[0] - 3.0) < 1e-6 and abs(strap.extents[1] - 0.5) < 1e-6 and abs(strap.extents[2] - 0.1) < 1e-6


def test_palette_dict_and_derived_tones():
    pal = prim.derive_palette((200, 50, 50))
    assert set(pal) == {"base", "shade", "accent", "trim"}
    assert pal["shade"] != pal["base"] and pal["trim"] != pal["accent"]
    m = prim.build("bead", {}, {"base": (10, 200, 10)})
    assert m.meta["palette"]["base"] == [10, 200, 10]
    assert m.meta["palette"]["shade"] != [10, 200, 10]


def test_recolour_changes_the_atlas_only():
    m = prim.build("charm", {}, (200, 40, 40))
    r = prim.recolour(m, (40, 40, 200))
    assert np.array_equal(m.vertices, r.vertices) and np.array_equal(m.uv, r.uv) and np.array_equal(m.faces, r.faces)
    a, b = np.asarray(m.texture, float).mean(axis=(0, 1)), np.asarray(r.texture, float).mean(axis=(0, 1))
    assert a[0] > a[2] and b[2] > b[0]
    with pytest.raises(MeshError):
        prim.recolour(__import__("duoskin.mesh.fixtures", fromlist=["x"]).f_fixture(), (1, 2, 3))


def test_black_and_white_palettes_are_still_not_flat():
    for rgb in ((0, 0, 0), (255, 255, 255)):
        assert not tx.is_flat(prim.build("sphere", {}, rgb).texture)


def test_unknown_kind_and_too_many_parts_are_clean_errors():
    with pytest.raises(MeshError) as e:
        prim.build("torpedo")
    assert e.value.code == "unknown_primitive"
    with pytest.raises(MeshError):
        a = prim.Atlas(256, 1, 1)
        a.alloc("base")
        a.alloc("base")


def test_attach_loop_keeps_the_mesh_watertight_with_its_uvs():
    base = prim.build("sphere", {"diameter": 1.2}, (230, 120, 160))
    out = prim.attach_loop(base, at=(0.0, 0.62, 0.0), width=0.5, height=0.45, tube=0.09)
    w = geo.weld(out.vertices, out.faces)
    st = geo.topology_stats(w.faces)
    assert st["watertight"] and out.meta["loop_attached"]
    assert out.texture.size[1] > out.texture.size[0]                 # the swatch strip was added at the bottom
    assert out.uv.min() >= 0 and out.uv.max() <= 1
    assert geo.shell_labels(w.faces, len(w.vertices))[1] == 1        # one fused shell
    assert out.extents[1] > base.extents[1]
