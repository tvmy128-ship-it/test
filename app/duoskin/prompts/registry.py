"""Template registry: loads and validates ``prompts/<ID>.md`` (bible §0.4, APP_SPEC §10.0) and guards the schema lock.

A template file is ``---`` YAML front matter ``---`` followed by the prompt text. The front matter carries ``id`` (equal to the
file name), ``version`` (an integer; a template without one refuses to load), ``kind`` (``image``, ``text`` or ``llm``), the
provider route, ``size``, ``background``, ``images`` (the role of every attached image, in order, with an optional ``when`` flag),
``image1_role`` (``edit_target`` or ``reference``, APP_SPEC §2 S29), ``mask``, ``must_lines`` (the largest number of MUST lines
the template can render), ``dna_fields`` (the routing row of bible §3.3), ``slots`` (every slot with its source and caps),
``inputs`` (what the caller must pass), ``flags``, ``style_block``, ``priming`` (bible §2.4c) and ``checks``.

``SCHEMAS.lock`` stores the schema hash of every LLM-facing class. ``check_lock()`` returns the differences between the code and
the lock; ``write_lock()`` regenerates it after a deliberate schema change (bump the bible version and re-run the schema tests
first). Kit enums are hashed as ``x-kit`` placeholders, so adding a hair style never changes the lock.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from duoskin.prompts import template_lang as TL

PROMPT_DIR = Path(__file__).resolve().parent
LOCK_PATH = PROMPT_DIR / "SCHEMAS.lock"
BIBLE_VERSION = "1.3"
SECTION_LABELS = ("PURPOSE", "IMAGES", "SUBJECT", "MUST", "STYLE", "KEEP", "OUTPUT", "EXCLUDE")
SYSTEM_SLOTS = frozenset({"images_line", "style_block"})
USER_MARKER = "=== USER ==="
BOOTSTRAP_SUFFIX = "@s0"


class TemplateError(ValueError):
    """A template file is missing a version, is malformed, or breaks a registry rule."""


class ImageRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    when: str = ""             # "flag" or "!flag": the image is attached only when the flag is true / false
    s0: bool = False           # kept in the S0 bootstrap variant (the IMAGES line lists only these)


class InputDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["flag", "str", "int", "list"] = "str"
    required: bool = False
    values: list[str] = Field(default_factory=list)


class SlotDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = ""
    max_words: int | None = None
    lint: list[str] = Field(default_factory=list)


class TemplateMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    version: int = Field(ge=1)
    kind: Literal["image", "text", "llm"]
    provider: str
    builder: str = ""
    route: dict[str, Any] = Field(default_factory=dict)
    size: Any = None
    background: str = "opaque"                     # transparent | opaque | sentinel | by_flag (the ``transparent`` flag decides)
    images: list[ImageRole] = Field(default_factory=list)
    image1_role: Literal["edit_target", "reference", "none"] = "none"
    mask: str = "none"
    must_lines: int = 0
    dna_fields: list[str] = Field(default_factory=list)
    slots: dict[str, SlotDecl] = Field(default_factory=dict)
    inputs: dict[str, InputDecl] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    style_block: str = "none"
    priming: list[str] = Field(default_factory=list)
    checks: dict[str, list[str]] = Field(default_factory=dict)
    flat: bool = False
    bootstrap: bool = False                        # the template has an ``@s0`` variant
    schema_name: str = Field(default="", alias="schema")
    budget: dict[str, int] = Field(default_factory=dict)      # per-template override of max_chars_excl_style / max_chars_total
    cache: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class Template:
    meta: TemplateMeta
    path: Path
    sha256: str
    lines: list[TL.Line] = field(default_factory=list)                 # sectioned image/text body
    parts: dict[str, list[TL.Line]] = field(default_factory=dict)      # multi-part flat bodies (R1)
    images: list[tuple[ImageRole, list]] = field(default_factory=list)
    role_text: str = ""                                                # llm: the verbatim role prompt
    user_nodes: list = field(default_factory=list)                     # llm: the user-message template

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def version(self) -> int:
        return self.meta.version

    @property
    def family(self) -> str:
        """``I2`` for ``I2.print``: the part of the id before the dot (the routing key's first part)."""
        return self.meta.id.split(".")[0]

    def all_lines(self) -> list[TL.Line]:
        """Every body line: the sectioned body plus every part of a multi-part flat body."""
        return list(self.lines) + [ln for p in self.parts.values() for ln in p]

    def literal(self) -> str:
        """The fixed text of the template (every condition branch included), for static lint."""
        if self.meta.kind == "llm":
            pieces = [self.role_text, TL.literal_text(self.user_nodes)]
        else:
            pieces = [TL.literal_text(ln.nodes) for ln in self.all_lines()]
        pieces += [TL.literal_text(nodes) for _, nodes in self.images]
        return "\n".join(pieces)

    def used_slots(self) -> set[str]:
        out: set[str] = set()
        for ln in self.all_lines():
            out |= TL.slots_used(ln.nodes)
        out |= TL.slots_used(self.user_nodes)
        for _, nodes in self.images:
            out |= TL.slots_used(nodes)
        return out

    def used_flags(self) -> set[str]:
        out: set[str] = set()
        for ln in self.all_lines():
            out |= TL.flags_used(ln.nodes)
        out |= TL.flags_used(self.user_nodes)
        for role, nodes in self.images:
            out |= TL.flags_used(nodes)
            if role.when:
                out.add(role.when.lstrip("!"))
        return out


_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)
_PART = re.compile(r"^@@ part=([a-z0-9_]+)\s*$")


