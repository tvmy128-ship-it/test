"""MockLLM: valid structured output for any schema, role registry, heuristics, faults (APP_SPEC 7.8, 16)."""
from __future__ import annotations

import json
import random
import re
import sys
import types
from typing import Literal

import pytest
from prov_helpers import png_bytes
from pydantic import BaseModel, Field, field_validator

from duoskin.providers.anthropic_llm import BatchItem
from duoskin.providers.base import CallCtx, ProviderError
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock import llm as L

CTX = CallCtx.null()
SYS = [{"type": "text", "text": "You are a careful designer.", "cache_control": {"type": "ephemeral"}}]


def content(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


class Colour(BaseModel):
    id: str = Field(pattern=r"^p[0-9]{1,2}$")
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class Inner(BaseModel):
    name: str = Field(max_length=20)
    count: int = Field(ge=2, le=9)
    ratio: float = Field(ge=0.1, le=0.9)
    flag: bool
    kind: Literal["alpha", "beta"]


class Spec(BaseModel):
    title: str = Field(min_length=3, max_length=30)
    colours: list[Colour] = Field(min_length=3, max_length=6)
    inner: Inner
    inners: list[Inner] = Field(min_length=2, max_length=2)
    note: str | None = None
    tags: list[str] = Field(min_length=1)


@pytest.fixture(autouse=True)
def _roles():
    L.reload_roles()
    saved = dict(L._ROLES)
    L._ROLES.clear()
    yield
    L._ROLES.clear()
    L._ROLES.update(saved)
    L.reload_roles()


def call(m, route="L3_planner", out=Spec, text="make a plan", **kw):
    return m.call(route, system=kw.pop("system", SYS), content=content(text), out=out, ctx=CTX, prompt_version=2, **kw)


def test_any_schema_gets_a_valid_object_and_the_result_looks_like_the_real_one():
    m = L.MockLLM()
    r = call(m)
    assert isinstance(r.parsed, Spec) and 3 <= len(r.parsed.colours) <= 6 and len(r.parsed.inners) == 2
    assert r.stop_reason == "end_turn" and json.loads(r.raw_text)["title"] and r.requested_model == r.served_model == "claude-opus-5"
    assert not r.fallback_used and r.thinking_summary and r.request_id.startswith("mock-claude-") and r.prompt_version == 2 and len(r.schema_hash) == 64
    assert r.cost["provider"] == "mock" and r.cost["usd"] > 0 and m.costs == [r.cost] and r.route == "L3_planner"
    assert set(r.usage) >= {"input", "output", "cache_read_input_tokens", "cache_creation_input_tokens"}


def test_deterministic_from_the_canonical_request():
    a, b = call(L.MockLLM(), text="same"), call(L.MockLLM(), text="same")
    assert a.raw_text == b.raw_text and a.request_id == b.request_id
    assert call(L.MockLLM(), text="different").request_id != a.request_id
    assert call(L.MockLLM(), text="same", route="L6_reviser").request_id != a.request_id


def test_every_route_is_served_with_the_pinned_models():
    m = L.MockLLM()
    for route in m.routes:
        if route == "smoke":
            continue
        r = call(m, route=route, out=Inner)
        assert r.requested_model == m.routes[route].model
    assert m.routes["L3_planner"].model == "claude-opus-5" and m.routes["L11_checker"].model == "claude-sonnet-5"


def test_with_pins_and_unknown_route():
    class Pins:
        planner = critic = judge = "claude-opus-5-2"
        checker = "claude-sonnet-5-2"

    assert call(L.MockLLM().with_pins(Pins())).requested_model == "claude-opus-5-2"
    with pytest.raises(ProviderError) as ei:
        call(L.MockLLM(), route="L99")
    assert ei.value.kind == "bad_request"


def test_preflight_matches_the_real_adapter_for_images():
    m = L.MockLLM()
    from duoskin.providers.anthropic_llm import image_block
    with pytest.raises(ProviderError) as ei:
        m.call("L11_checker", system=SYS, content=[image_block(png_bytes(100, 100)), *content("x")], out=Inner, ctx=CTX, prompt_version=1)
    assert ei.value.code == "image_too_small"
    assert m.call("L11_checker", system=SYS, content=[image_block(png_bytes(300, 300)), *content("x")], out=Inner, ctx=CTX, prompt_version=1).parsed


def test_thinking_progress_is_streamed_and_ctx_is_ticked():
    prog, beats = [], []
    ctx = CallCtx(progress=lambda f, m: prog.append((f, m)), heartbeat=lambda: beats.append(1))
    L.MockLLM().call("L3_planner", system=SYS, content=content("x"), out=Inner, ctx=ctx, prompt_version=1)
    assert len(prog) >= 3 and prog[0][1] == "Claude is thinking" and len(beats) >= 3


def test_second_call_with_the_same_system_block_reads_the_cache():
    m = L.MockLLM()
    a, b = call(m, text="one"), call(m, text="two")
    assert a.usage["cache_creation_input_tokens"] > 0 and a.usage["cache_read_input_tokens"] == 0
    assert b.usage["cache_read_input_tokens"] == a.usage["cache_creation_input_tokens"] and b.usage["cache_creation_input_tokens"] == 0
    assert m.monitor.alerts == []
    c = call(m, text="three", system=[{"type": "text", "text": "No cache control here."}])
    assert c.usage["cache_read_input_tokens"] == 0 and c.usage["cache_creation_input_tokens"] == 0


# ----- role registry -----------------------------------------------------------------------------------------------------

def test_register_role_by_route_and_by_schema_class_name():
    def plan_builder(c: L.RoleCall):
        return Spec(title="From builder", colours=[Colour(id=f"p{i}", hex="#112233") for i in range(3)],
                    inner=Inner(name="n", count=2, ratio=0.5, flag=True, kind="alpha"), inners=[Inner(name="a", count=2, ratio=0.5, flag=True, kind="beta")] * 2,
                    tags=[c.brief[:5]])

    L.register_role("L3_planner", plan_builder)
    r = call(L.MockLLM(), text="<user_brief>hello world</user_brief>")
    assert r.parsed.title == "From builder" and r.parsed.tags == ["hello"]
    assert call(L.MockLLM(), route="L6_reviser").parsed.title != "From builder"          # only the registered route
    L.register_role("Inner", lambda c: {"name": "by class", "count": 3, "ratio": 0.4, "flag": False, "kind": "beta"})
    assert call(L.MockLLM(), route="L7_change", out=Inner).parsed.name == "by class"
    assert {"Inner", "L3_planner"} <= set(L.registered_roles())
    L.unregister_role("L3_planner")
    assert call(L.MockLLM()).parsed.title != "From builder"


def test_builders_may_return_dicts_json_text_or_start_from_the_generic_answer():
    L.register_role("L4_critic", lambda c: json.dumps({"name": "text", "count": 4, "ratio": 0.2, "flag": True, "kind": "alpha"}))
    assert call(L.MockLLM(), route="L4_critic", out=Inner).parsed.name == "text"
    L.register_role("L5_pairwise", lambda c: c.fabricate(name="patched"))
    assert call(L.MockLLM(), route="L5_pairwise", out=Inner).parsed.name == "patched"


def test_roles_are_loaded_lazily_from_optional_modules(monkeypatch):
    mod = types.ModuleType("duoskin_test_mock_roles")
    mod.register_mock_roles = lambda register: register("L3_planner", lambda c: {"name": "lazy", "count": 5, "ratio": 0.5, "flag": True, "kind": "alpha"})
    monkeypatch.setitem(sys.modules, "duoskin_test_mock_roles", mod)
    monkeypatch.setattr(L, "LAZY_ROLE_MODULES", ("duoskin_test_mock_roles", "duoskin_does_not_exist"))
    L.reload_roles()
    assert "L3_planner" not in L._ROLES                       # nothing imported yet
    assert call(L.MockLLM(), out=Inner).parsed.name == "lazy"
    broken = types.ModuleType("duoskin_broken_roles")
    broken.register_mock_roles = lambda register: 1 / 0
    monkeypatch.setitem(sys.modules, "duoskin_broken_roles", broken)
    monkeypatch.setattr(L, "LAZY_ROLE_MODULES", ("duoskin_broken_roles",))
    L.reload_roles()
    with pytest.raises(ZeroDivisionError):                     # a module that is importable but whose hook fails is a real bug: surfaced
        L.registered_roles()


def test_fixture_planset_is_used_when_the_brief_names_it(tmp_path, monkeypatch):
    spec = {"title": "From fixture", "colours": [{"id": f"p{i}", "hex": "#0A0B0C"} for i in range(3)],
            "inner": {"name": "x", "count": 2, "ratio": 0.5, "flag": True, "kind": "alpha"},
            "inners": [{"name": "y", "count": 2, "ratio": 0.5, "flag": True, "kind": "beta"}] * 2, "note": None, "tags": ["f"]}
    (tmp_path / "pastel_pair.json").write_text(json.dumps(spec), encoding="utf-8")
    (tmp_path / "broken.json").write_text('{"title": 1}', encoding="utf-8")
    monkeypatch.setattr(L, "FIXTURE_DIR", tmp_path)
    m = L.MockLLM()
    assert call(m, text="<user_brief>Please do the Pastel_Pair brief</user_brief>").parsed.title == "From fixture"
    other = call(m, text="a brief about something else")
    assert other.parsed.title != "From fixture"
    assert call(m, text="use broken please").parsed.title != 1 and any("broken.json" in n for n in m.notes)
    assert call(m, route="L6_reviser", text="pastel_pair").parsed.title != "From fixture"        # only the planner route uses plansets


def test_unsatisfiable_schema_reports_how_to_register_a_builder():
    class Odd(BaseModel):
        x: int

        @field_validator("x")
        @classmethod
        def _never(cls, v):
            raise ValueError("never valid")

    with pytest.raises(ProviderError) as ei:
        call(L.MockLLM(), out=Odd)
    assert ei.value.code == "mock_cannot_fabricate" and "register_role" in ei.value.user_message


# ----- heuristics ---------------------------------------------------------------------------------------------------------------

class Check(BaseModel):
    rule_id: Literal["fp_round", "fp_one_part", "logo_none", "txt_none", "unused_rule"]
    observation: str
    verdict: Literal["pass", "fail", "unsure"]


class Verdicts(BaseModel):
    checks: list[Check] = Field(min_length=1, max_length=5)


class FreeCheck(BaseModel):
    rule_id: str
    evidence: str
    passed: bool


class FreeVerdicts(BaseModel):
    checks: list[FreeCheck]


def test_rule_verdicts_answer_exactly_the_requested_rules_with_pass():
    m = L.MockLLM()
    r = call(m, route="L11_checker", out=Verdicts, text="<rules>\n- txt_none: no text\n- fp_round: round\n- logo_none: no logo\n</rules>")
    assert [c.rule_id for c in r.parsed.checks] == ["txt_none", "fp_round", "logo_none"] and all(c.verdict == "pass" for c in r.parsed.checks)
    f = call(m, route="L11_checker", out=FreeVerdicts, text="Rules to judge:\n- shape_ok: it is a shape\n- colour_ok: right colour")
    assert [c.rule_id for c in f.parsed.checks] == ["shape_ok", "colour_ok"] and all(c.passed for c in f.parsed.checks)
    g = call(m, route="L11_checker", out=FreeVerdicts, text='<rule rule_id="aa_one"/> <rule rule_id="bb_two"/>')
    assert [c.rule_id for c in g.parsed.checks] == ["aa_one", "bb_two"]


def test_fail_rule_fault_fails_the_first_rule_only():
    m = L.MockLLM(faults=FaultInjector("anthropic:fail_rule@L11"))
    r = call(m, route="L11_checker", out=Verdicts, text="- fp_round: a\n- logo_none: b")
    assert [c.verdict for c in r.parsed.checks] == ["fail", "pass"]
    r2 = call(m, route="L11_checker", out=FreeVerdicts, text="- aa_x: a\n- bb_y: b")
    assert [c.passed for c in r2.parsed.checks] == [True, True]            # once


class Pair(BaseModel):
    winner: Literal["first", "second", "tie"]
    score: int = Field(ge=1, le=5)
    level: float = Field(ge=0, le=1)
    overall: bool


def test_pairwise_depends_on_the_order_so_both_agreement_and_ties_occur():
    m = L.MockLLM()
    outcomes = set()
    for i in range(30):
        a = call(m, route="L5_pairwise", out=Pair, text=f"<first>cand {i}a</first><second>cand {i}b</second>").parsed
        b = call(m, route="L5_pairwise", out=Pair, text=f"<first>cand {i}b</first><second>cand {i}a</second>").parsed
        a_wins = a.winner == "first"
        b_wins_a = b.winner == "second"
        outcomes.add("agree" if a_wins == b_wins_a else "tie")
        assert a.score == 4 and a.level == pytest.approx(0.7) and a.overall is True            # fixed levels
    assert outcomes == {"agree", "tie"}


class Change(BaseModel):
    status: Literal["needs_clarification", "ready"]
    note: str = Field(max_length=50)


def test_change_requests_need_clarification_unless_colour_or_size():
    m = L.MockLLM()
    def status(text):
        return call(m, route="L7_change", out=Change, text=f"<user_change_request>{text}</user_change_request>").parsed.status
    assert status("make the jacket teal") == "ready" and status("Make it bigger") == "ready"
    assert status("make it feel more premium") == "needs_clarification" and status("do something") == "needs_clarification"


def test_pattern_sampler():
    rng = random.Random(3)
    for pat in (r"^#[0-9A-Fa-f]{6}$", r"^p[0-9]{1,2}$", r"^(a|b)\.(face|hair)$", r"^[a-z0-9][a-z0-9_-]{0,40}$", r"^(duo|(a|b)\.(face|hair|shirt))$",
                r"^[^@\s]+$", r"^\d{4}-\d{2}$", r"^x+y*z?$"):
        s = L.sample_pattern(pat, rng)
        assert s is not None and re.fullmatch(pat, s), pat
    assert L.sample_pattern(r"(?=lookahead)x", rng) is None


def test_fabricate_handles_refs_unions_allof_and_formats():
    schema = {"$defs": {"Leaf": {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}, "when": {"type": "string", "format": "date-time"}}}},
              "type": "object",
              "properties": {"leaf": {"$ref": "#/$defs/Leaf"}, "maybe": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 5, "maximum": 5}]},
                             "both": {"allOf": [{"type": "object", "properties": {"a": {"type": "integer"}}}, {"type": "object", "properties": {"b": {"const": "k"}}}]},
                             "e": {"enum": ["only"]}, "u": {"type": "string", "format": "uri"}, "m": {"type": "string", "format": "email"},
                             "tree": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Tree"}}}},
              }
    schema["$defs"]["Tree"] = {"type": "object", "properties": {"child": {"$ref": "#/$defs/Tree"}, "items": {"type": "array", "items": {"$ref": "#/$defs/Tree"}}}}
    obj = L.fabricate_from_schema(schema, random.Random(1))
    assert obj["maybe"] == 5 and obj["both"] == {"a": 1 if isinstance(obj["both"]["a"], int) else 0, "b": "k"} and obj["e"] == "only"
    assert re.fullmatch(r"[0-9a-f-]{36}", obj["leaf"]["id"]) and obj["leaf"]["when"].endswith("Z") and obj["u"].startswith("https://")


