"""The upload checklist, generated from the item-type table (APP_SPEC §10.13, FAILURE_MODES EXP-01 / EXP-02 / EXP-09).

There is no upload API for avatar items, so the user uploads by hand. The checklist is built **only** from ``ITEM_TYPES``
(one row per item type with its channel, fee and steps) so the channel and fee are never typed twice (EXP-01):

* Classic Shirt / Pants: Creator Dashboard (browser), 80 Robux per submission, not refunded, ID verification.
* Hair and accessories: Studio (3D Importer, Accessory Fitting Tool, UGC Validation tool, Save to Roblox).
* Head and Body: Studio.

Every paid upload line is **locked** until the free "Studio test passed" step of the same item is ticked and the final-confirmation
step is ticked (EXP-02, EXP-09): ``tick()`` refuses to tick a locked step, and un-ticking a prerequisite un-ticks what depends on it.
The functions are pure (they return new objects), so tick state can be mirrored in the database and replayed.
"""
from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from duoskin.roblox import limits_clothing as LC

StepKind = Literal["info", "test", "confirm", "upload"]
CLASSIC_TYPES = ("Shirt", "Pants")
ACCESSORY_TYPES = ("Hair", "Hat", "Face", "Neck", "Shoulder", "Front", "Back", "Waist")
ALL_TYPES = (*CLASSIC_TYPES, *ACCESSORY_TYPES, "Head", "Body")


class ChecklistError(ValueError):
    """An illegal checklist operation (unknown item or step, or a locked step)."""


@dataclass(frozen=True)
class Step:
    step_id: str
    text: str
    kind: StepKind = "info"
    requires: tuple[str, ...] = ()           # step ids of the same item that must be ticked first


@dataclass(frozen=True)
class ItemTypeRow:
    """One row of the item-type table."""
    row_id: str                              # classic | accessory | head | body
    types: tuple[str, ...]
    channel: str                             # creator_dashboard | studio
    channel_text: str
    fee_robux: int
    fee_note: str
    requirements: tuple[str, ...]
    steps: tuple[Step, ...]
    fm_ids: tuple[str, ...] = ("EXP-01", "EXP-02")


_CONFIRM = Step("confirm_final", "Confirm the Gate 3 renders, the file list and the category for this item. Items cannot be edited after "
                "upload, so a mistake is permanent.", "confirm")

ITEM_TYPE_ROWS: tuple[ItemTypeRow, ...] = (
    ItemTypeRow(
        "classic", CLASSIC_TYPES, "creator_dashboard", LC.UPLOAD_PATH, LC.UPLOAD_FEE_ROBUX,
        f"{LC.UPLOAD_FEE_ROBUX} Robux per submission, not refunded",
        ("ID verification on the Roblox account", f"{LC.TEMPLATE_SIZE[0]}x{LC.TEMPLATE_SIZE[1]} RGBA 8-bit PNG (the file in this kit)"),
        (Step("studio_test", "Free Studio test: Avatar tab > Character > Block Avatar rig; insert a Shirt or Pants object under the Rig and "
                             "set its ShirtTemplate/PantsTemplate to the PNG. Tick when it looks right on the rig.", "test"),
         _CONFIRM,
         Step("upload", f"Upload: {LC.UPLOAD_PATH}. {LC.UPLOAD_FEE_ROBUX} Robux per submission, not refunded.", "upload",
              ("studio_test", "confirm_final"))),
    ),
    ItemTypeRow(
        "accessory", ACCESSORY_TYPES, "studio", "Studio: 3D Importer > Accessory Fitting Tool > UGC Validation tool > Save to Roblox", 80,
        "80 Robux each (500 with emissive, which this app never uses)",
        ("Mesh files from this kit (.gltf + .bin + PNG; .fbx only when produced)",),
        (Step("importer_settings", "3D Importer: 'Upload to Roblox' OFF while testing; Scale Unit = Studs; World Forward = Front; World Up = "
                                   "Top; Merge Meshes OFF; Rig Scale = Default (Classic).", "info"),
         Step("aft", "Accessory Fitting Tool: set the category, the attachment and the numeric offset from fit.json.", "info"),
         Step("meshpart", "MeshPart: Material Plastic, Transparency 0, VertexColor 1,1,1, no extra objects, DoubleSided off.", "info"),
         Step("studio_test", "Run the free UGC Validation tool (and the property_check.luau script). Tick when it passes.", "test",
              ("importer_settings", "aft", "meshpart")),
         _CONFIRM,
         Step("upload", "Save to Roblox (Avatar Asset). 80 Robux each.", "upload", ("studio_test", "confirm_final"))),
    ),
    ItemTypeRow(
        "head", ("Head",), "studio", "Studio (Avatar Setup or manual)", 80, "80 Robux",
        ("Head base present and validated once (FM-T2)",),
        (Step("studio_test", "Studio's head validator on this duo's head: the 17 required FACS poses, blink, mouth, happy and sad. Do NOT let "
                             "Avatar Setup generate FACS again: it would replace the rig the app rendered.", "test"),
         _CONFIRM,
         Step("upload", "Upload the head as authored. 80 Robux.", "upload", ("studio_test", "confirm_final"))),
    ),
    ItemTypeRow(
        "body", ("Body",), "studio", "Studio", 80, "80 Robux",
        ("Body base present",),
        (Step("studio_test", "Only with a body base: check the modesty layers, the bundle contents (only hair, brow and lash accessories) "
                             "and run the body validator.", "test"),
         _CONFIRM,
         Step("upload", "Upload the body bundle. 80 Robux.", "upload", ("studio_test", "confirm_final"))),
    ),
)
ITEM_TYPES: dict[str, ItemTypeRow] = {t: row for row in ITEM_TYPE_ROWS for t in row.types}

