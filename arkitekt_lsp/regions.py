"""The bloks of a Python source: its string literals whose text is BSX.

Every blok is one ``<Blok kind="..." context="...">`` (the vocabulary's ``blok``), so a literal
is one when its text, past whitespace and comments, opens with ``<Blok``. What kind of blok it is
and what draws it are read off that element, never guessed from where the literal sits: a blok
kept in a module constant is the same blok as one written inline. Literals are found with :mod:`tokenize`, so a
quote inside a comment or another string is never mistaken for one, and a source that does not
tokenize to the end (it is being typed) still yields the literals before the break. A string that
is a statement of its own (a docstring) is never a blok: it declares nothing, and editors colour it
as a docstring.

An f-string is not one of them: what the app declares is what it evaluates to, which the source
does not say. :func:`generated_bloks` finds the ones that open with a root, to warn about.
"""

import bisect
import io
import re
import tokenize
from dataclasses import dataclass, field
from functools import cache

from arkitekt_spec.blok.vocabulary import load_vocabulary

_PREFIX = re.compile(r"[rRbBuUfFtT]*")
_LEADING = re.compile(r"(?:\s+|<!--.*?-->)*", re.DOTALL)
_ATTRIBUTE = re.compile(r"""\s([A-Za-z_][\w]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
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
def blok_component() -> str:
    """The one element every blok is: ``Blok``."""
    return load_vocabulary().blok.component


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
    """``Blok``."""
    context: str | None
    """What its ``<Blok context>`` says (``PANEL``, ``SCENE``, ``LOGO``), None while it says none."""
    kind: str | None
    """What its ``<Blok kind>`` says (``agent``, ``action``, ...), None while it says none."""
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
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
"""Python 3.12 splits an f-string into tokens; before that it is one ``STRING``."""
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _root_of(value: str) -> str | None:
    """``Blok`` when ``value`` opens with it, past whitespace and comments."""
    lead = _LEADING.match(value)
    opening = re.match(r"<([A-Za-z_][\w]*)\b", value[lead.end() if lead else 0 :])
    if opening is None or opening.group(1) != blok_component():
        return None
    return opening.group(1)


def _header_of(value: str) -> dict[str, str]:
    """The literal attributes of the ``<Blok>`` tag ``value`` opens with (``kind``, ``context``, ...)."""
    lead = _LEADING.match(value)
    start = lead.end() if lead else 0
    end = value.find(">", start)
    tag = value[start : end if end >= 0 else len(value)]
    return {
        match.group(1): match.group(2) if match.group(2) is not None else match.group(3)
        for match in _ATTRIBUTE.finditer(tag)
    }


def is_bsx(path: str) -> bool:
    """Whether ``path`` is a ``.bsx`` file: one blok, its whole text."""
    return path.endswith(".bsx")


def literals_of(path: str, source: str) -> list[BlokLiteral]:
    """The bloks of the file at ``path``: a ``.bsx`` file's whole text, a Python file's literals."""
    if not is_bsx(path):
        return blok_literals(source)
    header = _header_of(source)
    kind = header.get("kind")
    return [
        BlokLiteral(
            start=0,
            end=len(source),
            line=1,
            value=source,
            root=_root_of(source) or blok_component(),
            context=load_vocabulary().blok.contexts.get(header.get("context", "")),
            kind=kind if kind in load_vocabulary().kinds else None,
        )
    ]


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
        root = _root_of(value)
        if root is None:
            continue
        header = _header_of(value)
        context = load_vocabulary().blok.contexts.get(header.get("context", ""))
        kind = (
            header.get("kind")
            if header.get("kind") in load_vocabulary().kinds
            else None
        )
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


@dataclass
class _OpenFString:
    span: tuple[int, int]
    statement: bool
    text: list[str] = field(default_factory=list[str])
    """What it says before its first field."""
    has_field: bool = False


def generated_bloks(source: str) -> list[tuple[int, int]]:
    """The opening (``f\"\"\"``) of each f-string with a field whose text opens with ``<Blok``."""
    starts = _line_starts(source)
    found: list[tuple[int, int]] = []
    open_strings: list[_OpenFString] = []
    previous: int | None = None
    for token in _tokens(source):
        statement = previous is None or previous in _STATEMENT_START
        if token.type not in _INSIGNIFICANT:
            previous = token.type
        at = starts[token.start[0] - 1] + token.start[1]
        if token.type == tokenize.STRING:
            prefix = _PREFIX.match(token.string)
            letters = prefix.group().lower() if prefix else ""
            if "f" not in letters or statement:
                continue
            rest = token.string[len(letters) :]
            quote = rest[:3] if rest[:3] in ('"""', "'''") else rest[:1]
            text = rest[len(quote) : len(rest) - len(quote)]
            if (
                "{" in text.replace("{{", "")
                and _root_of(text.partition("{")[0]) is not None
            ):
                found.append((at, at + len(letters) + len(quote)))
        elif token.type == _FSTRING_START:
            open_strings.append(_OpenFString((at, at + len(token.string)), statement))
        elif (
            token.type == _FSTRING_MIDDLE
            and open_strings
            and not open_strings[-1].has_field
        ):
            open_strings[-1].text.append(token.string)
        elif token.type == tokenize.OP and token.string == "{" and open_strings:
            open_strings[-1].has_field = True
        elif token.type == _FSTRING_END and open_strings:
            closed = open_strings.pop()
            if (
                closed.has_field
                and not closed.statement
                and _root_of("".join(closed.text)) is not None
            ):
                found.append(closed.span)
    return sorted(found)


__all__ = [
    "is_bsx",
    "literals_of",
    "BlokLiteral",
    "blok_component",
    "blok_literals",
    "generated_bloks",
]
