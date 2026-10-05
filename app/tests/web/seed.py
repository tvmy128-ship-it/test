"""Seed realistic data into a running app for the UI tests: blocky character pictures (drawn with PIL, no network), projects,
specs with DNA cards, and real gates and parts, so the UI is exercised against the real gate lifecycle (decisions, optimistic
locking, soft-warning release, approve-anyway) and not against canned JSON."""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from duoskin.engine.cas import make_prov
from duoskin.engine.gates import allowed_actions_for
from duoskin.models.common import new_id, sha256_of, utcnow
from duoskin.models.dna import DnaCard
from duoskin.models.gate import Gate, GateKind, GateTile, TileState
from duoskin.models.job import Job, JobKind
from duoskin.models.part import Part, PartKind, PartState
from duoskin.models.project import Project, ProjectSettings, Stage
from duoskin.models.spec import Anchor, CharacterDNA, WorldDNA
from duoskin.models.spec_record import SpecRecord

PALETTES = {
    "koi": {"skin": "#f2c9a0", "hair": "#2b2d42", "top": "#e4572e", "bottom": "#2e4057", "shoe": "#f6f1e9", "accent": "#ffb627"},
    "teal": {"skin": "#d9a679", "hair": "#8d5a97", "top": "#1b998b", "bottom": "#f4f1de", "shoe": "#3d405b", "accent": "#e07a5f"},
    "plum": {"skin": "#f5d3b8", "hair": "#5b3a29", "top": "#7b2d8e", "bottom": "#264653", "shoe": "#e9c46a", "accent": "#f4a261"},
}
SKINS = ["#f6dcc5", "#e8b894", "#c98f65", "#9a6540", "#5e3b26"]


def _rect(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str, r: int = 8) -> None:
    d.rounded_rectangle(box, radius=r, fill=fill, outline="#00000030", width=2)


