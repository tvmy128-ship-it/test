"""checks/policy.py and data/checks.json: hard vs soft policy (APP_SPEC §3.1)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from duoskin.checks import policy
from duoskin.checks.model import CheckResult, gate_verdict
from duoskin.checks.policy import HARD_CLASSES, NEVER_DEMOTABLE, SOFT_CLASSES, CheckMeta

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"


def _row(**kw):
    base = {"check_id": "X", "kind": "hard", "policy_class": "buildability", "stage": "gate2", "threshold_keys": [], "warn_text": "w", "demotable": False}
    base.update(kw)
    return base


def test_hard_classes_exactly_as_written():
    assert HARD_CLASSES == {"roblox", "buildability", "ip", "stray_text", "registry", "clone_lower_edge", "integrity", "security"}
    assert "consistency" in SOFT_CLASSES and "consistency" not in HARD_CLASSES


def test_policy_classes_taste_checks_are_soft():
    """test_policy_classes (APP_SPEC §17.2): every taste, colour_distance, restraint, novelty or consistency check is SOFT."""
    reg = policy.registry()
    for cls in SOFT_CLASSES:
        ids = policy.ids_in_class(cls)
        assert ids, f"no check in class {cls}"
        assert all(reg[i].kind == "soft" for i in ids), cls


def test_hard_and_assert_checks_only_in_hard_classes():
    for cid, m in policy.registry().items():
        if m.kind in ("hard", "assert"):
            assert m.policy_class in HARD_CLASSES, (cid, m.policy_class)


def test_demotion_never_touches_roblox_ip_stray_text_security_integrity():
    assert NEVER_DEMOTABLE == {"roblox", "ip", "stray_text", "security", "integrity"}
    for cid, m in policy.registry().items():
        if m.policy_class in NEVER_DEMOTABLE:
            assert not m.demotable, cid
            assert not policy.can_demote(cid)
    with pytest.raises(ValueError):
        policy.validate_overrides({"A_OCR": "soft"})
    with pytest.raises(ValueError):
        policy.validate_overrides({"A_SVG": "soft"})


def test_only_hard_checks_with_tunable_thresholds_are_demotable():
    from duoskin.checks import thresholds as TH

    for cid, m in policy.registry().items():
        if m.demotable:
            assert m.kind == "hard" and m.threshold_keys, cid
            assert all(TH.is_tunable(k) for k in m.threshold_keys), cid


def test_a_soft_check_can_never_be_marked_hard():
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="hard", policy_class="taste"))
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="assert", policy_class="novelty"))
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="hard", policy_class="consistency"))          # S24: consistency is not a hard class
    with pytest.raises(ValidationError):
        CheckMeta(**_row(requires=["Head Base"]))
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="hard", policy_class="made_up"))
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="hard", policy_class="ip", demotable=True))
    with pytest.raises(ValidationError):
        CheckMeta(**_row(kind="soft", policy_class="taste", demotable=True))


def test_overrides_can_only_demote_to_soft():
    with pytest.raises(ValueError):
        policy.validate_overrides({"A_PALETTE": "hard"})
    assert policy.validate_overrides({"A_PALETTE": "soft"}) == {"A_PALETTE": "soft"}
    with pytest.raises(ValueError):
        policy.validate_overrides({"A_MARGIN_NOT_A_CHECK": "soft"})       # unknown ids cannot be demoted either


def test_effective_kind_and_is_blocking():
    failing = CheckResult(check_id="A_PALETTE", kind="hard", passed=False)
    assert policy.is_blocking(failing)
    assert not policy.is_blocking(failing, {"A_PALETTE": "soft"})
    with policy.check_overrides({"A_PALETTE": "soft"}):
        assert policy.effective_kind("A_PALETTE") == "soft"
        assert not policy.is_blocking(failing)
    assert policy.effective_kind("A_PALETTE") == "hard"
    # a never-demotable check ignores a (hand-made) override
    ocr = CheckResult(check_id="A_OCR", kind="hard", passed=False)
    assert policy.is_blocking(ocr, {"A_OCR": "soft"})
    soft_fail = CheckResult(check_id="A_SWATCH", kind="soft", passed=False)
    assert not policy.is_blocking(soft_fail)


def test_not_applicable_and_not_run_blocking():
    from duoskin.checks.model import not_applicable, not_run

    na = not_applicable("F_WARP_IOU", "hard", "no_head_base")
    assert not policy.is_blocking(na) and na.passed and na.status == "not_applicable"
    nr = not_run("F_WARP_IOU", "hard", "boom")
    assert policy.is_blocking(nr) and not nr.passed and nr.status == "not_run"


def test_apply_policy_rekinds_results_for_gate_verdict():
    r_soft_by_registry = CheckResult(check_id="A_SWATCH", kind="hard", passed=False)       # producer lied
    r_hard = CheckResult(check_id="A_OCR", kind="soft", passed=False)                       # producer lied the other way
    out = policy.apply_policy([r_soft_by_registry, r_hard])
    assert [r.kind for r in out] == ["soft", "hard"]
    assert gate_verdict(out) == "fail"
    assert gate_verdict(policy.apply_policy([r_soft_by_registry])) == "pass"
    out2 = policy.apply_policy([CheckResult(check_id="A_PALETTE", kind="hard", passed=False)], {"A_PALETTE": "soft"})
    assert gate_verdict(out2) == "pass"


def test_unknown_ids_do_not_crash():
    m = policy.meta("TOTALLY-UNKNOWN-9")
    assert m.kind == "soft" and m.registered is False and m.policy_class == "unregistered"
    assert not policy.is_registered("TOTALLY-UNKNOWN-9")
    assert policy.effective_kind("TOTALLY-UNKNOWN-9") == "soft"
    assert policy.effective_kind("TOTALLY-UNKNOWN-9", declared="hard") == "hard"      # the producer's own declaration is kept
    r = CheckResult(check_id="TOTALLY-UNKNOWN-9", kind="hard", passed=False)
    assert policy.is_blocking(r) is True
    assert not policy.can_demote("TOTALLY-UNKNOWN-9")


def test_registry_has_no_duplicate_ids_and_valid_shape():
    data = json.loads((Path(policy._DATA)).read_text(encoding="utf-8"))
    ids = [r["check_id"] for r in data["checks"]]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 200
    for r in data["checks"]:
        assert r["stage"] in {"startup", "call", "G0", "gate1", "gate2", "build", "gate3", "export", "always"}
        assert r["warn_text"].strip()


def test_contract_ids_point_at_registered_ids():
    reg = policy.registry()
    for cid, m in reg.items():
        for other in m.contract_ids:
            assert other in reg, f"{cid} -> {other}"


def test_gate_a_rule_ids_from_the_bible_are_registered():
    for rid in ["A_SIZE", "A_ALPHA", "A_COMPONENTS", "A_MARGIN", "A_OCR", "A_GLYPH", "A_PALETTE", "A_SINGLE_COLOUR", "A_STROKE", "A_SIL_GUIDE",
                "A_SYMMETRY", "A_HIGHLIGHT", "A_GUIDE_LEFT", "A_HALO", "A_PHASH", "A_DRIFT", "A_PASTE", "A_SVG", "A_SENTINEL", "A_SWATCH",
                "A_LEAK", "A_CLONE", "A_VIEWS", "A_TEMPLATE", "A_MESH"]:
        assert policy.is_registered(rid), rid
    assert policy.meta("A_SWATCH").kind == "soft"
    assert policy.meta("A_PHASH").kind == "soft"
    assert policy.meta("A_OCR").kind == "hard" and policy.meta("A_OCR").policy_class == "stray_text"
    assert policy.meta("A_REGISTRY").policy_class == "registry"
    assert policy.meta("A_CLONE").policy_class == "clone_lower_edge"


def test_soft_decisions_from_the_issue_list_are_applied():
    """Issues #33/#51/#53: structure colour rules, colour anchors and the style-profile band are SOFT."""
    for cid in ("CHK-G0-06", "CHK-D03", "CHK-G1-10", "CHK-A16", "A_STYLE", "DUO-03", "DUO-04", "FACE-13", "PLN-15", "TASTE_RATIO"):
        assert policy.meta(cid).kind == "soft", cid
    for cid in ("A_LEAK", "CHK-G1-04", "CHK-G0-13", "CHK-G0-14", "PLN-DNA-01", "PLN-STR-01", "A_STROKE", "A_SINGLE_COLOUR", "CHK-D02"):
        assert policy.meta(cid).kind == "hard", cid


