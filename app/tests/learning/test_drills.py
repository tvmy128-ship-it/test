"""APP_SPEC §3.8, §17.2: drills are a BLIND labelling screen made from the person's own approved duos, limited to ~20 items per session and
5 variants per base, at most a quarter of the calibration set, and isolated from everything that learns."""
from __future__ import annotations

import json
import random

import pytest
from lfix import approved_duo, dressed_duo, figure, seed_labels, spec_dict, store_renders

from duoskin.engine import calibration as cal
from duoskin.pipeline import brief as BR
from duoskin.pipeline import drills as D
from duoskin.pipeline import plan as PL

PLANS = ("ratio_60_30_10", "ratio_70_20_10", "block_50_50", "mono_accent", "allover_pattern")
COMBOS = ("bb", "gg", "bg", "gb")


def seed_duos(rt, n=6, *, gate_labels=120):
    """``n`` approved duos with renders: several combinations and colour plans, each with its own colours."""
    ids = []
    for i in range(n):
        spec = spec_dict()
        spec["combo"] = COMBOS[i % 4]
        spec["a"]["dna"]["colour_plan"] = PLANS[i % 5]
        spec["b"]["dna"]["colour_plan"] = PLANS[(i + 1) % 5]
        p = approved_duo(rt, f"Base {i}", spec=spec, combo=COMBOS[i % 4], minutes=i)
        store_renders(rt, p, (200 - 25 * i, 70 + 20 * i, 80 + 10 * i), (40 + 30 * i, 160 - 15 * i, 200 - 20 * i), a_shape=i % 3, b_shape=(i + 2) % 5)
        ids.append(p.id)
    seed_labels(rt, gate=gate_labels)
    return ids


# ------------------------------------------------------------------------------------------------------------------ images
def test_a_recolour_changes_clothes_and_hair_but_never_the_skin_or_the_background():
    from PIL import Image

    im = figure((200, 60, 60))
    skin = (232, 190, 150)
    out = D.hue_shift(im, 120, skin=skin)
    assert out.size == im.size
    a, b = im.convert("RGB"), out
    assert a.getpixel((2, 2)) == b.getpixel((2, 2)) == (242, 242, 242), "the sheet background stays"
    w, h = im.size
    head, torso = (int(w * .5), int(h * .17)), (int(w * .5), int(h * .45))
    assert b.getpixel(head) == a.getpixel(head), "the skin tone stays"
    assert b.getpixel(torso) != a.getpixel(torso)
    r, g, _blue = b.getpixel(torso)
    assert g > r, "a red shirt turned 120 degrees round the hue circle is green"
    hsv_a, hsv_b = a.convert("HSV").getpixel(torso), b.convert("HSV").getpixel(torso)
    assert abs(hsv_a[1] - hsv_b[1]) <= 3 and abs(hsv_a[2] - hsv_b[2]) <= 3, "brightness and saturation (the shading) survive"
    assert D.hue_shift(im, 0, skin=skin).tobytes() == a.tobytes() and isinstance(out, Image.Image)


def test_swapping_the_two_main_colours_trades_their_hues():
    im = figure((200, 60, 60))                                   # a red torso over darker red legs: both reds, so use a two-colour figure
    from PIL import ImageDraw

    d = ImageDraw.Draw(im)
    w, h = im.size
    d.rectangle([w * .30, h * .60, w * .70, h * .95], fill=(60, 60, 200))        # blue legs
    out = D.swap_main_colours(im, skin=(232, 190, 150))
    torso, legs = (int(w * .5), int(h * .45)), (int(w * .5), int(h * .8))
    t, lg = out.getpixel(torso), out.getpixel(legs)
    assert t[2] > t[0] and lg[0] > lg[2], "the red torso is blue now and the blue legs are red"


def test_a_pair_sheet_is_front_and_back_of_both_characters_downscaled():
    a = {"front": figure((200, 60, 60)), "back": figure((190, 70, 70))}
    b = {"front": figure((60, 60, 200)), "back": figure((70, 70, 190))}
    sheet = D.pair_sheet(a, b)
    assert sheet.width > sheet.height and sheet.width <= D.SERVED_WIDTH


