"""What a blok's BSX says about itself while it is typed, before any check has seen it.

``arkitekt check`` imports the app, so it runs on save. Between saves an edited blok is checked
here, in process, with the validator ``arkitekt check`` runs (:func:`check_blok_tree`): the
language and the catalog the blok is written for. A blok says what it is (``<Blok kind
context>``) and declares what it owns (``<local>``, ``<query>``, ``<subscription>``), so all of
that is checked from its text alone, mounted nowhere: only what its mount provides
(``deps.<key>``, ``args.<key>``, ``self``'s actions and states, a local's ``like``) waits for a
check, which says where it is mounted (its scope, see :mod:`.scopes`).

A blok names the catalog it is written for (its ``<Blok catalog=...>``): the base catalog when it
names none, which ships with arkitekt-spec, as does a snapshot of the Orkestrator's. Any other
is pulled from the address it is published at, once per session
(:func:`arkitekt_spec.blok.catalogs.fetch.resolve_catalog`: https only, capped in size and
time, read as data and kept for the next time). One that cannot be had leaves what it adds
unchecked: the base catalog still is.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arkitekt_spec.blok.bsx import parse_bsx_with_spans, source_catalog
from arkitekt_spec.blok.catalogs import (
    BlokView,
    blok_view,
    catalog_chain,
    default_view,
    is_catalog_target,
    known_catalog,
)
from arkitekt_spec.blok.catalogs.fetch import CatalogUnavailable, cache_file, resolve_catalog
from arkitekt_spec.blok.vocabulary import BlokError, load_vocabulary
from arkitekt_spec.declare.blok.validate import BlokScope, check_blok_tree, place

from arkitekt_lsp.placement import Span, place_in_literal
from arkitekt_lsp.regions import BlokLiteral
from arkitekt_lsp.scopes import to_blok_scope

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
        if is_address(catalog):
            try:
                resolve_catalog(catalog, timeout=PULL_TIMEOUT)
            except CatalogUnavailable:
                _unavailable.add(catalog)
        else:
            _unavailable.add(catalog)
    return blok_view(catalog) if known_catalog(catalog) is not None else blok_view(complete=False)


def is_address(catalog: object) -> bool:
    """Whether ``catalog`` is the address a catalog is published at: the only kind that is pulled."""
    return isinstance(catalog, str) and is_catalog_target(catalog) and load_vocabulary().catalog_id.fullmatch(catalog) is not None


def forget_unavailable() -> None:
    """Ask again for the catalogs that could not be had: the client changed what it names."""
    _unavailable.clear()


def published_catalogs(identifier: str, *, refresh: bool = False) -> list[str]:
    """The catalog published at ``identifier`` and those it extends, as the JSON they were
    published as, the one nearest the base first: what a client registers to complete against it.

    Those that ship (the base, the Orkestrator's) are left out: a client has them. Pulled as
    :func:`view_for` pulls (https only, capped, read as data), and kept.

    Raises:
        CatalogUnavailable: If ``identifier`` is no address, or it cannot be had.
    """
    if not is_address(identifier):
        raise CatalogUnavailable(f"{identifier!r} is not the address a catalog is published at (https://...)")
    if known_catalog(identifier) is None:
        try:
            resolve_catalog(identifier, refresh=refresh, timeout=PULL_TIMEOUT)
        except CatalogUnavailable:
            _unavailable.add(identifier)
            raise
        _unavailable.discard(identifier)
    shipped = set(load_vocabulary().bundled_catalogs)
    texts: list[str] = []
    for catalog in catalog_chain(identifier):
        if catalog.id in shipped:
            continue
        if catalog.id is None:
            continue
        try:
            texts.append(cache_file(catalog.id).read_text(encoding="utf-8"))
        except OSError as error:
            raise CatalogUnavailable(f"the catalog {catalog.id!r} is known but its published copy is not kept") from error
    return texts


def live_findings(literal: BlokLiteral, scope: Mapping[str, Any] | None = None) -> list[LiveFinding]:
    """What the language and the blok's catalog find in ``literal``, without the app.

    Without ``scope`` it is checked mounted nowhere; with one (where the last check said it is
    mounted) what its mount provides is judged too. It is read against the catalog it names.
    """
    try:
        named: object = source_catalog(literal.value)
    except BlokError:
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
    # Every finding, errors too. Mounted nowhere, the checker leaves what a mount provides alone.
    checked = to_blok_scope(scope) if scope is not None else BlokScope()
    told = _judged_by_scope
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


__all__ = [
    "UNSCOPED",
    "LiveFinding",
    "forget_unavailable",
    "is_address",
    "live_findings",
    "published_catalogs",
    "view_for",
]
