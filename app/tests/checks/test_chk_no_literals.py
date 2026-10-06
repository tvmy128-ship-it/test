"""No literal numbers in check code (FAILURE_MODES §4 "No literal numbers in check code").

Parses the check modules with ``ast``: every numeric literal other than 0, 1, -1, 2, array indexes and slice bounds, and the unit
conversions in ``UNIT_ALLOW`` must be read from ``checks/thresholds.py`` (``tv(key)``). ``thresholds.py`` itself is the registry and is
exempt. This is the imaging track's copy of the rule for the modules it owns; ``tests/unit/test_no_literal_thresholds.py`` runs the same
scan over the whole scope.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "duoskin"
SCOPE = ["checks/runner.py", "checks/policy.py", "checks/model.py", "imaging/checks.py", "imaging/similarity.py"]
SMALL = {0, 1, 2, -1}
UNIT_ALLOW = {255}          # 8-bit full scale (an image-format unit, not a threshold)


def _offenders(path: Path) -> list[tuple[int, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):                      # array indexes and slice bounds
            exempt.update(id(c) for c in ast.walk(node.slice))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if id(node) in exempt or node.value in SMALL or node.value in UNIT_ALLOW:
                continue
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("rel", SCOPE)
def test_no_literal_numbers(rel):
    bad = _offenders(PKG / rel)
    assert not bad, f"{rel}: numeric literals that belong in checks/thresholds.py: {bad}"


def test_the_scan_catches_a_literal(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("def f(v):\n    return v < 0.12 and v[3] == 1 and v > 7\n", encoding="utf-8")
    assert [v for _, v in _offenders(p)] == [0.12, 7]


def test_thresholds_py_holds_the_numbers():
    assert _offenders(PKG / "checks/thresholds.py")          # the registry is where the literals live