# ------------------------------------------------------------------------------------------------------------------ the gate to the screen
def test_drills_are_locked_until_five_approved_duos_with_renders_exist(rt):
    st = D.status(rt)
    assert st["locked"] and st["approved_duos"] == 0 and st["needed"] == 5
    for i in range(4):
        p = approved_duo(rt, f"A{i}")
        store_renders(rt, p, (200, 60 + i, 60), (60, 60, 200))
    assert D.status(rt)["locked"] and D.status(rt)["approved_duos"] == 4
    approved_duo(rt, "NoRenders")                                  # an approved duo whose renders are gone cannot be a base
    st = D.status(rt)
    assert st["approved_duos"] == 5 and st["usable_duos"] == 4 and st["locked"] is True
    with pytest.raises(D.DrillError) as e:
        D.start_session(rt)
    assert e.value.code == "locked" and e.value.status == 403
    view = D.session_view(rt)
    assert view["locked"] and view["items"] == [] and view["approved_duos"] == 5


def test_a_session_has_20_items_from_5_bases_and_at_most_5_variants_of_each(rt):
    ids = seed_duos(rt, 6)
    s = D.start_session(rt, seed=7)
    assert len(s["items"]) == 20 == s["limit"]
    items = [rt.repo.kv_get(D.ITEM_PREFIX + i) for i in s["items"]]
    per_base = {}
    for it in items:
        per_base[it["base"]] = per_base.get(it["base"], 0) + 1
    assert len(per_base) >= 5 and max(per_base.values()) <= 5 and set(per_base) <= set(ids)
    kinds = [it["kind"] for it in items]
    assert kinds.count("clone") >= 2 and kinds.count("strangers") >= 2 and kinds.count("original") >= 1, "obvious anchors"
    assert set(kinds) <= set(D.VARIANT_KINDS) and not (set(kinds) & set(D.SWAP_KINDS)), "no build files, so no part swaps"
    assert len(set(kinds)) >= 5, "more than colour: recolours, role swaps and the anchors"


def test_the_bases_of_a_session_cover_the_combinations_and_colour_plans_there_are(rt):
    seed_duos(rt, 8)
    bases = D.usable_bases(rt)
    chosen = D.pick_bases(bases, 5, {}, random.Random(1))
    assert len({b.combo for b in chosen}) == 4 and len({b.colour_plans for b in chosen}) >= 4


def test_the_least_used_bases_come_first_in_the_next_session(rt):
    ids = seed_duos(rt, 8)
    first = D.start_session(rt, seed=1)
    D.end_session(rt)
    used = set(first["bases"])
    second = D.start_session(rt, seed=2)
    assert len(used) >= 5 and set(second["bases"]) & (set(ids) - used) == set(ids) - used, "every base the first round skipped is in the second"


# ------------------------------------------------------------------------------------------------------------------ blind
def test_a_served_item_is_a_picture_and_a_question_and_nothing_else(rt):
    seed_duos(rt)
    view = D.session_view(rt)
    assert view["locked"] is False and 1 <= len(view["items"]) <= D.SERVE_AHEAD
    for item in view["items"]:
        assert set(item) == {"item_id", "images", "question"} and len(item["images"]) == 1 and len(item["images"][0]) == 64
    blob = json.dumps(view["items"]).lower()
    for secret in ("recipe", "kind", "metrics", "phash", "clone", "strangers", "real_duo", "expected", "base", "hue", "swap", "original", "dreamsim", "prj_"):
        assert secret not in blob, f"the blind screen leaks {secret!r}"
    assert "prj_" not in json.dumps(view) and "metrics" not in json.dumps(view) and "recipe" not in json.dumps(view)
    assert view["session"]["total"] == 20 and view["session"]["answered"] == 0
    hidden = rt.repo.kv_get(D.ITEM_PREFIX + view["items"][0]["item_id"])
    assert hidden["metrics"] and hidden["kind"] in D.VARIANT_KINDS and hidden["base"], "the numbers exist, on the server"


