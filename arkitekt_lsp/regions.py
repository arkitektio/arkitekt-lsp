"""The bloks of a Python source: its string literals whose text is BSX.

A blok starts with a root that names its context (``<Html>``, ``<Scene>``, ``<Overlay>``,
``<AgentLogo>``, ``<ActionLogo>``: the vocabulary's ``blok_roots``), so a literal is one when its
text, past whitespace and comments, opens with one. Literals are found with :mod:`tokenize`, so a
quote inside a comment or another string is never mistaken for one, and a source that does not
tokenize to the end (it is being typed) still yields the literals before the break. A string that
is a statement of its own (a docstring) is never a blok: it declares nothing, and editors colour it
as a docstring.
"""

import bisect
import io
import re
import tokenize
from dataclasses import dataclass
from functools import cache

from arkitekt_spec.blok.vocabulary import load_vocabulary

_PREFIX = re.compile(r"[rRbBuUfFtT]*")
_LEADING = re.compile(r"(?:\s+|<!--.*?-->)*", re.DOTALL)
_ACTION_BLOK = re.compile(r"(?:\bbloks\s*=\s*[\[(]?|\bActionBlok\s*\()\s*$")
_SIMPLE_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


@cache
def blok_roots() -> dict[str, str]:
    """Each root component and the context it names: ``{"Html": "HTML", ...}``."""
    return dict(load_vocabulary().blok_roots)


@dataclass(frozen=True)
class BlokLiteral:
    """One string literal of a Python source that holds a blok.

    Offsets are indices into the source string (code points). ``offsets`` maps each index of
    ``value`` (and one past its end) to the source: ``None`` when the literal has no escapes, so
    the value is the source text between ``start`` and ``end`` as written.
    """

    start: int
    """Where its text starts in the source: past the prefix and the opening quote."""
    end: int
    """Where its text ends: at the closing quote."""
    line: int
    """The 1-based line its opening quote is on."""
    value: str
    """The string, escapes resolved: the BSX the app declares."""
    root: str
    context: str
    kind: str
    """``agent``, ``action`` (an action's own blok) or ``action_logo``."""
    offsets: tuple[int, ...] | None = None

    @property
    def exact(self) -> bool:
        """Whether the value is the source text as written: an editor may map positions one to one."""
        return self.offsets is None

    def to_source(self, index: int) -> int:
        """The source offset of ``index`` in the value."""
        index = max(0, min(index, len(self.value)))
        return self.start + index if self.offsets is None else self.offsets[index]

    def to_value(self, offset: int) -> int | None:
        """The value index at source ``offset``, or None when it is outside the text."""
        if not self.start <= offset <= self.end:
            return None
        if self.offsets is None:
            return offset - self.start
        return max(0, bisect.bisect_right(self.offsets, offset) - 1)


def _decode(text: str, start: int) -> tuple[str, tuple[int, ...]] | None:
    """A non-raw literal's value and where each of its characters is; None for an escape it cannot read."""
    chars: list[str] = []
    offsets: list[int] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            chars.append(char)
            offsets.append(start + index)
            index += 1
            continue
        escape = text[index + 1 : index + 2]
        here = start + index
        if escape == "\n":
            index += 2
            continue
        if escape in _SIMPLE_ESCAPES:
            chars.append(_SIMPLE_ESCAPES[escape])
            offsets.append(here)
            index += 2
            continue
        width = {"x": 2, "u": 4, "U": 8}.get(escape)
        if width is not None:
            digits = text[index + 2 : index + 2 + width]
            try:
                chars.append(chr(int(digits, 16)))
            except ValueError:
                return None
            offsets.append(here)
            index += 2 + width
            continue
        octal = re.match(r"[0-7]{1,3}", text[index + 1 :])
        if octal:
            chars.append(chr(int(octal.group(), 8)))
            offsets.append(here)
            index += 1 + len(octal.group())
            continue
        if escape == "N":
            return None
        # An unknown escape keeps its backslash, as Python does.
        chars.append("\\")
        offsets.append(here)
        index += 1
    offsets.append(start + len(text))
    return "".join(chars), tuple(offsets)


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", source):
        starts.append(match.end())
    return starts


def _tokens(source: str) -> list[tokenize.TokenInfo]:
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return tokens


_STATEMENT_START = frozenset({tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT})
_INSIGNIFICANT = frozenset({tokenize.NL, tokenize.COMMENT, tokenize.ENCODING})


def blok_literals(source: str) -> list[BlokLiteral]:
    """Every string literal of ``source`` that holds a blok, in source order."""
    starts = _line_starts(source)
    found: list[BlokLiteral] = []
    previous: int | None = None
    for token in _tokens(source):
        statement = previous is None or previous in _STATEMENT_START
        if token.type not in _INSIGNIFICANT:
            previous = token.type
        if token.type != tokenize.STRING or statement:
            continue
        prefix = _PREFIX.match(token.string)
        letters = prefix.group().lower() if prefix else ""
        if {"b", "f", "t"} & set(letters):
            continue
        rest = token.string[len(letters) :]
        quote = rest[:3] if rest[:3] in ('"""', "'''") else rest[:1]
        literal_start = starts[token.start[0] - 1] + token.start[1]
        start = literal_start + len(letters) + len(quote)
        end = starts[token.end[0] - 1] + token.end[1] - len(quote)
        if end < start:
            continue
        text = source[start:end]
        offsets: tuple[int, ...] | None = None
        value = text
        if "r" not in letters and "\\" in text:
            decoded = _decode(text, start)
            if decoded is None:
                continue
            value, offsets = decoded
        lead = _LEADING.match(value)
        opening = re.match(r"<([A-Za-z_][\w]*)\b", value[lead.end() if lead else 0 :])
        if opening is None or opening.group(1) not in blok_roots():
            continue
        root = opening.group(1)
        context = blok_roots()[root]
        if context == "ACTION_LOGO":
            kind = "action_logo"
        elif context == "OVERLAY" or _ACTION_BLOK.search(source[max(0, literal_start - 200) : literal_start]):
            kind = "action"
        else:
            kind = "agent"
        found.append(
            BlokLiteral(
                start=start,
                end=end,
                line=token.start[0],
                value=value,
                root=root,
                context=context,
                kind=kind,
                offsets=offsets,
            )
        )
    return found


__all__ = ["BlokLiteral", "blok_literals", "blok_roots"]
