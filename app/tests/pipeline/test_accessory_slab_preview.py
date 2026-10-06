"""The preview sheet of a sticker-slab or primitive accessory tile (``acc.board``).

``sheets.mesh_judge_sheet`` defaults to the six judge views (front, left, back, right, top, three-quarter); the accessory tile renders four, so the
sheet must be asked for those four. It was not, and every duo with a hair-clip or sticker slab ended with "missing views: right, top" at the board
step of that accessory (found by clicking the whole app through, tests/e2e_ui).
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from duoskin.imaging import slab_art
from duoskin.mesh import slab as SL
from duoskin.pipeline import accessory
from duoskin.render import sheets


def test_the_four_view_preview_sheet_of_a_slab_renders():
    art = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(art).ellipse((40, 40, 216, 216), fill=(200, 80, 90, 255))
    badge = slab_art.build_badge(art, border_px=8, border_hex="#ffffff")
    mesh = SL.build_slab(badge.image, size_studs=1.0, thickness=0.1, tris_budget=2800, kind="sticker_slab").mesh
    views = sheets.render_mesh_views(mesh, accessory.PREVIEW_VIEWS, size=96)
    sheet = sheets.mesh_judge_sheet(views, order=accessory.PREVIEW_VIEWS, cols=4)
    assert sheet.width > sheet.height > 0 and len(accessory.PREVIEW_VIEWS) == 4


def test_the_default_judge_sheet_still_needs_all_six_views():
    import pytest

    with pytest.raises(ValueError, match="missing views"):
        sheets.mesh_judge_sheet({v: Image.new("RGB", (8, 8)) for v in accessory.PREVIEW_VIEWS})