# ----- faults -------------------------------------------------------------------------------------------------------------------

def test_refusal_fault_is_the_real_refusal_with_cost():
    m = L.MockLLM(faults=FaultInjector("anthropic:refusal@L13"))
    assert call(m, route="L12_duo_judge", out=Inner).parsed
    with pytest.raises(ProviderError) as ei:
        call(m, route="L13_ip", out=Inner)
    e = ei.value
    assert e.kind == "refusal" and e.group == "refusal" and not e.retryable and e.billed == "yes" and e.cost["provider"] == "mock"
    assert len(m.costs) == 2


@pytest.mark.parametrize(("fault", "code"), [("truncated", "max_tokens"), ("context_exceeded", "model_context_window_exceeded")])
def test_truncation_faults(fault, code):
    m = L.MockLLM(faults=FaultInjector(f"anthropic:{fault}@L3"))
    with pytest.raises(ProviderError) as ei:
        call(m)
    assert ei.value.kind == "truncated" and ei.value.code == code and ei.value.retryable and ei.value.cost
    assert call(m).parsed                                                              # once


def test_schema_invalid_fault_is_a_real_validation_error_with_the_raw_text():
    m = L.MockLLM(faults=FaultInjector("anthropic:schema_invalid@L3,anthropic:no_text@L4"))
    with pytest.raises(ProviderError) as ei:
        call(m)
    assert ei.value.kind == "validation" and ei.value.context["raw_text"] and ei.value.billed == "yes"
    with pytest.raises(ProviderError) as e2:
        call(m, route="L4_critic")
    assert e2.value.kind == "truncated" and e2.value.code == "no_text"