def test_the_checks_are_not_run_for_the_screen_and_no_verdict_is_stored_on_the_item(rt):
    seed_duos(rt)
    before = rt.db.conn().execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    D.session_view(rt)
    assert rt.db.conn().execute("SELECT COUNT(*) FROM checks").fetchone()[0] == before


# ------------------------------------------------------------------------------------------------------------------ answers and the cap
def served_ids(rt):
    return [i["item_id"] for i in D.session_view(rt)["items"]]


def test_an_answer_is_a_drill_label_with_the_hidden_numbers_beside_it(rt):
    seed_duos(rt)
    first = served_ids(rt)[0]
    out = D.answer(rt, first, "clone", like=False)
    assert out["stored"] == 2 and out["session"]["answered"] == 1
    labels = cal.list_labels(rt, source="drill")
    clone, like = labels[-2], labels[-1]
    assert (clone.kind, clone.source) == ("clone_real_stranger", "drill") and clone.value["label"] == "clone" and "phash_mean" in clone.value["metrics"]
    assert (like.kind, like.value) == ("like_dislike", {"liked": False}) and clone.subject_ids[0] == first
    with pytest.raises(D.DrillError) as e:
        D.answer(rt, first, "real_duo")
    assert e.value.code == "already_answered"
    with pytest.raises(D.DrillError) as e:
        D.answer(rt, served_ids(rt)[0], "banana")
    assert e.value.code == "bad_label" and e.value.status == 422
    with pytest.raises(D.DrillError) as e:
        D.answer(rt, "dri_unknown", "clone")
    assert e.value.code == "unknown_item" and e.value.status == 404


def test_skipping_stores_nothing_and_does_not_count_toward_the_cap(rt):
    seed_duos(rt)
    first = served_ids(rt)[0]
    before = cal.label_counts(rt).drill
    out = D.answer(rt, first, "skip")
    assert out["stored"] == 0 and cal.label_counts(rt).drill == before and out["session"]["skipped"] == 1
    assert first not in served_ids(rt), "a skipped item does not come back"


def test_the_session_ends_after_its_items_and_a_new_one_starts_on_the_next_request(rt):
    seed_duos(rt, gate_labels=400)
    first_session = D.session_view(rt)["session"]["id"]
    for _ in range(20):
        v = D.session_view(rt)
        if not v["items"]:
            break
        D.answer(rt, v["items"][0]["item_id"], "real_duo")
    assert cal.label_counts(rt).by_kind["clone_real_stranger"] == 20
    assert D.current_session(rt) is None, "20 answers close the round"
    v = D.session_view(rt)
    assert v["session"]["id"] != first_session and v["session"]["answered"] == 0 and v["items"]


def test_drill_answers_stop_at_a_quarter_of_the_calibration_set(rt):
    seed_duos(rt, gate_labels=12)                        # 12 real labels allow 4 drill labels (4 of 16 = 25%)
    assert cal.label_counts(rt).drill_room == 4
    s = D.session_view(rt)
    assert s["session"]["total"] == 4, "no more is planned than the cap allows"
    for _ in range(4):
        D.answer(rt, D.session_view(rt)["items"][0]["item_id"], "clone")
    c = cal.label_counts(rt)
    assert c.drill == 4 and c.drill_share == pytest.approx(0.25) and c.effective_drill_share <= 0.25 + 1e-9
    v = D.session_view(rt)
    assert v["items"] == [] and v.get("cap_reached") is True and "quarter" in v["message"]
    assert D.status(rt)["cap_reached"] is True
    with pytest.raises(D.DrillError) as e:
        D.start_session(rt)
    assert e.value.code == "cap_reached"


