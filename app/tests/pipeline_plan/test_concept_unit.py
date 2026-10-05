"""``pipeline/concept.py`` and the selection of ``pipeline/plan.py``, piece by piece: the code guide and its masks, the Gate A checks, the ladder,
the palette lock, the part crops and the Gate 1 tile state."""
from __future__ import annotations

import random

import numpy as np
import pytest
from planhelpers import db_project

from duoskin.imaging import guides, masks
from duoskin.models import kitenums
from duoskin.models.gate import GateKind, TileState
from duoskin.models.spec import PlanSet
from duoskin.pipeline import common, kits
from duoskin.pipeline import concept as CO
from duoskin.pipeline import mock_roles as MR
from duoskin.pipeline import plan as PL
from duoskin.prompts.llm import compile_llm
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock.llm import RoleCall


@pytest.fixture
def spec(unit_rt):
    kits.install_inventory(unit_rt)
    inputs = {"brief_text": "a duo", "structure_request": "auto", "must_include": "none", "combo": "bg", "reference_analysis": "none",
              "taste_profile": "none", "recent_cards": "none", "recently_used": "none", "avoid": ""}
    text = compile_llm("L3.planner", inputs).user_text
    out = MR.planner_builder(RoleCall(route="L3_planner", out=PlanSet, schema={}, system_text="", content_text=text, content=[], seed=2,
                                       rng=random.Random(2), prompt_version=1))
    yield out["specs"][0]
    kitenums.set_default_inventory(None)


# ---------------------------------------------------------------------------------------------------- the guide
def test_the_figure_follows_the_spec_cuts_and_colours(spec):
    pal = common.palette_map(spec)
    spec["a"]["top"]["sleeve"], spec["a"]["bottom"]["leg"], spec["a"]["top"]["block_layout"] = "none", "midi", "contrast_sleeves"
    spec["a"]["bottom"]["legwear"], spec["a"]["bottom"]["legwear_ref"] = "bare", "none"
    fig = CO.figure_from_spec(spec, "a")
    assert (fig.sleeve, fig.leg, fig.block_layout, fig.legwear) == ("sleeveless", "knee", "raglan_split", None)
    assert fig.top_base == pal[spec["a"]["top"]["base_ref"]] and fig.bottom_base == pal[spec["a"]["bottom"]["base_ref"]]
    assert fig.shoes == pal[spec["a"]["bottom"]["shoes"]["base_ref"]] and 1 <= len(fig.swatches) <= 5
    spec["b"]["bottom"]["leg"] = "mini"
    assert CO.figure_from_spec(spec, "b").leg == "short"
    assert CO.build_guide(spec, "a").image.size == (guides.CANVAS_W, guides.CANVAS_H)


def paint(guide, mode, colour=(200, 40, 40)):
    ed = CO.editable_for(guide, mode)
    orig = guide.image.convert("RGBA")
    out = D.paint_masked(orig, masks.make_mask(ed), colour)
    return orig, out, ed, masks.paste_back(orig, out, ed).convert("RGB")


def test_the_three_masks_nest_and_only_the_figure_boxes_mask_breaks_the_silhouette(spec):
    g = CO.build_guide(spec, "a")
    head, tight, figure = CO.editable_for(g, "head"), CO.editable_for(g, "tight"), CO.editable_for(g, "figure")
    assert head.any() and (head <= figure).all() and (tight <= figure).all()
    assert not (head & g.body_masks["front"]).any(), "the head mask never reaches the body"
    assert (g.body_masks["front"] <= tight).all() and tight.sum() < figure.sum()
    # the mock repaints the whole editable area with one flat colour: that is what the ladder is for
    for mode, passes in (("figure", False), ("tight", True), ("head", True)):
        orig, raw, ed, merged = paint(g, mode)
        results, _close, _de = CO.gate_a(spec, "a", g, merged, "sha", orig, raw, ed, [])
        sil = [r for r in results if r.check_id == "A_SIL_GUIDE"]
        assert len(sil) == 2 and all(r.passed for r in sil) == passes, (mode, [r.evidence for r in sil])
        assert next(r for r in results if r.check_id == "A_PASTE").passed, "the paste-back keeps everything outside the mask"


