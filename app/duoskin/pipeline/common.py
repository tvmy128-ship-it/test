"""Helpers shared by every pipeline lane (APP_SPEC §8.2, §10): part ids, the project's spec, provider routing, assets and costs.

Nothing here knows a lane. It answers the questions every handler asks:

* ``split_part_id("a.acc.0")`` -> which character, kind and index;
* ``load_spec(rt, project_id)`` -> the spec record the parts are built from, as a dict (``spec_model`` validates it);
* ``provider_available(rt, "recraft")`` -> routing without keys (APP_SPEC §7, §16): a mock provider is available, a real one
  needs a stored key, a disabled one is not;
* ``put_png`` / ``put_bytes`` / ``link_asset`` -> assets with provenance, linked to a part and a role;
* ``record_cost`` -> a provider's cost dict as a ledger entry (idempotent per ``(step, attempt, operation)``);
* ``concept_refs`` -> the concept crop and the character's own style sheet of a part (written by the concept lock, C3);
  when they are missing a flat placeholder is made so the part board still works (flagged ``no_concept_crop``).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PIL import Image

from duoskin.checks.model import CheckResult
from duoskin.models.asset import Asset, AssetLink, Provenance
from duoskin.models.common import new_id, utcnow
from duoskin.models.cost import CostEntry, CostUnit

if TYPE_CHECKING:
    from duoskin.engine.context import StepContext
    from duoskin.engine.runtime import Runtime

PART_RE = re.compile(r"^(?P<c>[ab])\.(?P<kind>face|hair|shirt|pants|colours|acc|print)(?:\.(?P<a>top|bottom|shoes|\d))?(?:\.(?P<b>\d))?$")
CHARS = ("a", "b")
PLACEHOLDER_BG = (242, 242, 242)

log = logging.getLogger("duoskin.pipeline.common")


# ---------------------------------------------------------------------------------------------------- part ids
@dataclass(frozen=True)
class PartRef:
    id: str
    character: str                 # "a" | "b" | "duo"
    kind: str                      # face | hair | shirt | pants | colours | acc | print | duo
    index: int | None = None       # accessory index, print index
    slot: str | None = None        # print slot: top | bottom | shoes (no charm parts in v1, APP_SPEC §1.2)

    @property
    def is_character_part(self) -> bool:
        return self.character in CHARS


def split_part_id(part_id: str) -> PartRef:
    """``a.acc.0`` -> ``PartRef('a.acc.0', 'a', 'acc', 0)``; ``b.print.top.1`` -> slot ``top``, index 1; ``duo`` -> kind ``duo``."""
    if part_id == "duo":
        return PartRef("duo", "duo", "duo")
    m = PART_RE.match(part_id)
    if not m:
        raise ValueError(f"not a part id: {part_id!r}")
    kind, a, b = m.group("kind"), m.group("a"), m.group("b")
    if kind == "acc":
        return PartRef(part_id, m.group("c"), "acc", int(a) if a is not None else None)
    if kind == "print":
        return PartRef(part_id, m.group("c"), "print", int(b) if b is not None else 0, a)
    return PartRef(part_id, m.group("c"), kind)


def other_character(c: str) -> str:
    return "b" if c == "a" else "a"


def new_nonce() -> str:
    """A fresh nonce for Reimagine (APP_SPEC §8.5): a new cache key, so the user always gets a new result."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------------------------------- specs
def load_spec(rt: Runtime, project_id: str, *, spec_id: str | None = None) -> tuple[Any, dict[str, Any]]:
    """``(SpecRecord, spec dict)`` the parts are built from: ``spec_id``, else the approved spec, else the current one."""
    project = rt.repo.get_project(project_id)
    sid = spec_id or project.approved_spec_id or project.current_spec_id
    if not sid:
        raise ValueError("the project has no spec yet")
    rec = rt.repo.get_spec(sid)
    return rec, rec.spec


