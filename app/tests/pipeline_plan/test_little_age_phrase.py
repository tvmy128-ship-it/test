"""The word "little" is banned only where it forms an age phrase (little girl, little boy, little kid, ...), not as a size word in a fix
sentence ("a little shorter"). Every other age and minor-safety word stays banned (cleanup (d) of the plan track)."""
from __future__ import annotations

import pytest

from duoskin.prompts import freetext
from duoskin.prompts.catalog import default_ctx

FIX_WORDS = 25


@pytest.fixture(scope="module")
def banned():
    return default_ctx().banned


def problems(text: str, banned) -> list[str]:
    return [p.kind for p in freetext.free_text_problems(text, FIX_WORDS, banned, strict=True)]


@pytest.mark.parametrize("text", [
    "Make the sleeves a little shorter.",
    "Draw the bow a little larger and keep its colours.",
    "Make the jacket a little teal, keeping its shape.",
    "Shift the hair a little to the left.",
    "A little more contrast on the belt.",
    "Make the hem a little shorter and keep the pockets.",
])
def test_little_as_a_size_word_passes(text, banned):
    assert problems(text, banned) == []


@pytest.mark.parametrize("text", [
    "a little girl in a red coat", "A little boy with a backpack", "little kid costume", "the little princess dress",
    "her little sister", "a little brother", "little one outfit", "little miss sunshine", "a little man", "little guy",
])
def test_little_forming_an_age_phrase_is_banned(text, banned):
    assert "banned" in problems(text, banned)


@pytest.mark.parametrize("word", ["child", "children", "kid", "kids", "baby", "toddler", "teen", "teenager", "young", "preteen", "tween",
                                  "infant", "schoolgirl", "schoolboy", "juvenile", "youthful", "childlike", "babyface", "loli", "shota"])
def test_other_age_words_stay_banned(word, banned):
    assert "banned" in problems(f"a {word} outfit", banned)


def test_the_bare_word_is_no_longer_in_any_group(banned):
    assert "little" not in banned.all_terms()
    assert all(len(t.split()) > 1 for t in banned.all_terms() if "little" in t.split())    # only ever as part of a phrase


def test_the_age_phrases_are_whole_word_matches(banned):
    assert banned.hits("a little girl", ["age_words"]) == ["little girl"]
    assert banned.hits("a little girlish tone", ["age_words"]) == []
    assert banned.hits("a little shorter", ["age_words"]) == []
