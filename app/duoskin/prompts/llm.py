"""Compile the Claude (and Gemini) role prompts: system blocks + the user message (bible §8, §9, §10.5, §12.2, §16, §17).

``compile_llm(template_id, inputs)`` returns an :class:`LlmPrompt`: the cached system blocks (shared blocks, then the role text),
the user text with every data slot wrapped in its tag, the route (model, effort, ``max_tokens``, fallbacks) and the schema name that
``providers/anthropic_llm.py::call`` turns into a JSON schema. Data is text from the user, a model or an image and is never an
instruction (``SHARED_CONTEXT`` says so); user text only ever appears inside its tag.

The planner prompt carries **no example spec**: ``tests/prompts/test_planner_prompt_has_no_example`` checks the compiled L3 system
prompt for a JSON object with a ``palette`` key and for the retired example words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from duoskin.models import llm_io
from duoskin.models.common import canonical_json, sha256_of
from duoskin.models.kitenums import KitInventory
from duoskin.prompts import registry
from duoskin.prompts import template_lang as TL
from duoskin.prompts.slots import PromptBuildError
from duoskin.prompts.system_blocks import build_system


@dataclass(frozen=True)
class LlmPrompt:
    template_id: str
    template_version: int
    system: list[dict[str, Any]]
    user_text: str
    route: dict[str, Any]
    schema_name: str
    sha256: str
    images: str = ""                 # what the caller must attach before the text (a note for the pipeline)
    order_seed: int | None = None    # the seed of the shuffled <kit_inventory> / <structure_profiles> order, or None (sorted); logged by the caller


# Anything that could start a tag, a closing tag (also ``< /x>``), a comment, CDATA, a processing instruction or a doctype, with or without
# attributes (``</brief x="1">``), namespaces or hyphens, and with or without its closing ``>``. ``a < b``, ``3 <3`` and ``c > d`` stay as they are.
_TAG_LIKE = re.compile(r"<(?=\s*/|[A-Za-z_!?])([^<>]*)(>?)")
_ANGLE_LOOKALIKES = str.maketrans({"\uff1c": "<", "\uff1e": ">", "\ufe64": "<", "\ufe65": ">", "\u3008": "<", "\u3009": ">"})


def neutralise_tags(text: str) -> str:
    """Data may not close or open one of the prompt's tags: ``</user_change_request>`` inside a value becomes plain text (``&lt;...&gt;``),
    so a typed request cannot break out of its tag (bible §2: user text only ever appears inside its tag, as data). Full-width and other
    look-alike angle brackets are folded to ASCII first, and a tag with attributes, spaces, a namespace or no closing ``>`` is neutralised too."""
    return _TAG_LIKE.sub(lambda m: f"&lt;{m.group(1)}{'&gt;' if m.group(2) else ''}", text.translate(_ANGLE_LOOKALIKES))


def as_text(value: Any) -> str:
    """A slot value as text: strings stay as they are, everything else becomes canonical (sorted) JSON, so the cache key is stable.
    Tag-like text inside a value is neutralised (:func:`neutralise_tags`)."""
    if isinstance(value, str):
        return neutralise_tags(value)
    if isinstance(value, bytes):
        return neutralise_tags(value.decode("utf-8"))
    return neutralise_tags(canonical_json(value).decode("utf-8"))


def compile_llm(template_id: str, inputs: dict[str, Any] | None = None, *, inventory: KitInventory | None = None,
                ttl_1h: bool = False, order_seed: int | None = None) -> LlmPrompt:
    """Compile one LLM role template. Missing required inputs raise :class:`PromptBuildError`; ``flags`` are the booleans the user
    text branches on (``wildcard``, ``replacement``). ``order_seed`` shows the kit inventory and the structure profiles in a seeded
    order instead of sorted order (``system_blocks.order_seed(project_id, round)``): no kit id is listed first in every duo's prompt."""
    tpl = registry.get(template_id)
    meta = tpl.meta
    if meta.kind != "llm":
        raise PromptBuildError(f"{meta.id} is not an LLM template")
    given = dict(inputs or {})
    unknown = sorted(set(given) - set(meta.inputs))
    if unknown:
        raise PromptBuildError(f"{meta.id}: unknown input(s) {unknown}; declared: {sorted(meta.inputs)}")
    values: dict[str, Any] = {}
    flags: dict[str, bool] = {f: False for f in meta.flags}
    for name, decl in meta.inputs.items():
        if name not in given:
            if decl.required:
                raise PromptBuildError(f"{meta.id}: input {name!r} is required")
            if decl.kind == "flag":
                flags[name] = False
            else:
                values[name] = ""
            continue
        if decl.kind == "flag":
            flags[name] = bool(given[name])
        else:
            values[name] = as_text(given[name])
    for f in meta.flags:
        flags.setdefault(f, False)
    for slot in TL.slots_used(tpl.user_nodes):
        values.setdefault(slot, "")
    try:
        user = TL.render(tpl.user_nodes, values, flags).strip() if tpl.user_nodes else ""
    except TL.EmptySlot as exc:
        raise PromptBuildError(f"{meta.id}: required slot {exc.name!r} is empty") from exc
    except TL.MissingSlot as exc:
        raise PromptBuildError(f"{meta.id}: slot {exc.args[0]!r} has no value") from exc
    route_name = meta.id.split(".")[0]
    plan_loop = bool(meta.cache.get("plan_loop"))
    system = build_system(tpl.role_text, plan_loop=plan_loop, inventory=inventory, ttl_1h=ttl_1h, seed=order_seed) if meta.provider == "anthropic" else \
        [{"type": "text", "text": tpl.role_text}]
    sha = sha256_of({"id": meta.id, "version": meta.version, "role": tpl.role_text, "user": user, "route": meta.route,
                     "plan_loop": plan_loop, "schema": meta.schema_name, **({} if order_seed is None else {"order_seed": order_seed})})
    _ = route_name
    return LlmPrompt(template_id=meta.id, template_version=meta.version, system=system, user_text=user, route=dict(meta.route),
                     schema_name=meta.schema_name, sha256=sha, images=str(meta.cache.get("images", "")), order_seed=order_seed)


def schema_class(prompt: LlmPrompt) -> type:
    """The pydantic class of the prompt's route schema (``PlanSet``, ``Critique``, ...)."""
    from duoskin.models import spec

    if prompt.schema_name in llm_io.SCHEMA_CLASSES:
        return llm_io.SCHEMA_CLASSES[prompt.schema_name]
    return spec.SPEC_SCHEMAS[prompt.schema_name]
