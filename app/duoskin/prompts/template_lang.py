"""The tiny template language of ``prompts/<ID>.md`` (bible §2.3, APP_SPEC §10.0).

Syntax (the same for every template, so the compiler stays deterministic and testable):

* ``{name}``       a slot that must have a value. If its value is empty, the enclosing ``[[ ... ]]`` clause is dropped; with no
                   enclosing clause, a numbered line (``3. ...`` or ``3) ...``) is dropped; anywhere else it is an error
                   (bible §2.3 rule 5, FAILURE_MODES PRM-05: a slot is never filled with "none").
* ``{name?}``      a slot that may be empty (it renders as an empty string).
* ``[[ ... ]]``    an optional clause, removed as a whole when a required slot inside it is empty.
* ``{{if flag}} ... {{else}} ... {{/if}}`` and ``{{unless flag}} ... {{/unless}}``: choose text by a boolean input (``bootstrap``,
                   ``mood``, ``transparent``, ...). The ``{{else}}`` part is optional.

A slot whose key is missing from the values (as opposed to empty) is a template bug and raises :class:`MissingSlot`.
Numbered lines are renumbered after dropped lines are removed, so the final MUST list is always ``1. ... n.``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


class TemplateSyntaxError(ValueError):
    """The template text cannot be parsed."""


class MissingSlot(KeyError):
    """A slot named in the template has no value at all (PRM-05)."""


class EmptySlot(Exception):
    """A required slot is empty. Caught by ``[[ ]]`` clauses and by numbered lines."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


@dataclass
class Text:
    text: str


@dataclass
class Slot:
    name: str
    optional: bool = False


@dataclass
class Cond:
    flag: str
    negate: bool
    then: list = field(default_factory=list)
    other: list = field(default_factory=list)


@dataclass
class Opt:
    children: list = field(default_factory=list)


_TOKEN = re.compile(r"\{\{(if|unless) ([a-z_][a-z0-9_]*)\}\}|\{\{else\}\}|\{\{/(if|unless)\}\}|\[\[|\]\]|\{([a-z_][a-z0-9_]*)(\?)?\}")


def parse(src: str) -> list:
    """Parse one line (or flat paragraph) of template text into nodes."""
    root: list = []
    stack: list[tuple[str, list, object]] = [("root", root, None)]
    pos = 0

    def cur() -> list:
        return stack[-1][1]

    for m in _TOKEN.finditer(src):
        if m.start() > pos:
            cur().append(Text(src[pos:m.start()]))
        pos = m.end()
        tok = m.group(0)
        if m.group(1):                                  # {{if x}} / {{unless x}}
            node = Cond(flag=m.group(2), negate=(m.group(1) == "unless"))
            cur().append(node)
            stack.append(("cond", node.then, node))
        elif tok == "{{else}}":
            kind, _, node = stack[-1]
            if kind != "cond":
                raise TemplateSyntaxError("{{else}} outside an {{if}}")
            stack[-1] = ("cond", node.other, node)      # type: ignore[attr-defined]
        elif m.group(3):                                # {{/if}} / {{/unless}}
            if stack[-1][0] != "cond":
                raise TemplateSyntaxError(f"unmatched {tok}")
            stack.pop()
        elif tok == "[[":
            node2 = Opt()
            cur().append(node2)
            stack.append(("opt", node2.children, node2))
        elif tok == "]]":
            if stack[-1][0] != "opt":
                raise TemplateSyntaxError("unmatched ]]")
            stack.pop()
        else:
            cur().append(Slot(m.group(4), optional=bool(m.group(5))))
    if pos < len(src):
        cur().append(Text(src[pos:]))
    if len(stack) != 1:
        raise TemplateSyntaxError(f"unclosed {stack[-1][0]} in {src[:60]!r}")
    stray = re.search(r"\{\{|\}\}|\[\[|\]\]|(?<!\{)\{[^{}]*\}(?!\})", "".join(n.text for n in _walk_text(root)))
    if stray:
        raise TemplateSyntaxError(f"stray template syntax {stray.group(0)!r} in {src[:60]!r}")
    return root