@pytest.mark.parametrize(("fault", "kind"), [("rate_limit", "rate_limit"), ("overloaded", "overloaded"), ("auth", "auth"), ("billing", "billing"),
                                              ("schema_too_complex", "schema_too_complex"), ("timeout", "timeout"), ("server", "server")])
def test_transport_level_faults(fault, kind):
    with pytest.raises(ProviderError) as ei:
        call(L.MockLLM(faults=FaultInjector(f"anthropic:{fault}@first")))
    assert ei.value.kind == kind


# ----- files, fan-out, batches, capabilities ------------------------------------------------------------------------------------------

def test_files_fanout_capabilities_smoke():
    m = L.MockLLM()
    fid = m.upload_file(b"abc", name="a.png", mime="image/png")
    assert fid.startswith("mock-file-") and fid == m.upload_file(b"abc", name="a.png", mime="image/png")
    m.delete_file(fid)
    res = m.call_fanout("L11_checker", [{"system": SYS, "content": content(f"x{i}"), "out": Inner, "prompt_version": 1} for i in range(3)], ctx=CTX)
    assert len(res) == 3 and all(isinstance(r, L.LLMResult) for r in res)
    m2 = L.MockLLM(faults=FaultInjector("anthropic:auth@n2"))
    res2 = m2.call_fanout("L11_checker", [{"system": SYS, "content": content(f"x{i}"), "out": Inner, "prompt_version": 1} for i in range(3)], ctx=CTX)
    assert isinstance(res2[1], ProviderError)
    assert set(m.capabilities()) == {"claude-opus-5", "claude-sonnet-5"} and m.allowed_fallbacks("claude-opus-5") and m.allowed_fallbacks("claude-sonnet-5") == []
    ok = m.smoke_test("L3_planner", Spec)
    assert ok.passed and ok.ran and ok.check_id == "CHK-S10"


