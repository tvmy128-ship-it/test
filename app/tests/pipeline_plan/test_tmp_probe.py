import json
import pytest
from test_mock_roles import inv, plan, lint_clean
from duoskin.pipeline import lint as LI

@pytest.mark.parametrize("brief", ["two friends at a lantern festival", "two friends sharing a rainy-day picnic", "a koi pond duo", "space explorers", "a duo"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_probe(inv, brief, seed):
    out = plan(seed=seed, brief_text=brief)
    b = LI.lint_candidates([(f"s{i}", s) for i, s in enumerate(out["specs"])], LI.lint_context(None), how_they_differ=out["how_they_differ"])
    bad = {}
    for sid in b.order:
        hf = b.hard_findings(sid)
        if hf: bad[sid] = [(f.check_id, f.rule, f.message[:120]) for f in hf]
    assert not bad and b.set_clean, (brief, seed, bad, [f.message for f in b.global_findings][:3])