def spec_model(spec: dict[str, Any], *, rules: bool = False):
    """The ``DuoSpec`` for a spec dict (``rules=False`` skips ``spec_rules``: the part pipeline only reads fields)."""
    from duoskin.models.spec import DuoSpec

    return DuoSpec.model_validate(spec, context=None if rules else {"skip_rules": True})


def palette_map(spec: dict[str, Any]) -> dict[str, str]:
    """``palette id -> '#rrggbb'`` (lower case)."""
    return {c["id"]: str(c["hex"]).lower() for c in spec.get("palette", [])}


def hex_of(spec: dict[str, Any], ref: str | None, default: str | None = None) -> str | None:
    if ref in (None, "", "none"):
        return default
    return palette_map(spec).get(str(ref), default)


def palette_hexes(spec: dict[str, Any], refs: list[str] | None = None) -> list[str]:
    pal = palette_map(spec)
    if refs is None:
        return list(pal.values())
    return [pal[r] for r in refs if r in pal]


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def pins_of(rt: Runtime, project_id: str):
    return rt.repo.get_project(project_id).pins


# ---------------------------------------------------------------------------------------------------- providers
def provider_mode(rt: Runtime, provider: str) -> str:
    """``real | mock | disabled`` as the registry will resolve it (settings, demo mode and ``DUOSKIN_PROVIDERS``)."""
    try:
        from duoskin.providers import registry as preg

        return preg.default_registry().mode_of(provider)
    except Exception:  # noqa: BLE001
        return str(rt.effective_settings().mode_of(provider).value)


def handlers_present(*kinds: str) -> bool:
    """Are the step handlers of this lane registered in this process? The job factories live for the whole process, but a test may clear the handler
    registry: a factory then makes no steps instead of failing on an unknown kind."""
    from duoskin.engine import registry

    try:
        for k in kinds:
            registry.get(k)
    except registry.UnknownStepKind:
        return False
    return True


def provider_available(rt: Runtime, provider: str) -> bool:
    """Can a call to ``provider`` be made? Mock: yes. Disabled: no. Real: only with a stored key (APP_SPEC §7, D22)."""
    mode = provider_mode(rt, provider)
    if mode == "mock":
        return True
    if mode == "disabled":
        return False
    try:
        return bool(rt.keys.get_key(provider))
    except Exception:  # noqa: BLE001
        return False


def is_mock(rt: Runtime, provider: str) -> bool:
    return provider_mode(rt, provider) == "mock"


def available_providers(rt: Runtime) -> set[str]:
    return {p for p in ("anthropic", "openai", "recraft", "tripo", "gemini") if provider_available(rt, p)}


# ---------------------------------------------------------------------------------------------------- images
def open_image(data: bytes) -> Image.Image:
    """Decode to RGBA through the one ingest path (``imaging.files.normalise_image``: orientation, ICC, 8-bit)."""
    from duoskin.imaging import files as F

    im, _ = F.normalise_image(data)
    return im


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def flat_png(size: tuple[int, int], rgb: tuple[int, int, int] = PLACEHOLDER_BG, alpha: int = 255) -> bytes:
    return png_bytes(Image.new("RGBA", size, rgb + (alpha,)))


# ---------------------------------------------------------------------------------------------------- provenance and assets
def prov(source: str = "code", **kw: Any) -> Provenance:
    """A ``Provenance`` with ``created_at`` filled."""
    return Provenance(source=source, created_at=kw.pop("created_at", utcnow()), **kw)   # type: ignore[arg-type]


def put_bytes(ctx: StepContext, data: bytes, ext: str, *, role: str, part_id: str | None, provenance: Provenance,
              status: str = "candidate", rank: int | None = None) -> Asset:
    return ctx.put_asset(data, ext, role=role, prov=provenance, part_id=part_id, status=status, rank=rank)   # type: ignore[arg-type]


def put_png(ctx: StepContext, image: Image.Image | bytes, *, role: str, part_id: str | None, provenance: Provenance,
            status: str = "candidate", rank: int | None = None) -> Asset:
    data = image if isinstance(image, (bytes, bytearray)) else png_bytes(image)
    return put_bytes(ctx, bytes(data), "png", role=role, part_id=part_id, provenance=provenance, status=status, rank=rank)


