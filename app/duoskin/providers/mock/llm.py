"""MockLLM: a deterministic Claude stand-in that returns a valid object of the requested schema (APP_SPEC 7.8).

How an answer is built for ``call(route, ..., out=Model)``:

1. ``providers/fixtures/plansets/<name>.json``: for the planner route, when the brief text contains a fixture's name and the
   file validates against ``out``, that fixture is the answer.
2. A role builder registered with ``register_role(role_name, builder)`` (``role_name`` is the route, for example ``"L3_planner"``,
   or the schema class name). Builders are looked up lazily: the first call imports the modules in ``LAZY_ROLE_MODULES`` and
   calls their ``register_mock_roles(register_role)``, so the DuoSpec schema and its planner generator (written by another
   track) plug in without this file importing them. A builder receives a ``RoleCall`` and returns a model, a dict or JSON text.
3. The generic fallback: ``fabricate_from_schema`` builds JSON from ANY JSON schema (``$ref``, enums, ``anyOf``, patterns, lengths,
   ranges, array bounds), with name-based heuristics that make the common roles behave: rule verdict arrays answer exactly the
   requested ``rule_id`` set with ``pass`` (L11; the ``fail_rule`` fault fails the first), pairwise ``first``/``second``/``tie`` fields
   are decided by a hash of the content so the both-orders logic sees agreement and ties, scores and levels are fixed, and a
   ``needs_clarification`` status is chosen unless the request names a colour change or "bigger".

Everything is a function of the canonical request, so reruns give the same answers. The same ``finish_call`` as the real adapter does
the ``stop_reason`` and validation handling, so refusal, truncation and schema violations produce the real errors. Fake thinking
progress is streamed through ``ctx``; usage and the prompt-cache behaviour are simulated (a second call of a route with the same
system block reads the cache).
"""
from __future__ import annotations

import importlib
import json
import random
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from duoskin.checks.model import CheckResult
from duoskin.providers.anthropic_llm import (
    BatchItem,
    BatchItemResult,
    CacheMonitor,
    LLMResult,
    RouteCfg,
    build_routes,
    finish_call,
)
from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, jsonable, request_hash, seed_from_hash
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock._common import MockBase
from duoskin.providers.pricing import claude_cost

PROVIDER = "anthropic"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plansets"
LAZY_ROLE_MODULES = ("duoskin.providers.mock.roles", "duoskin.models.mock_roles", "duoskin.pipeline.mock_roles", "duoskin.models.llm_io")
_NO = object()


# --------------------------------------------------------------------------------------------------------------
# Role registry
# --------------------------------------------------------------------------------------------------------------

@dataclass
class RoleCall:
    """What a role builder gets."""

    route: str
    out: type[BaseModel]
    schema: dict[str, Any]
    system_text: str
    content_text: str
    content: list[dict[str, Any]]
    seed: int
    rng: random.Random
    prompt_version: int
    faults: frozenset[str] = frozenset()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def brief(self) -> str:
        """The text of ``<user_brief>`` when the content has one, else the whole text."""
        m = re.search(r"<user_brief>(.*?)</user_brief>", self.content_text, re.DOTALL)
        return (m.group(1) if m else self.content_text).strip()

    def fabricate(self, **overrides: Any) -> dict[str, Any]:
        """The generic answer for this call as a dict, with top-level ``overrides`` applied (a builder can start from it)."""
        obj = fabricate_from_schema(self.schema, self.rng, hints=Hints(self))
        if isinstance(obj, dict):
            obj.update(overrides)
        return obj


RoleBuilder = Callable[[RoleCall], Any]
_ROLES: dict[str, RoleBuilder] = {}
_ROLES_LOCK = threading.Lock()
_LOADED = False


def register_role(role_name: str, builder: RoleBuilder) -> None:
    """Register ``builder`` for a route name (``"L3_planner"``) or a schema class name (``"PlanSet"``)."""
    with _ROLES_LOCK:
        _ROLES[role_name] = builder


def unregister_role(role_name: str) -> None:
    with _ROLES_LOCK:
        _ROLES.pop(role_name, None)


def registered_roles() -> list[str]:
    _ensure_roles_loaded()
    with _ROLES_LOCK:
        return sorted(_ROLES)


