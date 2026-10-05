"""Threshold reader with clearly marked fallbacks (track S).

Every number a check or the prompt compiler uses comes from ``checks/thresholds.py`` (FAILURE_MODES §4.1). The imaging track
owns that file. A few keys that the FAILURE_MODES table lists (or that track S needs) are not in it yet, so :func:`thr` reads
the registry first and only then falls back to :data:`FALLBACKS` below. When a key is added to ``thresholds.py`` the registry
value wins automatically and the fallback becomes dead data (``missing_keys()`` lists the ones still in use).

Nothing here may be used to *tune* a threshold: the values are the FAILURE_MODES defaults, copied verbatim.
"""
from __future__ import annotations

from typing import Any

from duoskin.checks import thresholds as TH

# FALLBACKS: keys that FAILURE_MODES §4.1 lists but checks/thresholds.py did not have when track S was written, plus the
# track S taste constants. Format: name -> (value, status, failure-mode ids) like thresholds.T.
FALLBACKS: dict[str, tuple[Any, str, list[str]]] = {
    "pln.colour_axes_max":     ((2, 1), "SPEC", ["PLN-03", "PLN-04"]),       # (default, same_club): colour axes among the >= 5 contrasts
    "prm.must_max":            (5, "SPEC", ["PRM-01"]),                      # <= 5 constraints per image call
    "prm.dna_fields_max":      (2, "SPEC", ["PRM-01", "PRM-10"]),            # <= 2 DNA fields per image call
    "prm.max_chars_excl_style": (1500, "DES", ["PRM-01"]),                    # characters, excluding the verbatim STYLE block
    "prm.max_chars_total":     (2200, "DES", ["PRM-01"]),
    "prm.exclude_nouns_max":   (10, "DES", ["PRM-01"]),
    "pln.nearest_card_share":  (0.8, "UNV", ["PLN-16"]),                     # share of identical card fields that counts as "very close" (SOFT)
    "taste.kmeans_k":          (5, "DES", ["DUO-03"]),                       # clusters for the phone-size top colours
    "taste.top_n":             (2, "SPEC", ["DUO-03"]),                      # the planned main colour must be in the top N
    "taste.layout_ari_warn":   (0.80, "DES", ["DUO-11"]),                    # used only when thresholds.py lacks the key
    "taste.acc_min_px":        (8, "DES", ["DUO-04", "PLN-14"]),             # used only when thresholds.py lacks the key
    "taste.plan_ratio_share_off": (15, "DES", ["DUO-03"]),                  # TASTE_RATIO (SOFT): share points a built colour may be off its declared plan
}


def thr(name: str) -> Any:
    """The current value of ``name``: the threshold registry first, then :data:`FALLBACKS`."""
    try:
        return TH.get(name)
    except TH.UnknownThreshold:
        if name in FALLBACKS:
            return FALLBACKS[name][0]
        raise


def status_of(name: str) -> str:
    try:
        return TH.status_of(name)
    except TH.UnknownThreshold:
        return FALLBACKS[name][1]


def fm_ids_of(name: str) -> list[str]:
    try:
        return TH.fm_ids_of(name)
    except TH.UnknownThreshold:
        return list(FALLBACKS[name][2])


def describe(name: str, op: str = "") -> str:
    """Text for ``CheckResult.threshold`` (like ``thresholds.describe``, with the fallback)."""
    try:
        return TH.describe(name, op)
    except TH.UnknownThreshold:
        value, status, _ = FALLBACKS[name]
        return f"{op} {value} ({name}, {status}, fallback)".strip()


def missing_keys() -> list[str]:
    """Fallback keys that ``checks/thresholds.py`` still lacks (report these to the owner of that file)."""
    out = []
    for k in FALLBACKS:
        try:
            TH.get(k)
        except TH.UnknownThreshold:
            out.append(k)
    return sorted(out)


# Small constants that check modules need but that are not thresholds (so the "no literal numbers in check code" scan stays clean).
EVIDENCE_ITEMS = 4        # how many findings an evidence string lists before "(+n more)"
RGB_CHANNELS = 3          # colour channels of an RGB image
