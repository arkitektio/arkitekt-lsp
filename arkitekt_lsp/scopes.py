"""Which blok a literal is, once a check has seen it, so its scope follows it while it is edited.

A check's report carries each blok's ``scope``: where it is mounted (the kinds the mount takes,
its dependencies, args and assets), what ``deps.<key>`` and ``args.<key>`` resolve against. What
the blok owns (locals, queries, subscriptions) is in its own text. What is being typed no longer
equals the source the check saw, so a literal is matched to its blok by where it is: when the
report comes, each literal of a file it names is anchored to the blok the check found there.
While the file has as many blok literals as it had then, the n-th literal is the n-th anchor's
blok, however its text changed; once a blok literal is added or removed, only a literal whose
text is still a checked source is known, until the next check.

A report without scopes (version 1) anchors nothing: every literal is checked as the language
alone sees it.
"""

import os
from collections.abc import Mapping, Sequence
from typing import Any

from arkitekt_spec.actions import AgentDependencyInput
from arkitekt_spec.declare.blok.validate import BlokScope

from arkitekt_lsp.placement import find_literal
from arkitekt_lsp.regions import BlokLiteral

Anchors = tuple[str | None, ...]
"""Per blok literal of a file, in source order, the blok it held when the report came."""


def _blok_context(blok: Mapping[str, Any]) -> str | None:
    context = blok.get("context")
    return context if isinstance(context, str) else None


def anchor_bloks(
    source: str, literals: Sequence[BlokLiteral], path: str, report: Mapping[str, Any]
) -> Anchors:
    """The blok each literal of ``source`` (the file at real ``path``) held when ``report`` came."""
    anchors: list[str | None] = [None] * len(literals)
    index_of = {id(literal): index for index, literal in enumerate(literals)}
    for blok in report.get("bloks", []):
        key, blok_source = blok.get("blok"), blok.get("source")
        if not isinstance(key, str) or "scope" not in blok:
            continue
        file, call_line = blok.get("file"), blok.get("call_line")
        if isinstance(file, str) and isinstance(call_line, int):
            if os.path.realpath(file) != path:
                continue
            literal = find_literal(
                source, literals, call_line, blok_source, _blok_context(blok)
            )
        else:
            # Declared nowhere it says: only its text tells which literal it is.
            literal = next(
                (literal for literal in literals if literal.value == blok_source), None
            )
        if literal is not None:
            index = index_of[id(literal)]
            if anchors[index] is None:
                anchors[index] = key
    return tuple(anchors)


def blok_of(
    literals: Sequence[BlokLiteral],
    index: int,
    anchors: Anchors | None,
    sources: Mapping[str, str],
) -> str | None:
    """The blok the ``index``-th literal is: its anchor while the literals are as many as they were,
    else the blok whose checked source it still is."""
    if (
        anchors is not None
        and len(anchors) == len(literals)
        and anchors[index] is not None
    ):
        return anchors[index]
    value = literals[index].value
    return next((key for key, source in sources.items() if source == value), None)


def to_blok_scope(scope: Mapping[str, Any]) -> BlokScope:
    """A report's ``scope`` (snake_case, the mount ``arkitekt check`` says) as the validator takes it."""
    args = scope.get("args")
    assets = scope.get("assets")
    kinds = scope.get("kinds")
    return BlokScope(
        kinds=tuple(str(kind) for kind in kinds) if isinstance(kinds, list) else (),
        mount="where the app declares it",
        dependencies=tuple(
            AgentDependencyInput.model_validate(d)
            for d in scope.get("dependencies") or ()
        ),
        args=tuple(args) if isinstance(args, list) else None,
        assets=frozenset(assets) if isinstance(assets, list) else None,
    )


__all__ = ["Anchors", "anchor_bloks", "blok_of", "to_blok_scope"]
