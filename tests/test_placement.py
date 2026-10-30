"""Placing a finding of `arkitekt check --json-out` in the Python source it is about."""

from arkitekt_lsp.placement import place
from arkitekt_lsp.regions import blok_literals

SOURCE = '''from arkitekt import App
app = App("checked", "1.0")

app.blok(
    "panel",
    """
    <Html><Card><Slidr /></Card></Html>
    """,
)
'''
BLOK = "\n    <Html><Card><Slidr /></Card></Html>\n    "


def _place(diagnostic: dict[str, object], source: str = SOURCE, bloks: dict[str, str] | None = None) -> str | None:
    span = place(diagnostic, source, blok_literals(source), {"panel": BLOK} if bloks is None else bloks)
    return None if span is None else source[span[0] : span[1]]


def test_a_finding_about_a_node_is_placed_at_its_tag() -> None:
    finding = {"call_line": 4, "blok": "panel", "code": "catalog.unknown_component", "node": "Html[0]/Card[0]/Slidr[0]"}

    assert _place(finding) == "<Slidr"


def test_a_finding_with_a_line_and_column_is_placed_there() -> None:
    # line 2 of the BSX (the text starts with a newline), column 17: `<Slidr`
    assert _place({"call_line": 4, "blok": "panel", "code": "bsx.syntax", "line": 2, "column": 17}) == "<Slidr"


def test_a_finding_follows_its_blok_when_the_code_above_moves() -> None:
    moved = SOURCE.replace("app = App(", "# a comment\n\n\napp = App(")

    assert _place({"call_line": 4, "blok": "panel", "code": "bsx.syntax", "line": 2, "column": 17}, moved) == "<Slidr"


def test_a_finding_about_a_blok_edited_since_is_dropped() -> None:
    edited = SOURCE.replace("<Slidr />", "<Slider />")
    finding = {"call_line": 4, "blok": "panel", "code": "catalog.unknown_component"}

    assert place(finding, edited, blok_literals(edited), {"panel": BLOK}, located={"panel"}) is None


def test_a_blok_that_was_never_a_literal_is_told_at_its_declaring_line() -> None:
    dedented = SOURCE.replace('app.blok(\n    "panel",\n    """', 'app.blok(\n    "panel",\n    dedent("""').replace(
        '""",\n)', '"""),\n)'
    )
    finding = {"call_line": 4, "blok": "panel", "code": "catalog.unknown_component"}
    span = place(finding, dedented, blok_literals(dedented), {"panel": "<Html><Card><Slidr /></Card></Html>\n"})

    assert span is not None and dedented[span[0] : span[1]] == "app.blok("


def test_a_refusal_about_no_listed_blok_goes_to_the_declaring_statement() -> None:
    assert _place({"call_line": 4, "code": "catalog.unknown_component", "line": 2, "column": 17}, bloks={}) == "<Slidr"


def test_a_finding_about_no_blok_goes_to_its_line() -> None:
    assert _place({"call_line": 2, "message": "TypeError: no"}) == 'app = App("checked", "1.0")'