def test_batches_end_after_one_poll_and_flag_failures_for_rerun():
    m = L.MockLLM(faults=FaultInjector("anthropic:refusal@n2"))
    items = [BatchItem(f"i{k}", "L11_checker", SYS, content(f"x{k}"), Inner) for k in range(3)]
    bid = m.batch_submit(items)
    assert m.batch_status(bid) == "in_progress" and m.batch_status(bid) == "ended"
    res = {r.custom_id: r for r in m.batch_results(bid)}
    assert res["i0"].status == "succeeded" and res["i0"].cost["batch"] is True and res["i0"].parsed.name
    assert res["i1"].status == "refused" and res["i1"].rerun and res["i2"].status == "succeeded"


# ----- the real role schemas (models/llm_io.py, models/spec.py) -------------------------------------------------------------------

def _llm_io():
    return pytest.importorskip("duoskin.models.llm_io")


def test_every_role_schema_gets_a_valid_answer_from_the_generic_fallback():
    llm_io = _llm_io()
    m = L.MockLLM()
    for route, cls in llm_io.ROLE_SCHEMAS.items():
        if route == "L3":
            continue                                                  # the planner has its own builder (below)
        r = m.call("L11_checker", system=SYS, content=content("<rules>\n- fp_round: x\n</rules> make the jacket teal"), out=cls, ctx=CTX, prompt_version=1)
        assert isinstance(r.parsed, cls), route


