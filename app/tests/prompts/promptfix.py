"""Helpers of the prompt tests: valid inputs for every template, spec mutation. Test code only."""
from __future__ import annotations

import itertools
import typing
from collections.abc import Iterator
from typing import Any

import specfix

from duoskin.models import kitenums
from duoskin.models.spec import CharacterDNA, DuoSpec
from duoskin.prompts import registry

FIX = "Make the main shape rounder."
SUBJECT = "A small leaf print."
EDIT = ["Make the leaf larger."]
KEEP = ["the colours", "the outline weight"]


def image_ids(kinds=("image", "text")) -> list[str]:
    return [i for i in registry.template_ids() if registry.get(i).meta.kind in kinds]


def _flag_combos(meta: registry.TemplateMeta, flags: tuple[str, ...] | None) -> Iterator[dict[str, bool]]:
    names = [n for n, d in meta.inputs.items() if d.kind == "flag"] if flags is None else list(flags)
    for combo in itertools.product([False, True], repeat=len(names)):
        yield dict(zip(names, combo, strict=True))


def input_variants(template_id: str, spec: DuoSpec, char: str | None, *, all_flags: bool = False) -> Iterator[dict[str, Any]]:
    """Every valid ``slots`` dict of a template for one character; yields nothing when the spec lacks what the template needs
    (a print, an accessory)."""
    meta = registry.get(template_id).meta
    me = None if char is None else (spec.a if char == "a" else spec.b)
    domains: list[list[tuple[str, Any]]] = []
    for name, decl in meta.inputs.items():
        if decl.kind == "flag":
            continue
        if decl.values and name != "print":
            domains.append([(name, v) for v in decl.values])
        elif name == "print":
            if me is None:
                return
            if decl.values == ["shoes"]:
                if not me.bottom.shoes.motif:
                    return
                domains.append([(name, "shoes")])
            else:
                refs = [f"top.{i}" for i in range(len(me.top.prints))] + [f"bottom.{i}" for i in range(len(me.bottom.prints))]
                if not refs:
                    return
                domains.append([(name, r) for r in refs])
        elif name == "accessory":
            if me is None or not me.accessories:
                if decl.required:
                    return
                continue
            domains.append([(name, i) for i in range(len(me.accessories))])
        elif name == "fix_sentence":
            domains.append([(name, FIX)])
        elif name == "subject_sentence":
            domains.append([(name, SUBJECT)])
        elif name == "edit":
            domains.append([(name, list(EDIT))])
        elif name in ("keep", "template_keep"):
            domains.append([(name, list(KEEP))])
        elif name == "fabric_id":
            domains.append([(name, next(iter(kitenums.current_inventory().fabrics)))])
        elif name == "recipe_id":
            domains.append([(name, next(iter(kitenums.current_inventory().recipes)))])
        elif decl.required:
            raise AssertionError(f"promptfix has no value for the required input {template_id}.{name}")
    for combo in itertools.product(*domains) if domains else [()]:
        base = dict(combo)
        flag_sets = list(_flag_combos(meta, None if all_flags else ()))
        for fl in flag_sets:
            yield {**base, **fl}


def with_other_dna_changed(spec: DuoSpec, char: str) -> DuoSpec:
    """A copy of the spec in which every CHARACTER DNA field of the *other* character differs (rules skipped)."""
    d = spec.model_dump(mode="json")
    other = "b" if char == "a" else "a"
    dna = d[other]["dna"]
    for name, field in CharacterDNA.model_fields.items():
        args = typing.get_args(field.annotation)
        if args and all(isinstance(x, str) for x in args):
            dna[name] = next(x for x in args if x != dna[name])
        else:
            dna[name] = "zzqx orbit ring" if name == "motif_object" else "zzqx quiet"
    return DuoSpec.model_validate(d, context={"skip_rules": True})


def with_world_changed(spec: DuoSpec) -> DuoSpec:
    d = spec.model_dump(mode="json")
    d["world"]["detail_level"] = {"minimal": "maximal", "standard": "minimal", "maximal": "minimal"}[d["world"]["detail_level"]]
    return DuoSpec.model_validate(d, context={"skip_rules": True})


def specs() -> dict[str, DuoSpec]:
    return specfix.all_specs()