def _walk_text(nodes: Iterable) -> Iterable[Text]:
    for n in nodes:
        if isinstance(n, Text):
            yield n
        elif isinstance(n, Cond):
            yield from _walk_text(n.then)
            yield from _walk_text(n.other)
        elif isinstance(n, Opt):
            yield from _walk_text(n.children)


def slots_used(nodes: Iterable) -> set[str]:
    out: set[str] = set()
    for n in nodes:
        if isinstance(n, Slot):
            out.add(n.name)
        elif isinstance(n, Cond):
            out |= slots_used(n.then) | slots_used(n.other)
        elif isinstance(n, Opt):
            out |= slots_used(n.children)
    return out


def flags_used(nodes: Iterable) -> set[str]:
    out: set[str] = set()
    for n in nodes:
        if isinstance(n, Cond):
            out.add(n.flag)
            out |= flags_used(n.then) | flags_used(n.other)
        elif isinstance(n, Opt):
            out |= flags_used(n.children)
    return out


def literal_text(nodes: Iterable, flags: Mapping[str, bool] | None = None) -> str:
    """The fixed (non-slot) text of ``nodes``; both branches of a condition are included when ``flags`` is None."""
    out: list[str] = []
    for n in nodes:
        if isinstance(n, Text):
            out.append(n.text)
        elif isinstance(n, Cond):
            if flags is None:
                out.append(literal_text(n.then, None))
                out.append(literal_text(n.other, None))
            else:
                truth = bool(flags.get(n.flag, False)) != n.negate
                out.append(literal_text(n.then if truth else n.other, flags))
        elif isinstance(n, Opt):
            out.append(literal_text(n.children, flags))
    return "".join(out)


def render(nodes: Iterable, values: Mapping[str, object], flags: Mapping[str, bool]) -> str:
    """Render nodes. Raises :class:`EmptySlot` (caught by clauses and numbered lines) or :class:`MissingSlot`."""
    out: list[str] = []
    for n in nodes:
        if isinstance(n, Text):
            out.append(n.text)
        elif isinstance(n, Slot):
            if n.name not in values or values[n.name] is None:
                raise MissingSlot(n.name)
            v = str(values[n.name])
            if v == "":
                if n.optional:
                    continue
                raise EmptySlot(n.name)
            out.append(v)
        elif isinstance(n, Cond):
            if n.flag not in flags:
                raise MissingSlot(f"flag:{n.flag}")
            truth = bool(flags[n.flag]) != n.negate
            out.append(render(n.then if truth else n.other, values, flags))
        elif isinstance(n, Opt):
            try:
                out.append(render(n.children, values, flags))
            except EmptySlot:
                continue
    return "".join(out)


_NUM = re.compile(r"^(\d+)([.)])\s+(.*)$")
_EMPTY_LABEL = re.compile(r"^[A-Z]+:$")


@dataclass
class Line:
    nodes: list
    number_delim: str = ""        # "" for an unnumbered line, "." or ")" for a numbered one
    raw: str = ""


def parse_lines(body: str) -> list[Line]:
    """Parse a body (one logical line per physical line) into :class:`Line` objects."""
    lines: list[Line] = []
    for raw in body.split("\n"):
        if not raw.strip():
            continue
        m = _NUM.match(raw)
        if m:
            lines.append(Line(parse(m.group(3)), m.group(2), raw))
        else:
            lines.append(Line(parse(raw), "", raw))
    return lines


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def render_lines(lines: list[Line], values: Mapping[str, object], flags: Mapping[str, bool], *, joiner: str = "\n") -> str:
    """Render parsed lines: drop numbered lines with an empty required slot, drop lines that render empty, renumber, join."""
    kept: list[tuple[str, str]] = []                    # (delimiter, text)
    for ln in lines:
        try:
            text = _tidy(render(ln.nodes, values, flags))
        except EmptySlot as e:
            if ln.number_delim:
                continue
            raise EmptySlot(e.name) from None
        if not text or (_EMPTY_LABEL.match(text) and text != "MUST:"):
            continue                                    # a label with nothing after it (a conditional line that vanished)
        kept.append((ln.number_delim, text))
    out: list[str] = []
    n = 0
    for delim, text in kept:
        if delim:
            n += 1
            out.append(f"{n}{delim} {text}")
        else:
            n = 0
            out.append(text)
    return joiner.join(out)