def figure_png(palette: str = "koi", view: str = "front", size: tuple[int, int] = (240, 360), skin: str | None = None) -> bytes:
    """A blocky character on a transparent background: head, torso, arms, legs; the back view has no face."""
    p = PALETTES[palette]
    w, h = size
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    u = w // 6
    cx = w // 2
    skin_c = skin or p["skin"]
    _rect(d, (cx - u, int(h * .04), cx + u, int(h * .04) + 2 * u), skin_c, 14)                       # head
    if view == "back":
        _rect(d, (cx - u - 4, int(h * .03), cx + u + 4, int(h * .04) + int(1.6 * u)), p["hair"], 14)
    else:
        _rect(d, (cx - u - 4, int(h * .03), cx + u + 4, int(h * .04) + int(.8 * u)), p["hair"], 12)
        for ex in (cx - u // 2, cx + u // 2):
            d.ellipse((ex - 6, int(h * .04) + int(1.05 * u), ex + 6, int(h * .04) + int(1.05 * u) + 14), fill="#1d1d2b")
        d.arc((cx - u // 2, int(h * .04) + int(1.25 * u), cx + u // 2, int(h * .04) + int(1.6 * u)), 20, 160, fill="#7a2e2e", width=3)
    top = int(h * .04) + 2 * u + 6
    _rect(d, (cx - int(1.5 * u), top, cx + int(1.5 * u), top + int(2.6 * u)), p["top"], 10)         # torso
    if view == "front":
        d.ellipse((cx - u // 2, top + u // 2, cx + u // 2, top + int(1.5 * u)), fill=p["accent"], outline="#00000030")
    for sx in (-1, 1):
        x0 = cx + sx * int(1.5 * u) + (0 if sx > 0 else -u + 4)
        _rect(d, (x0, top, x0 + u - 4, top + int(2.4 * u)), p["top"] if sx > 0 else skin_c, 10)
    leg_top = top + int(2.6 * u) + 4
    for sx in (-1, 1):
        x0 = cx + (4 if sx > 0 else -int(1.5 * u) + 2)
        _rect(d, (x0, leg_top, x0 + int(1.5 * u) - 6, leg_top + int(2.3 * u)), p["bottom"], 8)
        _rect(d, (x0, leg_top + int(2.0 * u), x0 + int(1.5 * u) - 6, leg_top + int(2.3 * u) + 8), p["shoe"], 6)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def part_png(kind: str, label: str = "", palette: str = "koi", size: tuple[int, int] = (300, 300), variant: int = 0) -> bytes:
    """A stand-in picture for a part: a flat shirt/pants shape, a face, an accessory blob, a print, swatches."""
    p = PALETTES[palette]
    w, h = size
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cols = [p["top"], p["accent"], p["bottom"], p["hair"], p["shoe"]]
    if kind == "shirt":
        d.polygon([(w * .25, h * .1), (w * .75, h * .1), (w * .95, h * .35), (w * .8, h * .45), (w * .75, h * .35), (w * .75, h * .9), (w * .25, h * .9), (w * .25, h * .35), (w * .2, h * .45), (w * .05, h * .35)], fill=p["top"], outline="#00000040")
        d.ellipse((w * .4, h * .3, w * .6, h * .5), fill=p["accent"])
    elif kind == "pants":
        d.polygon([(w * .25, h * .08), (w * .75, h * .08), (w * .8, h * .95), (w * .56, h * .95), (w * .5, h * .4), (w * .44, h * .95), (w * .2, h * .95)], fill=p["bottom"], outline="#00000040")
    elif kind == "face":
        d.rounded_rectangle((w * .1, h * .1, w * .9, h * .9), radius=40, fill=variant_skin(variant), outline="#00000040", width=3)
        mouth = ((w * .38, h * .62, w * .62, h * .78), 10, 170) if variant % 2 else ((w * .36, h * .68, w * .64, h * .74), 0, 180)
        d.ellipse((w * .28, h * .38, w * .4, h * .5), fill="#1d1d2b")
        d.ellipse((w * .6, h * .38, w * .72, h * .5), fill="#1d1d2b")
        d.arc(mouth[0], mouth[1], mouth[2], fill="#7a2e2e", width=4)
    elif kind == "hair":
        d.pieslice((w * .1, h * .1, w * .9, h * 1.1), 180, 360, fill=p["hair"], outline="#00000040")
        d.rectangle((w * .1, h * .6, w * .3, h * .9), fill=p["hair"])
    elif kind == "acc":
        d.ellipse((w * .2, h * .25, w * .8, h * .8), fill=p["accent"], outline="#00000040", width=3)
        d.ellipse((w * .55, h * .35, w * .85, h * .6), fill=p["top"])
        d.ellipse((w * .35, h * .45, w * .42, h * .52), fill="#1d1d2b")
    elif kind == "print":
        d.regular_polygon((w / 2, h / 2, w * .35), 6, fill=p["top"], outline="#00000040")
        d.ellipse((w * .35, h * .35, w * .65, h * .65), fill=p["accent"])
    elif kind == "swatches":
        for i, c in enumerate(cols):
            d.rounded_rectangle((8 + i * (w - 16) / 5, h * .2, 8 + (i + .85) * (w - 16) / 5, h * .8), radius=10, fill=c, outline="#00000040")
    elif kind == "tone":
        d.rounded_rectangle((w * .1, h * .1, w * .9, h * .9), radius=30, fill=SKINS[variant % 5], outline="#00000040", width=3)
        d.ellipse((w * .28, h * .4, w * .38, h * .5), fill="#1d1d2b")
        d.ellipse((w * .62, h * .4, w * .72, h * .5), fill="#1d1d2b")
    else:
        d.rounded_rectangle((8, 8, w - 8, h - 8), radius=20, fill=cols[variant % 5])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def variant_skin(i: int) -> str:
    return SKINS[i % 5]


def put(rt: Any, data: bytes, ext: str = "png") -> str:
    return rt.cas.put(data, ext, prov=make_prov("mock", notes=["ui test seed"])).sha256


def project(rt: Any, name: str = "Plush Koi", combo: str = "bg", stage: Stage = Stage.BRIEF, brief: str = "A calm koi pond duo: soft hoodie, plush koi bag.",
            spent: float = 0.0, **settings: Any) -> Project:
    now = utcnow()
    p = Project(id=new_id("prj"), name=name, slug=rt.repo.unique_slug(name), created_at=now, updated_at=now, combo=combo, brief=brief,
                stage=stage, settings=ProjectSettings(**settings), spent_usd=spent, must_include=["koi on the back"])
    rt.repo.create_project(p)
    return p


def job(rt: Any, project_id: str, kind: JobKind = JobKind.PARTS) -> Job:
    return rt.repo.insert_job(Job(id=new_id("job"), project_id=project_id, kind=kind, created_at=utcnow()))


_WORLD = {"theme": "koi pond at dusk", "pair_structure": "complement", "structure_note": "", "story": "Two friends who share a pond garden.",
              "palette_family": "warm_pastel", "material_family": "fleece", "detail_level": "standard"}


def dna_card(spec_id: str, structure: str = "complement", theme: str = "koi pond at dusk", a_shape: str = "round_soft", b_shape: str = "geometric_clean") -> DnaCard:
    world = WorldDNA(**{**_WORLD, "theme": theme, "pair_structure": structure})
    return DnaCard(
        spec_id=spec_id, version=1, locked=False, source="planner", world=world,
        anchors=[Anchor(kind="motif", description="a small koi on the chest", on_a="chest patch", on_b="bag charm", visible_from="front")],
        palette_family="warm_pastel", palette_hexes={"p1": "#E4572E", "p2": "#2E4057", "p3": "#FFB627", "p4": "#F6F1E9"},
        a=CharacterDNA(shape_language=a_shape, colour_plan="ratio_60_30_10", focal_location="chest", motif_object="plush koi", accessory_style="soft plush charms", energy="calm"),
        b=CharacterDNA(shape_language=b_shape, colour_plan="block_50_50", focal_location="back_print", motif_object="paper lantern", accessory_style="clean enamel pins", energy="bright"),
        hair_kit={"a": "bob_soft", "b": "hair_custom"})


def concept_gate(rt: Any, p: Project, *, warnings: bool = False, not_buildable: bool = True, failed_plan: int | None = None) -> Gate:
    """Three plan cards (the second is the wildcard) with real pictures, DNA cards and facts."""
    j = job(rt, p.id, JobKind.PLAN)
    plans = [("Plan 1", "koi", "complement", "koi pond at dusk", "round_soft", "geometric_clean"),
             ("Plan 2", "teal", "mirror", "lantern festival night", "flowing_curved", "boxy_sturdy"),
             ("Plan 3", "plum", "same_club", "moonlit tea garden", "sharp_angular", "round_soft")]
    tiles = []
    for i, (label, pal, structure, theme, sa, sb) in enumerate(plans):
        spec_id = new_id("spc")
        rt.repo.add_spec(SpecRecord(id=spec_id, project_id=p.id, plan_set_id="set1", plan_index=i, spec={"is_wildcard": i == 1, "world": {"theme": theme}},
                                    sha256=sha256_of({"i": i, "p": p.id}), critic_levels={"coherence": "strong", "buildable": "ok"}, rank=i))
        rt.repo.save_dna_card(spec_id, 1, p.id, dna_card(spec_id, structure, theme, sa, sb).model_dump(mode="json"))
        assets = {"a_front": put(rt, figure_png(pal, "front")), "a_back": put(rt, figure_png(pal, "back")),
                  "b_front": put(rt, figure_png("teal" if pal == "koi" else "koi", "front", skin=SKINS[1])), "b_back": put(rt, figure_png("teal" if pal == "koi" else "koi", "back", skin=SKINS[1]))}
        facts: dict[str, Any] = {"spec_id": spec_id, "rank": i, "critic_levels": {"belong_together": "strong", "buildable": "ok", "back_view_interest": "weak"},
                                 "must_include": [{"text": "koi on the back", "covered": i != 2}], "wildcard": i == 1, "brief_read_as": ["complement", "mirror", "same_club"]}
        if failed_plan == i:                                  # what the plan loop stores when one character has no usable drawing
            facts["hard_failures"] = ["A_LEAK"]
            facts["failed"] = {"b": {"checks": ["A_LEAK"], "message": "None of the drawings passed the required checks."}}
            assets = {k: v for k, v in assets.items() if k.startswith("a_")}
        if i == 0 and not_buildable:
            facts["not_buildable"] = [{"element": "A's skirt flares out past the leg boxes", "reason": "it is built as a straight skirt"}]
        if warnings and i == 0:
            facts["warnings"] = [{"id": "w1", "text": "Both outfits lean on the same orange.", "severity": "medium", "catch_rate": .4}]
        tiles.append(GateTile(tile_id=f"plan{i}", label=label, state=TileState.READY, assets=assets, facts=facts,
                              allowed_actions=allowed_actions_for(GateKind.CONCEPT)))
    return rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=GateKind.CONCEPT, tiles=tiles, opened_at=utcnow()))


def tone_sheet_png(palette: str = "koi", cell: int = 110) -> bytes:
    """The face on the head in 4 expressions (rows) x 5 skin tones (columns), as one picture, like the face lane's ``tone_sheet``."""
    im = Image.new("RGBA", (cell * 5 + 60, cell * 4 + 30), (0, 0, 0, 0))
    for r in range(4):
        for c in range(5):
            tile = Image.open(io.BytesIO(part_png("tone", palette=palette, size=(cell - 8, cell - 8), variant=c))).convert("RGBA")
            d = ImageDraw.Draw(tile)
            if r == 1:      # blink
                d.rectangle((tile.width * .25, tile.height * .38, tile.width * .75, tile.height * .52), fill=SKINS[c])
                d.line((tile.width * .28, tile.height * .46, tile.width * .4, tile.height * .46), fill="#1d1d2b", width=3)
                d.line((tile.width * .6, tile.height * .46, tile.width * .72, tile.height * .46), fill="#1d1d2b", width=3)
            if r == 2:      # mouth open
                d.ellipse((tile.width * .4, tile.height * .62, tile.width * .6, tile.height * .8), fill="#7a2e2e")
            if r == 3:      # happy
                d.arc((tile.width * .3, tile.height * .55, tile.width * .7, tile.height * .82), 15, 165, fill="#7a2e2e", width=4)
            im.alpha_composite(tile, (30 + c * cell, 15 + r * cell))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def part_assets(rt: Any, kind: PartKind, pal: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Board assets under the role names the real lanes use (pipeline/face.py, hair.py, clothing.py, prints.py, colours.py)."""
    alt: list[dict[str, str]] = []
    if kind == PartKind.COLOURS:
        return {"swatches": put(rt, part_png("swatches", palette=pal)), "body_front": put(rt, figure_png(pal, "front", (160, 240))),
                "body_back": put(rt, figure_png(pal, "back", (160, 240)))}, alt
    if kind == PartKind.FACE:
        a = {"tone_sheet": put(rt, tone_sheet_png(pal))}
        for i, role in enumerate(["neutral", "blink", "mouth_open", "happy"]):
            a[role] = put(rt, part_png("tone", palette=pal, size=(120, 120), variant=i))
        a["canvas"] = put(rt, part_png("face", palette=pal, variant=2))
        return a, [{"neutral": put(rt, part_png("face", palette=pal, variant=1))}]
    if kind == PartKind.HAIR:
        a = {"front": put(rt, part_png("hair", palette=pal)), "hair_only": put(rt, part_png("hair", palette=pal, size=(200, 200)))}
        for v in ("front", "left", "back", "right"):
            a[f"view.{v}"] = put(rt, part_png("hair", palette=pal, size=(200, 200)))
        return a, alt
    if kind == PartKind.ACCESSORY:
        a = {"front": put(rt, part_png("acc", palette=pal))}
        for v in ("left", "back", "right"):
            a[f"view.{v}"] = put(rt, part_png("acc", palette=pal, size=(200, 200)))
        a["scale"] = put(rt, figure_png(pal, "front", (200, 300)))
        return a, alt
    if kind == PartKind.PRINT:
        return {"final": put(rt, part_png("print", palette=pal)), "preview100": put(rt, part_png("print", palette=pal, size=(100, 100)))}, [{"final": put(rt, part_png("print", palette=pal, variant=2))}]
    shirt = kind == PartKind.SHIRT
    return {"template": put(rt, part_png("shirt" if shirt else "pants", palette=pal, size=(585, 559))),
            "flat_front": put(rt, part_png("shirt" if shirt else "pants", palette=pal)), "flat_back": put(rt, part_png("shirt" if shirt else "pants", palette=pal, variant=1)),
            "preview_boxes": put(rt, part_png("swatches", palette=pal, size=(220, 160)))}, alt


def board_gate(rt: Any, p: Project, *, warnings: int = 0, hard_fail_on: str | None = None) -> Gate:
    """The part board as the pipeline builds it: real Part rows with board assets, tile facts in the store, tiles from
    ``pipeline.parts.tile_for`` (so approvals stamp parts and later syncs keep the same facts)."""
    from duoskin.pipeline import parts as pparts

    j = job(rt, p.id, JobKind.PARTS)
    tiles: list[GateTile] = []
    kinds = [("colours", PartKind.COLOURS, "Colours and body", []), ("face", PartKind.FACE, "Face", ["no_head_base"]), ("hair", PartKind.HAIR, "Hair", []),
             ("acc.0", PartKind.ACCESSORY, "Plush koi bag", ["views_from_gpt"]), ("print.top.0", PartKind.PRINT, "Koi print", []),
             ("shirt", PartKind.SHIRT, "Shirt", ["procedural_folds"]), ("pants", PartKind.PANTS, "Pants", [])]
    n_warn = 0
    for ch in ("a", "b"):
        pal = "koi" if ch == "a" else "teal"
        for suffix, pk, label, flags in kinds:
            pid = f"{ch}.{suffix}"
            assets, alt = part_assets(rt, pk, pal)
            hard: list[dict[str, Any]] = []
            soft: list[dict[str, Any]] = []
            if warnings and n_warn < warnings and pk in (PartKind.SHIRT, PartKind.HAIR, PartKind.FACE):
                soft.append({"id": f"w-{pid}", "text": f"The {label.lower()} is very close in colour to the partner's.", "severity": "medium", "catch_rate": 0.3 + n_warn / 10,
                             "visible": True, "fresh": True})
                n_warn += 1
            if hard_fail_on == pid:
                hard.append({"id": "A_PALETTE", "evidence": "The shirt colour is too far from the palette.", "ran": True})
            facts: dict[str, Any] = {"checks": {"hard_failures": hard, "warnings": soft, "not_applicable": [], "passed": 5, "total": 5 + len(hard)}}
            if soft:
                facts["warnings"] = soft                      # the key the gate service withholds until the first choice
            if hard:
                facts["hard_failures"] = hard                 # the key the gate service's "approve all" looks at (the lane nests them under checks)
            if pk == PartKind.HAIR:
                facts["kit_match"] = {"style_id": "bob_soft", "iou": 0.91}
            part = rt.repo.save_part(Part(id=pid, project_id=p.id, character=ch, kind=pk, label=f"{ch.upper()} · {label}", state=PartState.READY,   # type: ignore[arg-type]
                                          board_assets=assets, alternatives=alt, flags=list(flags if not (ch == "a" and "views_from_gpt" in flags) else [])))
            rt.repo.kv_set(pparts.facts_key(p.id, pid), facts)
            tiles.append(pparts.tile_for(rt, p.id, part, version=0))
    return rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=GateKind.PART_BOARD, tiles=tiles, opened_at=utcnow()))


def final_gate(rt: Any, p: Project, candidates: int = 1, glb_sha: str | None = None, *, similarity_on: bool = False) -> Gate:
    """Gate 3 as pipeline/duo.py builds it: ``a.front``-style pictures, one ``phone_strip`` and one ``face_poses`` picture, ``facts.judge``,
    ``facts.ip``, ``facts.similarity.on`` and the banners as badges."""
    j = job(rt, p.id, JobKind.DUO)
    tiles = []
    for i in range(candidates):
        assets: dict[str, str] = {}
        for c, pal in (("a", "koi"), ("b", "teal")):
            for s in ("front", "back", "left", "right", "three_quarter"):
                assets[f"{c}.{s}"] = put(rt, figure_png(pal, "back" if s == "back" else "front", (160, 240), skin=SKINS[1] if c == "b" else None))
        strip = Image.new("RGBA", (160, 112), (0, 0, 0, 0))
        strip.alpha_composite(Image.open(io.BytesIO(figure_png("koi", "front", (75, 112)))).convert("RGBA"), (0, 0))
        strip.alpha_composite(Image.open(io.BytesIO(figure_png("koi", "back", (75, 112)))).convert("RGBA"), (85, 0))
        buf = io.BytesIO()
        strip.save(buf, "PNG")
        assets["phone_strip"] = put(rt, buf.getvalue())
        faces = Image.new("RGBA", (5 * 120, 120), (0, 0, 0, 0))
        for k in range(5):
            faces.alpha_composite(Image.open(io.BytesIO(part_png("face", size=(116, 116), variant=k))).convert("RGBA"), (k * 120, 2))
        buf = io.BytesIO()
        faces.save(buf, "PNG")
        assets["face_poses"] = put(rt, buf.getvalue())
        if glb_sha:
            assets["a.glb"] = glb_sha
            assets["b.glb"] = glb_sha
        banners = ["Reference-similarity check is on" if similarity_on else "Reference-similarity check is off"]
        facts = {"checks": {"clone_band": "well clear of the nearest duo", "clipping": "none found"},
                 "judge": {"levels": {"belong_together": "strong", "thumbnail_readability": "ok"}, "notes": ["The pair reads as a set from across the room.", "B's lantern is the clear focal point."]},
                 "ip": {"passed": True, "unsure": False}, "similarity": {"on": similarity_on}, "banners": banners, "clone_evidence": "no close match in your earlier duos",
                 "blocking": [], "ip_unsure": False, "rebuilt": [], "warnings": []}
        tiles.append(GateTile(tile_id=f"cand{i}", label=f"Candidate {i + 1}", state=TileState.READY, assets=assets, facts=facts, badges=banners, allowed_actions=allowed_actions_for(GateKind.FINAL_PICK)))
    return rt.gates.open_gate(Gate(id="", project_id=p.id, job_id=j.id, kind=GateKind.FINAL_PICK, tiles=tiles, opened_at=utcnow()))