def _read_template(path: Path) -> Template:
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise TemplateError(f"{path.name}: no YAML front matter")
    try:
        raw = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise TemplateError(f"{path.name}: bad front matter: {exc}") from exc
    if not isinstance(raw, dict) or "version" not in raw:
        raise TemplateError(f"{path.name}: a template without a front-matter version refuses to load")
    try:
        meta = TemplateMeta.model_validate(raw)
    except ValidationError as exc:
        raise TemplateError(f"{path.name}: invalid front matter: {exc}") from exc
    if meta.id != path.stem:
        raise TemplateError(f"{path.name}: front-matter id {meta.id!r} must equal the file name")
    body = m.group(2).strip("\n")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        images = [(r, TL.parse(r.text)) for r in meta.images]
        if meta.kind == "llm":
            role, _, user = body.partition(USER_MARKER)
            return Template(meta=meta, path=path, sha256=sha, images=images, role_text=role.strip(),
                            user_nodes=TL.parse(user.strip()) if user.strip() else [])
        if meta.flat:
            parts: dict[str, list[TL.Line]] = {}
            cur: str | None = None
            buf: list[str] = []
            for raw_line in body.split("\n"):
                pm = _PART.match(raw_line)
                if pm:
                    if cur is not None:
                        parts[cur] = TL.parse_lines("\n".join(buf))
                    cur, buf = pm.group(1), []
                else:
                    buf.append(raw_line)
            if cur is not None:
                parts[cur] = TL.parse_lines("\n".join(buf))
            if not parts:
                parts = {"main": TL.parse_lines(body)}
            return Template(meta=meta, path=path, sha256=sha, parts=parts, images=images)
        return Template(meta=meta, path=path, sha256=sha, lines=TL.parse_lines(body), images=images)
    except TL.TemplateSyntaxError as exc:
        raise TemplateError(f"{path.name}: {exc}") from exc


@lru_cache(maxsize=1)
def all_templates() -> dict[str, Template]:
    """Every template by id (loaded once; a malformed template raises :class:`TemplateError` at startup)."""
    out: dict[str, Template] = {}
    for p in sorted(PROMPT_DIR.glob("*.md")):
        t = _read_template(p)
        if t.id in out:
            raise TemplateError(f"duplicate template id {t.id}")
        out[t.id] = t
    return out


def reload() -> None:
    """Drop the cache (tests that write temporary templates)."""
    all_templates.cache_clear()


def get(template_id: str) -> Template:
    """The template for ``template_id`` (the ``@s0`` bootstrap suffix is handled by the compiler, not here)."""
    base = template_id[: -len(BOOTSTRAP_SUFFIX)] if template_id.endswith(BOOTSTRAP_SUFFIX) else template_id
    try:
        return all_templates()[base]
    except KeyError:
        raise TemplateError(f"unknown template {template_id!r}") from None


def template_ids(kind: str | None = None) -> list[str]:
    return sorted(i for i, t in all_templates().items() if kind is None or t.meta.kind == kind)


def prompt_versions() -> dict[str, int]:
    """Template id -> version (``VersionPins.prompt_versions``)."""
    return {i: t.version for i, t in sorted(all_templates().items())}


def priming_table() -> dict[str, frozenset[str]]:
    """Bible §2.4c: the words each template must keep out of PURPOSE, SUBJECT and MUST (templates with a row only)."""
    return {i: frozenset(t.meta.priming) for i, t in all_templates().items() if t.meta.priming}


