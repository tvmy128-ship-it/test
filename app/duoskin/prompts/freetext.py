"""The free-text lint for every model-written string before it can become a slot (bible §2.3 rule 2; PLN-12; CHK-G0-08).

A string passes when it has

* at most ``max_words`` words;
* no banned word (platform, brand, franchise, artist, age, romance and body, and the text-inviting group) as a whole word, with
  NFKC normalisation and common plural and verb endings;
* no quote character, no digit, no hex code and no letters-as-words ("the letter A", a lone ``x``);
* no negation ("no ...", "without ...", "not ..."): negations belong only in the fixed EXCLUDE line of a template.

If the lint fails, the string goes back to the reviser (L6) as a finding. Code never edits it silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from duoskin.prompts.catalog import TEXT_INVITING, Banned, norm

QUOTES = re.compile("[\"“”‘’]")
DIGITS = re.compile(r"\d")
HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
NEGATION = re.compile(r"\b(?:no|not|without|never|none|nothing|cannot|can't|don't|doesn't|isn't|aren't|won't)\b", re.IGNORECASE)
LETTER_WORDS = re.compile(r"\b(?:the\s+)?(?:letter|letters|number|numbers|digit|digits|initial|initials|monogram|alphabet)\b|"
                          r"(?<![\w-])[b-hj-zB-HJ-Z](?![\w-])", re.IGNORECASE)


@dataclass(frozen=True)
class FreeTextProblem:
    kind: str          # words | banned | quote | digit | hex | negation | letters | empty
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def free_text_problems(text: str, max_words: int | None, banned: Banned, *, allow_empty: bool = True,
                       strict: bool = True) -> list[FreeTextProblem]:
    """Every free-text lint problem of ``text`` (an empty list means it passes).

    ``strict=True`` is the full slot lint (fields that become image-prompt slots). ``strict=False`` is the lighter lint of
    FAILURE_MODES §5.3 (word cap, banned terms and quote characters) for metadata fields such as ``story`` and ``theme``.
    """
    out: list[FreeTextProblem] = []
    t = str(text)
    if not t.strip():
        return [] if allow_empty else [FreeTextProblem("empty", "a value is required")]
    n = len(t.split())
    if max_words is not None and n > max_words:
        out.append(FreeTextProblem("words", f"{n} words, at most {max_words}"))
    hits = banned.hits(t, [*banned.everywhere, TEXT_INVITING])
    if hits:
        out.append(FreeTextProblem("banned", ", ".join(hits)))
    if QUOTES.search(t):
        out.append(FreeTextProblem("quote", "quote characters are not allowed"))
    if not strict:
        return out
    if DIGITS.search(t):
        out.append(FreeTextProblem("digit", "digits are not allowed"))
    if HEX.search(t):
        out.append(FreeTextProblem("hex", "hex colour codes are not allowed"))
    neg = NEGATION.search(norm(t))
    if neg:
        out.append(FreeTextProblem("negation", f"'{neg.group(0)}': describe what is there, not what is missing"))
    letters = LETTER_WORDS.search(t)
    if letters:
        out.append(FreeTextProblem("letters", f"'{letters.group(0)}' reads like a letter or number on the artwork"))
    return out