def _ensure_roles_loaded() -> None:
    """Import the optional role modules once. A module may define ``register_mock_roles(register_role)``."""
    global _LOADED
    if _LOADED:
        return
    with _ROLES_LOCK:
        if _LOADED:
            return
        _LOADED = True
    for name in LAZY_ROLE_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        except Exception:  # noqa: BLE001, S112 - a broken optional module must not break mock mode
            continue
        fn = getattr(mod, "register_mock_roles", None)
        if callable(fn):
            fn(register_role)


def reload_roles() -> None:
    """Forget that the lazy modules were loaded (tests)."""
    global _LOADED
    with _ROLES_LOCK:
        _LOADED = False


# --------------------------------------------------------------------------------------------------------------
# Pattern sampler (enough regex for schema ``pattern`` fields)
# --------------------------------------------------------------------------------------------------------------

_PRINTABLE = "abcdefghijklmnopqrstuvwxyz0123456789"
_CLASS_ESC = {"d": "0123456789", "w": _PRINTABLE + "_", "s": " "}


def sample_pattern(pattern: str, rng: random.Random, tries: int = 12) -> str | None:
    """A string matching ``pattern`` (anchors, literals, escapes, classes, groups, alternation, quantifiers), or ``None``."""
    for _ in range(tries):
        try:
            s, pos = _alt(pattern, 0, rng)
        except (IndexError, ValueError):
            return None
        if pos == len(pattern) and re.fullmatch(pattern, s):
            return s
    return None


def _alt(p: str, i: int, rng: random.Random) -> tuple[str, int]:
    branches: list[tuple[int, int]] = []
    start = i
    depth = 0
    j = i
    cls = False
    while j < len(p):
        ch = p[j]
        if ch == "\\":
            j += 2
            continue
        if cls:
            cls = ch != "]"
        elif ch == "[":
            cls = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "|" and depth == 0:
            branches.append((start, j))
            start = j + 1
        j += 1
    branches.append((start, j))
    a, b = rng.choice(branches)
    return _seq(p, a, b, rng), j


def _seq(p: str, i: int, end: int, rng: random.Random) -> str:
    out = []
    while i < end:
        atom, i = _atom(p, i, end, rng)
        lo, hi, i = _quant(p, i, end)
        n = rng.randint(lo, hi)
        out.append("".join(atom() for _ in range(n)))
    return "".join(out)


def _quant(p: str, i: int, end: int) -> tuple[int, int, int]:
    if i < end:
        c = p[i]
        if c == "?":
            return 0, 1, i + 1
        if c == "*":
            return 0, 3, i + 1
        if c == "+":
            return 1, 3, i + 1
        if c == "{":
            j = p.index("}", i)
            body = p[i + 1:j]
            if "," in body:
                a, b = body.split(",")
                lo = int(a or 0)
                return lo, int(b) if b else lo + 3, j + 1
            return int(body), int(body), j + 1
    return 1, 1, i


def _atom(p: str, i: int, end: int, rng: random.Random) -> tuple[Callable[[], str], int]:
    c = p[i]
    if c in "^$":
        return (lambda: ""), i + 1
    if c == "(":
        j = i + 1
        if p.startswith("?:", j):
            j += 2
        k, depth = j, 1
        while depth:
            if p[k] == "\\":
                k += 2
                continue
            depth += {"(": 1, ")": -1}.get(p[k], 0)
            k += 1
        inner = p[j:k - 1]
        return (lambda: _alt(inner, 0, rng)[0]), k
    if c == "[":
        j = i + 1
        neg = p[j] == "^"
        if neg:
            j += 1
        chars: list[str] = []
        while p[j] != "]" or (not chars and j == i + 1 + neg):
            if p[j] == "\\":
                e = p[j + 1]
                chars += list(_CLASS_ESC.get(e, e))
                j += 2
            elif j + 2 < len(p) and p[j + 1] == "-" and p[j + 2] != "]":
                chars += [chr(x) for x in range(ord(p[j]), ord(p[j + 2]) + 1)]
                j += 3
            else:
                chars.append(p[j])
                j += 1
        pool = [x for x in _PRINTABLE if x not in chars] if neg else chars
        return (lambda: rng.choice(pool)), j + 1
    if c == "\\":
        e = p[i + 1]
        pool = list(_CLASS_ESC.get(e.lower(), e))
        return (lambda: rng.choice(pool)), i + 2
    if c == ".":
        return (lambda: rng.choice(_PRINTABLE)), i + 1
    return (lambda: c), i + 1