COMMON_NOTES = (
    "Confirm the Gate 3 renders, the file list and the category per item before any upload (items cannot be edited after upload).",
    "R6 games map each classic region whole and without the R15 seams; the garments were designed to read unsplit.",
)


def item_type_row(item_type: str) -> ItemTypeRow:
    """The table row for an item type (``Shirt``, ``Pants``, ``Hair``, ``Hat`` ... ``Head``, ``Body``)."""
    try:
        return ITEM_TYPES[item_type]
    except KeyError:
        raise ChecklistError(f"unknown item type {item_type!r}; known: {list(ALL_TYPES)}") from None


@dataclass(frozen=True)
class ChecklistStep:
    step_id: str
    text: str
    kind: StepKind
    requires: tuple[str, ...]
    ticked: bool = False


@dataclass(frozen=True)
class ChecklistItem:
    item_id: str
    character: str
    type: str
    row_id: str
    channel: str
    channel_text: str
    fee_robux: int
    fee_note: str
    requirements: tuple[str, ...]
    steps: tuple[ChecklistStep, ...]
    banners: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Checklist:
    items: tuple[ChecklistItem, ...]
    banners: tuple[str, ...] = ()
    notes: tuple[str, ...] = COMMON_NOTES
    context: Mapping[str, Any] = field(default_factory=dict)      # creator-docs commit, validator defaults (EXP-03)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def build_checklist(items: Iterable[Any], *, banners: Sequence[str] = (), creator_docs_commit: str = "",
                    validator_defaults: Mapping[str, Any] | None = None, lineage_unknown_items: Sequence[str] = (),
                    ticked: Mapping[str, Sequence[str]] | None = None) -> Checklist:
    """Build the checklist for manifest items (each with ``item_id``, ``character``, ``type``; extra keys are ignored).

    ``lineage_unknown_items``: item ids whose kit lineage has ``license: unknown`` (they get a manual confirmation line, POL-07);
    ``ticked``: ``{item_id: [step_id, ...]}`` previously ticked steps (invalid combinations raise ``ChecklistError``)."""
    out: list[ChecklistItem] = []
    for it in items:
        item_id = str(_get(it, "item_id"))
        itype = str(_get(it, "type"))
        row = item_type_row(itype)
        steps = [ChecklistStep(s.step_id, s.text, s.kind, s.requires) for s in row.steps]
        notes: list[str] = []
        cat, att = _get(it, "category"), _get(it, "attachment")
        if cat or att:
            notes.append(f"category {cat or '-'}, attachment {att or '-'}")
        if item_id in lineage_unknown_items:
            confirm = ChecklistStep("lineage_confirm", "A kit asset in this item's lineage has licence 'unknown' (for example a head base "
                                                       "built on BlockyCharacter.fbx). Confirm you may upload it.", "confirm", ())
            steps.insert(len(steps) - 1, confirm)
            steps[-1] = ChecklistStep(steps[-1].step_id, steps[-1].text, steps[-1].kind, (*steps[-1].requires, "lineage_confirm"))
        out.append(ChecklistItem(item_id, str(_get(it, "character", "")), itype, row.row_id, row.channel, row.channel_text, row.fee_robux,
                                 row.fee_note, row.requirements, tuple(steps), (), tuple(notes)))
    context: dict[str, Any] = {}
    if creator_docs_commit:
        context["creator_docs_commit"] = creator_docs_commit
    if validator_defaults:
        context["validator_defaults"] = dict(validator_defaults)
    cl = Checklist(tuple(out), tuple(banners), COMMON_NOTES, context)
    for item_id, step_ids in (ticked or {}).items():
        for sid in step_ids:
            cl = tick(cl, item_id, sid, True)
    return cl


def _find(cl: Checklist, item_id: str) -> tuple[int, ChecklistItem]:
    for i, it in enumerate(cl.items):
        if it.item_id == item_id:
            return i, it
    raise ChecklistError(f"unknown item {item_id!r}")


def _step(item: ChecklistItem, step_id: str) -> ChecklistStep:
    for s in item.steps:
        if s.step_id == step_id:
            return s
    raise ChecklistError(f"item {item.item_id!r} has no step {step_id!r}")


def is_locked(cl: Checklist, item_id: str, step_id: str) -> bool:
    """True while a prerequisite of the step is not ticked (the upload line is locked until 'Studio test passed')."""
    _, item = _find(cl, item_id)
    step = _step(item, step_id)
    ticked = {s.step_id for s in item.steps if s.ticked}
    return any(r not in ticked for r in step.requires)


