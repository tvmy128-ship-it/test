"""Roblox upload fees, publishing advances and creator requirements, with the wording the app may show about them.

Every figure here comes from the creator-docs checkout of 2026-09-26 (commit ``0b817b5cd955a63936bebf7b90746e34488af079``):

* ``content/en-us/marketplace/marketplace-fees-and-commissions.md``: the 80 Robux upload fee per submission (500 with an
  emissive mask), "in general, upload fees are not refunded if an item is rejected through moderation", and the table of
  non-limited publishing advances (refundable: Roblox pays it back from the item's sales).
* ``content/en-us/marketplace/marketplace-policy.md#creator-requirements``: ID verification (or a linked parental account
  with ID verification) to upload; 2-step verification and Roblox Plus or Premium 1000/2200 to publish and to keep on sale.
* ``content/en-us/avatar/classic-clothing.md``: classic Shirt / Pants / T-shirt are uploaded in the Creator Dashboard for the
  same 80 Robux per submission.

Roblox changes prices ("This payment depends on a range of market-based factors and may change over time"), so **every text
this module produces ends with** :data:`PRICE_NOTE`; no figure may be shown to the user without it. The functions are pure.
"""
from __future__ import annotations

DOCS_COMMIT = "0b817b5cd955a63936bebf7b90746e34488af079"
DOCS_DATE = "2026-09-26"
PRICE_NOTE = "Figures are from Roblox's creator-docs of 2026-09-26: check Roblox for current prices."

UPLOAD_FEE_ROBUX = 80                  # DOC: per submission, every avatar asset type
UPLOAD_FEE_EMISSIVE_ROBUX = 500        # DOC: items that use an emissive mask (this app never makes one)

#: DOC: "Non-limited publishing advance" column of the table in marketplace-fees-and-commissions.md. The ``Classic ...`` rows are
#: the 2D classic clothing rows (the table also has 3D ``Shirt`` = 600 and ``Pants`` = 600 rows, which this app does not make).
PUBLISHING_ADVANCE_NON_LIMITED: dict[str, int] = {
    "Shirt": 10, "Pants": 10, "TShirt": 10,
    "Hat": 1500, "Face": 1500, "Hair": 1000, "Neck": 1000, "Shoulder": 1000, "Front": 1000, "Back": 1000, "Waist": 1000,
    "Head": 1500, "Body": 2500,
}

#: DOC: marketplace-policy.md#creator-requirements
REQ_UPLOAD = "ID verification (your own government ID or a linked parental account with ID verification) is needed to upload"
REQ_PUBLISH = ("To put an item on sale later you also need 2-step verification, a Roblox Plus or Premium 1000/2200 membership "
               "and a refundable publishing advance")

CLASSIC_TYPES = ("Shirt", "Pants", "TShirt")


def publishing_advance(item_type: str) -> int | None:
    """The non-limited publishing advance in Robux for an item type (``Shirt``, ``Pants``, ``Hair`` ... ``Head``, ``Body``), or None."""
    return PUBLISHING_ADVANCE_NON_LIMITED.get(item_type)


def fee_note(item_type: str) -> str:
    """One self-contained sentence-group for the checklist ("Fee:" line): upload fee, refund rule, the publishing advance for this
    type and the price disclaimer. ``"Accessory"`` gives the generic text for all rigid accessory types. Never states a figure
    without :data:`PRICE_NOTE`."""
    parts = [f"{UPLOAD_FEE_ROBUX} Robux upload fee per submission, generally not refunded if moderation rejects the item"]
    if item_type not in CLASSIC_TYPES:
        parts.append(f"{UPLOAD_FEE_EMISSIVE_ROBUX} with an emissive mask, which this app never uses")
    adv = publishing_advance(item_type)
    if item_type == "Accessory":
        parts.append("putting it on sale later needs a refundable publishing advance (non-limited: Hat and Face "
                     f"{PUBLISHING_ADVANCE_NON_LIMITED['Hat']} Robux; Hair, Neck, Shoulder, Front, Back and Waist {PUBLISHING_ADVANCE_NON_LIMITED['Hair']} Robux)")
    elif adv is not None:
        label = "a classic clothing item" if item_type in CLASSIC_TYPES else f"the {item_type} type"
        parts.append(f"putting it on sale later needs a refundable publishing advance ({adv} Robux for {label}, non-limited)")
    return "; ".join(parts) + ". " + PRICE_NOTE