def test_gate_a_catches_text_a_wrong_size_a_colour_leak_and_a_near_copy(spec):
    g = CO.build_guide(spec, "a")
    orig, raw, ed, merged = paint(g, "head", colour=(30, 150, 150))
    ok, close, de = CO.gate_a(spec, "a", g, merged, "sha", orig, raw, ed, [])
    assert not CO.blocking_failures(ok) and close is False and de == pytest.approx(0.0, abs=2.0), "the guide colours are exactly the plan's"
    from duoskin.imaging import similarity

    again, close2, _ = CO.gate_a(spec, "a", g, merged, "sha", orig, raw, ed, [similarity.phash(merged)])
    assert close2 is True and any(r.check_id == "A_PHASH" and not r.passed for r in again) and not CO.blocking_failures(again), "A_PHASH is a SOFT check"
    partner = common.palette_map(spec)[spec["b"]["top"]["base_ref"]]
    leak = merged.copy()
    arr = np.asarray(leak).copy()
    arr[guides.NECK_Y:guides.FEET_Y, :guides.SLOT_W] = [int(partner[i:i + 2], 16) for i in (1, 3, 5)]
    leaked = CO.leak_check(__import__("PIL.Image", fromlist=["Image"]).fromarray(arr), CO.figure_hexes(spec, "a"), "sha")
    assert not leaked.passed and leaked.check_id == "A_LEAK" and leaked.kind == "hard"


def test_the_ladder_and_the_first_mask(unit_rt):
    assert [(m, n) for m, n, _ in CO.ladder("figure")] == [("figure", 4), ("tight", 4), ("tight", 8)]
    assert [(m, n) for m, n, _ in CO.ladder("head")] == [("head", 4), ("head", 8)]
    p = CO.CharParams(project_id="p", plan_set_id="s", spec_id="x", char="a")
    assert CO.initial_mask_mode(unit_rt, p) == "head", "the mock repaints flat, so the mock starts with the head-only mask"
    assert CO.initial_mask_mode(unit_rt, p.model_copy(update={"mask_mode": "figure"})) == "figure"


def test_candidates_rank_by_soft_passes_then_swatch_distance_and_near_copies_go_last(spec):
    from duoskin.checks.runner import build_result

    soft_ok = build_result("A_SWATCH", passed=True, metric="x")
    soft_bad = build_result("A_SWATCH", passed=False, metric="x")
    a = CO.Cand(0, b"", "r", checks=[soft_bad], swatch_de=20.0)
    b = CO.Cand(1, b"", "r", checks=[soft_ok], swatch_de=5.0)
    c = CO.Cand(2, b"", "r", checks=[soft_ok], swatch_de=1.0, phash_close=True)
    assert [x.index for x in sorted([a, b, c], key=CO.Cand.key)] == [1, 0, 2]
    assert [x.index for x in CO.pick_survivors([a, b, c])] == [1, 0, 2]
    a.hard_ok = False
    assert [x.index for x in CO.pick_survivors([a, b, c])] == [1, 2], "a draft that fails a HARD check is out"


# ---------------------------------------------------------------------------------------------------- the lock
def finals_for(spec, recolour: dict[str, tuple[int, int, int]] | None = None):
    out = {}
    for c in "ab":
        g = CO.build_guide(spec, c)
        im = g.image.convert("RGB").copy()
        if recolour and c in recolour:
            arr = np.asarray(im).copy()
            torso = guides.zone_masks(g, "front")["torso"]
            arr[torso] = recolour[c]
            im = __import__("PIL.Image", fromlist=["Image"]).fromarray(arr)
        out[c] = im
    return out


def locked_rec(unit_rt, spec):
    p = db_project(unit_rt)
    return PL.new_spec_record(unit_rt, p.id, "pls_lock", 0, spec, status="shown")


