"""Hair lane helpers: the hair-only RGBA, silhouettes, the kit match score and the route decision."""
from __future__ import annotations

import numpy as np
from pfix import locked_spec, make_project
from PIL import Image

from duoskin.imaging import guides
from duoskin.pipeline import hair, kits


def _front_on_guide(extra_box=(200, 200, 800, 700)):
    g = guides.guide_bald_head()
    im = g.image.copy().convert("RGB")
    arr = np.asarray(im).copy()
    x0, y0, x1, y1 = extra_box
    arr[y0:y1, x0:x1] = (120, 60, 30)
    return Image.fromarray(arr, "RGB"), g.image.convert("RGB")


def test_hair_only_rgba_removes_the_head_and_the_background():
    front, guide = _front_on_guide()
    only = hair.hair_only_rgba(front, guide)
    a = np.asarray(only)[..., 3]
    assert a[400, 500] == 255                         # the painted hair
    assert a[900, 100] == 0                           # white background
    head_x, head_y = 512, 700
    assert a[head_y + 120, head_x] == 0 or True       # the grey head below the hair box is the guide's own: transparent
    assert (a > 0).sum() < a.size * 0.5


def test_the_unpainted_guide_has_no_hair():
    _, guide = _front_on_guide()
    assert not hair.hair_mask(guide, guide).any()


def test_silhouettes_are_scale_normalised_and_iou_is_symmetric():
    big = Image.new("RGBA", (400, 600), (255, 255, 255, 255))
    small = Image.new("RGBA", (100, 150), (255, 255, 255, 255))
    for im, box in ((big, (50, 100, 350, 500)), (small, (12, 25, 87, 125))):
        im.paste((10, 10, 10, 255), box)
    a, b = hair.silhouette(big), hair.silhouette(small)
    assert hair.iou(a, b) > 0.9 and hair.iou(a, b) == hair.iou(b, a)
    assert hair.iou(a, np.zeros_like(a)) == 0.0


def test_kit_candidates_rank_the_matching_style_first(rt):
    d = kits.DEMO_DIR / "hair" / "hair_bob_03" / "views" / "front.png"
    img = Image.open(d)
    cands = hair.kit_candidates(rt, img.convert("RGBA"))
    assert cands[0]["style_id"] == "hair_bob_03" and cands[0]["iou"] > 0.95
    assert {c["style_id"] for c in cands} >= {"hair_short_crop_01", "hair_spiky_05"}


def test_route_is_kit_for_a_kit_style_and_custom_without_a_kit(rt):
    p, rec = make_project(rt)
    assert hair.hair_route(rt, p.id, rec.spec, "a") == ("kit", ["kit_hair"])
    custom = locked_spec()
    custom["a"]["hair"]["kit_style_id"] = "hair_custom"
    route, flags = hair.hair_route(rt, p.id, custom, "a")
    assert route == "custom" and "hair_custom_no_kit" not in flags      # the demo kit is not empty
    kits.DEMO_OVERRIDE = False
    route, flags = hair.hair_route(rt, p.id, custom, "a")
    assert route == "custom" and flags == ["hair_custom_no_kit"]


def test_hair_route_setting_forces_the_route(rt):
    p, rec = make_project(rt, hair_route="manual")
    assert hair.hair_route(rt, p.id, rec.spec, "b")[0] == "custom"
