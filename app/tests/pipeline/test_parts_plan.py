"""The part plan of an approved spec (APP_SPEC §6.5): which tiles exist, what they wait for, how they are routed."""
from __future__ import annotations

import pytest
from pfix import load_spec_dict, locked_spec

from duoskin.models.part import PartKind
from duoskin.pipeline import parts


def ids(spec, **kw):
    return [p.id for p in parts.plan_parts(spec, "prj", **kw)]


def test_fixture_plan_has_the_documented_tiles():
    got = ids(locked_spec())
    for c in "ab":
        for need in ("colours", "face", "hair", "acc.0", "print.top.0", "shirt", "pants"):
            assert f"{c}.{need}" in got
    assert got[-1] == "duo" and len(got) == len(set(got))


def test_empty_spec_has_no_accessory_or_print_tiles():
    got = ids(load_spec_dict("spec_empty_bb"))
    assert not [i for i in got if ".acc." in i or ".print." in i]


def test_shirt_waits_for_its_prints_and_pants_for_bottom_prints_and_shoes():
    spec = locked_spec()
    spec["a"]["bottom"]["shoes"]["motif"] = "tiny gear wheel"
    ps = {p.id: p for p in parts.plan_parts(spec, "prj")}
    assert ps["a.shirt"].part_deps == ["a.print.top.0"] and ps["b.shirt"].part_deps == ["b.print.top.0"]
    assert ps["a.pants"].part_deps == ["a.print.shoes.0"] and ps["b.pants"].part_deps == []


def test_head_area_accessories_wait_for_the_hair():
    spec = locked_spec()
    spec["a"]["accessories"][0].update({"category": "hat", "attachment": "hat", "kind": "small_hat"})
    ps = {p.id: p for p in parts.plan_parts(spec, "prj")}
    assert ps["a.acc.0"].part_deps == ["a.hair"] and ps["b.acc.0"].part_deps == []


@pytest.mark.parametrize("avail,route", [({"openai", "recraft", "tripo"}, "R1"), ({"openai", "tripo"}, "I3")])
def test_face_route_follows_the_keys(avail, route):
    ps = {p.id: p for p in parts.plan_parts(locked_spec(), "prj", available=avail)}
    assert ps["a.face"].route == route


def test_accessory_and_hair_routes_without_tripo():
    ps = {p.id: p for p in parts.plan_parts(locked_spec(), "prj", available={"openai", "recraft"})}
    assert ps["a.acc.0"].route == "I5+I10" and ps["a.hair"].route == "kit"
    custom = locked_spec()
    custom["a"]["hair"]["kit_style_id"] = "hair_custom"
    ps2 = {p.id: p for p in parts.plan_parts(custom, "prj", available={"openai"})}
    assert ps2["a.hair"].route == "I4+I10"


def test_labels_are_plain_words_and_every_kind_has_a_lane(rt):
    from duoskin.pipeline import parts as P

    for p in P.plan_parts(locked_spec(), "prj"):
        if p.kind != PartKind.DUO:
            assert p.label and "{" not in p.label
            assert P.lane_of(p.kind) is not None


def test_ensure_parts_is_idempotent_and_keeps_state(rt):
    from pfix import make_project

    p, rec = make_project(rt)
    first = parts.ensure_parts(rt, p.id, rec.spec)
    parts.set_part_state(rt, p.id, "a.hair", parts.PartState.READY)
    again = parts.ensure_parts(rt, p.id, rec.spec)
    assert [x.id for x in first] == [x.id for x in again]
    assert rt.repo.get_part(p.id, "a.hair").state.value == "ready"
