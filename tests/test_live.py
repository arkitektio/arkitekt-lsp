"""What an edited blok says about itself before a check sees it."""

from arkitekt_lsp.live import live_findings
from arkitekt_lsp.regions import blok_literals


def _found(source: str) -> list[tuple[str, str]]:
    (literal,) = blok_literals(source)
    return [(finding.code, source[finding.span[0] : finding.span[1]]) for finding in live_findings(literal)]


def test_an_unknown_component_is_told_at_its_tag() -> None:
    assert _found('app.blok("p", "<Html><Card><Frobnicator /></Card></Html>")\n') == [
        ("catalog.unknown_component", "<Frobnicator")
    ]


def test_a_parse_error_is_told_where_the_parser_stopped() -> None:
    ((code, _),) = _found('app.blok("p", "<Html><Card></Html>")\n')

    assert code.startswith("bsx.")


def test_what_needs_the_app_waits_for_the_check() -> None:
    assert _found('app.blok("p", "<Html><Card><Text text={deps.nowhere.x} /></Card></Html>")\n') == []


# -- the catalog a blok is written for

ORKESTRATOR = "https://arkitekt.live/catalogs/orkestrator/v1"
LAB = "https://acme.example/catalogs/lab/v1"
SCENE = 'app.blok("p", "<Html><MikroScene value={args.scene}><MikroSceneViewport /></MikroScene></Html>")\n'


def _scoped(source: str, catalog: str | None) -> list[str]:
    (literal,) = blok_literals(source)
    scope = {"kind": "agent", "context": "HTML", "args": ["scene"], "catalog": catalog}
    return [finding.code for finding in live_findings(literal, scope)]


def test_a_component_of_another_catalog_is_told_until_the_blok_names_it() -> None:
    assert _found(SCENE)[0][0] == "catalog.target"
    assert _scoped(SCENE, None) == ["catalog.target", "catalog.target"]
    assert _scoped(SCENE, ORKESTRATOR) == []


def test_a_catalog_that_cannot_be_had_leaves_what_it_adds_unchecked(monkeypatch) -> None:  # noqa: ANN001
    from arkitekt_spec.blok.catalogs.fetch import CatalogUnavailable

    import arkitekt_lsp.live as live

    asked: list[str] = []

    def unreachable(catalog: str, **_: object) -> None:
        asked.append(catalog)
        raise CatalogUnavailable("no route")

    monkeypatch.setattr(live, "resolve_catalog", unreachable)
    monkeypatch.setattr(live, "_unavailable", set())
    source = 'app.blok("p", "<Html><Card><Spectrum /></Card><Button frob={1}>Go</Button></Html>")\n'

    # What the lab adds is not known, so it is not reported; the base catalog still is checked.
    assert _scoped(source, LAB) == ["catalog.props"]
    assert _scoped(source, LAB) == ["catalog.props"]
    assert asked == [LAB], "asked for once a session, not on every keystroke"


def test_a_name_only_the_app_declares_is_never_pulled(monkeypatch) -> None:  # noqa: ANN001
    import arkitekt_lsp.live as live

    monkeypatch.setattr(live, "resolve_catalog", lambda *_, **__: (_ for _ in ()).throw(AssertionError("pulled")))
    monkeypatch.setattr(live, "_unavailable", set())

    assert _scoped('app.blok("p", "<Html><Gauge /></Html>")\n', "electron") == []


# -- a published catalog, handed to a client

LAB_CATALOG = (
    '{"id": "' + LAB + '", "name": "lab", "version": 1, "minor": 0, "extends": "https://arkitekt.live/catalogs/base/v3",'
    ' "description": "What the lab draws beyond the base catalog.",'
    ' "components": [{"name": "Spectrum", "description": "A spectrum.", "context": "HTML", "accepts_children": false, "props": []}]}'
)


def test_a_published_catalog_is_handed_out_as_published(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import pytest
    from arkitekt_spec.blok.catalogs import forget_catalogs
    from arkitekt_spec.blok.catalogs.fetch import CatalogUnavailable, cache_file

    import arkitekt_lsp.live as live

    monkeypatch.setenv("ARKITEKT_CATALOG_CACHE", str(tmp_path))
    monkeypatch.setattr(live, "_unavailable", set())
    forget_catalogs()
    try:
        # Not an address: never pulled, whatever asks.
        for name in ("electron", "http://acme.example/catalogs/lab/v1", "file:///etc/passwd", None):
            with pytest.raises(CatalogUnavailable):
                live.published_catalogs(name)  # type: ignore[arg-type]

        file = cache_file(LAB)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(LAB_CATALOG)
        monkeypatch.setattr("arkitekt_spec.blok.catalogs.fetch.fetch_catalog", lambda *_, **__: (_ for _ in ()).throw(CatalogUnavailable("offline")))

        assert live.published_catalogs(LAB) == [LAB_CATALOG], "the kept copy, and not the base it extends: a client has that"
        assert _scoped('app.blok("p", "<Html><Spectrum /><Spectrm /></Html>")\n', LAB) == ["catalog.unknown_component"]
    finally:
        forget_catalogs()


def test_only_addresses_are_taken_as_catalogs_to_pull() -> None:
    from arkitekt_lsp.server import Settings

    settings = Settings()
    settings.update({"catalogs": [LAB, "electron", "http://plain.example/c/v1", LAB, 3]})

    assert settings.catalogs == (LAB,)