def test_the_palette_lock_snaps_what_matches_and_takes_a_new_colour_from_the_picture(unit_rt, spec):
    rec = locked_rec(unit_rt, spec)
    same, log = CO.palette_lock(unit_rt, rec, finals_for(spec), "picture")
    assert same["palette"] == spec["palette"] and all(e["action"] == "snap" and e["used"] == e["planned"] for e in log)
    top_ref = spec["a"]["top"]["base_ref"]
    new, log = CO.palette_lock(unit_rt, rec, finals_for(spec, {"a": (200, 40, 40)}), "picture")
    entry = next(e for e in log if e["zone"] == "a.top")
    assert entry["action"] == "confirm" and entry["used"] == "#C82828" and entry["picture"].upper() == "#C82828"
    assert next(c for c in new["palette"] if c["id"] == top_ref)["hex"] == "#C82828"
    assert [c["id"] for c in new["palette"]] == [c["id"] for c in spec["palette"]], "palette ids stay stable"
    assert spec["palette"] != new["palette"] and rec.spec["palette"] == spec["palette"], "the plan's own record is never edited"
    kept, log = CO.palette_lock(unit_rt, rec, finals_for(spec, {"a": (200, 40, 40)}), "planned")
    assert kept["palette"] == spec["palette"] and next(e for e in log if e["zone"] == "a.top")["used"] == entry["planned"]


def test_part_boxes_name_valid_parts_and_stay_inside_the_picture(spec):
    spec["a"]["top"]["prints"] = [{"motif": "a paper lantern cluster", "region": "torso_f", "scale": "medium", "colour_refs": [spec["a"]["top"]["base_ref"]]}]
    boxes = CO.part_boxes(spec, "a")
    assert {"a.face", "a.hair", "a.shirt", "a.pants", "a.acc.0", "a.print.top.0"} <= set(boxes)
    for pid, box in boxes.items():
        assert common.PART_RE.match(pid), pid
        x0, y0, x1, y1 = CO.clamp_box(box, (guides.CANVAS_W, guides.CANVAS_H))
        assert 0 <= x0 < x1 <= guides.CANVAS_W and 0 <= y0 < y1 <= guides.CANVAS_H, pid
    crop = guides.prepare_part_crop(CO.build_guide(spec, "a").image, CO.clamp_box(boxes["a.face"], (guides.CANVAS_W, guides.CANVAS_H)))
    assert 512 <= max(crop.size) <= 1024


# ---------------------------------------------------------------------------------------------------- the tile and the selection
SHA, FRONT, BACK, SHEET = "1" * 64, "2" * 64, "3" * 64, "4" * 64
ALT = {"a": "5" * 64, "b": "6" * 64}


def test_the_tile_state_follows_the_concept_state(unit_rt, spec):
    p = db_project(unit_rt, must_include=["a teal bow"])
    rec = PL.new_spec_record(unit_rt, p.id, "pls_tile", 0, spec, status="shown")
    env = {"brief_constraints": [{"text": "a teal bow", "spec_paths": ["/a/accessories/0"]}], "notice": ""}
    t = CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env)
    assert t.state == TileState.GENERATING and t.tile_id == "plan0" and t.assets == {} and t.facts["must_include"][0]["covered"] is True
    assert t.allowed_actions == [a for a in t.allowed_actions] and "approve" in [a.value for a in t.allowed_actions]
    for c in "ab":
        CO.put_state(unit_rt, "pls_tile", 0, c, {"spec_id": rec.id, "chosen": {"sha": SHA, "front": FRONT, "back": BACK, "judged": True},
                                                 "alts": [{"front": ALT[c], "back": BACK}], "failed": None})
    assert CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env).state == TileState.GENERATING, "no checked sheet yet"
    CO.put_sheet(unit_rt, "pls_tile", 0, {"spec_id": rec.id, "ok": True, "pending": False, "labelled": SHEET, "warnings": [{"id": "A_SWATCH:a", "text": "x"}],
                                          "not_buildable": ["flowing cape: character B"], "hard_failures": []})
    t = CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env)
    assert t.state == TileState.READY and t.assets["a_front"] == FRONT and t.assets["sheet"] == SHEET
    assert t.alternatives == [{"a_front": ALT["a"], "b_front": ALT["b"]}] and t.facts["not_buildable"] == ["flowing cape: character B"]
    assert t.facts["warnings"][0]["id"] == "A_SWATCH:a" and t.badges == [] and GateKind.CONCEPT
    CO.put_sheet(unit_rt, "pls_tile", 0, {"spec_id": rec.id, "ok": False, "hard_failures": ["A_LEAK"]})
    assert CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env).state == TileState.NEEDS_HUMAN
    CO.put_sheet(unit_rt, "pls_tile", 0, {"spec_id": "another", "ok": True, "pending": False})
    assert CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env).state == TileState.GENERATING, "a sheet of another version of the plan does not count"
    CO.put_state(unit_rt, "pls_tile", 0, "b", {"spec_id": rec.id, "chosen": None, "failed": {"checks": ["A_OCR"]}})
    assert CO.tile_for(unit_rt, p, "pls_tile", 0, rec, [rec], env).state == TileState.NEEDS_HUMAN


