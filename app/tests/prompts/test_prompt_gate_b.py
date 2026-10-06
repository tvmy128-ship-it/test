"""checks/gate_b.py: the yes/no rule library and the call wrapper (bible §2.9, §7.2, §16.1; FAILURE_MODES VLM-01..VLM-07).

No network: the provider is a callable that this test file supplies.
"""
from __future__ import annotations

import json
import re

import pytest

from duoskin.checks import gate_b as GB
from duoskin.checks import policy
from duoskin.models import llm_io

RULES_WITH_SLOTS = {r: d for r, d in llm_io.rule_library().items() if d.slots}
PLAIN_RULES = [r for r, d in llm_io.rule_library().items() if not d.slots and r not in GB.IP_RULES]


class Judge:
    """A mock provider: answers from ``plan`` (rule id -> verdict), records every request, and can misbehave on chosen attempts."""

    def __init__(self, plan=None, *, default="pass", bad_attempts=(), wrong_ids=False, as_type="model", location="none"):
        self.plan, self.default, self.bad_attempts, self.wrong_ids, self.as_type, self.location = plan or {}, default, set(bad_attempts), wrong_ids, as_type, location
        self.requests: list[GB.GateBRequest] = []

    def __call__(self, req: GB.GateBRequest):
        self.requests.append(req)
        if req.attempt in self.bad_attempts:
            return "this is not json"
        ids = list(req.rule_ids)
        if self.wrong_ids:
            ids = ids[:-1] + ["ip_no_text" if ids[-1] != "ip_no_text" else "ip_no_brand"] if len(ids) > 1 else ["ip_no_text" if ids[0] != "ip_no_text" else "ip_no_brand"]
        answer = {"verdicts": [{"rule_id": r, "observation": f"seen for {r}", "verdict": self.plan.get(r, self.default),
                                "location": self.location if self.plan.get(r, self.default) != "pass" else "none"} for r in ids]}
        if self.as_type == "dict":
            return answer
        if self.as_type == "json":
            return json.dumps(answer)
        if self.as_type == "bytes":
            return json.dumps(answer).encode()
        return llm_io.AssetCheck.model_validate(answer)


def reqs(*rule_ids, **slots):
    return [GB.RuleRequest(r, slots.get(r, {})) for r in rule_ids]


# ---------------------------------------------------------------------------------------------- the library
def test_the_library_has_65_rules_with_stable_ids_and_a_closed_enum():
    assert len(GB.ALL_RULE_IDS) == 65 and "ip_no_text" in GB.ALL_RULE_IDS
    assert GB.is_hard("ip_no_brand") and not GB.is_hard("cn_restraint")
    with pytest.raises(GB.GateBError):
        GB.rule("not_a_rule")


def test_only_the_statement_is_sent_and_it_is_a_yes_no_sentence():
    for rid in GB.ALL_RULE_IDS:
        text = GB.rule(rid).statement
        assert text.strip() and text.rstrip().endswith((".", '"')) and "pass" not in text.lower().split(), rid


def test_slots_are_filled_by_name_and_a_missing_one_raises():
    rid = "fp_shape_word"
    assert rid in RULES_WITH_SLOTS
    text = GB.statement(rid, grammar_phrase="a tall oval", part="iris")
    assert "a tall oval" in text and "iris" in text and "{" not in text
    with pytest.raises(GB.GateBError, match="slot"):
        GB.statement(rid, grammar_phrase="a tall oval")
    with pytest.raises(GB.GateBError, match="slot"):
        GB.statement(rid, grammar_phrase="  ", part="iris")


def test_a_rule_with_slots_in_a_request_without_values_fails_loudly():
    with pytest.raises(GB.GateBError):
        GB.rules_block([GB.RuleRequest("fp_shape_word")])


def test_change_aware_statements_exist_only_for_the_five_matches_concept_rules():
    for rid in GB.MATCHES_CONCEPT_RULES:
        slots = {s.replace(" ", "_"): "x" for s in GB.rule(rid).slots}
        text = GB.change_aware_statement(rid, "Make the jacket teal.", **slots)
        assert text.endswith("in everything except this requested change: Make the jacket teal.")
    with pytest.raises(GB.GateBError):
        GB.change_aware_statement("ip_no_text", "Make it teal.")
    with pytest.raises(GB.GateBError):
        GB.change_aware_statement("hr_matches_concept", "  ")


def test_rules_block_is_numbered_rule_id_then_statement():
    block = GB.rules_block(reqs("ip_no_text", "ip_no_brand"))
    lines = block.splitlines()
    assert lines[0] == f"1. ip_no_text: {GB.rule('ip_no_text').statement}" and lines[1].startswith("2. ip_no_brand: ")


