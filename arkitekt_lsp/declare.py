"""A dependency written out: the ``@app.declare`` class of what an agent offers, as Python source.

The server says what an agent offers as ports. A dependency on it is the class you would write by
hand, so that is what comes out: the check then treats it like any other. A structure, a model or an
enum is written as the class the app's own registry has for its identifier (the check's report lists
them, services included). One it has none for leaves the action or state as a comment saying what is
missing, never a type guessed at.
"""

import ast
import json
import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_SCALARS = {"INT": "int", "FLOAT": "float", "STRING": "str", "BOOL": "bool"}
_IMPORTS = {"Protocol": "typing", "Optional": "typing", "datetime": "datetime", "Iterator": "collections.abc"}
_BY_IDENTIFIER = ("STRUCTURE", "MODEL", "ENUM", "MEMORY_STRUCTURE")
_APP_NAMES = {"App", "QtApp"}


class CannotDeclare(Exception):
    """Why this source has no place for a dependency: said to the user as it is."""


Known = Mapping[str, Mapping[str, Any]]
"""The class behind each identifier, as the check's report lists it."""


def known_structures(report: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """What ``report`` says the app knows; a report from before it listed them knows nothing."""
    return {str(record["identifier"]): record for record in (report or {}).get("structures") or ()}


def _type_of(port: Mapping[str, Any], needs: dict[str, str], known: Known = {}) -> str | None:  # noqa: B006 - never written to
    """The annotation of ``port``, or None when the app has no class for it. ``needs`` collects what
    the annotation names: each name with its module."""
    kind = port.get("kind")
    children = port.get("children") or ()
    if kind in _SCALARS:
        name: str | None = _SCALARS[kind]
    elif kind == "DATE":
        needs["datetime"] = _IMPORTS["datetime"]
        name = "datetime"
    elif kind in _BY_IDENTIFIER:
        record = known.get(str(port.get("identifier")))
        if record is None or record.get("kind") != kind:
            return None
        name = str(record["name"])
        needs[name] = str(record["module"])
    elif kind in ("LIST", "DICT") and len(children) == 1:
        child = _type_of(children[0], needs, known)
        if child is None:
            return None
        name = f"list[{child}]" if kind == "LIST" else f"dict[str, {child}]"
    else:
        return None
    if port.get("nullable"):
        needs["Optional"] = _IMPORTS["Optional"]
        return f"Optional[{name}]"
    return name


def _missing(ports: Sequence[Mapping[str, Any]], known: Known = {}) -> list[str]:  # noqa: B006 - never written to
    """What the ports need that the app has no class for: identifiers where they have one."""
    found: list[str] = []
    for port in ports:
        if _type_of(port, {}, known) is None:
            children = port.get("children") or ()
            inner = _missing(children, known) if port.get("kind") in ("LIST", "DICT") and children else []
            found.extend(inner or [str(port.get("identifier") or port.get("kind"))])
    return found


def _default(port: Mapping[str, Any]) -> str | None:
    value = port.get("default")
    if value is None:
        return "None" if port.get("nullable") else None
    return repr(value) if isinstance(value, (bool, int, float, str)) else None


def _parameters(args: Sequence[Mapping[str, Any]], needs: dict[str, str], known: Known) -> list[str]:
    parameters = ["self"]
    defaulted = keyword_only = False
    for port in args:
        default = _default(port)
        if default is None and defaulted and not keyword_only:
            parameters.append("*")  # a required one after a defaulted one: as its own action must have it
            keyword_only = True
        defaulted = defaulted or default is not None
        annotation = f"{port['key']}: {_type_of(port, needs, known)}"
        parameters.append(annotation if default is None else f"{annotation} = {default}")
    return parameters


def _returns(action: Mapping[str, Any], needs: dict[str, str], known: Known) -> str:
    types = [_type_of(port, needs, known) for port in action.get("returns") or ()]
    returned = "None" if not types else str(types[0]) if len(types) == 1 else f"tuple[{', '.join(map(str, types))}]"
    if action.get("kind") == "GENERATOR":
        needs["Iterator"] = _IMPORTS["Iterator"]
        return f"Iterator[{returned}]"
    return returned


def action_missing(action: Mapping[str, Any], known: Known = {}) -> list[str]:  # noqa: B006 - never written to
    return _missing([*(action.get("args") or ()), *(action.get("returns") or ())], known)


def state_missing(state: Mapping[str, Any], known: Known = {}) -> list[str]:  # noqa: B006 - never written to
    return _missing(state.get("ports") or (), known)


def signature(action: Mapping[str, Any], known: Known = {}) -> str:  # noqa: B006 - never written to
    """How an action reads in a picker: ``move_to(x_um: float, y_um: float) → float``."""
    args = ", ".join(f"{p['key']}: {_type_of(p, {}, known) or p.get('identifier') or p.get('kind')}" for p in action.get("args") or ())
    returns = [_type_of(p, {}, known) or p.get("identifier") or p.get("kind") for p in action.get("returns") or ()]
    return f"{action['key']}({args})" + (f" → {', '.join(map(str, returns))}" if returns else "")


def class_name_for(app: str | None, taken: Sequence[str] = ()) -> str:
    """``microscope-stage`` as a class: ``MicroscopeStage``, and not one the file already has."""
    words = re.findall(r"[A-Za-z0-9]+", (app or "").rsplit(".", 1)[-1])
    name = "".join(word[:1].upper() + word[1:] for word in words) or "Dependency"
    if name[0].isdigit():
        name = f"App{name}"
    candidate, count = name, 1
    while candidate in taken:
        count += 1
        candidate = f"{name}{count}"
    return candidate


def _docstring(text: str | None, fallback: str) -> str:
    # The server's word for an action that has no description is no description.
    line = (text or "").strip().splitlines()[0] if (text or "").strip() and text != "No Description" else fallback
    return line.replace("\\", "\\\\").replace('"""', "'''").rstrip('"')


def _state_class(name: str, state: Mapping[str, Any], needs: dict[str, str], known: Known) -> str:
    fields = [f"    {port['key']}: {_type_of(port, needs, known)}" for port in state.get("ports") or ()]
    return "\n".join([f"class {name}:", f'    """The state as {state["key"]} publishes it."""', "", *fields]) + "\n"


def _class_of_state(key: str) -> str:
    return key if key[:1].isupper() else class_name_for(key)


@dataclass(frozen=True)
class Written:
    source: str
    """What goes in, as one block of source."""
    imports: tuple[str, ...]
    """The imports it needs that the file has not got."""
    line: int
    """Where the block goes: the line (0-based) it is inserted before."""
    class_line: int
    """Where the class's own line ends up, once the imports and the block are in."""


def _top_level(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise CannotDeclare(f"The file does not parse yet (line {error.lineno}): fix that first.") from error


def _app_variable(tree: ast.Module) -> ast.Assign:
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.targets[0], ast.Name):
            func = node.value.func
            func = func.value if isinstance(func, ast.Subscript) else func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name in _APP_NAMES:
                return node
    raise CannotDeclare("This file declares no app: open the one with `app = App(...)`.")


def _imported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name) for alias in node.names)
    return names