def test_v13_final_decisions():
    """APP_SPEC v1.3 / FAILURE_MODES v1.3: CHK-A13, CHK-D05, CHK-S09 and CHK-G0-10 are SOFT; finalize drift is HARD integrity."""
    for cid in ("CHK-A13", "CHK-D05", "CHK-S09", "CHK-G0-10"):
        assert policy.meta(cid).kind == "soft", cid
    assert policy.meta("CHK-A13").policy_class == "consistency" and policy.meta("CHK-D05").policy_class == "consistency"
    assert policy.meta("CHK-G0-10").policy_class == "restraint"
    for cid in ("A_DRIFT", "CHK-A10", "CHK-G1-08", "CHK-D09", "CHK-E02"):
        assert policy.meta(cid).kind == "hard" and policy.meta(cid).policy_class == "integrity", cid
        assert not policy.meta(cid).demotable
    assert policy.meta("CHK-S09").requires == ["blender_present"]
    assert policy.meta("CHK-S14").kind == "soft" and policy.meta("CHK-S15").kind == "soft"
    assert policy.meta("CHK-A17").kind == "hard"


def test_head_and_body_base_checks_declare_what_they_need():
    for cid in ("F_BLINK_IRIS", "F_WARP_IOU", "F_STRETCH", "F_NECK_SEAM", "CHK-B09", "F_LUT_KEY"):
        assert policy.meta(cid).requires == ["head_base_present"], cid
    assert policy.meta("CHK-B10").requires == ["body_base_present"]
    assert policy.meta("F_LINE_COLOURS").requires == []


@pytest.mark.skipif(not (DOCS / "FAILURE_MODES.md").exists(), reason="docs not present")
def test_every_chk_id_in_the_docs_is_registered():
    text = "\n".join((DOCS / n).read_text(encoding="utf-8") for n in ("FAILURE_MODES.md", "APP_SPEC.md"))
    ids = set(re.findall(r"\bCHK-(?:[A-Z]\d{2}|G0-\d{2}|G1-\d{2})\b", text))
    ids = {i for i in ids if i != "CHK-B"}
    assert len(ids) > 100
    missing = sorted(i for i in ids if not policy.is_registered(i))
    assert not missing, missing


@pytest.mark.skipif(not (DOCS / "PROMPT_BIBLE.md").exists(), reason="docs not present")
def test_every_gate_b_rule_in_the_bible_is_registered():
    text = (DOCS / "PROMPT_BIBLE.md").read_text(encoding="utf-8")
    sec = text.split("### 7.2 Gate B", 1)[1].split("\n## ", 1)[0]
    rules = re.findall(r"^\| ([a-z0-9]+_[a-z0-9_]+) \|", sec, flags=re.MULTILINE)
    assert len(rules) > 40
    assert [r for r in rules if not policy.is_registered(r)] == []
    assert policy.meta("ip_no_text").policy_class == "stray_text"
    assert policy.meta("dj_not_clones").policy_class == "clone_lower_edge"
