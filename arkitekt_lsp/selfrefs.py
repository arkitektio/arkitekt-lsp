"""``self.<state>[.<field>]``, ``self.<action>`` and ``self.tasks.<action>`` in a blok: where each is declared.

Report records name the file; the place is found in the file's text as it reads now, so it holds
while lines above it are edited.
"""

import ast
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_PATH = re.compile(r"(?<![\w.])self((?:\.[A-Za-z_]\w*)+)")
_TASKS = "tasks"


@dataclass(frozen=True)
class SelfPath:
    segments: tuple[str, ...]
    """After ``self``: ``("spectroscope", "chart")``."""
    position: int
    """The segment the cursor is on."""
    span: tuple[int, int]
    """That segment, in the blok's value."""


def self_path_at(text: str, index: int) -> SelfPath | None:
    """The ``self.…`` path segment at ``index`` of a blok's BSX; None on ``self`` itself or elsewhere."""
    for match in _PATH.finditer(text):
        if not match.start(1) < index <= match.end():
            continue
        segments = tuple(match.group(1)[1:].split("."))
        at = match.start(1) + 1
        for position, segment in enumerate(segments):
            if index <= at + len(segment):
                return SelfPath(segments, position, (at, at + len(segment)))
            at += len(segment) + 1
    return None


@dataclass(frozen=True)
class Target:
    kind: Literal["state", "field", "action"]
    file: str
    path: str
    """``self.spectroscope.chart``, for a hover."""
    name: str
    """The state's class, or the action's interface."""
    field: str | None = None
    line: int | None = None
    """An action's decorator line, as the report saw it: picks among defs of the same name."""


def target_of(report: Mapping[str, Any], path: SelfPath) -> Target | None:
    """What ``path``'s segment names, by the report's ``states`` and ``actions``; None for what has no source."""
    states = {s["key"]: s for s in report.get("states") or () if s.get("file")}
    actions = {a["interface"]: a for a in report.get("actions") or () if a.get("file")}
    head, position = path.segments[0], path.position
    written = "self." + ".".join(path.segments[: position + 1])
    if head == _TASKS and position == 1 and path.segments[1] in actions:
        action = actions[path.segments[1]]
        return Target(
            "action",
            action["file"],
            written,
            action["interface"],
            line=action.get("line"),
        )
    if head in states and position <= 1:
        state = states[head]
        field = path.segments[1] if position == 1 else None
        return Target(
            "field" if field else "state",
            state["file"],
            written,
            state["name"],
            field=field,
        )
    if head in actions and position == 0:
        action = actions[head]
        return Target(
            "action",
            action["file"],
            written,
            action["interface"],
            line=action.get("line"),
        )
    return None


@dataclass(frozen=True)
class Found:
    start: int
    end: int
    """The name, as offsets into the source."""
    hover: str
    """Markdown."""


def find(source: str, target: Target) -> Found | None:
    """Where ``target`` is declared in ``source``, and what a hover says of it."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    lines = io.StringIO(source, newline="").readlines()  # the line breaks ast counts
    if target.kind == "action":
        return _action(source, lines, module, target)
    cls = next(
        (
            n
            for n in ast.walk(module)
            if isinstance(n, ast.ClassDef) and n.name == target.name.rsplit(".", 1)[-1]
        ),
        None,
    )
    if cls is None:
        return None
    fields = [
        n
        for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    if target.field is None:
        start = _offset(
            lines, cls.lineno, _name_column(lines[cls.lineno - 1], "class", cls.name)
        )
        listed = "\n".join(f"    {_segment(source, f)}" for f in fields) or "    ..."
        doc = _first_paragraph(ast.get_docstring(cls))
        return Found(
            start,
            start + len(cls.name),
            _hover(f"class {cls.name}:\n{listed}", target, doc),
        )
    for node in fields:
        name = node.target
        assert isinstance(name, ast.Name)
        if name.id != target.field:
            continue
        start = _offset(
            lines, name.lineno, _chars(lines[name.lineno - 1], name.col_offset)
        )
        annotation = ast.get_source_segment(source, node.annotation) or ast.unparse(
            node.annotation
        )
        doc = _field_doc(lines, cls, node)
        return Found(
            start,
            start + len(name.id),
            _hover(f"{target.path}: {annotation}", target, doc, cls.name),
        )
    return None


def _action(
    source: str, lines: list[str], module: ast.Module, target: Target
) -> Found | None:
    defs = [
        n
        for n in ast.walk(module)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    named = [n for n in defs if n.name == target.name]
    at_line = [
        n
        for n in defs
        if target.line in (n.lineno, *(d.lineno for d in n.decorator_list))
    ]
    node = named[0] if len(named) == 1 else next(iter(at_line), None)
    if node is None:
        return None
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    start = _offset(
        lines, node.lineno, _name_column(lines[node.lineno - 1], "def", node.name)
    )
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    signature = f"{keyword} {node.name}({ast.unparse(node.args)}){returns}"
    doc = _first_paragraph(ast.get_docstring(node))
    return Found(start, start + len(node.name), _hover(signature, target, doc))


def _hover(code: str, target: Target, doc: str | None, owner: str | None = None) -> str:
    said = f"({target.kind}) `{target.path}`" + (f" on `{owner}`" if owner else "")
    return "\n\n".join(part for part in (f"```python\n{code}\n```", said, doc) if part)


def _field_doc(lines: list[str], cls: ast.ClassDef, node: ast.AnnAssign) -> str | None:
    """A ``#:`` comment above the field, else a string right after it (an attribute docstring)."""
    above: list[str] = []
    row = node.lineno - 2
    while row >= 0 and lines[row].strip().startswith("#:"):
        above.insert(0, lines[row].strip()[2:].strip())
        row -= 1
    if above:
        return " ".join(above)
    after = cls.body.index(node) + 1
    following = cls.body[after] if after < len(cls.body) else None
    if (
        isinstance(following, ast.Expr)
        and isinstance(following.value, ast.Constant)
        and isinstance(following.value.value, str)
    ):
        return _first_paragraph(following.value.value)
    return None


def _first_paragraph(doc: str | None) -> str | None:
    return doc.strip().split("\n\n")[0] if doc and doc.strip() else None


def _segment(source: str, node: ast.AnnAssign) -> str:
    annotation = ast.get_source_segment(source, node.annotation) or ast.unparse(
        node.annotation
    )
    return f"{ast.unparse(node.target)}: {annotation}"


def _name_column(line: str, keyword: str, name: str) -> int:
    match = re.search(rf"\b{keyword}\s+({re.escape(name)})\b", line)
    return match.start(1) if match else 0


def _chars(line: str, byte_column: int) -> int:
    """``ast`` columns count UTF-8 bytes."""
    return len(line.encode()[:byte_column].decode(errors="ignore"))


def _offset(lines: list[str], lineno: int, column: int) -> int:
    return sum(len(line) for line in lines[: lineno - 1]) + column
