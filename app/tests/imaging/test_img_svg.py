"""imaging/svg.py: sanitizer, colour normaliser, seam closing, two-pass matte, security (SYS-03, IMG-14)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np
import pytest
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.imaging import svg as S

NS = 'xmlns="http://www.w3.org/2000/svg"'
PAL = ["#1f8a8a", "#22335c", "#f2735e", "#000000"]


def svg(body: str, attrs: str = 'viewBox="0 0 100 100"') -> str:
    return f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" {attrs}>{body}</svg>'


GOOD = svg('<style>.a{fill:#1F8A8A} .b{stroke:#F2735E;stroke-width:6}</style><rect width="100" height="100" fill="#00FF00"/>'
           '<g fill="rgb(34,51,92)"><circle class="a" cx="50" cy="50" r="30"/><path d="M10 90 L90 90 L50 60 Z"/></g>'
           '<path class="b" d="M10 10 L90 40" fill="none"/><polygon points="5,5 20,5 5,20"/>', 'viewBox="0 0 100 100" width="100%"')


# ---------------------------------------------------------------- the sanitizer rejects every banned thing
@pytest.mark.parametrize("name,body,rule", [
    ("script", "<script>alert(1)</script>", "element:script"),
    ("foreignObject", "<foreignObject><div/></foreignObject>", "element:foreignObject"),
    ("image", '<image xlink:href="/etc/passwd"/>', "element:image"),
    ("image-no-href", '<image width="5" height="5"/>', "element:image"),
    ("text", "<text>hi</text>", "element:text"),
    ("filter", '<filter id="f"/><rect width="5" height="5" filter="url(#f)"/>', "element:filter"),
    ("mask", '<mask id="m"/>', "element:mask"),
    ("pattern", '<pattern id="p"/>', "element:pattern"),
    ("linearGradient", '<linearGradient id="g"/>', "element:linearGradient"),
    ("radialGradient", '<radialGradient id="g"/>', "element:radialGradient"),
    ("unknown element", "<animate/>", "element:animate"),
    ("external use", '<use href="http://evil.example/x.svg#a"/>', "href"),
    ("file use", '<use xlink:href="file:///etc/passwd#a"/>', "href"),
    ("a href", '<a href="http://x"><rect width="5" height="5"/></a>', "element:a"),
    ("on attribute", '<rect width="5" height="5" onclick="x()"/>', "event"),
    ("opacity", '<rect width="5" height="5" opacity="0.5"/>', "opacity"),
    ("fill-opacity", '<rect width="5" height="5" fill-opacity="0.2"/>', "opacity"),
    ("stroke-opacity css", '<style>.q{stroke-opacity:.4}</style><rect class="q" width="5" height="5"/>', "opacity"),
    ("inherited opacity", '<g opacity="0.9"><rect width="5" height="5"/></g>', "opacity"),
    ("rgba alpha", '<rect width="5" height="5" fill="rgba(0,0,0,0.3)"/>', "opacity"),
    ("gradient fill", '<rect width="5" height="5" fill="url(#g)"/>', "paint-server"),
    ("css url", '<style>.q{fill:url(#g)}</style><rect class="q" width="5" height="5"/>', "css"),
    ("css import", "<style>@import url(http://x/y.css);</style>", "css"),
    ("style attr url", '<rect width="5" height="5" style="fill:url(#g)"/>', "style"),
    ("javascript url", '<rect width="5" height="5" id="javascript:alert(1)"/>', "script"),
    ("marker", '<path d="M0 0 L5 5" marker-end="url(#m)"/>', "attr:marker-end"),
    ("bad colour", '<rect width="5" height="5" fill="notacolour"/>', "colour"),
])
def test_sanitizer_rejects(name, body, rule):
    with pytest.raises(S.SvgRejected) as ei:
        S.sanitize_and_normalize(svg(body))
    assert ei.value.rule == rule, name


def test_percent_size_without_viewbox_and_with_viewbox():
    with pytest.raises(S.SvgRejected) as ei:
        S.sanitize_and_normalize(svg('<path d="M0 0 L5 5"/>', 'width="100%" height="100%"'))
    assert ei.value.rule == "viewbox"
    S.sanitize_and_normalize(svg('<path d="M0 0 L5 5"/>', 'viewBox="0 0 10 10" width="100%"'))          # fine: the viewBox gives the geometry
    root = S.sanitize_and_normalize(svg('<path d="M0 0 L5 5"/>', 'width="64" height="32"'))
    with pytest.raises(S.SvgRejected):
        S.prepare(S.sanitize_and_normalize(svg('<path d="M0 0"/>', 'width="5mm" height="5mm"')), 8, 8)     # units that are not plain numbers
    assert root.get("viewBox") is None and S.prepare(root, 64, 32) > 0 and root.get("viewBox") == "0 0 64.0 32.0"


def test_dtd_entities_and_garbage_are_rejected_by_defusedxml():
    bomb = '<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><svg xmlns="http://www.w3.org/2000/svg"/>'
    with pytest.raises(S.SvgRejected) as ei:
        S.sanitize_and_normalize(bomb)
    assert ei.value.rule == "xml"
    xxe = '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(xxe)
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize("<svg><path")
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize("<html/>")
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(svg("<g/>") + " " * 2_100_000)


def test_path_count_limit():
    n = int(TH.get("svg.max_paths"))
    ok = svg("".join('<rect width="1" height="1" fill="#000"/>' for _ in range(n)))
    assert S.path_count(S.sanitize_and_normalize(ok)) == n
    with pytest.raises(S.SvgRejected) as ei:
        S.sanitize_and_normalize(svg("".join('<rect width="1" height="1"/>' for _ in range(n + 1))))
    assert ei.value.rule == "paths"
    r = S.check_svg(svg("".join('<rect width="1" height="1"/>' for _ in range(n + 1))))
    assert not r.passed and r.check_id == "A_SVG" and r.kind == "hard"
    assert S.check_svg(GOOD).passed


# ---------------------------------------------------------------- normalisation
def _shapes(root):
    return [e for e in root.iter() if S.local(e.tag) in S.SHAPES]


def test_missing_fill_is_black_and_everything_becomes_explicit():
    root = S.sanitize_and_normalize(svg('<path d="M0 0 L9 0 L9 9 Z"/><path d="M0 0 L9 9" stroke="#fff"/>'))
    p1, p2 = _shapes(root)
    assert (p1.get("fill"), p1.get("stroke")) == ("#000000", "none")                      # the SVG default fill is black
    assert (p2.get("fill"), p2.get("stroke")) == ("#000000", "#ffffff")


def test_classes_style_attr_inherited_fill_current_color_and_rgb():
    root = S.sanitize_and_normalize(svg(
        '<style>.a{fill:#1F8A8A} path{stroke:#F2735E} #x{fill:#22335C}</style>'
        '<g fill="rgb(34,51,92)" color="#112233">'
        '<circle class="a" r="4"/><rect id="x" width="3" height="3" fill="none"/><path d="M0 0 L5 5" fill="currentColor"/>'
        '<ellipse rx="2" ry="3" style="fill: hsl(120, 100%, 25%); stroke: red"/></g><polyline points="0,0 5,5" fill="none"/>'))
    c, r, p, e, pl = _shapes(root)
    assert c.get("fill") == "#1f8a8a"                          # class beats the inherited group fill
    assert r.get("fill") == "#22335c"                          # an id selector beats the attribute
    assert (p.get("fill"), p.get("stroke")) == ("#112233", "#f2735e")                    # currentColor via color; tag selector stroke
    assert e.get("fill") == "#008000" and e.get("stroke") == "#ff0000" and e.get("style") is None
    assert pl.get("fill") == "none"
    assert all(s.get("class") is None for s in _shapes(root))


def test_style_elements_title_and_metadata_are_removed_and_namespace_survives():
    root = S.sanitize_and_normalize(svg('<title>x</title><metadata/><style>.a{fill:#fff}</style><rect class="a" width="2" height="2"/>'))
    assert [S.local(e.tag) for e in root.iter()] == ["svg", "rect"]
    out = ET.tostring(root, encoding="unicode")
    assert "ns0:" not in out and out.startswith("<svg xmlns=")                           # registered namespaces: no ns0 prefixes
    assert 'xmlns:xlink' not in out or "ns1:" not in out


def test_use_is_inlined_with_translate_and_defs_are_cleaned():
    root = S.sanitize_and_normalize(svg('<defs><circle id="c" r="10" fill="#ff0000"/></defs><use xlink:href="#c" x="30" y="30"/><use href="#c" x="70" y="70"/>'))
    out = ET.tostring(root, encoding="unicode")
    assert "<use" not in out and "href" not in out and out.count("<circle") == 2
    assert 'translate(30.0 30.0)' in out and 'translate(70.0 70.0)' in out
    assert S.path_count(root) == 2


def test_use_cycles_and_dangling_refs_are_rejected():
    cyc = svg('<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></defs><use href="#a"/>')
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(cyc)
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(svg('<use href="#nowhere"/>'))
    bomb = svg('<defs><g id="l0"><rect width="1" height="1"/></g>' + "".join(
        f'<g id="l{i}">' + "".join(f'<use href="#l{i - 1}"/>' for _ in range(6)) + "</g>" for i in range(1, 8)) + '</defs><use href="#l7"/>')
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(bomb)                                                  # exponential <use> growth is capped


def test_clip_path_internal_only():
    ok = svg('<defs><clipPath id="c"><rect width="5" height="5"/></clipPath></defs><rect width="9" height="9" fill="#000" clip-path="url(#c)"/>')
    root = S.sanitize_and_normalize(ok)
    assert S.path_count(root) == 1                                                       # clip shapes do not draw
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(svg('<rect width="9" height="9" clip-path="url(http://x/y#c)"/>'))
    with pytest.raises(S.SvgRejected):
        S.sanitize_and_normalize(svg('<rect width="9" height="9" clip-path="url(#missing)"/>'))


# ---------------------------------------------------------------- snapping
def test_snap_colours_snaps_and_rejects_far_fills():
    root = S.sanitize_and_normalize(svg('<rect width="5" height="5" fill="#1f8b8b"/><rect width="5" height="5" fill="#f3745f"/>'))
    mapping = S.snap_colours(root, PAL)
    assert mapping == {"#1f8b8b": "#1f8a8a", "#f3745f": "#f2735e"}
    assert S.explicit_colours(root) == {"#1f8a8a", "#f2735e"}
    bad = S.sanitize_and_normalize(svg('<rect width="5" height="5" fill="#00ff7f"/>'))
    with pytest.raises(S.SvgRejected) as ei:
        S.snap_colours(bad, PAL)
    assert ei.value.rule == "off-palette"
    assert S.snap_colours(S.sanitize_and_normalize(svg('<rect width="5" height="5" fill="#00ff7f"/>')), PAL, max_de=200)


# ---------------------------------------------------------------- rendering
def test_render_sizes_never_stretch_and_preserve_aspect():
    wide = S.sanitize_and_normalize(svg('<rect width="200" height="100" fill="#1f8a8a"/>', 'viewBox="0 0 200 100"'))
    img = S.render_part(wide, 64, 64)
    assert img.size == (64, 64)
    a = np.asarray(img)[..., 3]
    assert a[16:48].min() == 255 and a[:14].max() == 0 and a[50:].max() == 0             # meet-fitted, centred, not stretched
    tall = S.render_part(wide, 40, 80)
    assert tall.size == (40, 80)


def test_seam_closing_gives_interior_alpha_255_on_diagonal_shared_edges():
    body = ('<path d="M0 0 L100 100 L0 100 Z" fill="#1f8a8a"/><path d="M0 0 L100 0 L100 100 Z" fill="#22335c"/>')
    root = S.sanitize_and_normalize(svg(body))
    img = S.render_part(root, 96, 96)
    assert S.interior_alpha_min(img) == 255                                              # no see-through seam along the diagonal
    root2 = S.sanitize_and_normalize(svg(body))
    S.prepare(root2, 96, 96)
    raw = S._render(root2, (384, 384)).resize((96, 96), Image.Resampling.BOX).convert("RGBA")       # without closing: seam remains
    assert S.interior_alpha_min(raw) < 255


def test_two_pass_matte_edge_error_is_within_6_over_255_and_has_no_sentinel_spill():
    shape = '<polygon points="20,80 50,15 80,80" fill="#f2735e"/>'
    with_bg = S.sanitize_and_normalize(svg('<rect width="100" height="100" fill="#00ff00"/>' + shape))
    direct = np.asarray(S.render_part(S.sanitize_and_normalize(svg(shape)), 96, 96)).astype(float)       # reference: no background at all
    matte = np.asarray(S.render_part(with_bg, 96, 96, sentinel="#00ff00", ss=4)).astype(float)
    assert np.abs(matte[..., 3] - direct[..., 3]).max() <= float(TH.get("svg.matte_edge_err_max"))        # spec: edge error <= 6/255
    weight = matte[..., 3:4] / 255.0
    assert np.abs((matte[..., :3] - np.array([242, 115, 94])) * weight).max() <= float(TH.get("svg.matte_edge_err_max"))   # alpha-weighted colour error
    solid = matte[..., 3] >= 128
    assert np.abs(matte[..., :3][solid] - np.array([242, 115, 94])).max() <= float(TH.get("svg.matte_edge_err_max"))
    assert (matte[..., 1][matte[..., 3] >= 32] <= 130).all()                                             # zero green spill at the edges
    assert matte[0, 0, 3] == 0 and matte[48, 48, 3] == 255
    edge = (matte[..., 3] > 0) & (matte[..., 3] < 255)
    assert edge.sum() > 50                                                                               # the test really has anti-aliased edges
    naive = np.asarray(S.render_flat(with_bg, 96, 96)).astype(float)                                     # a pixel key on the flat render spills
    edge_n = (naive[..., 1] > 130) & (naive[..., 0] < 200) & (naive[..., 1] < 240)
    assert edge_n.sum() > 0


def test_matte_without_sentinel_is_a_plain_render():
    root = S.sanitize_and_normalize(svg('<circle cx="50" cy="50" r="40" fill="#1f8a8a"/>'))
    img = S.render_part(root, 64, 64)
    assert np.asarray(img)[0, 0, 3] == 0 and np.asarray(img)[32, 32, 3] == 255


def test_border_sentinel_check():
    root = S.sanitize_and_normalize(svg('<rect width="100" height="100" fill="#00ff00"/><circle cx="50" cy="50" r="30" fill="#1f8a8a"/>'))
    flat = S.render_flat(root, 64, 64)
    assert S.border_sentinel_share(flat, "#00ff00") == 1.0
    assert S.check_sentinel_border(flat, "#00ff00").passed
    off = S.sanitize_and_normalize(svg('<rect width="100" height="50" fill="#00ff00"/><circle cx="50" cy="50" r="30" fill="#1f8a8a"/>'))
    r = S.check_sentinel_border(S.render_flat(off, 64, 64), "#00ff00")
    assert not r.passed and r.check_id == "A_SENTINEL" and r.value < 0.95


def test_svg_to_part_end_to_end():
    part = S.svg_to_part(GOOD, PAL, (128, 128), sentinel="#00ff00")
    assert part.path_count == 5 and part.border_share == 1.0 and part.seam_alpha_min == 255
    assert set(part.colours) <= {"#000000", "#00ff00", "#1f8a8a", "#22335c", "#f2735e"}
    a = np.asarray(part.image)
    assert a[0, 0, 3] == 0 and tuple(a[64, 64]) == (31, 138, 138, 255)
    assert part.image.size == (128, 128)
    with pytest.raises(S.SvgRejected):
        S.svg_to_part(svg('<image href="/etc/passwd"/>'), PAL, (16, 16))


# ---------------------------------------------------------------- security: resvg must never read a local file
def test_render_always_uses_an_empty_resources_dir_and_skips_system_fonts(monkeypatch):
    import resvg_py

    seen = {}
    real = resvg_py.svg_to_bytes

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)
    S.render_part(S.sanitize_and_normalize(svg('<rect width="9" height="9" fill="#1f8a8a"/>')), 16, 16)
    assert seen["skip_system_fonts"] is True and seen["resources_dir"] == S.EMPTY_DIR
    import os

    assert os.path.isdir(S.EMPTY_DIR) and os.listdir(S.EMPTY_DIR) == []
    assert "svg_path" not in seen                                                         # the SVG string is passed, never a path


def test_a_local_file_reference_never_reaches_the_renderer(tmp_path, monkeypatch):
    secret = tmp_path / "secret.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(secret)
    hostile = svg(f'<image xlink:href="{secret.as_posix()}" width="100" height="100"/>')
    called = []
    import resvg_py

    monkeypatch.setattr(resvg_py, "svg_to_bytes", lambda **kw: called.append(kw) or b"")
    with pytest.raises(S.SvgRejected):
        S.svg_to_part(hostile, PAL, (16, 16))
    assert not called                                                                     # rejected before any render call
