"""What a blok's BSX says about itself while it is typed, before any check has seen it.

``arkitekt check`` imports the app, so it runs on save. Between saves an edited blok is checked
here, in process, with the validator ``arkitekt check`` runs (:func:`check_blok_tree`): the
language and the catalogs, and what the last check said the blok declares (its scope, see
:mod:`.scopes`). A blok no check has seen has an empty scope, so beyond what the parser refuses
only the findings that do not depend on what the blok declares are told; a reference to a
dependency, a local or a query waits for the check.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from arkitekt_spec.blok.bsx import parse_bsx_with_spans
from arkitekt_spec.blok.catalogs import default_view
from arkitekt_spec.blok.vocabulary import BlokError
from arkitekt_spec.declare.blok.registry import catalogs_of
from arkitekt_spec.declare.blok.validate import BlokScope, check_blok_tree, place

from arkitekt_lsp.placement import Span, place_in_literal
from arkitekt_lsp.regions import BlokLiteral
from arkitekt_lsp.scopes import to_blok_scope

SCOPE_FREE = frozenset(
    {"bsx", "expr", "callback", "palette", "props", "foreach", "context", "logo", "catalog", "toast"}
)
"""The diagnostic families (a code's first segment) an empty scope cannot get wrong."""
UNSCOPED = frozenset({"query"})
"""The families a check's scope still cannot judge: a query's value is typed by its service's
schema, which the report does not carry."""


@dataclass(frozen=True)
class LiveFinding:
    """A finding of one blok literal, placed in the Python source."""

    span: Span
    level: str
    code: str
    message: str
    hint: str | None = None


def _scope_free(code: str) -> bool:
    return code.split(".", 1)[0] in SCOPE_FREE


def _judged_by_scope(code: str) -> bool:
    return code.split(".", 1)[0] not in UNSCOPED


def live_findings(literal: BlokLiteral, scope: Mapping[str, Any] | None = None) -> list[LiveFinding]:
    """What the language and the default catalogs find in ``literal``, without the app.

    With ``scope`` (what the last check said this blok declares) a reference is judged too.
    """
    view = default_view()
    try:
        tree, spans = parse_bsx_with_spans(literal.value, view=view)
    except BlokError as error:
        # The parser knows no scope: whatever it refuses is the text's own.
        where = {"line": error.line, "column": error.column, "node": error.node}
        return [
            LiveFinding(
                span=place_in_literal(literal, {key: value for key, value in where.items() if value is not None}),
                level="error",
                code=error.code,
                message=error.message,
                hint=error.hint,
            )
        ]
    # Every finding, errors too: one the empty scope gets wrong must not hide the rest.
    # The catalogs it draws from, as the app's own check takes them when none are declared.
    if scope is not None:
        checked = to_blok_scope(scope)
        checked = replace(checked, catalogs=checked.catalogs or catalogs_of(tree, view))
        told = _judged_by_scope
    else:
        checked = BlokScope(kind=literal.kind, context=literal.context, catalogs=catalogs_of(tree, view))
        told = _scope_free
    findings = [place(finding, spans) for finding in check_blok_tree(tree, checked, view)]
    placed: list[LiveFinding] = []
    for finding in findings:
        if not told(finding.code):
            continue
        where = {"line": finding.line, "column": finding.column, "node": finding.path}
        placed.append(
            LiveFinding(
                span=place_in_literal(literal, {key: value for key, value in where.items() if value is not None}),
                level=finding.level,
                code=finding.code,
                message=finding.message,
                hint=finding.hint,
            )
        )
    return placed


__all__ = ["SCOPE_FREE", "UNSCOPED", "LiveFinding", "live_findings"]
