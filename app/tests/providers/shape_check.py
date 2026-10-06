"""Validate a JSON-like value against a ``TypedDict`` / typing hint taken from an installed SDK.

The provider adapters were only ever run against mocks, so a wrong key name or a wrong nesting would show up on the user's first
paid call. This module is the offline ground truth: it walks a captured request body and reports

* **unknown keys** (a key that the SDK's request type does not declare),
* missing required keys,
* values of the wrong type or outside a ``Literal``.

``TypedDict`` classes that declare ``extra_items`` (e.g. ``BetaFallbackParam``) accept any extra key. Hints are read with
``typing_extensions.get_type_hints(include_extras=True)`` so ``Required[...]``, ``NotRequired[...]`` and ``Annotated[...]`` are
unwrapped here (``PropertyInfo(alias=...)`` is honoured for the wire name).
"""
from __future__ import annotations

import collections.abc as cabc
import types
import typing
from typing import Any

import typing_extensions as te

_NONE = type(None)


def _unwrap(hint: Any) -> Any:
    """Strip Required / NotRequired / Annotated wrappers."""
    while True:
        origin = te.get_origin(hint)
        if origin in (te.Required, te.NotRequired, te.Annotated):
            hint = te.get_args(hint)[0]
        else:
            return hint


def _alias_of(hint: Any, name: str) -> str:
    """The wire name of a field (``Annotated[..., PropertyInfo(alias="from")]``)."""
    h = hint
    while True:
        origin = te.get_origin(h)
        if origin in (te.Required, te.NotRequired):
            h = te.get_args(h)[0]
        elif origin is te.Annotated:
            for meta in te.get_args(h)[1:]:
                alias = getattr(meta, "alias", None)
                if isinstance(alias, str):
                    return alias
            return name
        else:
            return name


def typeddict_fields(cls: type) -> dict[str, tuple[str, Any, bool]]:
    """``wire name -> (python name, hint, required)`` for a TypedDict class."""
    hints = te.get_type_hints(cls, include_extras=True)
    required = set(getattr(cls, "__required_keys__", ()))
    out: dict[str, tuple[str, Any, bool]] = {}
    for name, h in hints.items():
        out[_alias_of(h, name)] = (name, _unwrap(h), name in required)
    return out


def known_keys(cls: type) -> set[str]:
    return set(typeddict_fields(cls))


def check(value: Any, hint: Any, path: str = "$") -> list[str]:
    """Problems of ``value`` against ``hint`` (an empty list means it fits)."""
    hint = _unwrap(hint)
    if hint is Any or hint is object:
        return []
    origin = te.get_origin(hint)
    args = te.get_args(hint)

    if te.is_typeddict(hint):
        return _check_typeddict(value, hint, path)

    if origin in (typing.Union, types.UnionType):
        best: list[str] | None = None
        best_score: tuple[int, int] = (2, 0)
        for a in args:
            errs = check(value, a, path)
            if not errs:
                return []
            # report the member the value is shaped like: a bare "expected <type>" at this path is the weakest explanation
            score = (1 if len(errs) == 1 and errs[0].startswith(f"{path}: expected") else 0, len(errs))
            if best is None or score < best_score:
                best, best_score = errs, score
        return best or [f"{path}: no union member fits {value!r}"]

    if origin is typing.Literal or origin is te.Literal:
        return [] if value in args else [f"{path}: {value!r} is not one of {list(args)}"]

    if hint is _NONE or hint is None:
        return [] if value is None else [f"{path}: expected null, got {value!r}"]

    if origin in (list, cabc.Iterable, cabc.Sequence, cabc.Collection, set, frozenset) or (
            isinstance(hint, type) and getattr(hint, "__name__", "") == "SequenceNotStr"):
        if not isinstance(value, list):
            return [f"{path}: expected an array, got {type(value).__name__}"]
        item = args[0] if args else Any
        errs: list[str] = []
        for i, v in enumerate(value):
            errs += check(v, item, f"{path}[{i}]")
        return errs

    if origin in (dict, cabc.Mapping, cabc.MutableMapping):
        if not isinstance(value, dict):
            return [f"{path}: expected an object, got {type(value).__name__}"]
        vt = args[1] if len(args) == 2 else Any
        errs = []
        for k, v in value.items():
            errs += check(v, vt, f"{path}.{k}")
        return errs

    if hint is str:
        return [] if isinstance(value, str) else [f"{path}: expected string, got {type(value).__name__}"]
    if hint is bool:
        return [] if isinstance(value, bool) else [f"{path}: expected boolean, got {type(value).__name__}"]
    if hint is int:
        return [] if isinstance(value, int) and not isinstance(value, bool) else [f"{path}: expected integer, got {type(value).__name__}"]
    if hint is float:
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) else [f"{path}: expected number, got {type(value).__name__}"]

    # a type we do not model (``Omit``, ``NotGiven``, file types, ...): a plain isinstance when possible
    if isinstance(hint, type):
        try:
            return [] if isinstance(value, hint) else [f"{path}: expected {hint.__name__}, got {type(value).__name__}"]
        except TypeError:
            return []
    return []


def _check_typeddict(value: Any, cls: type, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: expected an object for {cls.__name__}, got {type(value).__name__}"]
    fields = typeddict_fields(cls)
    extra_ok = getattr(cls, "__extra_items__", te.NoExtraItems) not in (te.NoExtraItems, None)
    errs: list[str] = []
    for k, v in value.items():
        if k not in fields:
            if not extra_ok:
                errs.append(f"{path}: unknown key {k!r} (not a parameter of {cls.__name__})")
            continue
        errs += check(v, fields[k][1], f"{path}.{k}")
    for k, (_py, _h, req) in fields.items():
        if req and k not in value:
            errs.append(f"{path}: missing required key {k!r} of {cls.__name__}")
    return errs
