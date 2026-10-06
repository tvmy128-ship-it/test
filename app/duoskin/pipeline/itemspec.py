"""What the spec says about a hair or accessory as a Roblox item: asset type, attachment, size in studs, triangle and texture budgets.

``accessory_item(spec, part_id)`` and ``hair_item(spec, character)`` are the only places that turn the spec's words (``category``, ``attachment``,
``size_class``, ``kind``) into the numbers the 3D side needs (APP_SPEC §10.8, §10.9, bible §15). The target size is a cube of a fraction of the
Classic box (every item stays inside it by construction); the 3D worker scales the model to it (``scale_mode="fit"``) and ``mesh.validate`` checks
that every vertex is inside the box (CHK-M10).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from duoskin.pipeline import common
from duoskin.roblox import limits

ASSET_TYPE = {"hat": "Hat", "hair": "Hair", "face": "Face", "neck": "Neck", "shoulder": "Shoulder", "front": "Front", "back": "Back", "waist": "Waist"}
SIZE_FRACTION = {"small": 0.40, "medium": 0.62, "large": 0.85}
FACE_LIMIT = {"plush_pet": 3000, "bag": 3000, "small_hat": 3000, "prop": 3000, "keychain_charm": 1500, "hair_clip_slab": 1500, "sticker_slab": 1500}
HAIR_FACE_LIMIT = 3500
TRIS_ACCESSORY = 3800
TRIS_HAIR = 3600


@dataclass(frozen=True)
class Item:
    part_id: str
    character: str
    kind: str                       # plush_pet | bag | ... | hair
    build: str                      # tripo | sticker_slab | code_primitive | hair
    category: str                   # hat | hair | face | ...
    asset_type: str                 # Hat | Hair | ... (Roblox)
    attachment: str                 # RightCollarAttachment
    target_studs: tuple[float, float, float]
    tris_target: int
    texture_px: int
    face_limit: int
    size_class: str
    description: str
    colour_refs: tuple[str, ...]

    @property
    def is_hair(self) -> bool:
        return self.asset_type == "Hair" and self.kind == "hair"


def roblox_attachment(name: str, asset_type: str) -> str:
    """``right_collar`` -> ``RightCollarAttachment``; a name this type does not allow falls back to the type's default (never silently)."""
    camel = "".join(w.capitalize() for w in str(name).split("_")) + "Attachment"
    att = limits.normalise_attachment(camel)
    return att if att in limits.attachments_for(asset_type) else limits.default_attachment(asset_type)


def target_for(asset_type: str, attachment: str, size_class: str) -> tuple[float, float, float]:
    box = limits.box_for(asset_type, attachment)
    s = round(SIZE_FRACTION.get(size_class, 0.5) * min(box.size), 3)
    return (s, s, s)


def accessory_item(spec: dict[str, Any], part_id: str) -> Item:
    ref = common.split_part_id(part_id)
    acc = spec[ref.character]["accessories"][ref.index or 0]
    asset_type = ASSET_TYPE[acc["category"]]
    attachment = roblox_attachment(acc["attachment"], asset_type)
    studs = target_for(asset_type, attachment, acc["size_class"])
    kind = acc["kind"]
    return Item(part_id, ref.character, kind, acc["build"], acc["category"], asset_type, attachment, studs, TRIS_ACCESSORY,
                512 if studs[0] <= 1.6 else 1024, FACE_LIMIT.get(kind, 3000), acc["size_class"], acc.get("description", ""),
                tuple(acc.get("colour_refs", [])))


def hair_item(spec: dict[str, Any], character: str) -> Item:
    h = spec[character]["hair"]
    box = limits.box_for("Hair", "HairAttachment")
    return Item(f"{character}.hair", character, "hair", "hair", "hair", "Hair", "HairAttachment", tuple(float(x) for x in box.size),   # type: ignore[arg-type]
                TRIS_HAIR, 1024, HAIR_FACE_LIMIT, "n/a", h.get("description", ""),
                tuple(r for r in (h.get("colour_ref"), h.get("shadow_ref"), h.get("highlight_ref")) if r and r != "none"))


def item_of(spec: dict[str, Any], part_id: str) -> Item:
    ref = common.split_part_id(part_id)
    return hair_item(spec, ref.character) if ref.kind == "hair" else accessory_item(spec, part_id)
