"""Roblox rigid-accessory limits (FAILURE_MODES 4.2, 4.3) read from ``limits.json``.

Boxes are measured from the attachment point, in studs, in the FILE frame we export (Y up, front = +Z).
Roblox's own frame has the front at -Z, so the Z component of an offset is negated between the two.

Thresholds: the single registry ``duoskin.checks.thresholds`` is the source of truth. A few mesh-only values that the
registry does not list live in ``limits.json -> extra_thresholds`` (status DES unless noted) and are read through
:func:`threshold`, which asks the registry first.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

AssetType = str
ASSET_TYPES = ("Hat", "Hair", "Face", "Neck", "Shoulder", "Front", "Back", "Waist")
_LIMITS_PATH = Path(__file__).with_name("limits.json")


@dataclass(frozen=True)
class AccessoryBox:
    """The Classic box of one asset type on one attachment."""

    asset_type: str
    attachment: str
    size: tuple[float, float, float]            # W x H x D (X, Y, Z) in studs
    offset_file: tuple[float, float, float]     # box centre from the attachment, file frame (front = +Z)
    offset_roblox: tuple[float, float, float]   # same, Roblox frame (front = -Z)

    def lo_hi(self, attachment_pos: tuple[float, float, float] | np.ndarray = (0.0, 0.0, 0.0)) -> tuple[np.ndarray, np.ndarray]:
        """Min and max corner of the box when the attachment point sits at ``attachment_pos`` (same frame as the mesh)."""
        c = np.asarray(attachment_pos, float) + np.asarray(self.offset_file, float)
        half = np.asarray(self.size, float) / 2.0
        return c - half, c + half


@lru_cache(maxsize=1)
def load_limits() -> dict[str, Any]:
    """The parsed ``limits.json`` (cached; treat as read-only)."""
    with open(_LIMITS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def budgets() -> dict[str, Any]:
    return load_limits()["budgets"]


def normalise_attachment(name: str) -> str:
    """``"RightCollar"`` and ``"rightcollarattachment"`` both become ``"RightCollarAttachment"``."""
    n = (name or "").strip().replace(" ", "")
    if not n:
        return ""
    if not n.lower().endswith("attachment"):
        n += "Attachment"
    low = n.lower()
    for t in load_limits()["types"].values():
        for a in t["attachments"]:
            if a.lower() == low:
                return a
    return n[0].upper() + n[1:]


def attachments_for(asset_type: str) -> list[str]:
    t = load_limits()["types"].get(asset_type)
    return list(t["attachments"]) if t else []


def default_attachment(asset_type: str) -> str:
    return load_limits()["default_attachment"].get(asset_type, "")


def default_anchor(asset_type: str) -> str:
    t = load_limits()["types"].get(asset_type)
    return t.get("default_anchor", "centre") if t else "centre"


def box_for(asset_type: str, attachment: str | None = None) -> AccessoryBox:
    """The Classic box for ``asset_type`` on ``attachment`` (default attachment when omitted).

    Raises ``KeyError`` for an unknown type or for an attachment that this type does not allow (there is no wrist type).
    """
    types = load_limits()["types"]
    if asset_type not in types:
        raise KeyError(f"unknown asset type {asset_type!r}; expected one of {', '.join(ASSET_TYPES)}")
    att = normalise_attachment(attachment) if attachment else default_attachment(asset_type)
    entry = types[asset_type]["attachments"].get(att)
    if entry is None:
        raise KeyError(f"asset type {asset_type!r} cannot use attachment {att!r}; allowed: {', '.join(types[asset_type]['attachments'])}")
    return AccessoryBox(asset_type, att, tuple(map(float, entry["size"])), tuple(map(float, entry["offset_file"])),
                        tuple(map(float, entry["offset_roblox"])))


def threshold(name: str) -> Any:
    """Current value of a mesh threshold: the registry first, then ``extra_thresholds`` in ``limits.json``."""
    try:
        from duoskin.checks import thresholds as th

        return th.get(name)
    except Exception:  # noqa: BLE001  (UnknownThreshold, or the registry being unavailable)
        pass
    extra = load_limits()["extra_thresholds"]
    if name in extra:
        return extra[name][0]
    raise KeyError(f"unknown threshold {name!r}")


def describe(name: str, op: str = "") -> str:
    """Text for ``CheckResult.threshold`` such as ``"<= 3800 (mesh.tris_max, DES)"``."""
    try:
        from duoskin.checks import thresholds as th

        return th.describe(name, op)
    except Exception:  # noqa: BLE001
        pass
    extra = load_limits()["extra_thresholds"]
    if name in extra:
        value, status, _ = extra[name]
        return f"{op} {value} ({name}, {status})".strip()
    return f"{op} ? ({name})".strip()


def fm_ids(name: str) -> list[str]:
    try:
        from duoskin.checks import thresholds as th

        return th.fm_ids_of(name)
    except Exception:  # noqa: BLE001
        pass
    extra = load_limits()["extra_thresholds"]
    return list(extra[name][2]) if name in extra else []