def tick(cl: Checklist, item_id: str, step_id: str, ticked: bool = True) -> Checklist:
    """Return a new checklist with the step ticked (or un-ticked). A locked step cannot be ticked; un-ticking a step also
    un-ticks every step that requires it (directly or not)."""
    idx, item = _find(cl, item_id)
    step = _step(item, step_id)
    if ticked and is_locked(cl, item_id, step_id):
        missing = [r for r in step.requires if not _step(item, r).ticked]
        raise ChecklistError(f"{item_id}/{step_id} is locked until {missing} are ticked")
    state = {s.step_id: s.ticked for s in item.steps}
    state[step_id] = ticked
    if not ticked:
        changed = True
        while changed:
            changed = False
            for s in item.steps:
                if state[s.step_id] and any(not state[r] for r in s.requires):
                    state[s.step_id] = False
                    changed = True
    new_steps = tuple(ChecklistStep(s.step_id, s.text, s.kind, s.requires, state[s.step_id]) for s in item.steps)
    new_item = ChecklistItem(item.item_id, item.character, item.type, item.row_id, item.channel, item.channel_text, item.fee_robux,
                             item.fee_note, item.requirements, new_steps, item.banners, item.notes)
    return Checklist(cl.items[:idx] + (new_item,) + cl.items[idx + 1:], cl.banners, cl.notes, cl.context)


def upload_ready(cl: Checklist, item_id: str) -> bool:
    """True when the upload step of the item is unlocked."""
    _, item = _find(cl, item_id)
    return not is_locked(cl, item_id, next(s.step_id for s in item.steps if s.kind == "upload"))


def to_json(cl: Checklist) -> dict[str, Any]:
    """``checklist.json``: item-type rows with their steps and tick state."""
    return {
        "version": 1, "banners": list(cl.banners), "notes": list(cl.notes), "context": dict(cl.context),
        "items": [{"item_id": it.item_id, "character": it.character, "type": it.type, "channel": it.channel, "channel_text": it.channel_text,
                   "fee_robux": it.fee_robux, "fee_note": it.fee_note, "requirements": list(it.requirements), "notes": list(it.notes),
                   "steps": [{"step_id": s.step_id, "text": s.text, "kind": s.kind, "requires": list(s.requires), "ticked": s.ticked,
                              "locked": is_locked(cl, it.item_id, s.step_id)} for s in it.steps]} for it in cl.items]}


def from_json(data: Mapping[str, Any]) -> Checklist:
    """Rebuild a checklist from ``to_json`` output (tick state restored; locked/unlocked is recomputed)."""
    items = []
    for it in data["items"]:
        steps = tuple(ChecklistStep(s["step_id"], s["text"], s["kind"], tuple(s.get("requires", ())), bool(s.get("ticked", False)))
                      for s in it["steps"])
        row = item_type_row(it["type"])
        items.append(ChecklistItem(it["item_id"], it.get("character", ""), it["type"], row.row_id, it["channel"], it["channel_text"],
                                   int(it["fee_robux"]), it["fee_note"], tuple(it.get("requirements", ())), steps, (), tuple(it.get("notes", ()))))
    return Checklist(tuple(items), tuple(data.get("banners", ())), tuple(data.get("notes", COMMON_NOTES)), dict(data.get("context", {})))


def render_html(cl: Checklist) -> str:
    """A small self-contained ``CHECKLIST.html`` (all text escaped; tick boxes are disabled when the step is locked)."""
    e = html.escape
    parts = ["<!doctype html><meta charset='utf-8'><title>DuoSkin upload checklist</title>",
             ("<style>body{font:15px/1.5 sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}li.locked{opacity:.55}"
             ".banner{background:#fff4d6;border:1px solid #e0b84c;padding:.5rem 1rem;margin:.5rem 0}h2{margin-top:2rem}</style>"),
             "<h1>Upload checklist</h1>"]
    for b in cl.banners:
        parts.append(f"<div class='banner'>{e(b)}</div>")
    for n in cl.notes:
        parts.append(f"<p>{e(n)}</p>")
    for it in cl.items:
        parts.append(f"<h2>{e(it.character)} {e(it.type)} <small>({e(it.item_id)})</small></h2>")
        parts.append(f"<p><b>Channel:</b> {e(it.channel_text)}<br><b>Fee:</b> {e(it.fee_note)}</p>")
        if it.requirements:
            parts.append("<p><b>Requirements:</b> " + "; ".join(e(r) for r in it.requirements) + "</p>")
        parts.append("<ol>")
        for s in it.steps:
            locked = is_locked(cl, it.item_id, s.step_id)
            attrs = ("checked " if s.ticked else "") + ("disabled " if locked else "")
            parts.append(f"<li class='{'locked' if locked else ''}'><label><input type='checkbox' {attrs}data-item='{e(it.item_id)}' "
                         f"data-step='{e(s.step_id)}'> {e(s.text)}</label></li>")
        parts.append("</ol>")
    if cl.context:
        parts.append("<h2>Validation context</h2><pre>" + e(str(dict(cl.context))) + "</pre>")
    return "".join(parts)