def class_names(source: str) -> list[str]:
    """The classes ``source`` has at its top; none while it does not parse."""
    try:
        return [node.name for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    except SyntaxError:
        return []


def write_dependency(
    source: str,
    *,
    app: str | None,
    class_name: str,
    actions: Sequence[Mapping[str, Any]] = (),
    states: Sequence[Mapping[str, Any]] = (),
    known: Known = {},  # noqa: B006 - never written to
) -> Written:
    """The ``@<app>.declare`` class of ``actions`` and ``states`` of ``app``, for the file ``source``."""
    if not class_name.isidentifier() or keyword.iskeyword(class_name):
        raise CannotDeclare(f"{class_name!r} is not a name for a class.")
    tree = _top_level(source)
    assign = _app_variable(tree)
    variable = assign.targets[0].id  # type: ignore[union-attr] - _app_variable looked
    taken = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    if class_name in taken:
        raise CannotDeclare(f"This file already has a class {class_name}.")

    needs: dict[str, str] = {"Protocol": _IMPORTS["Protocol"]}
    above: list[str] = []
    members: list[str] = []
    for state in states:
        key = str(state["key"])
        lacking = state_missing(state, known)
        if lacking:
            members.append(f"    # state {key}: needs {', '.join(lacking)}, which no service of this app declares")
            continue
        name = _class_of_state(key)
        if name not in taken:
            above.append(_state_class(name, state, needs, known))
            taken.add(name)
        members.append(f"    {key}: {name}")
    for action in actions:
        key = str(action["key"])
        lacking = action_missing(action, known)
        if not key.isidentifier() or keyword.iskeyword(key):
            members.append(f"    # {key}: not a name a method can have")
        elif lacking:
            members.append(f"    # {signature(action, known)}: needs {', '.join(lacking)}, which no service of this app declares")
        else:
            members.append(
                f"    def {key}({', '.join(_parameters(action.get('args') or (), needs, known))}) -> {_returns(action, needs, known)}:\n"
                f'        """{_docstring(action.get("description"), key)}"""\n'
                "        ..."
            )
    if not members:
        raise CannotDeclare("Nothing was picked.")

    declared = f"@{variable}.declare(app={json.dumps(app)})" if app else f"@{variable}.declare()"
    block = "\n\n".join([*above, f"{declared}\nclass {class_name}(Protocol):\n" + "\n\n".join(members)])
    source_lines = source.splitlines()
    after = assign.end_lineno or len(source_lines)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(_is_declare(decorator, variable) for decorator in node.decorator_list):
            after = max(after, node.end_lineno or after)
    have = _imported(tree)
    defined_here = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    modules: dict[str, list[str]] = {}
    for need, module in sorted(needs.items()):
        if need in defined_here:  # this very file's class: it only has to come first
            after = max(after, defined_here[need].end_lineno or after)
        elif need not in have:
            modules.setdefault(module, []).append(need)
    imports = tuple(f"from {module} import {', '.join(names)}" for module, names in sorted(modules.items()))
    head = "\n\n" + block + "\n"
    offset = len(imports) + head[: head.index(f"class {class_name}(")].count("\n")
    return Written(source=head, imports=imports, line=after, class_line=after + offset)


def _is_declare(decorator: ast.expr, variable: str) -> bool:
    call = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(call, ast.Attribute)
        and call.attr == "declare"
        and isinstance(call.value, ast.Name)
        and call.value.id == variable
    )


def import_line(source: str) -> int:
    """Where new imports go: after the last one at the top of ``source``."""
    tree = _top_level(source)
    line = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            line = node.end_lineno or line
    return line


__all__ = [
    "CannotDeclare",
    "Written",
    "action_missing",
    "class_name_for",
    "class_names",
    "import_line",
    "known_structures",
    "signature",
    "state_missing",
    "write_dependency",
]
