"""A SOFT warning reaches the person as one calm sentence, never as a measurement ("blush L* 78 > 45.0; lightens tone_2").
(Found in the "Approve anyway?" dialog of the real app, tests/e2e_ui.)"""
from __future__ import annotations

import pytest

from duoskin.checks.model import CheckResult
from duoskin.pipeline import common


def test_a_known_soft_check_has_its_own_words():
    assert common.plain_warning("F_BLUSH", "blush L* 78 > 45.0; lightens tone_2; lightens tone_3") == "The blush may look pale on some of the skin tones."


def test_evidence_that_already_reads_as_a_sentence_is_kept():
    assert common.plain_warning("SOME_NEW_CHECK", "The left sleeve looks a little busy.") == "The left sleeve looks a little busy."


@pytest.mark.parametrize("evidence", ["clusters off palette: #70b5e6(13.8)", "worst dE 14.2", "lash_ref: 0.31 < 0.5", "", "snake_case_metric"])
def test_a_measurement_of_an_unknown_check_becomes_a_general_note(evidence):
    text = common.plain_warning("SOME_NEW_CHECK", evidence)
    assert text == "One of the quality checks has a small note about this part."


def test_summarize_checks_uses_plain_warning_text_and_keeps_the_hard_evidence():
    soft = CheckResult(check_id="F_BLUSH", kind="soft", passed=False, ran=True, status="failed", evidence="blush L* 78 > 45.0", metric="blush_l")
    hard = CheckResult(check_id="A_PALETTE", kind="hard", passed=False, ran=True, status="failed", evidence="clusters off palette: #70b5e6(13.8)")
    summary = common.summarize_checks([soft, hard])
    assert [w["text"] for w in summary["warnings"]] == ["The blush may look pale on some of the skin tones."]
    assert summary["hard_failures"][0]["evidence"].startswith("clusters off palette")          # the page turns it into words (checkLabel)