# --------------------------------------------------------------------------------------------------------------
# Generic fabricator
# --------------------------------------------------------------------------------------------------------------

class Hints:
    """Name-based behaviour for the common roles (see the module docstring)."""

    def __init__(self, call: RoleCall | None = None) -> None:
        self.call = call
        self.text = call.content_text if call else ""
        self.fail_rule = bool(call and "fail_rule" in call.faults)
        self._failed_one = False

    def requested_rule_ids(self, enum_values: Sequence[str] | None) -> list[str]:
        """The rule ids the request asks about: enum values mentioned in the content, in order of appearance; without an enum,
        ids listed as ``- rule_id:``, ``rule_id="..."`` or ``"rule_id": "..."``."""
        found: list[tuple[int, str]] = []
        if enum_values:
            for v in enum_values:
                m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(v)}(?![A-Za-z0-9_])", self.text)
                if m:
                    found.append((m.start(), v))
            return [v for _, v in sorted(found)]
        for m in re.finditer(r'(?m)^\s*[-*]\s*`?([a-z][a-z0-9_]{2,})`?\s*[:|]|rule[_ ]id["\']?\s*[:=]\s*["\']?([a-z][a-z0-9_]{2,})', self.text):
            rid = m.group(1) or m.group(2)
            if rid not in [v for _, v in found]:
                found.append((m.start(), rid))
        return [v for _, v in sorted(found)]

    def pick_pair(self, values: Sequence[str], path: str) -> str:
        """First or second by a hash of the content: order-dependent, so both orders sometimes agree and sometimes tie."""
        h = int(request_hash({"t": self.text, "p": path})[:8], 16)
        pool = [v for v in values if v in ("first", "second", "a", "b")] or list(values)
        return pool[h % len(pool)]

    def clarification_needed(self) -> bool:
        t = self.text.lower()
        names = "red|blue|green|teal|pink|purple|orange|yellow|black|white|grey|gray|brown|navy|gold|silver"
        return not (re.search(rf"\bmake\b.*\b({names})\b", t) or "bigger" in t)

    def value_for(self, name: str, sub: dict[str, Any], path: str, rng: random.Random) -> Any:
        enum = sub.get("enum")
        lname = name.lower()
        if enum and isinstance(enum, list):
            if lname == "verdict" and "pass" in enum:
                if self.fail_rule and not self._failed_one:
                    self._failed_one = True
                    return "fail"
                return "pass"
            if "first" in enum and "second" in enum:
                return self.pick_pair(enum, path)
            if "needs_clarification" in enum:
                if self.clarification_needed():
                    return "needs_clarification"
                for pref in ("ready", "ok", "interpreted", "applied", "proposed", "awaiting_confirm"):
                    if pref in enum:
                        return pref
                return next(v for v in enum if v != "needs_clarification")
        if sub.get("type") == "boolean" and lname in ("pass", "passed", "ok", "valid", "matches"):
            if self.fail_rule and not self._failed_one:
                self._failed_one = True
                return False
            return True
        if sub.get("type") in ("integer", "number") and lname in ("score", "level", "rating", "overall", "overall_score", "quality"):
            lo, hi = sub.get("minimum"), sub.get("maximum")
            if lo is not None and hi is not None:
                v = lo + 0.7 * (hi - lo)
                return round(v) if sub.get("type") == "integer" else round(v, 3)
            return 4 if sub.get("type") == "integer" else 0.8
        return _NO


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while "$ref" in schema and seen < 20:
        ref = schema["$ref"]
        node: Any = root
        for part in ref.lstrip("#/").split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        schema = {**node, **{k: v for k, v in schema.items() if k != "$ref"}}
        seen += 1
    return schema


def _hexcolor(rng: random.Random) -> str:
    return f"#{rng.randrange(40, 230):02X}{rng.randrange(40, 230):02X}{rng.randrange(40, 230):02X}"