def link_asset(rt: Runtime, project_id: str | None, part_id: str | None, sha: str, role: str, status: str, provenance: Provenance,
               *, step_id: str | None = None, rank: int | None = None) -> AssetLink:
    """Link an asset that already exists (a board asset chosen from candidates, a copied kit file) to a part and role."""
    link = AssetLink(id=new_id("lnk"), asset_sha=sha, project_id=project_id, part_id=part_id, step_id=step_id, role=role,
                     status=status, rank=rank, provenance=provenance)   # type: ignore[arg-type]
    return rt.cas.add_link(link)


def latest_link(rt: Runtime, project_id: str, role: str, part_id: str | None = None) -> AssetLink | None:
    links = rt.repo.list_links(project_id=project_id, part_id=part_id, role=role)
    links = [lk for lk in links if lk.status != "superseded"]
    return links[-1] if links else None


def latest_sha(rt: Runtime, project_id: str, role: str, part_id: str | None = None) -> str | None:
    lk = latest_link(rt, project_id, role, part_id)
    return lk.asset_sha if lk else None


# ---------------------------------------------------------------------------------------------------- costs and checks
def record_cost(ctx: StepContext, cost: dict[str, Any] | None, operation: str, *, fallback_provider: str = "mock",
                part_id: str | None = None) -> CostEntry | None:
    """Turn a provider's ``CostEntry``-like dict into a ledger row (idempotent per step, attempt and ``operation``)."""
    if not cost:
        return None
    units = []
    for u in cost.get("units") or []:
        try:
            units.append(CostUnit(**u))
        except Exception as exc:  # noqa: BLE001 - an unknown unit name must not lose the cost row
            log.warning("cost unit %r skipped: %s", u.get("name") if isinstance(u, dict) else u, exc)
    provider = cost.get("provider") or fallback_provider
    if provider not in ("anthropic", "openai", "recraft", "tripo", "gemini", "fal", "mock"):
        provider = "mock"
    entry = CostEntry(ts=utcnow(), provider=provider, model=str(cost.get("model") or ""), operation=operation,   # type: ignore[arg-type]
                      units=units, usd=float(cost.get("usd") or 0.0), credits=cost.get("credits"),
                      basis=cost.get("basis") or "usage", price_table=str(cost.get("price_table") or ""),
                      request_id=cost.get("request_id"), remote_task_id=cost.get("remote_task_id"),
                      batch=bool(cost.get("batch", False)), part_id=part_id)
    return ctx.add_cost(entry)


def store_checks(ctx: StepContext, results: list[CheckResult]) -> list[str]:
    """Record check results with the step; returns their row ids (for ``Provenance.check_ids``)."""
    return ctx.record_checks(list(results)) if results else []


def mk_result(check_id: str, passed: bool, *, kind: str = "hard", metric: str = "", value: float | None = None, threshold: str = "",
              evidence: str = "", fix_hint: str = "none", fm_ids: list[str] | None = None, subject_sha: str = "") -> CheckResult:
    """A ``CheckResult`` for a check this pipeline owns that has no registry entry (its ``kind`` is what it declares here).
    Registered checks go through ``checks.runner.build_result`` instead, so their kind always comes from the registry."""
    from duoskin.checks import thresholds

    return CheckResult(check_id=check_id, fm_ids=fm_ids or [], subject_sha=subject_sha, kind=kind, passed=bool(passed), metric=metric,   # type: ignore[arg-type]
                       value=value, threshold=threshold, evidence=evidence[:240], ran=True, fix_hint=fix_hint if not passed else "none",   # type: ignore[arg-type]
                       thresholds_version=thresholds.THRESHOLDS_VERSION)


def hard_failures(results: list[CheckResult]) -> list[CheckResult]:
    """HARD and ASSERT results that failed or did not run (what blocks a tile)."""
    from duoskin.checks import policy

    return [r for r in results if policy.is_blocking(r) and not r.passed]