# ---------------------------------------------------------------------------------------------- grouping
def test_the_default_is_one_rule_per_call():
    assert GB.plan_calls(["pr_single_graphic", "pr_flat_front", "pr_readable_small"]) == [["pr_single_graphic"], ["pr_flat_front"], ["pr_readable_small"]]


def test_ip_rules_never_share_a_call_and_other_rules_group_up_to_five():
    rules = ["ip_no_brand", "ip_no_text", *PLAIN_RULES[:7]]
    calls = GB.plan_calls(rules, rules_per_call=5)
    assert calls[0] == ["ip_no_brand", "ip_no_text"] and [len(c) for c in calls[1:]] == [5, 2]
    assert all(not (set(c) & set(GB.IP_RULES) and set(c) - set(GB.IP_RULES)) for c in calls)


@pytest.mark.parametrize("n", [0, 6, -1])
def test_rules_per_call_is_one_to_five(n):
    if n == 0:
        assert GB.plan_calls(["ip_no_text"], rules_per_call=0) == [["ip_no_text"]]       # 0 means "the default"
    else:
        with pytest.raises(GB.GateBError):
            GB.plan_calls(["ip_no_text"], rules_per_call=n)


def test_duplicate_rules_in_one_run_are_rejected():
    with pytest.raises(GB.GateBError):
        GB.run_gate_b(Judge(), [GB.RuleRequest("ip_no_text"), GB.RuleRequest("ip_no_text")])


# ---------------------------------------------------------------------------------------------- the call wrapper
def test_one_rule_per_call_and_each_request_carries_the_compiled_prompt():
    judge = Judge()
    rules = ["pr_single_graphic", "pr_flat_front", "ip_no_text"]
    out = GB.run_gate_b(judge, reqs(*rules), measured_facts="size 1024x1024", context={"images": ["a.png"]})
    assert out.calls == 3 and [r.rule_ids for r in judge.requests] == [("ip_no_text",), ("pr_single_graphic",), ("pr_flat_front",)]
    for r in judge.requests:
        assert r.prompt.template_id == "L11.asset_checker" and r.prompt.schema_name == "AssetCheck"
        assert "size 1024x1024" in r.prompt.user_text and r.rule_ids[0] in r.prompt.user_text
        assert r.context == {"images": ["a.png"]} and r.attempt == 0 and r.vote == 0
        assert len(re.findall(r"\b\d+\. [a-z_]+: ", r.prompt.user_text)) == 1
    assert out.passed and len(out.results) == 3 and out.escalate == []


def test_results_take_their_kind_from_the_registry_not_from_the_wrapper():
    out = GB.run_gate_b(Judge(), reqs("ip_no_text", "cn_restraint"), subject_sha="abc")
    for r in out.results:
        assert r.kind == policy.meta(r.check_id).kind and r.subject_sha == "abc" and r.ran and r.passed
    assert {r.check_id: r.kind for r in out.results} == {"ip_no_text": "hard", "cn_restraint": "soft"}


def test_a_failed_hard_rule_fails_the_asset_and_a_failed_soft_rule_only_warns():
    out = GB.run_gate_b(Judge({"ip_no_text": "fail"}), reqs("ip_no_text", "pr_single_graphic"))
    assert not out.passed and out.failed_rules() == ["ip_no_text"] and out.failed_rules(hard_only=True) == ["ip_no_text"]
    soft = GB.run_gate_b(Judge({"cn_restraint": "fail"}), reqs("cn_restraint", "ip_no_text"))
    assert soft.passed and soft.failed_rules() == ["cn_restraint"] and soft.failed_rules(hard_only=True) == []


def test_unsure_counts_as_a_fail_and_an_unsure_ip_rule_goes_to_l13():
    out = GB.run_gate_b(Judge({"ip_no_brand": "unsure", "pr_flat_front": "unsure"}), reqs("ip_no_brand", "pr_flat_front"))
    assert not out.passed and set(out.failed_rules()) == {"ip_no_brand", "pr_flat_front"}
    assert out.escalate == ["ip_no_brand"], "only an unsure ip_* rule is escalated to L13"


def test_the_failing_location_is_part_of_the_evidence():
    out = GB.run_gate_b(Judge({"pr_flat_front": "fail"}, location="top_left"), reqs("pr_flat_front"))
    assert "fail [top_left]: seen for pr_flat_front" in out.results[0].evidence
    assert out.verdicts["pr_flat_front"][0].location == "top_left"


@pytest.mark.parametrize("kind", ["model", "dict", "json", "bytes"])
def test_the_provider_may_return_a_model_dict_or_json(kind):
    assert GB.run_gate_b(Judge(as_type=kind), reqs("ip_no_text")).passed


def test_an_unparsable_answer_is_retried_once_then_it_passes():
    judge = Judge(bad_attempts={0})
    out = GB.run_gate_b(judge, reqs("ip_no_text"))
    assert out.passed and [r.attempt for r in judge.requests] == [0, 1] and out.calls == 1


