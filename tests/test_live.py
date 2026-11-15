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