def plan(brief: str, **kw):
    llm_io = _llm_io()
    m = kw.pop("m", None) or L.MockLLM()
    return m.call("L3_planner", system=SYS, content=content(f"<user_brief>{brief}</user_brief>"), out=llm_io.ROLE_SCHEMAS["L3"], ctx=CTX, prompt_version=1).parsed


def test_planner_builds_three_valid_specs_with_three_structures_and_one_wildcard():
    from duoskin.models.spec_rules import plan_set_problems
    for brief in ("a teal and gold duo", "two girls at a night market", "boy and girl who run a bakery"):
        p = plan(brief)
        assert len(p.specs) == 3 and plan_set_problems(p) == []
        assert len({s.world.pair_structure for s in p.specs}) == 3 and sum(s.is_wildcard for s in p.specs) == 1
        wild = next(s for s in p.specs if s.is_wildcard)
        others = [s for s in p.specs if not s.is_wildcard]
        assert all(wild.world.palette_family != o.world.palette_family for o in others)           # the wildcard departs in palette family
        assert all(wild.shared_anchors[0].kind != o.shared_anchors[0].kind for o in others)         # ... and anchor kind
        assert len({s.world.palette_family for s in p.specs}) == 3


def test_planner_follows_the_brief_for_combo_colours_and_named_structure():
    p = plan("same club, boy and girl, teal and gold")
    assert {s.world.pair_structure for s in p.specs} == {"same_club"} and all(s.combo == "bg" for s in p.specs)
    assert plan("two girls").specs[0].combo == "gg" and plan("gb pair").specs[0].combo == "gb"
    non_wild = next(s for s in p.specs if not s.is_wildcard)
    assert {"teal", "gold"} & {c.name for c in non_wild.palette}
    assert sum(1 for s in p.specs if s.is_wildcard) == 1