def test_an_answer_that_would_pass_the_cap_is_refused(rt):
    seed_duos(rt, gate_labels=3)                         # room for exactly one drill label
    ids = served_ids(rt)
    D.answer(rt, ids[0], "clone")
    assert cal.label_counts(rt).drill_room == 0
    # a second round-trip is impossible too: the session closed itself at the cap
    with pytest.raises(D.DrillError) as e:
        D.answer(rt, ids[-1], "clone")
    assert e.value.code in ("cap_reached", "session_closed", "already_answered")
    assert cal.label_counts(rt).drill == 1


def test_a_like_that_would_pass_the_cap_is_not_stored_but_the_answer_is(rt):
    seed_duos(rt, gate_labels=3)
    out = D.answer(rt, served_ids(rt)[0], "real_duo", like=True)
    assert out["stored"] == 1 and cal.label_counts(rt).drill == 1


def test_anchor_agreement_tells_how_attentive_the_answers_were(rt):
    seed_duos(rt, gate_labels=300)
    for _ in range(20):
        v = D.session_view(rt)
        if not v["items"]:
            break
        it = rt.repo.kv_get(D.ITEM_PREFIX + v["items"][0]["item_id"])
        D.answer(rt, it["id"], it["expected"] or "real_duo")        # an honest person calls the anchors right
    a = D.anchor_agreement(rt)
    assert a["anchors"] >= 5 and a["rate"] == 1.0
    lazy = rt.repo.kv_get(D.ITEM_PREFIX + D.session_view(rt)["items"][0]["item_id"])
    wrong = "real_duo" if lazy["expected"] != "real_duo" else "clone"
    if lazy["expected"]:
        D.answer(rt, lazy["id"], wrong)
        assert D.anchor_agreement(rt)["rate"] < 1.0


# ------------------------------------------------------------------------------------------------------------------ isolation
def test_drill_isolation(rt):
    """Drill pictures and answers never enter the registries, the taste profile, the planner's memory or the critic's inputs."""
    from duoskin.pipeline import registries

    seed_duos(rt, gate_labels=300)
    for i in range(3):
        approved_duo(rt, f"Memory {i}", spec=spec_dict(pair_structure="mirror"), minutes=100 + i)
    before = {"taste": PL.taste_tables(rt), "used": BR.recently_used(rt), "cards": BR.recent_cards(rt), "approved": BR.approved_specs(rt),
              "structures": BR.structure_shares(rt)}
    tables_before = {t: rt.db.conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("registry_face", "registry_print", "specs", "dna_cards", "gates")}
    shas = []
    for _ in range(5):
        v = D.session_view(rt)
        it = v["items"][0]
        shas.append(it["images"][0])
        D.answer(rt, it["item_id"], "clone", like=True)
    assert PL.taste_tables(rt) == before["taste"] and BR.recently_used(rt) == before["used"] and BR.recent_cards(rt) == before["cards"]
    assert BR.approved_specs(rt) == before["approved"] and BR.structure_shares(rt) == before["structures"]
    assert tables_before == {t: rt.db.conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables_before}
    # every drill picture is tagged on its link, in its provenance and in its own bytes
    for sha in shas:
        assert cal.is_isolated_asset(rt, sha) == "drill"
        assert rt.cas.get_asset(sha).first_provenance.stream == "drill"
        links = rt.repo.list_links()
        assert [lk for lk in links if lk.asset_sha == sha and lk.provenance.stream == "drill" and lk.project_id is None]
        assert b"duoskin-stream" in rt.cas.get(sha)
    with pytest.raises(cal.DrillIsolationError):
        cal.assert_not_drill(rt, shas)
    pipeline_sha = rt.repo.kv_get(f"duo:{cal.approved_duo_ids(rt)[0]}")["renders"]["a.front"]
    cal.assert_not_drill(rt, [pipeline_sha])                       # a pipeline asset passes
    # the registries refuse a drill asset whatever the caller says
    assert registries.register(rt, cal.approved_duo_ids(rt)[0], "print", shas[0], duo_seq=1) is None
    assert rt.db.conn().execute("SELECT COUNT(*) FROM registry_print").fetchone()[0] == 0


