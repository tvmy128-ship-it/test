"""The mock planner and the mock pictures, as the demo (mock) flow needs them (found by clicking the whole app through, tests/e2e_ui).

In demo mode every duo comes from the mock planner, so each colour it picks must survive the checks the real pipeline runs later:

* a mock picture is drawn from the colour NAMES of the prompt, so a palette colour must be a dictionary colour (the compiler names a colour by
  its nearest dictionary name, the mock paints that name back as exactly that colour) or the Gate A palette check fails and the accessory
  or print ends at "needs your help";
* the two characters' main colours must stay far apart in every family (the concept check A_LEAK: partner-only colours);
* the face lines must be at least dE 20 from all five preview skin tones and must not match the pupil, or every face of every duo fails FACE-06.
"""
from __future__ import annotations

import io
import json
import random

import pytest
from PIL import Image
from test_mock_roles import inv, plan  # noqa: F401  (the fixture and the helper of the planner tests)

from duoskin.imaging import checks as image_checks
from duoskin.imaging import colournames
from duoskin.imaging import face_canvas as FC
from duoskin.imaging.palette import de2000_hex
from duoskin.pipeline import face as face_lane
from duoskin.providers.base import CallCtx
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock import roles as R
from duoskin.providers.mock.images import MockImages
from duoskin.providers.openai_images import ImageRequest


def hexes(palette):
    return {p["role"]: p["hex"] for p in palette}


@pytest.mark.parametrize("family", sorted(R.FAMILY_HUES))
def test_every_palette_family_is_made_of_dictionary_colours_and_keeps_the_pair_apart(family):
    pal = R._palette(family, [], random.Random(1))
    dictionary = {h.upper() for h in colournames.load_names().values()}
    assert {p["hex"] for p in pal} <= dictionary, "a palette colour that is not a dictionary colour cannot be drawn back by name"
    assert len({p["name"] for p in pal}) == len(pal), "every colour has its own name"
    h = hexes(pal)
    assert de2000_hex(h["a_main"], h["b_main"]) >= 20, f"{family}: the two main colours are too close (the concept check reads one as the other's leak)"


def test_a_colour_name_of_a_prompt_is_drawn_as_exactly_that_dictionary_colour():
    assert D.colors_from_text("a light blue hoodie") == [(140, 196, 240)]         # not "blue" (50, 90, 200)
    assert D.colors_from_text("hot pink star on a teal bag, white background")[:2] == [(232, 69, 143), (31, 138, 138)]
    assert D.colors_from_text("coral") == [(242, 115, 94)]
    assert D.colors_from_text("no colour words here") == []
    assert D.colors_from_text("cream") and D.colors_from_text("maroon")           # a word only the built-in list knows keeps its built-in colour


def test_a_palette_colour_survives_the_round_trip_name_and_back():
    for family in R.FAMILY_HUES:
        for p in R._palette(family, [], random.Random(2)):
            name = colournames.colour_name(p["hex"])
            (back,) = D.colors_from_text(name)[:1]
            assert de2000_hex("#{:02X}{:02X}{:02X}".format(*back), p["hex"]) < 1.0, (family, p, name)


@pytest.mark.timeout(300)
def test_the_faces_of_the_mock_planner_pass_the_face_checks_on_every_skin_tone(inv):  # noqa: F811
    families = ["monochrome_accent", "warm_pastel", "neon_night"]
    out = plan(seed=5, palette_suggestion=json.dumps(families))
    assert {s["world"]["palette_family"] for s in out["specs"]} == set(families)
    canvas = FC.load_canvas()
    for spec in out["specs"]:
        for c in "ab":
            fspec = face_lane.face_spec(spec, c)
            parts = FC.FaceParts(sources={})
            for name in face_lane.AI_PARTS:
                setattr(parts, face_lane.PART_ATTR[name], face_lane.code_part(fspec, canvas, name))
            comp = FC.compose_face(fspec, parts, canvas)
            for check in (FC.check_line_skin, FC.check_lid_covers, FC.check_mouth_interior, FC.check_lash_lid_split):
                r = check(comp)
                assert r.passed, f"{spec['world']['palette_family']} character {c}: {r.check_id}: {r.evidence}"


@pytest.mark.parametrize("colour_name", ["grey", "charcoal", "pale sky blue", "chartreuse", "maroon", "pearl", "black", "teal", "dawn pink"])
def test_a_mock_part_picture_is_one_flat_colour_of_the_prompt_so_the_palette_check_passes(colour_name):
    """The first colour named in the prompt, drawn flat with no outline or highlight: an outline or highlight is a colour that is not in the duo's
    palette (A_PALETTE rejected every mock accessory whose planned colours were not dark enough to contain the darker outline)."""
    hexed = colournames.load_names()[colour_name]
    req = ImageRequest(model="gpt-image-2.5-flare-2026-09-08", prompt=f"A small bag in {colour_name}, matte, on a plain background.", size="1024x1024",
                       quality="low", background="transparent", n=1)
    png = MockImages().generate(req, CallCtx.null()).images[0]
    result = image_checks.check_palette(Image.open(io.BytesIO(png)), [hexed])
    assert result.passed, f"{colour_name} {hexed}: {result.evidence}"
