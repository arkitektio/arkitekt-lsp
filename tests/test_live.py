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