def test_drill_labels_never_feed_the_gate_statistics_or_the_report_of_checks(rt):
    seed_duos(rt, gate_labels=300)
    D.answer(rt, served_ids(rt)[0], "real_duo", like=True)
    assert cal.stat_rows(rt) == []
    assert cal.weekly_report(rt)["per_check"] == []


# ------------------------------------------------------------------------------------------------------------------ part swaps (re-dressed through the real renderer)
def test_part_swaps_dress_b_with_something_of_a_through_the_duo_renderer(rt, tmp_path, demo_inventory):
    from PIL import ImageChops

    p = dressed_duo(rt, tmp_path, "Dressed")
    base = D.load_base(rt, p.id)
    assert base is not None and base.swappable == {"swap_print", "swap_face", "swap_hair", "swap_accessory"}
    rng = random.Random(3)
    original = base.views["b"]["front"]
    for kind in D.SWAP_KINDS:
        v = D.make_variant(rt, kind, base, rng)
        assert v.a is base.views["a"] and set(v.b) == {"front", "back"} and v.detail == {"swapped": kind.removeprefix("swap_")}
        assert v.b["front"].size == original.size, "the same camera and scale as the duo render"
        assert ImageChops.difference(v.b["front"].convert("RGB"), original.convert("RGB")).getbbox() is not None, f"{kind} changes B's picture"
    # a swap of nothing changes nothing: B dressed with its own parts is the stored render
    own = D.dress_variant(rt, base, "original", tmp_path / "own")
    assert ImageChops.difference(own["front"].convert("RGB"), original.convert("RGB")).getbbox() is None, "the renderer reproduces the stored render"


def test_a_duo_without_build_files_cannot_be_part_swapped(rt):
    p = approved_duo(rt, "Plain")
    store_renders(rt, p, (200, 60, 60), (60, 60, 200))
    base = D.load_base(rt, p.id)
    assert base.swappable == set()
    with pytest.raises(D.DrillError) as e:
        D.make_variant(rt, "swap_hair", base, random.Random(1))
    assert e.value.code == "not_swappable"


def test_a_session_over_duos_with_build_files_offers_the_part_swaps(rt, tmp_path, demo_inventory):
    import dataclasses

    p = dressed_duo(rt, tmp_path, "Dressed")
    base = D.load_base(rt, p.id)
    bases = [dataclasses.replace(base, project_id=f"prj_{i}") for i in range(5)]
    plan = D.plan_recipes(bases, 20, 5, random.Random(5), have_second_duo=True)
    kinds = {k for _, k in plan}
    assert len(plan) == 20 and set(D.SWAP_KINDS) <= kinds, "hair, accessory, print and face swaps are in the round, with the recolours and anchors"
    assert max(sum(1 for b, _ in plan if b is x) for x in bases) <= 5


def test_a_swapped_item_is_made_when_it_is_served_and_is_still_blind(rt, tmp_path, demo_inventory):
    ids = [dressed_duo(rt, tmp_path, "Dressed 0").id]
    for i in range(1, 5):
        p = approved_duo(rt, f"Plain {i}", minutes=i)
        store_renders(rt, p, (200 - 20 * i, 60, 60), (60, 60 + 20 * i, 200))
        ids.append(p.id)
    seed_labels(rt, gate=200)
    s = D.start_session(rt, seed=11)
    kinds = [rt.repo.kv_get(D.ITEM_PREFIX + i)["kind"] for i in s["items"]]
    swaps = [i for i, k in zip(s["items"], kinds, strict=True) if k in D.SWAP_KINDS]
    assert swaps, "the one dressed duo contributes part swaps"
    item = rt.repo.kv_get(D.ITEM_PREFIX + swaps[0])
    assert item["state"] == "planned", "nothing is drawn until it is served"
    made = D.next_items(rt, {**s, "items": swaps[:1]}, 1)
    assert made[0]["state"] == "ready" and cal.is_isolated_asset(rt, made[0]["image"]) == "drill"
    assert set(D.served(made[0])) == {"item_id", "images", "question"}