def soft_warnings(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.kind == "soft" and r.ran and not r.passed]


def na_results(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.status == "not_applicable"]


#: what a SOFT check says to a person (the evidence of a check is a measurement: "blush L* 78 > 45.0; lightens tone_2")
SOFT_WORDS: dict[str, str] = {
    "F_BLUSH": "The blush may look pale on some of the skin tones.", "A_SWATCH": "Some colours in the picture drifted from the plan.",
    "CHK-G1-10": "Some colours in the picture drifted from the plan.", "A_PHASH": "This version is close to one you already turned down.",
    "CHK-A14": "This version is close to one you already turned down.", "A_STYLE": "The drawing style differs a little from your house style.",
    "CHK-A16": "The drawing style differs a little from your house style.", "CHK-A13": "This part may not match the approved design closely.",
    "CHK-D05": "This part may not match the approved design closely.", "CHK-D03": "Some colours are hard to tell apart at phone size.",
    "CHK-D07": "The duo judge has a small note about this pair.", "CHK-D08": "Some details are hard to see at phone size.",
    "DUO-03": "The two outfits may look alike at phone size.", "DUO-04": "The two hairstyles and accessories look similar from a distance.",
    "DUO-10": "This duo is close to one you made before.", "TASTE_DETAIL": "The amount of detail may not match what the plan asked for.",
    "TASTE_LAYOUT": "The two garments are laid out in a similar way.", "TASTE_RATIO": "The colour shares differ from the plan.",
    "FACE-13": "Hair may cover the eyes or the brows.", "PLN-15": "The two hairstyles have similar shapes.",
    "CHK-M18": "The 3D shading may look flat.", "CHK-G1-11": "The two characters may look too alike.", "CHK-G0-06": "The colours may not fit the pair style.",
}
_TECHNICAL = re.compile(r"[<>_*{}#;]|\bdE\b|\d\.\d")


def plain_warning(check_id: str, evidence: str) -> str:
    """One calm sentence for a SOFT check: its own words when it has them, the evidence when that already reads as a sentence, else a general note."""
    if check_id in SOFT_WORDS:
        return SOFT_WORDS[check_id]
    ev = " ".join((evidence or "").split())
    if ev and ev[0].isupper() and " " in ev and not _TECHNICAL.search(ev):
        return ev[:200]
    return "One of the quality checks has a small note about this part."


def summarize_checks(results: list[CheckResult]) -> dict[str, Any]:
    """The compact check summary a tile shows (SOFT warnings go to ``warnings``, which the gate withholds until the first choice)."""
    hard = hard_failures(results)
    warnings = []
    for r in soft_warnings(results):
        warnings.append({"id": r.check_id, "text": plain_warning(r.check_id, r.evidence or r.metric), "severity": "low", "catch_rate": 0.0,
                         "visible": True, "fresh": True})
    return {"hard_failures": [{"id": r.check_id, "evidence": r.evidence[:200], "ran": r.ran} for r in hard],
            "warnings": warnings,
            "not_applicable": [{"id": r.check_id, "reason": r.na_reason} for r in na_results(results)],
            "passed": sum(1 for r in results if r.passed and r.status == "passed"), "total": len(results)}


# ---------------------------------------------------------------------------------------------------- concept references
CONCEPT_ROLES = {
    "style_sheet": "style_sheet_{c}",           # the character's OWN style sheet (APP_SPEC S35); never the partner's or the judge sheet
    "judge_sheet": "style_sheet_judge",
    "concept": "concept_of_record",
    "crop": "crop.{part}",                      # the part crop made by the concept lock (face, hair, accessory, print region, shoe band)
}


@dataclass
class ConceptRefs:
    crop_sha: str | None
    style_sha: str | None
    placeholder_crop: bool = False
    placeholder_style: bool = False

    @property
    def flags(self) -> list[str]:
        out = []
        if self.placeholder_crop:
            out.append("no_concept_crop")
        if self.placeholder_style:
            out.append("no_style_sheet")
        return out


