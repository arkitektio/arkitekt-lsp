"""What a blok's BSX says about itself while it is typed, before any check has seen it.

``arkitekt check`` imports the app, so it runs on save. Between saves an edited blok is checked
here, in process, with the validator ``arkitekt check`` runs (:func:`check_blok_tree`): the
language and the catalog the blok is written for, and what the last check said the blok
declares (its scope, see :mod:`.scopes`). A blok no check has seen has an empty scope, so
beyond what the parser refuses only the findings that do not depend on what the blok declares
are told; a reference to a dependency, a local or a query waits for the check.

A blok names the catalog it is written for (the scope's ``catalog``): the base catalog when it
names none, which ships with arkitekt-spec, as does a snapshot of the Orkestrator's. Any other
is pulled from the address it is published at, once per session
(:func:`arkitekt_spec.blok.catalogs.fetch.resolve_catalog`: https only, capped in size and
time, read as data and kept for the next time). One that cannot be had leaves what it adds
unchecked: the base catalog still is.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arkitekt_spec.blok.bsx import parse_bsx_with_spans
from arkitekt_spec.blok.catalogs import BlokView, blok_view, default_view, is_catalog_target, known_catalog
from arkitekt_spec.blok.catalogs.fetch import CatalogUnavailable, resolve_catalog
from arkitekt_spec.blok.vocabulary import BlokError, load_vocabulary
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


_unavailable: set[str] = set()
"""Catalogs that could not be had this session: not asked for again on every keystroke."""

PULL_TIMEOUT = 3.0
"""Seconds an editor waits for a catalog: a check on save may wait longer."""


def view_for(catalog: str | None) -> BlokView:
    """The view a blok written for ``catalog`` is read against while it is typed.

    The base catalog's when it names none. One that ships, was pulled before or can be pulled
    now is read whole; any other (a name only its app declares, an address nothing answers at)
    is the base catalog's with nothing reported unknown.
    """
    if catalog is None:
        return default_view()
    if known_catalog(catalog) is None and catalog not in _unavailable:
        # Only an address is pulled: a plain name is a catalog the app declared, which a check knows.
        if is_catalog_target(catalog) and load_vocabulary().catalog_id.fullmatch(catalog):
            try:
                resolve_catalog(catalog, timeout=PULL_TIMEOUT)
            except CatalogUnavailable:
                _unavailable.add(catalog)
        else:
            _unavailable.add(catalog)
    return blok_view(catalog) if known_catalog(catalog) is not None else blok_view(complete=False)


def live_findings(literal: BlokLiteral, scope: Mapping[str, Any] | None = None) -> list[LiveFinding]:
    """What the language and the blok's catalog find in ``literal``, without the app.

    With ``scope`` (what the last check said this blok declares) a reference is judged too,
    and the blok is read against the catalog it names.
    """
    named = scope.get("catalog") if scope is not None else None
    view = view_for(named if isinstance(named, str) else None)
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
    if scope is not None:
        checked = to_blok_scope(scope)
        told = _judged_by_scope
    else:
        checked = BlokScope(kind=literal.kind, context=literal.context)
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


__all__ = ["SCOPE_FREE", "UNSCOPED", "LiveFinding", "live_findings", "view_for"]
