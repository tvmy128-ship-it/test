"""Roblox limits for classic clothing (Shirt / Pants templates): the clothing counterpart of ``roblox/limits.py``.

Numbers that are shared with the checks live in the single registry (``duoskin.checks.thresholds``, keys ``tpl.*``,
``fabric.*``, ``fold.*``); geometry lives in ``template_regions.json`` (read by ``roblox.template``). This module only adds the
facts that neither of those owns (file format, size, upload fee) and accessors that read the registry, so no check hard-codes
a number. ``consistency_problems()`` compares the template geometry with the registry and is run by the unit tests and by
``duoskin doctor``.

Status markers follow FAILURE_MODES §0.2: DOC (official), DER (derived), DES (design choice), UNV (unverified).
"""
from __future__ import annotations

from typing import Any

from duoskin.checks import thresholds as TH
from duoskin.roblox import template as T

# ---- the file (DOC unless marked) -------------------------------------------------------------------------------------
TEMPLATE_SIZE: tuple[int, int] = (585, 559)                       # DOC: classic Shirt and Pants templates
TSHIRT_SIZE_HINT: tuple[int, int] = (512, 512)                    # DOC: a T-shirt is a square image such as 512x512 (not produced by v1)
FILE_FORMAT = "PNG"                                              # we only ever write PNG: JPEG loses the alpha that lets skin show through
MAX_FILE_BYTES = 4 * 1024 * 1024                                  # UNV: a conservative cap; Studio/Creator Dashboard is the final authority
SOFT_FILE_BYTES = 1 * 1024 * 1024                                 # DES: a file bigger than this is unusual for a flat template (a warning)
BIT_DEPTH = 8                                                     # DOC: "supports 8-bit alpha channels"
COLOUR_TYPE_RGBA = 6

# ---- the words printed on the official template PNG (CLO-18: none of them may appear on a shipped file) ----------------------
TEMPLATE_LABEL_WORDS: tuple[str, ...] = (
    "FRONT", "BACK", "UP", "DOWN", "TORSO", "RIGHT", "LEFT", "ARM", "LEG", "ROBLOX", "TEMPLATE", "SHIRT", "PANTS",
    "R", "L", "B", "F", "U", "D")


def find_label_words(texts: list[str] | tuple[str, ...]) -> list[str]:
    """The template label words among OCR'd ``texts`` (case-insensitive, whole words; single letters only when the text is exactly that
    letter, as on the official template's face labels)."""
    hits: list[str] = []
    for t in texts:
        words = [w for w in "".join(c if c.isalnum() else " " for c in str(t)).upper().split() if w]
        for w in words:
            if w in TEMPLATE_LABEL_WORDS and (len(w) > 1 or len(words) == 1) and w not in hits:
                hits.append(w)
    return hits


# ---- uploading (EXP-01 / EXP-02) ----------------------------------------------------------------------------------------
UPLOAD_CHANNEL = "creator_dashboard"
UPLOAD_PATH = "Creator Dashboard > Avatar Items > Classics > Upload Asset"
UPLOAD_FEE_ROBUX = 80                                              # DOC: per submission, not refunded
UPLOAD_REFUNDABLE = False
UPLOAD_NEEDS_ID_VERIFICATION = True                               # DOC
STUDIO_TEST = "Studio > Avatar tab > Character > Block Avatar rig; insert Shirt or Pants and set the template (free)"

# ---- design choices of the validators (DES; not in the registry because no gate uses them as a bar) ------------------------
SKIN_SHARE_FLAG = 0.01                    # CLO-11: warn when at least this share of garment pixels is within tpl.skin_in_clothing_de of the skin tone
WAISTBAND_HIDDEN_SHARE = 0.9              # CLO-09: share of the waistband pixels covered by an opaque shirt before we warn
MIRROR_MIN_PX = 40                        # CLO-12: a print mask smaller than this is not tested for mirroring
SPLIT_FLAG_MIN_COLUMNS = 2                # CLO-07: a split-row step must touch at least this many columns to be flagged
MIDRIFF_MAX_GAP_PX = 8                    # bible §6.2 crop_top: a bare midriff gap is at most 8 px even when the brief asks for one
SEAM_ALPHA_MISMATCH_SOFT = 0.25           # CLO-04 (composite, SOFT): share of seam pixels where only one side is garment


def seam_de_limits() -> tuple[float, float]:
    """(mean, max) CIEDE2000 across a seam on the base fabric layer (CLO-04, HARD)."""
    return float(TH.get("tpl.seam_de_mean_max")), float(TH.get("tpl.seam_de_max"))


