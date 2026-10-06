"""SVG sanitizer, colour normaliser and renderer (APP_SPEC §10.4.2, PROMPT_BIBLE Appendix A, FAILURE_MODES IMG-14 / SYS-03).

Why this is a security module: ``resvg`` reads local files by absolute path even without ``resources_dir``, so any ``<image>`` or
``href`` in an SVG from a provider is a file-disclosure risk. The sanitizer therefore **whitelists** elements and rejects every
reference to an external resource; an internal ``<use href="#id">`` is inlined instead. Rendering always passes an empty
``resources_dir`` and ``skip_system_fonts=True``.

Pipeline: ``sanitize_and_normalize`` (defusedxml; missing fill => black; classes, ``style=""``, inherited ``<g fill>``, ``currentColor``
resolved) -> ``snap_colours`` (CIEDE2000; a large fill farther than 15 from the palette is rejected) -> path-count check (<= 300)
-> border pre-check (>= 95% sentinel) -> ``close_seams`` (0.5 output px same-colour stroke) -> 4x render through the root
``width``/``height`` -> BOX downsample in premultiplied alpha -> two-pass matte (sentinel to black; sentinel black / rest white;
``alpha = M/255``, ``colour = C/alpha``). A pixel chroma key is never used.
"""
from __future__ import annotations

import atexit
import copy
import io
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring
from PIL import Image, ImageColor

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import palette as P

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

MAX_SVG_BYTES = 2_000_000
MAX_ELEMENTS = 5000
MAX_DEPTH = 64
MAX_USE_DEPTH = 8
MAX_PATH_SEGMENTS = 50_000        # path commands + polygon points in the whole file: 160 000 segments cost resvg 13 s of CPU
_SEGMENT = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_STYLE_BAD_FUNCS = ("expression(", "@import", "javascript:", "image(", "image-set(", "element(", "src(", "cross-fade(", "paint(")
_CLIP_URL = re.compile(r"url\(\s*#[\w.-]+\s*\)")