def test_planner_specs_have_the_differences_c1_looks_for():
    for s in plan("a duo").specs:
        assert len(s.shared_anchors) == 2 and len(s.contrasts) >= 5 and len({c.axis for c in s.contrasts}) == len(s.contrasts)
        assert all(c.a_value != c.b_value for c in s.contrasts)
        fa, fb = s.a.face.model_dump(), s.b.face.model_dump()
        assert sum(fa[k] != fb[k] for k in fa if not k.endswith("_ref")) >= 3                    # faces differ in at least 3 fields
        da, db = s.a.dna.model_dump(), s.b.dna.model_dump()
        assert sum(da[k] != db[k] for k in da) >= 2
        assert s.a.top.recipe_id != s.b.top.recipe_id
        assert s.a.hair.kit_style_id == s.b.hair.kit_style_id == "hair_custom" and any(c.axis == "hair_shape" for c in s.contrasts)
        assert 5 <= len(s.palette) <= 12 and len({c.id for c in s.palette}) == len(s.palette)


def test_planner_picks_garment_attributes_the_recipe_can_draw():
    from duoskin.models import kitenums
    inv = kitenums.current_inventory()
    for s in plan("cut check").specs:
        for ch in (s.a, s.b):
            r = inv.recipe(ch.top.recipe_id)
            for attr in ("sleeve", "hem", "neckline", "front", "block_layout"):
                allowed = r.cut.get(attr)
                assert not allowed or getattr(ch.top, attr) in allowed, (ch.top.recipe_id, attr)
            assert (ch.top.front == "closed") == (ch.top.inner_recipe_id == "none")
            rb = inv.recipe(ch.bottom.recipe_id)
            assert not rb.cut.get("leg") or ch.bottom.leg in rb.cut["leg"]


def test_planner_is_deterministic_and_reads_must_include_lines():
    m1, m2 = L.MockLLM(), L.MockLLM()
    brief = "a duo <must_include>\n- a plush pet on the shoulder\n- matching keychains\n</must_include>"
    a, b = plan(brief, m=m1), plan(brief, m=m2)
    assert a.model_dump() == b.model_dump()
    assert [c.text for c in a.brief_constraints] == ["a plush pet on the shoulder", "matching keychains"]
    assert plan("a different duo").model_dump() != a.model_dump()


def test_the_planner_builder_is_registered_lazily_for_the_schema_not_the_route():
    _llm_io()
    L.reload_roles()
    assert "PlanSet" in L.registered_roles() and "L3_planner" not in L.registered_roles()
    assert call(L.MockLLM()).parsed                              # the L3 route with another schema still gets the generic answer