def fabricate_from_schema(schema: dict[str, Any], rng: random.Random, *, root: dict[str, Any] | None = None, name: str = "",
                          hints: Hints | None = None, depth: int = 0, path: str = "$", minimal: bool = False) -> Any:
    """Build a JSON value that satisfies ``schema`` (best effort: Pydantic-only rules are the validator's business)."""
    root = root if root is not None else schema
    hints = hints or Hints()
    schema = _resolve(schema, root)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][rng.randrange(len(schema["enum"]))]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            opts = [o for o in schema[key] if _resolve(o, root).get("type") != "null"] or schema[key]
            return fabricate_from_schema(opts[0], rng, root=root, name=name, hints=hints, depth=depth, path=path, minimal=minimal)
    if schema.get("allOf"):
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            part = _resolve(part, root)
            for k, v in part.items():
                if k == "properties":
                    merged.setdefault("properties", {}).update(v)
                elif k == "required":
                    merged.setdefault("required", []).extend(v)
                else:
                    merged[k] = v
        return fabricate_from_schema({**{k: v for k, v in schema.items() if k != "allOf"}, **merged}, rng, root=root, name=name, hints=hints,
                                     depth=depth, path=path, minimal=minimal)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])
    if t is None:
        t = "object" if "properties" in schema else "array" if "items" in schema else "string"
    if t == "object":
        out: dict[str, Any] = {}
        for k, sub in (schema.get("properties") or {}).items():
            sub_r = _resolve(sub, root)
            if depth > 6 and (sub_r.get("type") == "object" or "properties" in sub_r):      # recursive schemas stop growing here
                continue
            v = hints.value_for(k, sub_r, f"{path}.{k}", rng)
            out[k] = v if v is not _NO else fabricate_from_schema(sub_r, rng, root=root, name=k, hints=hints, depth=depth + 1,
                                                                  path=f"{path}.{k}", minimal=minimal)
        return out
    if t == "array":
        items = _resolve(schema.get("items") or {}, root)
        lo, hi = int(schema.get("minItems", 0)), schema.get("maxItems")
        iprops = items.get("properties") or {}
        if "rule_id" in iprops:
            rid_schema = _resolve(iprops["rule_id"], root)
            ids = hints.requested_rule_ids(rid_schema.get("enum"))
            if ids:
                n = len(ids)
                res = []
                for k in range(n):
                    obj = fabricate_from_schema(items, rng, root=root, name=name, hints=hints, depth=depth + 1, path=f"{path}[{k}]", minimal=minimal)
                    obj["rule_id"] = ids[k]
                    res.append(obj)
                return res
        n = max(lo, 0 if (minimal or depth > 6) else 1)
        if hi is not None:
            n = min(n, int(hi))
        if depth > 24:                                   # a pathological recursive schema: stop rather than overflow the stack
            n = 0
        return [fabricate_from_schema(items, rng, root=root, name=name, hints=hints, depth=depth + 1, path=f"{path}[{k}]", minimal=minimal)
                for k in range(n)]
    if t == "string":
        return _fab_string(schema, rng, name)
    if t in ("integer", "number"):
        lo = schema.get("minimum", schema.get("exclusiveMinimum"))
        hi = schema.get("maximum", schema.get("exclusiveMaximum"))
        if lo is None and hi is None:
            v: float = rng.randint(1, 5)
        elif lo is None:
            v = hi - 1 if hi is not None else 0
        elif hi is None:
            v = lo + 1
        else:
            v = lo + (hi - lo) / 2
        if "exclusiveMinimum" in schema and v <= schema["exclusiveMinimum"]:
            v = schema["exclusiveMinimum"] + 1
        return round(v) if t == "integer" else float(v)
    if t == "boolean":
        return True
    if t == "null":
        return None
    return None


def _fab_string(schema: dict[str, Any], rng: random.Random, name: str) -> str:
    lo, hi = int(schema.get("minLength", 0)), schema.get("maxLength")
    pat = schema.get("pattern")
    if pat:
        s = sample_pattern(pat, rng)
        if s is not None:
            return s if hi is None else s[: int(hi)]
        if "#" in pat and "0-9" in pat:
            return _hexcolor(rng)
    fmt = schema.get("format")
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if fmt in ("uri", "url"):
        return "https://example.invalid/mock"
    if fmt == "email":
        return "mock@example.invalid"
    desc = str(schema.get("description", "")).lower()
    if "hex" in desc and "#" in desc:
        return _hexcolor(rng)
    s = f"Mock {name.replace('_', ' ')}".strip() if name else "Mock text"
    while len(s) < lo:
        s += " text"
    return s[: int(hi)] if hi is not None else s