# ---------------------------------------------------------------------------------------------- static checks
def static_problems(t: Template) -> list[str]:
    """Registry-level problems of one template (the tests assert this is empty for every shipped template)."""
    bad: list[str] = []
    m = t.meta
    declared = set(m.slots)
    used = t.used_slots()
    undeclared = sorted(used - declared - SYSTEM_SLOTS)
    if undeclared:
        bad.append(f"{m.id}: slots used but not declared in the front matter: {undeclared}")
    flags_ok = set(m.flags) | {"bootstrap"}
    undeclared_flags = sorted(t.used_flags() - flags_ok)
    if undeclared_flags:
        bad.append(f"{m.id}: flags used but not declared: {undeclared_flags}")
    for name, decl in m.slots.items():
        if decl.source.startswith("dna.") and decl.source[4:] not in ("shape_language", "detail_level", "motif_object"):
            bad.append(f"{m.id}: slot {name} has an unknown DNA source {decl.source}")
    if m.kind == "image" and not m.flat:
        labels = [ln.nodes[0].text.split(":")[0] for ln in t.lines if ln.nodes and isinstance(ln.nodes[0], TL.Text)
                  and re.match(r"^[A-Z]+:", ln.nodes[0].text)]
        order = [lab for lab in labels if lab in SECTION_LABELS]
        idx = [SECTION_LABELS.index(lab) for lab in order]
        if idx != sorted(idx):
            bad.append(f"{m.id}: sections out of order {order} (want {SECTION_LABELS})")
        for need in ("PURPOSE", "SUBJECT", "MUST"):
            if need not in order:
                bad.append(f"{m.id}: missing {need}")
        if ("IMAGES" in order) != bool(m.images):
            bad.append(f"{m.id}: IMAGES line and front-matter images disagree")
    if m.kind == "image" and m.images and m.image1_role == "none":
        bad.append(f"{m.id}: image templates with images need image1_role")
    if m.must_lines > 5:
        bad.append(f"{m.id}: must_lines {m.must_lines} > 5")
    for r in m.images:
        if r.when and not re.match(r"^!?[a-z_][a-z0-9_]*$", r.when):
            bad.append(f"{m.id}: bad image condition {r.when!r}")
    if m.bootstrap and not any(r.s0 for r in m.images):
        bad.append(f"{m.id}: bootstrap variant without an s0 image")
    if m.kind == "llm" and m.provider == "anthropic" and not m.schema_name:
        bad.append(f"{m.id}: llm templates name their schema")
    return bad


def validate_all() -> list[str]:
    """Static problems of every template (empty means the shipped set is consistent)."""
    return [p for t in all_templates().values() for p in static_problems(t)]


# ---------------------------------------------------------------------------------------------- schema lock
def _lock_classes() -> dict[str, type]:
    from duoskin.models import llm_io, spec

    classes: dict[str, type] = dict(llm_io.SCHEMA_CLASSES)
    for name in ("Colour", "WorldDNA", "Anchor", "Contrast", "CharacterDNA", "Body", "Face", "Hair", "Print", "Top", "Shoes", "Bottom",
                 "Accessory", "Makeup", "Character", "DuoSpec", "BriefConstraint", "PlanSet"):
        classes[name] = getattr(spec, name)
    return classes


def schema_for_lock(cls: type) -> dict[str, Any]:
    """The JSON schema of a model with kit enums as ``x-kit`` placeholders (so the lock ignores the user's kits)."""
    from duoskin.models import kitenums

    with kitenums.schema_mode("placeholder"):
        return cls.model_json_schema()      # type: ignore[attr-defined]


def schema_hash(cls: type) -> str:
    from duoskin.models.common import sha256_of

    return sha256_of(schema_for_lock(cls))


def schema_hashes() -> dict[str, str]:
    """Hash of every LLM-facing class, sorted by name (the content of ``SCHEMAS.lock``)."""
    return {n: schema_hash(c) for n, c in sorted(_lock_classes().items())}


def read_lock(path: Path | None = None) -> dict[str, str]:
    p = path or LOCK_PATH
    return dict(json.loads(p.read_text(encoding="utf-8"))["schemas"])


def write_lock(path: Path | None = None) -> dict[str, str]:
    """Regenerate ``SCHEMAS.lock`` from the code (run after a deliberate schema change)."""
    p = path or LOCK_PATH
    hashes = schema_hashes()
    doc = {"bible_version": BIBLE_VERSION, "kit_enums": "x-kit placeholders", "schemas": hashes}
    p.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return hashes


def check_lock(path: Path | None = None) -> list[str]:
    """Differences between the code and ``SCHEMAS.lock`` (empty when they agree). Fails closed when the lock is missing."""
    p = path or LOCK_PATH
    if not p.exists():
        return [f"{p.name} is missing"]
    locked = read_lock(p)
    now = schema_hashes()
    diff = [f"{n}: schema changed (lock {locked[n][:12]}, code {now[n][:12]})" for n in sorted(now) if n in locked and locked[n] != now[n]]
    diff += [f"{n}: not in the lock" for n in sorted(set(now) - set(locked))]
    diff += [f"{n}: in the lock but not in the code" for n in sorted(set(locked) - set(now))]
    return diff


def iter_images(t: Template, flags: dict[str, bool]) -> Iterable[tuple[ImageRole, list]]:
    """The attached images of ``t`` for the given flags, in order (``Image 1`` first)."""
    boot = bool(flags.get("bootstrap"))
    for role, nodes in t.images:
        if boot and not role.s0:
            continue
        if role.when:
            neg = role.when.startswith("!")
            truth = bool(flags.get(role.when.lstrip("!"), False))
            if truth == neg:
                continue
        yield role, nodes
