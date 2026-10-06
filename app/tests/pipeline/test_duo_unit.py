"""The duo lane's pure parts: the 3D interpenetration measure (CHK-D04), the render names of the ID pass, the Gate 3 tile banners."""
from __future__ import annotations

import numpy as np
import pytest
from pfix import make_project

from duoskin.mesh import boolean
from duoskin.mesh.types import MeshData
from duoskin.pipeline import duo, parts
from duoskin.render import avatar


def box(lo, hi) -> MeshData:
    m = boolean.box_mesh(np.array(lo, float), np.array(hi, float), uv=(0.5, 0.5))
    m.meta = {"attachment_offset": [0.0, 0.0, 0.0]}
    return m


def info(placed):
    return {"mannequin": avatar.default_mannequin(), "placed": placed}


def test_an_item_outside_the_body_has_no_interpenetration():
    out = box((-0.3, 0.0, 0.6), (0.3, 0.6, 1.0))                  # in front of the face, clear of every body box
    share, who = duo.interpenetration(info({"a.acc.0": (out, "HairAttachment")}))
    assert share == 0.0 and who == ""


def test_an_item_buried_in_the_body_is_measured_and_named():
    inside = box((-0.3, -0.9, -0.3), (0.3, -0.3, 0.3))             # inside the head cube (the attachment frame is the HairAttachment point)
    clean = box((-0.3, 0.0, 0.6), (0.3, 0.6, 1.0))
    share, who = duo.interpenetration(info({"a.acc.0": (clean, "HairAttachment"), "b.acc.0": (inside, "HairAttachment")}))
    assert who == "b.acc.0" and share > 0.5


def test_accessories_are_named_for_the_render_id_pass():
    assert avatar.object_id("acc.0") == avatar.ACCESSORY_ID_BASE and avatar.object_id("sticker.1") == avatar.STICKER_ID_BASE + 1
    with pytest.raises(KeyError):
        avatar.object_id("a_acc_0")                                  # the name the first version used: the ID pass has no such object


def test_the_gate_3_tile_says_what_is_mock_and_what_is_free_plan(rt):
    p, rec = make_project(rt)
    parts.ensure_parts(rt, p.id, rec.spec)
    duo.put_state(rt, p.id, mock=True, clone_mode="degraded", similarity={"on": False}, warnings=[], blocking=[])
    rt.repo.mutate_part(p.id, "a.acc.0", lambda x: setattr(x, "license", "tripo_free_public_ccby_noncommercial"))
    tile = duo.build_tile(rt, p.id)
    assert tile.tile_id == duo.CANDIDATE
    text = " | ".join(tile.badges)
    assert "DEMO" in text and "clone check degraded" in text and "Reference-similarity check is off" in text
    assert "a.acc.0" in text and "FREE plan" in text
    assert tile.facts["blocking"] == [] and tile.facts["ip_unsure"] is False
