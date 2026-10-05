"""The face lane without the scheduler: code-drawn parts, the assembled face checks on both fixture specs, the finalize rule for empty layers."""
from __future__ import annotations

import pytest
from pfix import locked_spec

from duoskin.imaging import face_canvas as FC
from duoskin.pipeline import common, face

HARD_KINDS = ("hard", "assert")


def composite(spec: dict, character: str):
    pal = common.palette_map(spec)
    fspec = FC.FaceSpec.from_spec(spec[character]["face"], pal)
    canvas = FC.load_canvas()
    fp = FC.FaceParts(sources={})
    for name in face.AI_PARTS:
        setattr(fp, face.PART_ATTR[name], face.code_part(fspec, canvas, name))
    return fspec, FC.compose_face(fspec, fp, canvas), pal


@pytest.mark.parametrize("name", ["spec_complement_gb", "spec_empty_bb"])
def test_code_drawn_faces_pass_every_hard_face_check_on_the_fixture_specs(name):
    spec = locked_spec(name)
    for c, other in (("a", "b"), ("b", "a")):
        fspec, comp, pal = composite(spec, c)
        results = FC.face_check_suite(comp, hair_hexes=face.hair_hexes(spec, c), head_base_present=False,
                                      other_face=FC.FaceSpec.from_spec(spec[other]["face"], pal), subject_sha="t")
        failed = [(r.check_id, r.evidence) for r in results if r.kind in HARD_KINDS and not r.passed]
        assert not failed, (name, c, failed)


def test_the_layer_pack_has_every_code_layer_and_the_five_parts():
    _, comp, _ = composite(locked_spec(), "a")
    pack = comp.layer_pack()
    assert set(pack) >= {"sclera", "iris", "lash", "brow", "mouth_closed", "mouth_open", "shading", "highlights", "closed_lid"}
    assert all(im.mode == "RGBA" and im.size == (comp.canvas.size, comp.canvas.size) for im in pack.values())


@pytest.mark.parametrize("part", face.AI_PARTS)
def test_every_code_part_is_a_transparent_png_with_paint(part):
    spec = locked_spec()
    fspec = FC.FaceSpec.from_spec(spec["a"]["face"], common.palette_map(spec))
    im = face.code_part(fspec, FC.load_canvas(), part)
    assert im.mode == "RGBA" and im.getchannel("A").getextrema() == (0, 255)


def test_a_new_ink_colour_redraws_the_line_parts_without_ai():
    spec = locked_spec()
    other = locked_spec()
    for c in other["palette"]:
        if c["id"] == "p6":
            c["hex"] = "#1F3D2B"
    a = face.part_colours(FC.FaceSpec.from_spec(spec["a"]["face"], common.palette_map(spec)), "brow")
    b = face.part_colours(FC.FaceSpec.from_spec(other["a"]["face"], common.palette_map(other)), "brow")
    assert a == ["#2d1b69"] and b == ["#1f3d2b"]


def test_only_the_layers_that_must_hold_paint_fail_the_finalize_rule():
    assert set(face.REQUIRED_LAYERS) == {"sclera", "iris", "lash", "brow", "mouth_closed", "mouth_open"}
    assert not {"blush", "nose", "lower_ticks", "shading", "highlights", "closed_lid"} & set(face.REQUIRED_LAYERS)
