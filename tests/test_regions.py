"""Finding the bloks of a Python source: string literals whose text opens with ``<Blok``."""

from arkitekt_lsp.regions import blok_literals, generated_bloks

APP = '''from arkitekt import App
app = App("checked", "1.0")

app.blok(
    "panel",
    """
    <Blok kind="agent" context="panel"><Card><Text text="hi" /></Card></Blok>
    """,
)

# app.blok("commented", "<Blok />")
NOT_A_BLOK = "<b>bold</b>"
PROGRESS = '<Blok kind="action" context="panel"><Progress value={task.progress} /></Blok>'

@app.workflow(bloks=['<Blok kind="action" context="scene"><Space room="lab"><Placed dep="robot" /></Space></Blok>', PROGRESS])
def run() -> None:
    """<Blok> in a docstring is a docstring."""
'''


def test_each_blok_literal_is_found_with_what_its_blok_says() -> None:
    found = blok_literals(APP)

    # A blok kept in a module constant is the same blok as one written inline: its <Blok> says
    # what it is, wherever the literal sits.
    assert [(literal.root, literal.context, literal.kind) for literal in found] == [
        ("Blok", "PANEL", "agent"),
        ("Blok", "PANEL", "action"),
        ("Blok", "SCENE", "action"),
    ]
    assert found[0].line == 6 and found[0].exact
    assert APP[found[0].start : found[0].end] == found[0].value


def test_a_blok_that_does_not_say_what_it_is_yet_is_still_a_blok() -> None:
    (literal,) = blok_literals(
        "@app.workflow(bloks='<Blok context=\"panel\"><Card /></Blok>')\ndef f(): ...\n"
    )

    assert (literal.context, literal.kind) == ("PANEL", None)


def test_a_value_maps_to_and_from_the_source() -> None:
    (panel, *_) = blok_literals(APP)
    card = panel.value.index("<Card")

    assert APP[panel.to_source(card) :].startswith("<Card")
    assert panel.to_value(panel.to_source(card)) == card
    assert panel.to_value(panel.start - 1) is None


def test_escapes_are_resolved_and_mapped() -> None:
    source = 'x = "<Blok kind=\\"agent\\" context=\\"panel\\"><Text text=\\"a\\" /></Blok>"\n'

    (literal,) = blok_literals(source)

    assert (
        literal.value == '<Blok kind="agent" context="panel"><Text text="a" /></Blok>'
    )
    assert (literal.context, literal.kind) == ("PANEL", "agent")
    assert not literal.exact
    quote = literal.value.index('"')
    assert source[literal.to_source(quote) : literal.to_source(quote) + 2] == '\\"'


def test_a_source_being_typed_still_yields_the_bloks_before_the_break() -> None:
    source = 'app.blok("panel", "<Blok><Card /></Blok>")\napp.blok("open", """<Blok><Ca'

    assert [literal.value for literal in blok_literals(source)] == [
        "<Blok><Card /></Blok>"
    ]


def test_a_docstring_is_never_a_blok() -> None:
    assert (
        blok_literals('def f():\n    """<Blok> in a docstring."""\n\n"""<Blok />"""\n')
        == []
    )


def test_bytes_and_f_strings_are_never_bloks() -> None:
    assert blok_literals('a = b"<Blok />"\nb = f"<Blok>{x}</Blok>"\n') == []


def test_only_an_f_string_blok_with_a_field_counts_as_generated() -> None:
    source = (
        'app.blok("panel", f"""\n  <Blok>{{self.a}}<Row>{"".join(parts)}</Row></Blok>\n""")\n'
        'app.blok("constant", f"<Blok>{{self.a}}</Blok>")\n'
        'app.blok("plain", "<Blok />")\n'
        'FRAGMENT = f"<Row>{parts}</Row>"\n'
        'f"<Blok>{parts}</Blok>"\n'
    )

    assert [source[start:end] for start, end in generated_bloks(source)] == ['f"""']