def ranked(unit_rt, spec, specs):
    p = db_project(unit_rt)
    out = []
    for i, (wild, wins, pts, novelty) in enumerate(specs):
        s = {**spec, "is_wildcard": wild}
        rec = PL.new_spec_record(unit_rt, p.id, "pls_rank", i, s, status="candidate")
        out.append(PL.Ranked(rec, wins, pts, novelty))
    return out


def test_selection_orders_by_wins_then_points_then_novelty_and_the_wildcard_is_always_in(unit_rt, spec):
    rows = ranked(unit_rt, spec, [(False, 2.0, 20, 0.5), (False, 1.0, 30, 0.1), (False, 1.0, 30, 0.9), (True, 0.0, 5, 0.5)])
    shown = PL.select_shown(rows)
    assert len(shown) == 3 and sum(r.rec.spec["is_wildcard"] for r in shown) == 1, "the weakest wildcard still takes a slot"
    assert [r.rec.plan_index for r in shown] == [0, 1, 3]
    two_wild = ranked(unit_rt, spec, [(True, 0.0, 5, 0.5), (True, 2.0, 5, 0.5), (False, 1.0, 5, 0.5), (False, 0.5, 5, 0.5)])
    shown = PL.select_shown(two_wild)
    assert sum(r.rec.spec["is_wildcard"] for r in shown) == 1 and shown[0].rec.plan_index == 1, "never two wildcards: the better ranked stays"
    none = PL.select_shown(ranked(unit_rt, spec, [(False, 1.0, 1, 0.1), (False, 2.0, 1, 0.1)]))
    assert [r.rec.plan_index for r in none] == [1, 0]
    ties = PL.select_shown(ranked(unit_rt, spec, [(False, 1.0, 10, 0.3)] * 3))
    assert [r.rec.plan_index for r in ties] == [0, 1, 2], "a full tie keeps the planner's order"


def test_a_revised_plan_inherits_the_scores_of_the_version_that_was_judged(unit_rt, spec):
    p = db_project(unit_rt)
    old = PL.new_spec_record(unit_rt, p.id, "pls_lin", 0, spec, status="candidate")
    new = PL.new_spec_record(unit_rt, p.id, "pls_lin", 0, spec, parent=old, created_by="reviser", status="candidate")
    newer = PL.new_spec_record(unit_rt, p.id, "pls_lin", 0, spec, parent=new, created_by="reviser", status="candidate")
    assert PL.lineage_value(unit_rt, newer, {old.id: 2.0}) == 2.0
    assert PL.lineage_value(unit_rt, newer, {new.id: 1.0, old.id: 2.0}) == 1.0, "the nearest ancestor wins"
    assert PL.lineage_value(unit_rt, newer, {"other": 9}) is None


def test_the_records_of_a_plan_set_are_the_newest_live_one_per_index(unit_rt, spec):
    p = db_project(unit_rt)
    first = PL.new_spec_record(unit_rt, p.id, "pls_live", 0, spec, status="candidate")
    PL.save_spec(unit_rt, first.model_copy(update={"status": "superseded"}))
    second = PL.new_spec_record(unit_rt, p.id, "pls_live", 0, spec, parent=first, created_by="reviser", status="candidate")
    other = PL.new_spec_record(unit_rt, p.id, "pls_live", 1, spec, status="dropped")
    live = PL.plan_records(unit_rt, p.id, "pls_live")
    assert [r.id for r in live] == [second.id]
    assert {r.id for r in PL.plan_records(unit_rt, p.id, "pls_live", live_only=False)} == {second.id, other.id}
