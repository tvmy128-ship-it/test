"""No literal numbers in the check code of track S (FAILURE_MODES §4 "No literal numbers in check code").

Same scan as ``tests/checks/test_chk_no_literals.py``, applied to the modules this track owns: every number a check uses is read from
``checks/thresholds.py`` (through ``prompts/limits.py``, which marks the few fallbacks) or from ``data/*.json``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "duoskin"
SCOPE = ["checks/gate_b.py", "checks/taste.py", "checks/plan_rules.py", "prompts/compiler.py", "prompts/dna_router.py", "prompts/freetext.py"]
SMALL = {0, 1, 2, -1}
UNIT_ALLOW = {255}


def offenders(path: Path) -> list[tuple[int, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            exempt.update(id(c) for c in ast.walk(node.slice))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
            and id(n) not in exempt and n.value not in SMALL and n.value not in UNIT_ALLOW]


@pytest.mark.parametrize("rel", SCOPE)
def test_no_literal_numbers(rel):
    bad = offenders(PKG / rel)
    assert not bad, f"{rel}: numeric literals that belong in checks/thresholds.py or data/*.json: {bad}"


def test_the_scan_catches_a_literal(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("def f(v):\n    return v < 0.12 and v[3] == 1 and v > 7\n", encoding="utf-8")
    assert [v for _, v in offenders(p)] == [0.12, 7]


def test_every_fallback_is_marked_and_listed():
    from duoskin.prompts import limits

    for key, (value, status, fm_ids) in limits.FALLBACKS.items():
        assert status in ("SPEC", "DES", "UNV", "DEC") and fm_ids and value is not None, key
    assert set(limits.missing_keys()) <= set(limits.FALLBACKS)
