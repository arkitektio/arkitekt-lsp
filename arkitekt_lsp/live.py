"""What a blok's BSX says about itself while it is typed, before any check has seen it.

``arkitekt check`` imports the app, so it runs on save. Between saves an edited blok is checked
here, in process, with the validator ``arkitekt check`` runs (:func:`check_blok_tree`): the
language and the catalogs, nothing of the app. Without the app its scope is empty, so beyond what
the parser refuses only the findings that do not depend on what the blok declares are told; a
reference to a dependency, a local or a query waits for the check.
"""

from dataclasses import dataclass

from arkitekt_spec.blok.bsx import parse_bsx_with_spans
from arkitekt_spec.blok.catalogs import default_view
from arkitekt_spec.blok.vocabulary import BlokError
from arkitekt_spec.declare.blok.registry import catalogs_of
from arkitekt_spec.declare.blok.validate import BlokScope, check_blok_tree, place

from arkitekt_lsp.placement import Span, place_in_literal
from arkitekt_lsp.regions import BlokLiteral

SCOPE_FREE = frozenset(
    {"bsx", "expr", "callback", "palette", "props", "foreach", "context", "logo", "catalog", "toast"}
)
"""The diagnostic families (a code's first segment) an empty scope cannot get wrong."""


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


def live_findings(literal: BlokLiteral) -> list[LiveFinding]:
    """What the language and the default catalogs find in ``literal``, without the app."""
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
    scope = BlokScope(kind=literal.kind, context=literal.context, catalogs=catalogs_of(tree, view))
    findings = [place(finding, spans) for finding in check_blok_tree(tree, scope, view)]
    placed: list[LiveFinding] = []
    for finding in findings:
        if not _scope_free(finding.code):
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


__all__ = ["SCOPE_FREE", "LiveFinding", "live_findings"]