# --------------------------------------------------------------------------------------------------------------
# The mock provider
# --------------------------------------------------------------------------------------------------------------

def _text_of(blocks: Sequence[dict[str, Any]]) -> str:
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(str(b.get("text", "")))
        elif b.get("type") == "image":
            src = b.get("source", {})
            parts.append(f"[image {request_hash(src)[:8]}]")
    return "\n".join(parts)


def _tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))


class MockLLM(MockBase):
    """Deterministic Claude provider with the same public methods as ``AnthropicProvider``."""

    name = PROVIDER

    def __init__(self, *, routes: dict[str, RouteCfg] | None = None, faults: FaultInjector | None = None,
                 cost_sink: Callable[[dict[str, Any]], None] | None = None, flags: CapabilityFlags | None = None,
                 cache_monitor: CacheMonitor | None = None) -> None:
        super().__init__(faults=faults, cost_sink=cost_sink, flags=flags)
        self.routes = dict(routes or build_routes())
        self.monitor = cache_monitor or CacheMonitor()
        self._cache_seen: set[tuple[str, str]] = set()
        self._batches: dict[str, dict[str, Any]] = {}
        self.notes: list[str] = []

    def __repr__(self) -> str:
        return "MockLLM()"

    def with_pins(self, pins: Any) -> MockLLM:
        import copy
        clone = copy.copy(self)
        clone.routes = build_routes(planner=pins.planner, critic=pins.critic, judge=pins.judge, checker=pins.checker)
        return clone

    # ----- the one call function ---------------------------------------------------------------------------
    def call(self, route: str, *, system: list[dict], content: list[dict], out: type[BaseModel], ctx: CallCtx, prompt_version: int,
             max_tokens_override: int | None = None) -> LLMResult:
        cfg = self.routes.get(route)
        if cfg is None:
            raise ProviderError(PROVIDER, "bad_request", f"unknown route {route!r}", billed="no")
        from duoskin.providers.anthropic_llm import AnthropicProvider
        schema, schema_hash = _schema_of(out)
        kw = {"model": cfg.model, "max_tokens": int(max_tokens_override or cfg.max_tokens),
              "thinking": {"type": "adaptive", "display": "summarized"} if cfg.thinking else {"type": "disabled"},
              "output_config": {"effort": cfg.effort, "format": {"type": "json_schema", "schema": schema}}, "system": list(system),
              "messages": [{"role": "user", "content": list(content)}]}
        AnthropicProvider.preflight(kw, content)          # the same pre-flight as the real adapter (also checks the images)
        sys_text, text_in = _text_of(system), _text_of(content)
        digest = request_hash({"route": route, "system": system, "content": content, "out": out.__qualname__, "pv": prompt_version})
        self.record(PROVIDER, f"messages.stream:{route}", {"route": route, "system_sha": request_hash(system), "content_text": text_in,
                                                              "out": out.__qualname__, "prompt_version": prompt_version, "max_tokens": kw["max_tokens"]})
        ctx.tick()
        behaviour = self.fault(PROVIDER, tag=route)
        for k in range(3):                                          # a few fake thinking deltas
            ctx.progress((k + 1) / 4.0, "Claude is thinking")
            ctx.tick()
        rng = random.Random(seed_from_hash(digest))
        text: str | None
        stop = "end_turn"
        if behaviour == "refusal":
            stop, text = "refusal", None
        elif behaviour in ("truncated", "context_exceeded"):
            stop, text = ("max_tokens" if behaviour == "truncated" else "model_context_window_exceeded"), '{"truncated": '
        elif behaviour == "no_text":
            text = None
        else:
            text = self._answer(route, out, schema, rng, sys_text, text_in, prompt_version, frozenset({behaviour} if behaviour else ()))
            if behaviour in ("schema_invalid", "validation"):
                text = _corrupt(text, out)
        usage = self._usage(route, sys_text, text_in, text or "", system)
        served = cfg.model
        cost = self.record_cost(claude_cost(served, usage, operation=f"messages.stream:{route}", request_id=f"mock-claude-{digest[:12]}"))
        self.monitor.observe(route, usage)
        rid = f"mock-claude-{digest[:12]}"
        thinking = "Practice mode: a stand-in model read the request and wrote an answer that passes the checks."   # shown on the plan page: no route or schema names
        return finish_call(route=route, cfg=cfg, out=out, text=text, stop=stop, rid=rid, usage=usage, served=served, cost=cost or {},
                           schema_hash=schema_hash, prompt_version=prompt_version, thinking_summary=thinking,
                           stop_details={"category": "mock_refusal", "explanation": "injected fault"} if stop == "refusal" else None)

    # ----- building the answer ------------------------------------------------------------------------------
    def _answer(self, route: str, out: type[BaseModel], schema: dict[str, Any], rng: random.Random, sys_text: str, text_in: str,
                prompt_version: int, faults: frozenset[str]) -> str:
        _ensure_roles_loaded()
        try:
            schema = out.model_json_schema()      # the full Pydantic schema keeps the patterns and bounds that transform_schema moves into descriptions
        except Exception:  # noqa: BLE001, S110 - fall back to the transformed schema
            pass
        call = RoleCall(route=route, out=out, schema=schema, system_text=sys_text, content_text=text_in, content=[], seed=rng.getrandbits(32),
                        rng=rng, prompt_version=prompt_version, faults=faults)
        fixture = self._fixture(route, text_in, out)
        if fixture is not None:
            return json.dumps(fixture, sort_keys=True)
        with _ROLES_LOCK:
            builder = _ROLES.get(route) or _ROLES.get(out.__name__)
        if builder is not None:
            res = builder(call)
            if isinstance(res, BaseModel):
                return res.model_dump_json()
            if isinstance(res, str):
                return res
            return json.dumps(res, sort_keys=True)
        last: ValidationError | None = None
        for attempt in range(6):
            r = random.Random(call.seed + attempt * 7919)
            obj = fabricate_from_schema(schema, r, hints=Hints(call), minimal=attempt >= 3)
            try:
                out.model_validate(obj)
            except ValidationError as e:
                last = e
                continue
            return json.dumps(obj, sort_keys=True)
        raise ProviderError(PROVIDER, "validation", f"the mock could not fabricate a valid {out.__name__} from its JSON schema: "
                            f"{str(last)[:300]}", code="mock_cannot_fabricate", billed="no",
                            user_hint="Register a mock role builder for this schema with providers.mock.llm.register_role(route, builder).")

    def _fixture(self, route: str, text_in: str, out: type[BaseModel]) -> Any | None:
        if not route.startswith("L3") or not FIXTURE_DIR.is_dir():
            return None
        low = text_in.lower()
        for f in sorted(FIXTURE_DIR.glob("*.json")):
            if f.stem.lower() in low:
                try:
                    obj = json.loads(f.read_text(encoding="utf-8"))
                    out.model_validate(obj)
                    return obj
                except (OSError, ValueError, ValidationError) as e:
                    self.notes.append(f"fixture {f.name} unusable: {type(e).__name__}")
        return None

    def _usage(self, route: str, sys_text: str, text_in: str, answer: str, system: Sequence[dict[str, Any]]) -> dict[str, int]:
        sys_tokens = _tokens(sys_text)
        key = (route, request_hash(system))
        usage = {"input": _tokens(text_in), "output": max(20, _tokens(answer)),
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        cached = any(isinstance(b, dict) and "cache_control" in b for b in system)
        if cached:
            with self._lock:
                seen = key in self._cache_seen
                self._cache_seen.add(key)
            usage["cache_read_input_tokens" if seen else "cache_creation_input_tokens"] = sys_tokens
        else:
            usage["input"] += sys_tokens
        return usage

    # ----- fan-out, files, batches, capabilities --------------------------------------------------------------
    def call_fanout(self, route: str, requests: Sequence[dict[str, Any]], *, ctx: CallCtx, max_workers: int | None = None) -> list[Any]:
        res: list[Any] = []
        for rq in requests:
            try:
                res.append(self.call(route, system=rq["system"], content=rq["content"], out=rq["out"], ctx=ctx,
                                     prompt_version=rq.get("prompt_version", 1), max_tokens_override=rq.get("max_tokens_override")))
            except ProviderError as e:
                res.append(e)
        return res

    def upload_file(self, data: bytes, *, name: str, mime: str) -> str:
        self.record(PROVIDER, "files.upload", {"name": name, "mime": mime, "data": data})
        return "mock-file-" + request_hash({"d": data})[:16]

    def delete_file(self, file_id: str) -> None:
        self.record(PROVIDER, "files.delete", {"id": file_id})

    def batch_submit(self, items: list[BatchItem]) -> str:
        bid = "mock-batch-" + request_hash([{"id": i.custom_id, "route": i.route, "c": i.content, "s": i.system} for i in items])[:16]
        results: list[BatchItemResult] = []
        for it in items:
            try:
                r = self.call(it.route, system=it.system, content=it.content, out=it.out, ctx=CallCtx.null(), prompt_version=it.prompt_version)
                cost = dict(r.cost or {})
                cost["batch"] = True
                results.append(BatchItemResult(it.custom_id, "succeeded", parsed=r.parsed, raw_text=r.raw_text, stop_reason=r.stop_reason,
                                               usage=r.usage, served_model=r.served_model, cost=cost))
            except ProviderError as e:
                status = {"refusal": "refused", "truncated": "truncated", "validation": "invalid"}.get(e.kind, "errored")
                results.append(BatchItemResult(it.custom_id, status, error=e.message, cost=e.cost))      # type: ignore[arg-type]
        with self._lock:
            self._batches[bid] = {"polls": 0, "results": results}
        return bid

    def batch_status(self, batch_id: str) -> Literal["in_progress", "ended", "canceled", "expired"]:
        with self._lock:
            b = self._batches[batch_id]
            b["polls"] += 1
            return "in_progress" if b["polls"] == 1 else "ended"

    def batch_results(self, batch_id: str) -> Iterator[BatchItemResult]:
        yield from self._batches[batch_id]["results"]

    def capabilities(self) -> dict[str, Any]:
        return {m: {"structured_outputs": {"supported": True}, "thinking": {"supported": True, "types": ["adaptive"]}, "effort": {"supported": True}}
                for m in sorted({c.model for c in self.routes.values()})}

    def allowed_fallbacks(self, model: str) -> list[str]:
        return ["claude-opus-4-8"] if "opus" in model else []

    def smoke_test(self, route: str, out: type[BaseModel]) -> CheckResult:
        try:
            _schema_of(out)
            return CheckResult(check_id="CHK-S10", fm_ids=["LLM-04"], kind="assert", passed=True, metric="schema_smoke", evidence=f"{route}: mock accepts the schema")
        except Exception as e:  # noqa: BLE001 - fail closed
            return CheckResult(check_id="CHK-S10", fm_ids=["LLM-04"], kind="assert", passed=False, ran=False, evidence=f"{type(e).__name__}: {e}")


def _schema_of(out: type[BaseModel]) -> tuple[dict[str, Any], str]:
    try:
        from duoskin.providers.anthropic_llm import SCHEMA_CACHE
        return SCHEMA_CACHE.get(out)
    except Exception:  # noqa: BLE001 - the SDK helper may be unavailable; the plain Pydantic schema is a fine stand-in
        s = out.model_json_schema()
        return s, request_hash(s)


def _corrupt(text: str, out: type[BaseModel]) -> str:
    """Make a valid answer violate the schema (the ``schema_invalid`` fault)."""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    if isinstance(obj, dict) and obj:
        key = next(iter(obj))
        bad = dict(obj)
        bad[key] = {"unexpected": [1, 2, 3]} if not isinstance(obj[key], dict) else "not an object"
        try:
            out.model_validate(bad)
        except ValidationError:
            return json.dumps(bad)
    return text[:-1] if text.endswith(("}", "]")) else "{not json"


def jsonable_text(obj: Any) -> str:  # pragma: no cover - convenience for role builders
    return json.dumps(jsonable(obj), sort_keys=True)