def test_two_unparsable_answers_fail_closed():
    judge = Judge(bad_attempts={0, 1})
    out = GB.run_gate_b(judge, reqs("ip_no_text"))
    r = out.results[0]
    assert not out.passed and not r.ran and not r.passed and r.kind == "hard" and len(judge.requests) == 2


def test_a_wrong_rule_id_set_is_a_failed_check_after_one_retry():
    judge = Judge(wrong_ids=True)
    out = GB.run_gate_b(judge, reqs("pr_single_graphic"))
    assert not out.results[0].ran and len(judge.requests) == 2 and "not the requested" in out.results[0].evidence


def test_a_missing_or_extra_or_repeated_verdict_is_rejected():
    class Short(Judge):
        def __call__(self, req):
            self.requests.append(req)
            return {"verdicts": []}

    out = GB.run_gate_b(Short(), reqs("ip_no_text"))
    assert not out.results[0].ran

    class Twice(Judge):
        def __call__(self, req):
            self.requests.append(req)
            v = {"rule_id": "ip_no_text", "observation": "x", "verdict": "pass", "location": "none"}
            return {"verdicts": [v, v]}

    assert not GB.run_gate_b(Twice(), reqs("ip_no_text")).results[0].ran


def test_a_provider_that_returns_garbage_types_fails_closed():
    out = GB.run_gate_b(lambda req: 42, reqs("ip_no_text"))
    assert not out.results[0].ran and not out.passed


def test_five_rules_in_one_call_are_checked_against_the_set():
    rules = PLAIN_RULES[:5]
    judge = Judge()
    out = GB.run_gate_b(judge, reqs(*rules), rules_per_call=5)
    assert out.calls == 1 and set(judge.requests[0].rule_ids) == set(rules) and out.passed
    short = GB.run_gate_b(Judge(wrong_ids=True), reqs(*rules), rules_per_call=5)
    assert all(not r.ran for r in short.results)


def test_code_measurable_rules_never_reach_the_judge(monkeypatch):
    doc = GB._rules_doc()
    monkeypatch.setattr(GB, "_rules_doc", lambda: {**doc, "code_measurable": ["pr_readable_small"]})
    judge = Judge()
    out = GB.run_gate_b(judge, reqs("pr_readable_small", "ip_no_text"))
    r = next(x for x in out.results if x.check_id == "pr_readable_small")
    assert not r.ran and "VLM-05" in r.evidence and [q.rule_ids for q in judge.requests] == [("ip_no_text",)]


# ---------------------------------------------------------------------------------------------- votes
def test_votes_repeat_the_call_and_a_majority_of_three_passes():
    class Flaky(Judge):
        def __call__(self, req):
            self.plan = {"pr_single_graphic": "fail" if req.vote == 0 else "pass"}
            return super().__call__(req)

    judge = Flaky()
    out = GB.run_gate_b(judge, reqs("pr_single_graphic"), votes=3)
    assert out.calls == 3 and [q.vote for q in judge.requests] == [0, 1, 2]
    assert out.passed and out.results[0].value == pytest.approx(2 / 3)


def test_automatic_approval_needs_every_vote():
    class Flaky(Judge):
        def __call__(self, req):
            self.plan = {"pr_single_graphic": "fail" if req.vote == 0 else "pass"}
            return super().__call__(req)

    assert not GB.run_gate_b(Flaky(), reqs("pr_single_graphic"), votes=3, unanimous=True).passed
    assert GB.run_gate_b(Judge(), reqs("pr_single_graphic"), votes=3, unanimous=True).passed
    with pytest.raises(GB.GateBError):
        GB.run_gate_b(Judge(), reqs("pr_single_graphic"), votes=0)


def test_a_vote_with_no_valid_answer_fails_that_rule_closed():
    class Sometimes(Judge):
        def __call__(self, req):
            if req.vote == 2:
                return "garbage"
            return super().__call__(req)

    out = GB.run_gate_b(Sometimes(), reqs("pr_single_graphic"), votes=3)
    assert not out.results[0].ran and not out.passed


def test_run_rule_is_one_rule_one_call():
    judge = Judge()
    r = GB.run_rule(judge, GB.RuleRequest("ip_no_text"))
    assert r.passed and len(judge.requests) == 1


def test_locations_are_the_closed_repair_mask_seed_values():
    assert "none" in GB.locations() and len(set(GB.locations())) == len(GB.locations())


def test_the_wrapper_reads_no_key_and_does_no_network_io():
    import inspect

    src = inspect.getsource(GB)
    for word in ("requests.", "httpx", "urllib", "anthropic", "os.environ", "API_KEY"):
        assert word not in src, word