SHAPES = {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line"}
CONTAINERS = {"svg", "g", "defs", "symbol", "clipPath"}
INERT = {"title", "desc", "metadata", "style", "use"}
ALLOWED = SHAPES | CONTAINERS | INERT
# Everything the spec names explicitly. Anything else not in ALLOWED is rejected too ("unsupported element").
BANNED = {"script", "foreignObject", "image", "text", "tspan", "textPath", "use", "filter", "mask", "pattern", "linearGradient",
          "radialGradient"}
PAINT_PROPS = ("fill", "stroke", "color", "opacity", "fill-opacity", "stroke-opacity")
OPACITY_PROPS = ("opacity", "fill-opacity", "stroke-opacity")
REJECT_ATTRS = {"filter", "mask", "marker-start", "marker-mid", "marker-end", "mix-blend-mode", "enable-background"}
EMPTY_DIR = tempfile.mkdtemp(prefix="resvg_empty_")   # resources_dir that contains nothing (SYS-03)
atexit.register(shutil.rmtree, EMPTY_DIR, ignore_errors=True)   # one empty folder per start would pile up in %TEMP%


class SvgRejected(ValueError):
    """The SVG was rejected by the sanitizer. ``rule`` is a short machine-readable code (A_SVG evidence)."""

    def __init__(self, rule: str, message: str):
        super().__init__(f"{rule}: {message}")
        self.rule = rule


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# ------------------------------------------------------------------ CSS and paint helpers
def _parse_declarations(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip().lower()
            if k:
                out[k] = v.strip()
    return out


def _strip_css_comments(css: str) -> str:
    """Remove ``/* ... */`` in one pass. (The regex ``/\\*.*?\\*/`` is quadratic on many unterminated ``/*``: a hostile 2 MB style sheet froze the whole
    process, because ``re`` never lets go of the GIL.) An unterminated comment is left as it is, like the regex did."""
    out: list[str] = []
    pos = 0
    while True:
        start = css.find("/*", pos)
        if start < 0:
            break
        end = css.find("*/", start + 2)
        if end < 0:
            break
        out.append(css[pos:start])
        pos = end + 2
    out.append(css[pos:])
    return "".join(out)


def _css_rules(css: str) -> list[tuple[str, str]]:
    """``(selector, body)`` pairs of ``selector { body }`` in linear time. (``([^{}]+)\\{([^}]*)\\}`` through ``re.findall`` is quadratic on a style
    sheet without braces: 40 KB took 17 s.) A selector never contains ``{`` or ``}``; a body runs to the next ``}``."""
    rules: list[tuple[str, str]] = []
    pos = 0
    while True:
        open_ = css.find("{", pos)
        if open_ < 0:
            break
        close = css.find("}", open_ + 1)
        if close < 0:
            break
        selector = css[pos:open_].rsplit("}", 1)[-1]
        if selector:
            rules.append((selector, css[open_ + 1:close]))
        pos = close + 1
    return rules


def _parse_css(root: ET.Element) -> dict[str, dict[str, str]]:
    """Rules for ``.class``, ``#id``, ``tag`` and ``*`` selectors (comma lists allowed). At-rules and ``url(`` are rejected."""
    rules: dict[str, dict[str, str]] = {}
    for st in (e for e in root.iter() if local(e.tag) == "style"):
        css = _strip_css_comments(st.text or "")
        if "@" in css or "\\" in css or "url(" in css.lower() or any(t in css.lower() for t in _STYLE_BAD_FUNCS):
            raise SvgRejected("css", "at-rules, escapes, url() and script or image functions in <style> are not allowed")
        for sel, body in _css_rules(css):
            decl = _parse_declarations(body)
            for one in sel.split(","):
                one = one.strip()
                if re.fullmatch(r"[.#]?[\w-]+|\*", one):
                    rules.setdefault(one, {}).update(decl)
                else:
                    raise SvgRejected("css", f"unsupported selector {one!r}")
    return rules


def _opacity_value(v: str) -> float:
    v = v.strip()
    try:
        return float(v[:-1]) / 100.0 if v.endswith("%") else float(v)
    except ValueError:
        raise SvgRejected("opacity", f"unreadable opacity {v!r}") from None


def _to_hex(v: str) -> str:
    """Any CSS colour to ``#rrggbb`` (alpha below 1 is rejected); ``none``/``transparent`` stay ``none``."""
    s = v.strip()
    low = s.lower()
    if low in ("none", "transparent"):
        return "none"
    if low.startswith("url("):
        raise SvgRejected("paint-server", "gradients and patterns are not allowed")
    m = re.fullmatch(r"rgba?\(([^)]*)\)", low)
    if m and "," in m.group(1):
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) == 4 and _opacity_value(parts[3]) < 1:
            raise SvgRejected("opacity", "colour alpha below 1")
    m8 = re.fullmatch(r"#([0-9a-f]{8})", low)
    if m8 and int(m8.group(1)[6:], 16) < 255:
        raise SvgRejected("opacity", "colour alpha below 1")
    try:
        rgb = ImageColor.getrgb(s)
    except ValueError:
        raise SvgRejected("colour", f"unsupported colour {v!r}") from None
    if len(rgb) == 4 and rgb[3] < 255:
        raise SvgRejected("opacity", "colour alpha below 1")
    return "#{:02x}{:02x}{:02x}".format(*tuple(rgb[:3]))


def _num(v: str) -> float:
    try:
        return float(str(v).strip().replace("px", ""))
    except ValueError:
        raise SvgRejected("viewbox", f"size {v!r} is not a plain number (no % or units)") from None


# ------------------------------------------------------------------ sanitizer
def _check_style_attr(value: str) -> None:
    """A ``style=""`` attribute is checked declaration by declaration: no ``url()`` except ``clip-path:url(#id)``, none of the banned
    properties (filter, mask, blend modes, markers), no CSS escape (``\\75rl(`` would hide a ``url(``), no comment, no script or image function.
    Whatever passes is harmless to resvg (it cannot name a file or a host)."""
    if "\\" in value or "/*" in value:
        raise SvgRejected("style", "CSS escapes and comments are not allowed in a style attribute")
    for prop, val in _parse_declarations(value).items():
        if prop in REJECT_ATTRS:
            raise SvgRejected(f"attr:{prop}", f"{prop} is not allowed")
        low = val.lower()
        if "url(" in low and not (prop == "clip-path" and _CLIP_URL.fullmatch(val.strip())):
            raise SvgRejected("style", "url() in a style attribute (only clip-path:url(#id) is allowed)")
        if any(t in low for t in _STYLE_BAD_FUNCS):
            raise SvgRejected("style", "script or image function in a style attribute")


def _check_tree(root: ET.Element) -> None:
    n = 0

    def walk(el: ET.Element, depth: int) -> None:
        nonlocal n
        n += 1
        if n > MAX_ELEMENTS:
            raise SvgRejected("size", f"more than {MAX_ELEMENTS} elements")
        if depth > MAX_DEPTH:
            raise SvgRejected("size", "nesting too deep")
        name = local(el.tag)
        if name in BANNED and name != "use":
            raise SvgRejected(f"element:{name}", f"<{name}> is not allowed")
        if name not in ALLOWED:
            raise SvgRejected(f"element:{name}", f"unsupported element <{name}>")
        for k, v in el.attrib.items():
            lk = local(k).lower() if isinstance(k, str) else ""
            if lk.startswith("on"):
                raise SvgRejected("event", f"event attribute {local(k)}")
            if lk.endswith("href") and name != "use":
                raise SvgRejected("href", f"{local(k)} is not allowed")
            if lk in REJECT_ATTRS:
                raise SvgRejected(f"attr:{lk}", f"{lk} is not allowed")
            if lk == "style":
                _check_style_attr(v)
            if isinstance(v, str) and "javascript:" in v.lower():
                raise SvgRejected("script", "javascript: URL")
            if lk == "d":
                segments[0] += len(_SEGMENT.findall(v))
            elif lk == "points" and name in ("polygon", "polyline"):
                segments[0] += len(_NUMBER.findall(v)) // 2
            if segments[0] > MAX_PATH_SEGMENTS:
                raise SvgRejected("size", f"more than {MAX_PATH_SEGMENTS} path segments")
        for ch in el:
            walk(ch, depth + 1)

    segments = [0]
    walk(root, 0)


def _inline_uses(root: ET.Element) -> None:
    """Replace every internal ``<use href="#id">`` by a copy of its target (external hrefs are rejected).

    Only uses outside ``<defs>`` / ``<symbol>`` are expanded in place; each copied target is expanded recursively, so a cycle hits the depth
    limit and an exponential chain of uses hits the element cap before it can eat memory.
    """
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    produced = [sum(1 for _ in root.iter())]

    def target_of(use: ET.Element) -> ET.Element:
        href = None
        for k, v in use.attrib.items():
            if local(k) == "href":
                href = v
        if href is None or not href.startswith("#") or href[1:] not in ids:
            raise SvgRejected("href", f"<use> must reference an element in this file, got {href!r}")
        return ids[href[1:]]

    def expand(parent: ET.Element, depth: int) -> None:
        if depth > MAX_USE_DEPTH:
            raise SvgRejected("use", "<use> nesting is too deep (cycle?)")
        for i, ch in enumerate(list(parent)):
            name = local(ch.tag)
            if name == "use":
                tgt = target_of(ch)
                g = ET.Element(f"{{{SVG_NS}}}g")
                x, y = ch.get("x", "0"), ch.get("y", "0")
                tr = f"translate({_num(x)} {_num(y)})"
                if ch.get("transform"):
                    tr = f"{ch.get('transform')} {tr}"
                g.set("transform", tr)
                for a in ("fill", "stroke", "color", "opacity", "fill-opacity", "stroke-opacity", "style", "class"):
                    if ch.get(a) is not None:
                        g.set(a, ch.get(a))
                body = copy.deepcopy(tgt)
                produced[0] += sum(1 for _ in body.iter())
                if produced[0] > MAX_ELEMENTS:
                    raise SvgRejected("size", f"more than {MAX_ELEMENTS} elements after inlining <use>")
                if local(body.tag) == "symbol":
                    for c in list(body):
                        g.append(c)
                else:
                    body.attrib.pop("id", None)
                    g.append(body)
                parent[i] = g
                expand(g, depth + 1)
            elif name not in ("defs", "symbol") or depth > 0:
                expand(ch, depth)

    expand(root, 0)


def _clip_ids(root: ET.Element) -> set[str]:
    return {e.get("id", "") for e in root.iter() if local(e.tag) == "clipPath" and e.get("id")}


def sanitize_and_normalize(svg: str) -> ET.Element:
    """Parse with defusedxml, reject anything unsafe or unsupported, then make fill/stroke explicit on every shape.

    Rejects: ``<script>``, ``<foreignObject>``, ``<image>``, ``<text>``, ``<filter>``, ``<mask>``, ``<pattern>``, gradients, every
    ``href`` (an internal ``<use href="#id">`` is inlined), ``on*`` attributes, ``opacity`` / ``fill-opacity`` / ``stroke-opacity``
    below 1, ``width="100%"`` without a viewBox, and more than ``svg.max_paths`` (300) shapes. A missing ``fill`` becomes black
    (the SVG default); ``currentColor`` resolves through ``color``; ``<style>`` classes, ``style=""`` and inherited ``<g fill>`` are
    pushed down to each shape.
    """
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise SvgRejected("size", "SVG larger than 2 MB")
    try:
        root = safe_fromstring(svg, forbid_dtd=True)
    except DefusedXmlException as e:
        raise SvgRejected("xml", f"entities, DTDs or external references are not allowed ({type(e).__name__})") from None
    except ET.ParseError as e:
        raise SvgRejected("xml", f"not well-formed XML: {e}") from None
    if local(root.tag) != "svg":
        raise SvgRejected("root", "the root element must be <svg>")
    _check_tree(root)
    if root.get("viewBox") is None:
        for dim in ("width", "height"):
            if "%" in str(root.get(dim, "")):
                raise SvgRejected("viewbox", f'{dim}="{root.get(dim)}" without a viewBox')
    _inline_uses(root)
    clip_ids = _clip_ids(root)
    css = _parse_css(root)

    def visit(el: ET.Element, inherited: dict[str, str], in_clip: bool) -> None:
        name = local(el.tag)
        p = dict(inherited)
        for a in PAINT_PROPS:                                  # presentation attributes ...
            if el.get(a) is not None:
                p[a] = el.get(a)
        for sel in ("*", name):                                # ... are beaten by stylesheet rules (specificity: * < tag < class < id) ...
            if sel in css:
                p.update({k: v for k, v in css[sel].items() if k in PAINT_PROPS})
        for c in (el.get("class") or "").split():
            p.update({k: v for k, v in css.get(f".{c}", {}).items() if k in PAINT_PROPS})
        if el.get("id") and f"#{el.get('id')}" in css:
            p.update({k: v for k, v in css[f"#{el.get('id')}"].items() if k in PAINT_PROPS})
        decl = _parse_declarations(el.get("style") or "")      # ... and by the inline style attribute
        for k, v in decl.items():
            if k in PAINT_PROPS:
                p[k] = v
        cp = el.get("clip-path") or decl.get("clip-path")
        if cp:
            m = re.fullmatch(r"url\(\s*#([\w.-]+)\s*\)", cp.strip())
            if not m or m.group(1) not in clip_ids:
                raise SvgRejected("clip-path", f"clip-path must reference a <clipPath> in this file, got {cp!r}")
        for a in OPACITY_PROPS:
            if a in p and _opacity_value(p[a]) < 1:
                raise SvgRejected("opacity", f"{a} below 1 is not allowed")
        if name == "clipPath":
            in_clip = True
        if name in SHAPES and not in_clip:
            fill = p.get("fill", "#000000")
            stroke = p.get("stroke", "none")
            if fill.strip().lower() == "currentcolor":
                fill = p.get("color", "#000000")
            if stroke.strip().lower() == "currentcolor":
                stroke = p.get("color", "#000000")
            if name == "line":
                fill = "none"
            el.set("fill", _to_hex(fill))
            el.set("stroke", _to_hex(stroke))
            el.attrib.pop("style", None)
            el.attrib.pop("class", None)
            for a in OPACITY_PROPS:
                el.attrib.pop(a, None)
            el.attrib.pop("vector-effect", None)   # resvg ignores non-scaling-stroke; do not pretend it works
        elif name in SHAPES:
            el.attrib.pop("style", None)
            el.attrib.pop("class", None)
        for ch in el:
            visit(ch, p, in_clip)

    visit(root, {}, False)
    parents = {child: parent for parent in root.iter() for child in parent}      # one pass: removing 5000 elements one by one was quadratic
    for e in [e for e in root.iter() if local(e.tag) in ("style", "title", "desc", "metadata")]:
        parent = parents.get(e)
        if parent is not None:
            parent.remove(e)
    # <defs> only ever held inlined <use> targets and clipPaths: remove everything else so it cannot render by accident
    for defs in [e for e in root.iter() if local(e.tag) == "defs"]:
        for ch in list(defs):
            if local(ch.tag) != "clipPath":
                defs.remove(ch)
    n = path_count(root)
    if n > int(TH.get("svg.max_paths")):
        raise SvgRejected("paths", f"{n} shapes, at most {TH.get('svg.max_paths')} allowed")
    if root.tag and not root.tag.startswith("{") and root.get("xmlns") is None:
        root.set("xmlns", SVG_NS)
    return root


def path_count(root: ET.Element) -> int:
    """Number of drawn shape elements (shapes inside a ``<clipPath>`` do not draw)."""
    def walk(el: ET.Element, in_clip: bool) -> int:
        name = local(el.tag)
        c = 1 if (name in SHAPES and not in_clip) else 0
        for ch in el:
            c += walk(ch, in_clip or name == "clipPath")
        return c

    return walk(root, False)


def explicit_colours(root: ET.Element) -> set[str]:
    out: set[str] = set()
    for el in root.iter():
        if local(el.tag) in SHAPES:
            for a in ("fill", "stroke"):
                v = el.get(a)
                if v and v != "none":
                    out.add(v.lower())
    return out


# ------------------------------------------------------------------ palette snap
def snap_colours(root: ET.Element, palette_hex: Sequence[str], max_de: float | None = None) -> dict[str, str]:
    """Snap every fill/stroke to the nearest palette colour (CIEDE2000); raise if one is farther than ``max_de`` (default 15).

    Returns the mapping ``original -> snapped`` that was applied.
    """
    lim = float(TH.get("svg.snap_de_max")) if max_de is None else max_de
    pal = [P.normalise_hex(h) for h in palette_hex]
    mapping: dict[str, str] = {}
    for el in root.iter():
        if local(el.tag) not in SHAPES:
            continue
        for a in ("fill", "stroke"):
            v = el.get(a)
            if not v or v == "none":
                continue
            if v not in mapping:
                best, d = P.snap_hex(v, pal)
                if d > lim:
                    raise SvgRejected("off-palette", f"{v} is {d:.1f} from the palette (limit {lim})")
                mapping[v] = best
            el.set(a, mapping[v])
    return mapping


# ------------------------------------------------------------------ render
def prepare(root: ET.Element, w: int, h: int, ss: int = 4, fit: str = "xMidYMid meet") -> float:
    """Set the root ``viewBox`` (if missing), ``width``/``height`` (``w*ss`` x ``h*ss``) and ``preserveAspectRatio``.

    resvg fits and never stretches when ``width`` and ``height`` are passed through its API, so the size goes on the root element.
    Returns the user units per output pixel.
    """
    vb = root.get("viewBox")
    if vb is None:
        vb = f"0 0 {_num(root.get('width', 1024))} {_num(root.get('height', 1024))}"
        root.set("viewBox", vb)
    try:
        vbw, vbh = (float(x) for x in vb.replace(",", " ").split()[2:4])
    except ValueError:
        raise SvgRejected("viewbox", f"unreadable viewBox {vb!r}") from None
    if vbw <= 0 or vbh <= 0:
        raise SvgRejected("viewbox", "empty viewBox")
    root.set("width", str(w * ss))
    root.set("height", str(h * ss))
    root.set("preserveAspectRatio", fit)
    return max(vbw / w, vbh / h)


def close_seams(root: ET.Element, upp: float, px: float = 0.5) -> None:
    """Give every fill-only shape a same-colour stroke of ``px`` output pixels (closes the see-through seams of diagonal edges)."""
    def walk(el: ET.Element, in_clip: bool) -> None:
        name = local(el.tag)
        if name in SHAPES and not in_clip and el.get("stroke") in (None, "none") and el.get("fill") not in (None, "none"):
            el.set("stroke", el.get("fill"))
            el.set("stroke-width", f"{px * upp:.4f}")
            el.set("stroke-linejoin", "round")
        for ch in el:
            walk(ch, in_clip or name == "clipPath")

    walk(root, False)


def _render(root: ET.Element, size: tuple[int, int]) -> Image.Image:
    import resvg_py

    png = bytes(resvg_py.svg_to_bytes(svg_string=ET.tostring(root, encoding="unicode"), skip_system_fonts=True, resources_dir=EMPTY_DIR))
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    if im.size != size:
        raise SvgRejected("render", f"rendered {im.size}, expected {size}")
    return im.convert("RGBa")                                    # premultiplied for the BOX downsample


def _recolour(root: ET.Element, sentinel: str, other: str | None) -> ET.Element:
    tree = copy.deepcopy(root)
    for el in tree.iter():
        for a in ("fill", "stroke"):
            v = (el.get(a) or "").lower()
            if v and v != "none":
                el.set(a, "#000000" if v == sentinel else (other or v))
    return tree


def render_part(root: ET.Element, w: int, h: int, sentinel: str | None = None, ss: int = 4) -> Image.Image:
    """Render a sanitized, normalised, snapped tree to an RGBA ``w x h`` image.

    With a ``sentinel`` the background is removed by the **two-pass matte** (never by a pixel key): pass C maps the sentinel to black,
    pass M maps the sentinel to black and every other colour to white; ``alpha = M/255`` and ``colour = C/alpha``.
    """
    work = copy.deepcopy(root)
    upp = prepare(work, w, h, ss)
    close_seams(work, upp)
    big = (w * ss, h * ss)
    if sentinel is None:
        return _render(work, big).resize((w, h), Image.Resampling.BOX).convert("RGBA")
    s = P.normalise_hex(sentinel)
    c = np.asarray(_render(_recolour(work, s, None), big).resize((w, h), Image.Resampling.BOX), dtype=np.float64)
    m = np.asarray(_render(_recolour(work, s, "#ffffff"), big).resize((w, h), Image.Resampling.BOX), dtype=np.float64)
    a = m[..., 0] / 255.0
    rgb = np.where(a[..., None] > 0, c[..., :3] / np.maximum(a[..., None], 1e-6), 0)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), a * 255]).round().astype(np.uint8), "RGBA")


