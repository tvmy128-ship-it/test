"""The prompt compiler: template + spec + slots -> ``CompiledPrompt`` (APP_SPEC §10.0, bible §2.2 and §2.3, FAILURE_MODES §5.3).

``compile(template_id, spec, character, slots, ctx)`` fills the slots of a versioned ``prompts/<ID>.md`` template from the spec
(through ``prompts/slots.py``), routes the DNA fields (``prompts/dna_router.py``, at most 2, own character only), renders the
fixed skeleton PURPOSE, IMAGES, SUBJECT, MUST, STYLE, KEEP, OUTPUT, EXCLUDE, lints the result (CHK-P01: PRM-01..PRM-12) and hashes it.
It is deterministic: the same spec and inputs give byte-identical text. An ``@s0`` suffix on the template id compiles the S0
bootstrap variant (the IMAGES line lists only the guide).

``lint_prompt`` is an ASSERT (CHK-P01): ``compile`` raises :class:`PromptLintError` if any result fails, so a bad prompt never
reaches a provider. Slot values that come from the model (``print.motif``, ``fix_sentence``, ...) pass the free-text lint first
(``FreeTextLintError``). User text never becomes a slot: a typed change goes through L7, which returns one fix sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from duoskin.checks import policy
from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.models.common import CharKey, sha256_of
from duoskin.models.spec import DuoSpec
from duoskin.prompts import dna_router, registry
from duoskin.prompts import freetext as free_text
from duoskin.prompts import slots as S
from duoskin.prompts import template_lang as TL
from duoskin.prompts.catalog import TEXT_INVITING, CompileCtx, default_ctx, norm
from duoskin.prompts.limits import TEXT_PROMPT_MAX_SENTENCES, thr

SKELETON = ("PURPOSE", "IMAGES", "SUBJECT", "MUST", "STYLE", "KEEP", "OUTPUT", "EXCLUDE")
POSITIVE = ("PURPOSE", "IMAGES", "SUBJECT", "MUST")
CORE = ("PURPOSE", "SUBJECT", "MUST")                  # where the priming words are checked (bible §2.4c)
PAIR_WORDS = frozenset({"complement", "leader_chaotic", "leader", "chaotic", "same_club", "same club", "mirror", "seasonal_twins",
                        "seasonal", "twins", "object_mascot", "mascot"})
HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
SLOT_LEAK = re.compile(r"[{}]|\b(?:None|null|undefined)\b|,\s*,|  ")
COLOUR_COUNT = re.compile(r"\b(?:\d+|two|three|four|five|six)\s+(?:\w+\s+)?colou?rs?\b|\b\d{2}\s*/\s*\d{2}\b", re.IGNORECASE)
STATELESS = re.compile(r"\b(?:same as (?:before|earlier|previously|last time|the (?:last|previous|earlier))|as before|as previously|"
                       r"as (?:in|from) the (?:last|previous|earlier) (?:image|call|version|round|attempt|result)|"
                       r"(?:previous|earlier|last) (?:image|call|version|round|attempt|result)s?|last time|once more|try again|"
                       r"like the (?:last|previous)|unchanged from)\b", re.IGNORECASE)
_LABEL = re.compile(r"^(PURPOSE|IMAGES|SUBJECT|MUST|STYLE|KEEP|OUTPUT|EXCLUDE):", re.MULTILINE)
_MUST_LINE = re.compile(r"^\d+[.)]\s", re.MULTILINE)
_MUST_FLAT = re.compile(r"(?:^|\s)\d\)\s")
_SENTENCES = re.compile(r"[.!?](?:\s|$)")


class PromptLintError(AssertionError):
    """CHK-P01 failed: the prompt must not be sent. ``results`` holds every CheckResult (failed ones have ``passed=False``)."""

    def __init__(self, template_id: str, results: list[CheckResult]):
        self.template_id, self.results = template_id, results
        bad = [f"{r.fm_ids[0] if r.fm_ids else ''} {r.metric}: {r.evidence or r.value}" for r in results if not r.passed]
        super().__init__(f"{template_id}: prompt lint failed: " + "; ".join(bad))


@dataclass(frozen=True)
class CompiledPrompt:
    template_id: str
    template_version: int
    text: str                          # the exact text sent (stored in provenance)
    sha256: str
    must_lines: int
    dna_fields: list[str]
    character: CharKey | None
    images: list[str]                  # roles, in order (Image 1 first)
    provider_fields: dict[str, Any]    # e.g. Recraft controls.colors; never in the text


@dataclass(frozen=True)
class LintCtx:
    """Facts the compiler already holds; the linter reads nothing from disk (FAILURE_MODES §5.3)."""

    house_style_block: str             # HOUSE_STYLE_2D / HOUSE_STYLE_3D_INPUT of the template, verbatim ("" when it has none)
    background: str                    # "transparent" | "opaque" | "sentinel"
    n_attached_images: int
    story: str = ""                    # the spec's story: never allowed in a prompt
    pair_words: frozenset[str] = frozenset()   # PairStructure enum words and their phrase-map synonyms


# --------------------------------------------------------------------------------------------- skeleton helpers
def split_sections(text: str) -> dict[str, str]:
    parts = _LABEL.split(text)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def split_flat(text: str) -> dict[str, str]:
    """Recraft flat form: ``<subject sentence>. 1) ... 2) ...`` (no labels)."""
    head, _, rest = text.partition("1)")
    return {"SUBJECT": head.strip(), "MUST": ("1)" + rest) if rest else ""}


def _is_flat(template_id: str) -> bool:
    try:
        return registry.get(template_id).meta.flat
    except registry.TemplateError:
        return template_id.startswith("R")


def count_must(text: str, flat: bool) -> int:
    s = split_flat(text) if flat else split_sections(text)
    must = s.get("MUST", "")
    return len(_MUST_FLAT.findall(must)) if flat else len(_MUST_LINE.findall(must))


# --------------------------------------------------------------------------------------------- compile
def _coerce_spec(spec: Any) -> DuoSpec:
    if isinstance(spec, DuoSpec):
        return spec
    return DuoSpec.model_validate(spec, context={"skip_rules": True})


def _check_inputs(meta: registry.TemplateMeta, given: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(given) - set(meta.inputs))
    if unknown:
        raise S.PromptBuildError(f"{meta.id}: unknown input(s) {unknown}; declared: {sorted(meta.inputs)}")
    out: dict[str, Any] = {}
    for name, decl in meta.inputs.items():
        if name not in given:
            if decl.required:
                raise S.PromptBuildError(f"{meta.id}: input {name!r} is required")
            if decl.kind == "flag":
                out[name] = False
            continue
        v = given[name]
        if decl.kind == "flag" and not isinstance(v, bool):
            raise S.PromptBuildError(f"{meta.id}: input {name!r} must be a bool")
        if decl.kind == "int" and (not isinstance(v, int) or isinstance(v, bool)):
            raise S.PromptBuildError(f"{meta.id}: input {name!r} must be an int")
        if decl.kind == "list" and not isinstance(v, (list, tuple)):
            raise S.PromptBuildError(f"{meta.id}: input {name!r} must be a list")
        if decl.values and v not in decl.values:
            raise S.PromptBuildError(f"{meta.id}: input {name!r}={v!r} is not one of {decl.values}")
        out[name] = list(v) if decl.kind == "list" else v
    return out


def _resolve_roles(roles: list[str], character: CharKey | None) -> list[str]:
    return [f"style_sheet_{character}" if r == "style_sheet" and character in ("a", "b") else r for r in roles]


def _render_template(tpl: registry.Template, env: dict[str, Any], flags: dict[str, bool], part: str | None) -> str:
    meta = tpl.meta
    try:
        if meta.flat:
            lines = tpl.parts[part or "main"]
            return TL.render_lines(lines, env, flags, joiner=" ")
        return TL.render_lines(tpl.lines, env, flags, joiner="\n")
    except TL.MissingSlot as exc:
        raise S.PromptBuildError(f"{meta.id}: slot {exc.args[0]!r} has no value (PRM-05)") from exc
    except TL.EmptySlot as exc:
        raise S.PromptBuildError(f"{meta.id}: required slot {exc.name!r} is empty (PRM-05)") from exc


def compile(template_id: str, spec: DuoSpec | dict, character: CharKey | None, slots: dict | None = None,
            ctx: CompileCtx | None = None, *, lint: bool = True) -> CompiledPrompt:
    """Compile one image or text prompt. See the module docstring. ``slots`` are the caller's inputs (declared in the template's
    ``inputs``: a face ``part``, a ``print``, an ``accessory`` index, a ``fix_sentence`` from L7, boolean flags such as ``mood``)."""
    ctx = ctx or default_ctx()
    bootstrap = template_id.endswith(registry.BOOTSTRAP_SUFFIX)
    tpl = registry.get(template_id)
    meta = tpl.meta
    if meta.kind == "llm":
        raise S.PromptBuildError(f"{meta.id} is an LLM template: use prompts.llm.compile_llm")
    if bootstrap and not meta.bootstrap:
        raise S.PromptBuildError(f"{meta.id} has no {registry.BOOTSTRAP_SUFFIX} bootstrap variant")
    spec = _coerce_spec(spec)
    inputs = _check_inputs(meta, dict(slots or {}))
    if bootstrap:
        inputs["bootstrap"] = True
    part = inputs.get("part")
    if meta.flat and part not in tpl.parts and "main" not in tpl.parts:
        raise S.PromptBuildError(f"{meta.id}: part {part!r} is not one of {sorted(tpl.parts)}")
    builder, max_level = S.BUILDERS[meta.builder]
    style_block = ctx.style_block(meta.style_block)
    budget = meta.budget.get("max_chars_excl_style", thr("prm.max_chars_excl_style"))
    result: tuple[str, list[str], list[str], dict[str, Any], S.Built] | None = None
    for level in range(max_level + 1):
        built = builder(S.BuildArgs(spec=spec, char=character, ctx=ctx, inputs=inputs, level=level, meta=meta))
        flags = {name: bool(inputs.get(name, False)) for name in meta.flags}
        flags["bootstrap"] = bootstrap
        flags.update(built.flags)
        try:
            route = dna_router.route_dna(meta.id, spec, character, part=part, phrases=ctx.phrases) if meta.dna_fields else []
        except ValueError as exc:                                  # no character for a CHARACTER field, or a part with no routing row
            raise S.PromptBuildError(f"{meta.id}: {exc}") from exc
        dna_names = {d.slot for d in route}
        clash = dna_names & set(built.slots)
        if clash:
            raise S.PromptBuildError(f"{meta.id}: builder produced DNA slot(s) {sorted(clash)}; only the router may fill them")
        env: dict[str, Any] = {**built.slots, **{d.slot: d.value for d in route}, "style_block": style_block}
        for name, decl in meta.slots.items():                    # a DNA slot the router did not route is empty (its line drops)
            if decl.source.startswith("dna."):
                env.setdefault(name, "")
        _check_slots(meta, env, ctx, {d.slot for d in route})
        selected = list(registry.iter_images(tpl, flags))
        try:
            env["images_line"] = " ".join(f"Image {i} = {TL.render(nodes, env, flags).strip()}" for i, (_, nodes) in enumerate(selected, 1))
        except (TL.MissingSlot, TL.EmptySlot) as exc:
            raise S.PromptBuildError(f"{meta.id}: image role text needs slot {exc.args[0]!r}") from exc
        text = _render_template(tpl, env, flags, part)
        roles = _resolve_roles([r.id for r, _ in selected], character)
        used_dna = [d.field for d in route if d.used and _slot_survived(text, d, tpl, part)]
        result = (text, roles, used_dna, flags, built)
        s = split_flat(text) if meta.flat else split_sections(text)
        if len(text) - len(s.get("STYLE", "")) <= budget:
            break
    assert result is not None
    text, roles, used_dna, flags, built = result
    background = meta.background
    if background == "by_flag":
        background = "transparent" if flags.get("transparent") else "opaque"
    size = meta.size if isinstance(meta.size, str) else built.size
    provider_fields = {**built.provider_fields, "background": background, "image1_role": meta.image1_role, "mask": meta.mask}
    if size:
        provider_fields["size"] = size
    must = count_must(text, meta.flat) if meta.kind == "image" else 0
    tid = meta.id + (registry.BOOTSTRAP_SUFFIX if bootstrap else "")
    cp = CompiledPrompt(template_id=tid, template_version=meta.version, text=text,
                        sha256=sha256_of({"template_id": tid, "template_version": meta.version, "text": text, "images": roles,
                                          "provider_fields": provider_fields}),
                        must_lines=must, dna_fields=used_dna, character=character, images=roles, provider_fields=provider_fields)
    if lint:
        expected_style = style_block if re.search(r"^STYLE:", text, re.MULTILINE) else ""
        results = lint_prompt(cp, lint_ctx(tid, spec, ctx, background=background, n_images=len(roles), house_style_block=expected_style))
        if not all(r.passed for r in results):
            raise PromptLintError(tid, results)
    return cp


def _check_slots(meta: registry.TemplateMeta, env: dict[str, Any], ctx: CompileCtx, dna_slots: set[str]) -> None:
    """Caps declared in the front matter, and the free-text lint for routed DNA text (motif_object)."""
    for name, decl in meta.slots.items():
        value = str(env.get(name, ""))
        if decl.max_words and len(value.split()) > decl.max_words:
            raise S.PromptBuildError(f"{meta.id}: slot {name!r} has {len(value.split())} words, at most {decl.max_words}")
        if name in dna_slots and "free_text" in decl.lint and value:
            problems = free_text.free_text_problems(value, decl.max_words, ctx.banned, strict=True)
            if problems:
                raise S.FreeTextLintError(name, value, problems)


def _slot_survived(text: str, d: dna_router.DnaSlot, tpl: registry.Template, part: str | None) -> bool:
    """A DNA slot counts as used only if its text is in the rendered prompt (a dropped line is not a used field)."""
    return d.value in text


def lint_ctx(template_id: str, spec: DuoSpec | dict, ctx: CompileCtx, *, background: str | None = None,
             n_images: int | None = None, house_style_block: str | None = None) -> LintCtx:
    """The facts ``lint_prompt`` needs, built from the template front matter and the spec (APP_SPEC §10.0)."""
    spec = _coerce_spec(spec)
    meta = registry.get(template_id).meta
    bg = background or ("opaque" if meta.background == "by_flag" else meta.background)
    n = n_images if n_images is not None else len([r for r in meta.images if not r.when])
    block = ctx.style_block(meta.style_block) if house_style_block is None else house_style_block
    return LintCtx(house_style_block=block, background=bg, n_attached_images=n,
                   story=spec.world.story, pair_words=PAIR_WORDS)


# --------------------------------------------------------------------------------------------- lint (CHK-P01)
def _r(cp: CompiledPrompt, fm_id: str, ok: bool, metric: str, value: float | None = None, thr_text: str = "",
       ev: str = "") -> CheckResult:
    kind = policy.meta("CHK-P01").kind
    return CheckResult(check_id="CHK-P01", fm_ids=[fm_id], subject_sha=cp.sha256, kind=kind, passed=ok, metric=metric,
                       value=None if value is None else float(value), threshold=thr_text, evidence=ev[:200],
                       thresholds_version=TH.THRESHOLDS_VERSION)


def _has(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None


def lint_prompt(cp: CompiledPrompt, ctx: LintCtx, banned: Any = None) -> list[CheckResult]:
    """One CheckResult per rule (CHK-P01, ASSERT). Text-kind templates (Tripo) get the reduced rule set. An exception here is turned
    into ``ran=False, passed=False`` by ``checks/runner.py``: the lint fails closed."""
    from duoskin.prompts.catalog import _default_banned

    banned = banned or _default_banned()
    try:
        meta = registry.get(cp.template_id).meta
    except registry.TemplateError:
        meta = None
    flat = bool(meta.flat) if meta else cp.template_id.startswith("R")
    kind = meta.kind if meta else "image"
    full = norm(cp.text)
    res: list[CheckResult] = []

    def add(fm: str, ok: bool, metric: str, value: float | None = None, thr_text: str = "", ev: str = "") -> None:
        res.append(_r(cp, fm, ok, metric, value, thr_text, ev))

    add("PRM-04", not HEX.search(cp.text), "hex_in_prompt")
    add("PRM-05", not SLOT_LEAK.search(cp.text), "slot_leak")
    add("PRM-10", not (ctx.story and norm(ctx.story) in full), "story_in_prompt")
    hits = [w for w in sorted(ctx.pair_words) if _has(full, w)]
    add("PRM-10", not hits, "pair_structure_words", len(hits), "0", ", ".join(hits))
    add("PRM-10", not COLOUR_COUNT.search(cp.text), "colour_count_or_ratio")
    stale = STATELESS.search(cp.text)
    add("PRM-12", stale is None, "stateless_reference", ev=stale.group(0) if stale else "")
    for grp in banned.everywhere:
        hh = banned.hits(cp.text, [grp])
        add("PRM-06", not hh, f"banned_{grp}", len(hh), "0", ", ".join(hh))
    if kind == "text":
        add("PRM-01", len(cp.text) <= thr("prm.max_chars_total"), "chars_total", len(cp.text), "<= prm.max_chars_total")
        n_sent = len([x for x in _SENTENCES.split(cp.text) if x.strip()])
        add("PRM-01", n_sent <= TEXT_PROMPT_MAX_SENTENCES, "sentences", n_sent, f"<= {TEXT_PROMPT_MAX_SENTENCES} (bible §14.2)")
        return res

    s = split_flat(cp.text) if flat else split_sections(cp.text)
    positive = norm(" ".join(s.get(k, "") for k in POSITIVE))
    core = norm(" ".join(s.get(k, "") for k in CORE))
    bud = meta.budget if meta else {}
    must_n = count_must(cp.text, flat)
    n_excl_style = len(cp.text) - len(s.get("STYLE", ""))            # the verbatim STYLE block is not counted
    excl = [x for x in s.get("EXCLUDE", "").rstrip(".").split(",") if x.strip()]
    roles = len(re.findall(r"Image \d+ =", s.get("IMAGES", "")))
    add("PRM-01", must_n <= thr("prm.must_max"), "must_lines", must_n, "<= prm.must_max")
    lim_excl = bud.get("max_chars_excl_style", thr("prm.max_chars_excl_style"))
    lim_total = bud.get("max_chars_total", thr("prm.max_chars_total"))
    add("PRM-01", n_excl_style <= lim_excl, "chars_excl_style", n_excl_style,
        f"<= {lim_excl}" + ("" if "max_chars_excl_style" in bud else " (prm.max_chars_excl_style)"))
    add("PRM-01", len(cp.text) <= lim_total, "chars_total", len(cp.text),
        f"<= {lim_total}" + ("" if "max_chars_total" in bud else " (prm.max_chars_total)"))
    add("PRM-01", len(excl) <= thr("prm.exclude_nouns_max"), "exclude_nouns", len(excl), "<= prm.exclude_nouns_max")
    add("PRM-01", len(re.findall(r"^KEEP:", cp.text, re.MULTILINE)) <= 1 and len(re.findall(r"^EXCLUDE:", cp.text, re.MULTILINE)) <= 1,
        "keep_exclude_one_line")
    add("PRM-10", len(cp.dna_fields) <= thr("prm.dna_fields_max"), "dna_fields", len(cp.dna_fields), "<= prm.dna_fields_max")
    add("PRM-07", len(cp.images) == ctx.n_attached_images == roles, "image_roles", roles, f"== {ctx.n_attached_images} attached")
    add("PRM-09", ctx.house_style_block in cp.text, "house_style_verbatim")
    hh = banned.hits(" ".join(s.get(k, "") for k in POSITIVE), [TEXT_INVITING])
    add("PRM-02", not hh, "text_inviting_outside_OUTPUT_EXCLUDE", len(hh), "0", ", ".join(hh))
    prim = registry.priming_table().get(cp.template_id.split("@")[0], frozenset())
    ph = [w for w in sorted(prim) if _has(core, w)]
    add("PRM-02", not ph, "priming_words_outside_EXCLUDE", len(ph), "0", ", ".join(ph))
    if ctx.background == "transparent":
        from duoskin.prompts.catalog import data_json

        backdrop = data_json("rules.json")["lint"]["backdrop_words"]
        bh = [w for w in sorted(backdrop) if _has(positive, w)]
        add("PRM-03", not bh, "backdrop_words", len(bh), "0", ", ".join(bh))
        add("PRM-03", "fully transparent background" in norm(s.get("OUTPUT", "")), "isolation_line_in_OUTPUT")
    return res
