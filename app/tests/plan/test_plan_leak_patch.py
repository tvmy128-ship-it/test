"""Structure-keyed partner-only colours (A_LEAK / dj_no_leak, APP_SPEC §3.3, §2 S20) and the patch-scope assert (CHK-G0-11)."""
from __future__ import annotations

import planfix as F
import pytest

from duoskin.checks import plan_rules as P
from duoskin.models.llm_io import ChangeOp, RevisionOp


def spec(name="spec_complement_gb", mutate=None):
    d = F.load(name)
    if mutate:
        mutate(d)
    return F.spec_of(d)


def hexes(spec_, *roles):
    return {P.role_hex(spec_, r) for r in roles}


# ---------------------------------------------------------------------------------------------- leak sets
def test_complement_partner_mains_are_partner_only():
    s = spec()
    a, b = P.leak_sets(s, "a"), P.leak_sets(s, "b")
    assert P.role_hex(s, "b_main") in a.partner_only_hex and P.role_hex(s, "a_main") in b.partner_only_hex
    assert a.own_hex and a.shared_hex == [P.role_hex(s, "shared")] and a.exempt_roles == []


def test_a_colour_the_character_also_wears_is_never_partner_only():
    def m(d):
        for c in d["palette"]:
            if c["role"] == "b_second":
                c["hex"] = next(x["hex"] for x in d["palette"] if x["role"] == "a_second")     # B's second is A's second
    s = spec(mutate=m)
    assert P.role_hex(s, "b_second") not in P.leak_sets(s, "a").partner_only_hex
    assert P.role_hex(s, "b_main") in P.leak_sets(s, "a").partner_only_hex


def test_a_shared_anchor_colour_is_never_partner_only():
    def m(d):
        F.set_hex(d, "b_second", next(x["hex"] for x in d["palette"] if x["role"] == "shared"))
    s = spec(mutate=m)
    assert P.role_hex(s, "b_second") not in P.leak_sets(s, "a").partner_only_hex


def test_same_club_shares_a_main_by_design():
    def m(d):
        F.set_hex(d, "b_main", "#E07B39")                          # different from A's main, shared by policy
    s = spec("spec_same_club_bb", m)
    assert s.world.pair_structure == "same_club"
    club = P.leak_sets(s, "a")
    assert P.role_hex(s, "b_main") not in club.partner_only_hex and "b_main" in club.exempt_roles
    as_complement = P.leak_sets(s, "a", structure="complement")
    assert P.role_hex(s, "b_main") in as_complement.partner_only_hex, "a complement pair that leaks the partner's main still fails"


def test_same_club_still_tests_the_other_roles():
    def m(d):
        F.set_hex(d, "b_second", "#E91E8C")
    s = spec("spec_same_club_bb", m)
    assert P.role_hex(s, "b_second") in P.leak_sets(s, "a").partner_only_hex


def _swapped(d):
    """a_second is roughly B's main and b_second roughly A's main: close, but more than the leak distance apart."""
    F.set_hex(d, "a_main", "#2D5BD0")
    F.set_hex(d, "b_second", "#5C8AEE")
    F.set_hex(d, "b_main", "#E8A0C0")
    F.set_hex(d, "a_second", "#E69AB8")


def test_mirror_swaps_roles_by_design():
    s = spec("spec_mirror_gg", _swapped)
    assert s.world.pair_structure == "mirror"
    mirror = P.leak_sets(s, "a")
    assert set(mirror.exempt_roles) == {"a_main", "b_second", "a_second", "b_main"}
    assert P.role_hex(s, "b_second") not in mirror.partner_only_hex and P.role_hex(s, "b_main") not in mirror.partner_only_hex
    assert P.role_hex(s, "hair_b") in mirror.partner_only_hex, "only the colours shared by design are exempt; B's hair is still a leak"
    as_complement = P.leak_sets(s, "a", structure="complement")
    assert P.role_hex(s, "b_second") in as_complement.partner_only_hex


def test_the_leak_threshold_is_the_registry_value():
    from duoskin.prompts.limits import thr

    assert thr("con.partner_only_de") == 12


def test_leak_sets_rejects_an_unknown_character():
    with pytest.raises(ValueError):
        P.leak_sets(spec(), "c")


def test_every_structure_has_a_leak_policy_row():
    for name in ("complement", "leader_chaotic", "same_club", "mirror", "seasonal_twins", "object_mascot", "other"):
        pol = P.profile(name)["leak_policy"]
        assert pol["mode"] in ("standard", "shared_by_design", "role_swap") and pol["text"]


# ---------------------------------------------------------------------------------------------- patch scope
def rev(path, finding="1"):
    return RevisionOp(op="replace", path=path, value_json="\"p3\"", finding=finding)


def chg(path):
    return ChangeOp(op="replace", path=path, value_json="\"p3\"", reason="the user asked for it")


def test_reviser_ops_must_sit_at_or_below_a_finding_path():
    ok = [rev("/a/top/base_ref"), rev("/a/top/prints/0/motif")]
    assert P.patch_scope_problems(ok, finding_paths=["/a/top/base_ref", "/a/top/prints"]) == []
    bad = P.patch_scope_problems([rev("/b/top/base_ref")], finding_paths=["/a/top"])
    assert len(bad) == 1 and "not named by a finding" in bad[0]


def test_a_path_prefix_is_not_a_parent():
    assert P.patch_scope_problems([rev("/a/topper")], finding_paths=["/a/top"])


def test_the_reviser_may_touch_only_the_palette_entry_a_finding_references():
    ids = ["p1", "p2", "p3"]
    ok = P.patch_scope_problems([rev("/palette/2/hex")], finding_paths=["/a/top/base_ref"], palette_ids=ids, referenced_palette_ids=["p3"])
    assert ok == []
    bad = P.patch_scope_problems([rev("/palette/0/hex")], finding_paths=["/a/top/base_ref"], palette_ids=ids, referenced_palette_ids=["p3"])
    assert bad and "not named by a finding" in bad[0]
    assert P.patch_scope_problems([rev("/palette/9/hex")], finding_paths=["/x"], palette_ids=ids, referenced_palette_ids=["p3"])


def test_palette_ids_stay_stable_for_both_roles():
    ids = ["p1", "p2", "p3"]
    for ops, kw in (([rev("/palette/2/id")], {"finding_paths": ["/palette"]}), ([chg("/palette/2/id")], {})):
        bad = P.patch_scope_problems(ops, palette_ids=ids, referenced_palette_ids=["p3"], **kw)
        assert bad and "palette ids stay stable" in bad[0]


@pytest.mark.parametrize("path", ["/combo", "/is_wildcard", "/a/presentation", "/b/presentation"])
def test_change_requests_may_not_touch_combo_wildcard_or_presentation(path):
    bad = P.patch_scope_problems([chg(path)])
    assert bad and "may not be changed" in bad[0]


def test_change_requests_may_touch_anything_else_including_palette_hexes():
    ops = [chg("/b/top/base_ref"), chg("/palette/3/hex"), chg("/world/theme")]
    assert P.patch_scope_problems(ops) == []


def test_an_empty_patch_is_in_scope():
    assert P.patch_scope_problems([], finding_paths=[]) == [] and P.patch_scope_problems([]) == []
