"""Where in a Python source a finding of ``arkitekt check --json-out`` belongs.

A report diagnostic names the line that declared its blok (``call_line``), the blok (``blok``,
whose BSX the report lists), and inside it the node (``node``) or a line and column of the BSX.
The blok's literal is found by its text, so a finding follows its blok when the code around it
moves; inside the literal it goes to its line and column, else to the node's tag, else to the
root. A blok whose text is in no literal (``dedent(...)``, a constant from elsewhere, a file read)
has its findings at the declaring line; one whose literal was there when the check reported and
is gone now was edited since, and what the check found in it is dropped. A finding about no blok
(the app did not import) goes to the line it names.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from arkitekt_spec.blok.bsx import parse_bsx_with_spans

from arkitekt_lsp.regions import BlokLiteral

_TAG = re.compile(r"</?[A-Za-z_][\w:.\-]*")
_WORD = re.compile(r"[\w.\-]+")

Span = tuple[int, int]
"""A ``[start, end)`` of source offsets (code points)."""


def line_span(source: str, line: int) -> Span:
    """The text of 1-based ``line``, past its indentation (at least one character)."""
    starts = [0, *(match.end() for match in re.finditer(r"\n", source))]
    index = max(0, min(line - 1, len(starts) - 1))
    start = starts[index]
    end = source.find("\n", start)
    end = len(source) if end < 0 else end
    text = source[start:end]
    indent = len(text) - len(text.lstrip())
    return (start + indent, max(end, start + indent + 1))


def _offset_at(value: str, line: int, column: int) -> int:
    """The index of a 1-based line and code-point column of ``value``, clamped."""
    index = 0
    for _ in range(line - 1):
        newline = value.find("\n", index)
        if newline < 0:
            return len(value)
        index = newline + 1
    end = value.find("\n", index)
    end = len(value) if end < 0 else end
    return min(index + max(0, column - 1), end)


def _token_at(value: str, index: int) -> tuple[int, int]:
    """The tag (``<Card``) or word at ``index`` of ``value``, at least one character."""
    for pattern in (_TAG, _WORD):
        match = pattern.match(value, index)
        if match and match.end() > index:
            return (index, match.end())
    return (index, min(len(value), index + 1) if index < len(value) else index)


def _in_statement(source_lines: Sequence[str], call_line: int, literal: BlokLiteral) -> bool:
    """Whether ``literal`` is an argument of the statement that starts at ``call_line``: no line
    between them starts a new top-level statement (or closes the call at column 0)."""
    if literal.line < call_line:
        return False
    for text in source_lines[call_line : literal.line - 1]:
        if text and not text[0].isspace() and not text.startswith("#"):
            return False
    return True


def find_literal(
    source: str,
    literals: Sequence[BlokLiteral],
    call_line: int,
    blok_source: str | None,
    context: str | None = None,
) -> BlokLiteral | None:
    """The literal a diagnostic is about: the one holding ``blok_source`` nearest ``call_line``
    (after it first), else the first of the declaring statement (of ``context``, if named)."""
    if blok_source is not None:
        holding = [literal for literal in literals if literal.value == blok_source]
        if holding:
            after = [literal for literal in holding if literal.line >= call_line]
            if after:
                return min(after, key=lambda literal: literal.line)
            return min(holding, key=lambda literal: abs(literal.line - call_line))
    lines = source.splitlines()
    for literal in literals:
        if context is not None and literal.context != context:
            continue
        if _in_statement(lines, call_line, literal):
            return literal
    return None


def _blok_context(blok: str | None) -> str | None:
    """The context an action blok's id names (``crop:overlay`` → ``OVERLAY``)."""
    if blok is None or ":" not in blok:
        return None
    return blok.rsplit(":", 1)[1].upper()


def place_in_literal(literal: BlokLiteral, diagnostic: Mapping[str, Any]) -> Span:
    """Where in ``literal`` a diagnostic goes: its line and column, the tag of its node, or the root."""
    line, column = diagnostic.get("line"), diagnostic.get("column")
    index: int | None = None
    if isinstance(line, int) and isinstance(column, int):
        index = _offset_at(literal.value, line, column)
    elif isinstance(diagnostic.get("node"), str):
        try:
            _, spans = parse_bsx_with_spans(literal.value)
        except Exception:  # noqa: BLE001 - the text no longer parses: the root it is
            spans = {}
        at = spans.get(diagnostic["node"])
        if at is not None:
            index = _offset_at(literal.value, *at)
    if index is None:
        index = literal.value.find("<" + literal.root)
        index = max(index, 0)
    start, end = _token_at(literal.value, index)
    return (literal.to_source(start), literal.to_source(max(end, start + 1)))


def place(
    diagnostic: Mapping[str, Any],
    source: str,
    literals: Sequence[BlokLiteral],
    bloks: Mapping[str, str],
    located: Collection[str] = (),
) -> Span | None:
    """Where in ``source`` (the diagnostic's file) a report diagnostic goes; None if it is stale.

    Args:
        diagnostic: One of the report's ``diagnostics``.
        source: The file's text as it is now.
        literals: Its blok literals (:func:`~arkitekt_lsp.regions.blok_literals` of ``source``).
        bloks: The report's blok sources by blok id.
        located: The bloks whose source was a literal of this file when the report came: only
            their findings can go stale.
    """
    call_line = diagnostic.get("call_line")
    if not isinstance(call_line, int):
        return None
    blok = diagnostic.get("blok")
    about_blok = any(key in diagnostic for key in ("blok", "code", "node", "line"))
    if about_blok:
        blok_source = bloks.get(blok) if isinstance(blok, str) else None
        if blok_source is not None and not any(literal.value == blok_source for literal in literals):
            if blok in located:
                return None  # edited since the check: what it found there is stale
            return line_span(source, call_line)  # never a literal of this file: where it was declared
        literal = find_literal(
            source, literals, call_line, blok_source, _blok_context(blok if isinstance(blok, str) else None)
        )
        if literal is not None:
            return place_in_literal(literal, diagnostic)
    return line_span(source, call_line)


__all__ = ["Span", "find_literal", "line_span", "place", "place_in_literal"]
