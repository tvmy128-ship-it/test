"""prompts/freetext.py and the banned vocabulary (bible §2.3 rule 2, §2.4a; PLN-12, CHK-G0-08)."""
from __future__ import annotations

import json

import pytest

from duoskin.prompts import catalog, freetext
from duoskin.prompts.catalog import TEXT_INVITING, Banned, data_json, load_banned, norm

BANNED = catalog.default_ctx().banned


def problems(text, cap=None, **kw):
    return [p.kind for p in freetext.free_text_problems(text, cap, BANNED, **kw)]


# ---------------------------------------------------------------------------------------------- the vocabulary
def test_every_group_is_listed_with_terms_and_no_prose():
    groups = data_json("banned_terms.json")["groups"]
    assert set(groups) == {"ip_platform", "ip_brands", "ip_franchises", "ip_artists", "age_words", "romance", "sexual", "text_inviting"}
    for name, terms in groups.items():
        assert terms and len(terms) == len(set(terms)), name
        for t in terms:
            assert t == t.lower().strip() and len(t.split()) <= 5 and not t.endswith((".", ",")), (name, t)


def test_everywhere_groups_exclude_only_the_text_inviting_group():
    assert TEXT_INVITING not in BANNED.everywhere and set(BANNED.everywhere) == set(BANNED.groups) - {TEXT_INVITING}


@pytest.mark.parametrize("word", ["apple", "orange", "jordan", "amazon", "frozen", "minor", "bleach", "converse", "tesla", "gap"])
def test_common_words_are_not_banned(word):
    assert BANNED.hits(f"a {word} tree", [*BANNED.everywhere, TEXT_INVITING]) == [], word


@pytest.mark.parametrize("text,group", [("a nike swoosh", "ip_brands"), ("my little pony", "ip_franchises"), ("baby face", "age_words"),
                                        ("a bikini top", "sexual"), ("his boyfriend", "romance"), ("roblox logo", "ip_platform"),
                                        ("in the style of disney", "ip_artists"), ("a badge", TEXT_INVITING)])
def test_each_group_catches_its_terms(text, group):
    assert BANNED.hits(text, [group]), (text, group)


def test_matching_is_whole_word_nfkc_case_insensitive_and_takes_plurals():
    assert BANNED.hits("NIKE", ["ip_brands"]) and BANNED.hits("ｎｉｋｅ", ["ip_brands"]), "full-width letters are normalised"
    assert BANNED.hits("two logos", [TEXT_INVITING]) and BANNED.hits("lettering", [TEXT_INVITING])
    assert not BANNED.hits("a bikinipattern", ["sexual"]) and not BANNED.hits("snikers", ["ip_brands"])
    assert norm("ＡＢＣ") == "abc"


def test_multi_word_terms_match_across_any_whitespace():
    assert BANNED.hits("my   little\npony", ["ip_franchises"])


def test_a_user_file_extends_the_list_in_each_supported_shape(tmp_path):
    forms = {"list": ["zorbo"], "terms": {"terms": ["zorbo"]}, "groups": {"groups": {"mine": ["zorbo"]}}}
    for name, raw in forms.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        b = load_banned(extra_path=p)
        assert b.hits("a zorbo toy", [*b.everywhere]) == ["zorbo"], name
        assert not BANNED.hits("a zorbo toy", [*BANNED.everywhere])
    assert load_banned(extra_path=tmp_path / "missing.json").all_terms() == BANNED.all_terms()


def test_all_terms_is_sorted_and_distinct():
    t = BANNED.all_terms()
    assert t == sorted(set(t)) and len(t) > 200


# ---------------------------------------------------------------------------------------------- the free-text lint
def test_clean_text_passes_and_empty_text_depends_on_the_flag():
    assert problems("a small round hedgehog plush with soft quills", 15) == []
    assert problems("") == [] and problems("   ") == []
    assert problems("", allow_empty=False) == ["empty"]


@pytest.mark.parametrize("text,kind", [
    ("one two three four five six", "words"), ("a nike swoosh", "banned"), ("a logo", "banned"), ('a "quoted" leaf', "quote"),
    ("a “smart” leaf", "quote"), ("3 leaves", "digit"), ("a leaf #FF0000", "hex"), ("no stripes", "negation"),
    ("without a hat", "negation"), ("the letter A", "letters"), ("a big x", "letters"), ("a monogram", "letters")])
def test_each_rule_fires(text, kind):
    cap = 5 if kind == "words" else None
    assert kind in problems(text, cap)


def test_negations_belong_only_in_the_fixed_exclude_line():
    assert "negation" in problems("a hat that is not red")
    assert problems("a hat that is red") == []


def test_the_light_lint_is_for_metadata_only():
    assert problems("walks 3 miles with no hat", None, strict=False) == []
    assert "banned" in problems("pikachu fan", None, strict=False)
    assert "quote" in problems('a "story"', None, strict=False)
    assert "words" in problems("one two three", 2, strict=False)


def test_a_single_a_is_the_article_not_a_letter():
    assert problems("a leaf") == [] and problems("A leaf") == []
    assert "letters" in problems("a b c")


def test_problems_are_reported_together_not_one_at_a_time():
    kinds = problems("nike 3 leaves with no stripes", 3)
    assert {"words", "banned", "digit", "negation"} <= set(kinds)
    assert str(freetext.free_text_problems("nike", None, BANNED)[0]).startswith("banned: ")


def test_a_banned_group_hit_names_the_term_as_written():
    p = freetext.free_text_problems("a pikachu mask", None, BANNED)[0]
    assert p.kind == "banned" and "pikachu" in p.detail


def test_text_inviting_class_is_also_free_text_banned():
    assert Banned({"text_inviting": ["slogan"]}).hits("a slogan", ["text_inviting"]) == ["slogan"]