def render_flat(root: ET.Element, w: int, h: int) -> Image.Image:
    """The SVG rendered as it is at ``w x h`` (no matte): used for the border pre-check."""
    work = copy.deepcopy(root)
    prepare(work, w, h, 1)
    return _render(work, (w, h)).convert("RGBA")


def border_sentinel_share(flat: Image.Image, sentinel: str, frame_px: int = 1, tol: int = 6) -> float:
    """Share of the border pixels (outer ``frame_px`` ring) that equal the sentinel colour (within ``tol`` per channel)."""
    arr = np.asarray(flat.convert("RGBA")).astype(int)
    f = max(1, frame_px)
    ring = np.zeros(arr.shape[:2], bool)
    ring[:f] = ring[-f:] = True
    ring[:, :f] = ring[:, -f:] = True
    ref = np.array(P.hex_to_rgb(sentinel))
    ok = (np.abs(arr[..., :3] - ref).max(axis=2) <= tol) & (arr[..., 3] >= 250)
    return float(ok[ring].mean())


def interior_alpha_min(im: Image.Image, erode_px: int = 2) -> int:
    """Smallest alpha inside the shape after eroding it by ``erode_px`` (the seam test: should be 255)."""
    a = np.asarray(im.convert("RGBA"))[..., 3]
    inner = P.erode(a >= 128, erode_px)
    return int(a[inner].min()) if inner.any() else 255


