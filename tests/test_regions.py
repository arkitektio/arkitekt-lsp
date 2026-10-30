"""Finding the bloks of a Python source: string literals whose text opens with a blok root."""

from arkitekt_lsp.regions import blok_literals

APP = '''from arkitekt import App
app = App("checked", "1.0")

app.blok(
    "panel",
    """
    <Html><Card><Text text="hi" /></Card></Html>
    """,
)

# app.blok("commented", "<Html />")
NOT_A_BLOK = "<b>bold</b>"

@app.workflow(bloks="<Overlay><Glow on={task.running} /></Overlay>")
def run() -> None:
    """<Html> in a docstring is a docstring."""
'''


def test_each_blok_literal_is_found_with_its_context_and_kind() -> None:
    found = blok_literals(APP)

    assert [(literal.root, literal.context, literal.kind) for literal in found] == [
        ("Html", "HTML", "agent"),
        ("Overlay", "OVERLAY", "action"),
    ]
    assert found[0].line == 6 and found[0].exact
    assert APP[found[0].start : found[0].end] == found[0].value


def test_a_value_maps_to_and_from_the_source() -> None:
    (panel, _) = blok_literals(APP)
    card = panel.value.index("<Card")

    assert APP[panel.to_source(card) :].startswith("<Card")
    assert panel.to_value(panel.to_source(card)) == card
    assert panel.to_value(panel.start - 1) is None


def test_escapes_are_resolved_and_mapped() -> None:
    source = 'x = "<Html><Text text=\\"a\\" /></Html>"\n'

    (literal,) = blok_literals(source)

    assert literal.value == '<Html><Text text="a" /></Html>'
    assert not literal.exact
    quote = literal.value.index('"')
    assert source[literal.to_source(quote) : literal.to_source(quote) + 2] == '\\"'


def test_an_action_blok_in_a_bloks_argument_is_an_action_blok() -> None:
    (literal,) = blok_literals('@app.workflow(bloks="<Html><Card /></Html>")\ndef f(): ...\n')

    assert literal.kind == "action"


def test_a_source_being_typed_still_yields_the_bloks_before_the_break() -> None:
    source = 'app.blok("panel", "<Html><Card /></Html>")\napp.blok("open", """<Html><Ca'

    assert [literal.value for literal in blok_literals(source)] == ["<Html><Card /></Html>"]


def test_a_docstring_is_never_a_blok() -> None:
    assert blok_literals('def f():\n    """<Html> in a docstring."""\n\n"""<Html />"""\n') == []


def test_bytes_and_f_strings_are_never_bloks() -> None:
    assert blok_literals('a = b"<Html />"\nb = f"<Html>{x}</Html>"\n') == []