def bleed_range_px() -> tuple[int, int]:
    """(min, max) open-side bleed in pixels (CLO-03)."""
    lo, hi = TH.get("tpl.open_side_bleed_px")
    return int(lo), int(hi)


def gap_px() -> int:
    return int(TH.get("tpl.gap_px"))


def semi_alpha_share_max() -> float:
    return float(TH.get("tpl.semi_alpha_share_max"))


def skin_in_clothing_de() -> float:
    return float(TH.get("tpl.skin_in_clothing_de"))


def split_margin_px() -> int:
    return int(TH.get("tpl.split_margin_px"))


def limb_bands() -> tuple[tuple[int, int], ...]:
    return tuple(tuple(b) for b in TH.get("tpl.limb_bands"))  # type: ignore[return-value]


def shoe_top_row_range() -> tuple[int, int]:
    a, b = TH.get("tpl.shoe_top_row_range")
    return int(a), int(b)


def bevel_inset_px() -> int:
    return int(TH.get("tpl.bevel_inset_px"))


def hidden_leg_rows() -> tuple[int, int]:
    a, b = TH.get("tpl.hidden_leg_rows")
    return int(a), int(b)


def describe() -> dict[str, Any]:
    """The clothing limits as plain data (written into the export manifest next to the creator-docs commit)."""
    return {
        "template_size": list(TEMPLATE_SIZE), "format": FILE_FORMAT, "bit_depth": BIT_DEPTH, "max_file_bytes": MAX_FILE_BYTES,
        "gap_px": gap_px(), "open_side_bleed_px": list(bleed_range_px()), "seam_de_mean_max": seam_de_limits()[0],
        "seam_de_max": seam_de_limits()[1], "semi_alpha_share_max": semi_alpha_share_max(),
        "limb_bands": [list(b) for b in limb_bands()], "shoe_top_row_range": list(shoe_top_row_range()),
        "split_rows_torso": list(TH.get("tpl.split_rows_torso")), "split_rows_limb": list(TH.get("tpl.split_rows_limb")),
        "bevel_inset_px": bevel_inset_px(), "hidden_leg_rows": list(hidden_leg_rows()),
        "upload": {"channel": UPLOAD_CHANNEL, "path": UPLOAD_PATH, "fee_robux": UPLOAD_FEE_ROBUX,
                   "refundable": UPLOAD_REFUNDABLE, "id_verification": UPLOAD_NEEDS_ID_VERIFICATION},
        "studio_test": STUDIO_TEST,
    }


def consistency_problems() -> list[str]:
    """Differences between ``template_regions.json`` and the threshold registry (empty list = they agree)."""
    bad: list[str] = []

    def eq(name: str, have: Any, want: Any) -> None:
        if have != want:
            bad.append(f"{name}: template_regions.json has {have!r}, registry has {want!r}")

    eq("tpl.size_wh", T.SIZE_WH, tuple(TH.get("tpl.size_wh")))
    eq("tpl.gap_px", T.GAP_PX, int(TH.get("tpl.gap_px")))
    eq("tpl.open_side_bleed_px", tuple(T.OPEN_SIDE_BLEED_PX), tuple(TH.get("tpl.open_side_bleed_px")))
    eq("tpl.split_rows_torso", (T.TORSO_SPLIT_ROW,), tuple(TH.get("tpl.split_rows_torso")))
    eq("tpl.split_rows_limb", tuple(T.LIMB_SPLIT_ROWS), tuple(TH.get("tpl.split_rows_limb")))
    eq("tpl.split_margin_px", T.SPLIT_MARGIN_PX, int(TH.get("tpl.split_margin_px")))
    eq("tpl.shoe_top_row_range", tuple(T.SHOE_TOP_ROW_RANGE), tuple(TH.get("tpl.shoe_top_row_range")))
    eq("tpl.limb_bands", tuple(T.LIMB_BANDS), tuple(tuple(b) for b in TH.get("tpl.limb_bands")))
    eq("tpl.bevel_inset_px", T.BEVEL_INSET_PX, int(TH.get("tpl.bevel_inset_px")))
    eq("tpl.hidden_leg_rows", tuple(T.HIDDEN_LEG_ROWS), tuple(TH.get("tpl.hidden_leg_rows")))
    eq("template size", (T.WIDTH, T.HEIGHT), TEMPLATE_SIZE)
    return bad