@dataclass
class SvgPart:
    image: Image.Image
    path_count: int
    colours: list[str]
    border_share: float | None = None
    seam_alpha_min: int = 255
    snapped: dict[str, str] = field(default_factory=dict)


def svg_to_part(svg: str, palette_hex: Sequence[str], size: tuple[int, int], *, sentinel: str | None = None, ss: int = 4,
                snap_max_de: float | None = None) -> SvgPart:
    """The whole path for one Recraft SVG: sanitize, snap to the palette (+ the sentinel), pre-check the border, render.

    Raises ``SvgRejected`` with a ``rule`` for anything the sanitizer or the palette snap refuses.
    """
    root = sanitize_and_normalize(svg)
    pal = list(palette_hex) + ([sentinel] if sentinel else [])
    snapped = snap_colours(root, pal, snap_max_de)
    border = None
    if sentinel:
        flat = render_flat(root, size[0], size[1])
        border = border_sentinel_share(flat, sentinel)
    img = render_part(root, size[0], size[1], sentinel, ss)
    return SvgPart(img, path_count(root), sorted(explicit_colours(root)), border, interior_alpha_min(img), snapped)


# ------------------------------------------------------------------ checks
def check_svg(svg: str, *, subject_sha: str = "") -> CheckResult:
    """A_SVG (CHK-A11, HARD, class security): the SVG passes the sanitizer (banned elements, ``href``, opacity < 1, gradients,
    path count, viewBox). The evidence names the rule that rejected it."""
    try:
        root = sanitize_and_normalize(svg)
    except SvgRejected as e:
        return build_result("A_SVG", passed=False, subject_sha=subject_sha, metric="svg_rule", evidence=str(e), fix_hint="change_technique")
    n = path_count(root)
    return build_result("A_SVG", passed=True, subject_sha=subject_sha, metric="paths", value=float(n), threshold=TH.describe("svg.max_paths", "<="),
                        evidence=f"{n} shape(s), {len(explicit_colours(root))} colour(s)")


def check_sentinel_border(flat: Image.Image, sentinel: str, *, subject_sha: str = "") -> CheckResult:
    """A_SENTINEL (HARD): at least 95% of the border pixels are the sentinel colour (Recraft and Gemini output)."""
    share = border_sentinel_share(flat, sentinel)
    return build_result("A_SENTINEL", passed=share >= float(TH.get("svg.border_sentinel_min")), subject_sha=subject_sha,
                        metric="border_sentinel_share", value=share, threshold=TH.describe("svg.border_sentinel_min", ">="),
                        evidence=f"{share:.3f} of the border is {sentinel}", fix_hint="change_technique")