def _palette_swatch_image(spec: dict[str, Any], character: str, size: tuple[int, int]) -> Image.Image:
    """A flat ``#F2F2F2`` placeholder with the character's key colours as blocks (used only when the concept lock wrote no crop)."""
    pal = palette_map(spec)
    ch = spec.get(character, {})
    refs = [ch.get("top", {}).get("base_ref"), ch.get("bottom", {}).get("base_ref"), ch.get("hair", {}).get("colour_ref"),
            ch.get("top", {}).get("second_ref")]
    colours = [pal[r] for r in refs if r in pal] or list(pal.values())[:3] or ["#808080"]
    im = Image.new("RGBA", size, PLACEHOLDER_BG + (255,))
    w, h = size
    step = max(1, w // (len(colours) + 1))
    for i, hx in enumerate(colours):
        x0 = step // 2 + i * step
        block = Image.new("RGBA", (max(8, step - 8), max(8, h // 2)), hex_to_rgb(hx) + (255,))
        im.paste(block, (x0, h // 4))
    return im


def concept_refs(rt: Runtime, project_id: str, spec: dict[str, Any], part_id: str, *, crop_size: tuple[int, int] = (768, 768),
                 create: bool = True) -> ConceptRefs:
    """The concept crop of ``part_id`` and the character's own style sheet (written by the concept lock, C3).

    Missing ones are made as flat placeholders so the board still runs (``flags`` says so); ``create=False`` only looks."""
    ref = split_part_id(part_id)
    c = ref.character if ref.character in CHARS else "a"
    crop_role = CONCEPT_ROLES["crop"].format(part=part_id)
    crop = latest_sha(rt, project_id, crop_role)
    if crop is None:
        # accessory and print crops fall back to the character's whole concept crop of that kind
        for alt in (f"crop.{c}.{ref.kind}", f"crop.{c}"):
            crop = latest_sha(rt, project_id, alt)
            if crop:
                break
    style = latest_sha(rt, project_id, CONCEPT_ROLES["style_sheet"].format(c=c))
    placeholder_crop = placeholder_style = False
    if crop is None and create:
        asset = rt.cas.put(png_bytes(_palette_swatch_image(spec, c, crop_size)), "png",
                           link=AssetLink(id=new_id("lnk"), asset_sha="0" * 64, project_id=project_id, part_id=part_id, step_id=None,
                                          role=crop_role, status="final",
                                          provenance=prov("code", params={"placeholder": True}, notes=["no_concept_crop"])),   # type: ignore[arg-type]
                           prov=prov("code", params={"placeholder": True}, notes=["no_concept_crop"]))
        crop, placeholder_crop = asset.sha256, True
    if style is None and create:
        asset = rt.cas.put(png_bytes(_palette_swatch_image(spec, c, (1024, 768))), "png",
                           link=AssetLink(id=new_id("lnk"), asset_sha="0" * 64, project_id=project_id, part_id=None, step_id=None,
                                          role=CONCEPT_ROLES["style_sheet"].format(c=c), status="final",
                                          provenance=prov("code", params={"placeholder": True}, notes=["no_style_sheet"])),   # type: ignore[arg-type]
                           prov=prov("code", params={"placeholder": True}, notes=["no_style_sheet"]))
        style, placeholder_style = asset.sha256, True
    return ConceptRefs(crop, style, placeholder_crop, placeholder_style)


# ---------------------------------------------------------------------------------------------------- misc
def ids_by_prefix(parts: list[Any], prefix: str) -> list[str]:
    return [p.id for p in parts if p.id.startswith(prefix)]


def part_dir_name(part_id: str) -> str:
    """``a.acc.0`` -> ``a_acc_0`` (file and folder names)."""
    return part_id.replace(".", "_")


def ascii_slug(text: str, default: str = "duo", maxlen: int = 24) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:maxlen].strip("-")
    return s or default


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
