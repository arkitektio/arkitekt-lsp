"""A blok keeps its scope while it is edited: the literal is anchored to it, not its text."""

import os

from arkitekt_lsp.live import live_findings
from arkitekt_lsp.regions import blok_literals
from arkitekt_lsp.scopes import anchor_bloks, blok_of, to_blok_scope

PANEL = '<Blok kind="agent" context="panel"><Card /></Blok>'
RUN = '<Blok kind="action" context="panel"><Text>one</Text></Blok>'
SOURCE = (
    "from arkitekt import App\n"
    "app = App('a')\n"
    f"app.blok('panel', '{PANEL}')\n"
    f"@app.workflow(bloks='{RUN}')\n"
    "def run(lab, a: int) -> int:\n"
    "    return a\n"
)
PATH = os.path.realpath("/app.py")
"""As the server passes it: resolved, which on Windows gives it a drive."""
SCOPE = {
    "kinds": ["action", "assign"],
    "dependencies": [{"key": "lab"}],
    "args": ["a"],
    "catalog": None,
}
REPORT = {
    "bloks": [
        {
            "blok": "panel",
            "source": PANEL,
            "file": "/app.py",
            "call_line": 3,
            "scope": {"kinds": ["agent"]},
        },
        {
            "blok": "run:panel",
            "source": RUN,
            "file": "/app.py",
            "call_line": 4,
            "scope": SCOPE,
        },
    ]
}


def test_each_literal_is_anchored_to_the_blok_the_check_found_there() -> None:
    literals = blok_literals(SOURCE)

    assert anchor_bloks(SOURCE, literals, PATH, REPORT) == ("panel", "run:panel")


def test_a_blok_keeps_its_anchor_while_its_text_changes() -> None:
    anchors = anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, REPORT)
    edited = blok_literals(
        SOURCE.replace("<Text>one</Text>", "<Text>{deps.lab.x}</Text>")
    )

    assert blok_of(edited, 1, anchors, {}) == "run:panel"


def test_a_new_literal_voids_the_anchors_and_only_a_checked_text_is_known() -> None:
    anchors = anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, REPORT)
    source = (
        SOURCE
        + f"app.blok('other', '{PANEL}')\napp.blok('new', '<Blok kind=\"agent\" context=\"panel\"><Text>new</Text></Blok>')\n"
    )
    literals = blok_literals(source)

    assert len(literals) == 4
    assert blok_of(literals, 3, anchors, {"panel": PANEL}) is None
    assert blok_of(literals, 2, anchors, {"panel": PANEL}) == "panel"


def test_a_report_without_scopes_anchors_nothing() -> None:
    report = {
        "bloks": [{"blok": "panel", "source": PANEL, "file": "/app.py", "call_line": 3}]
    }

    assert anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, report) == (None, None)


def test_a_report_scope_reads_as_the_validators() -> None:
    scope = to_blok_scope(SCOPE)

    assert (scope.kinds, scope.mounted, scope.args) == (
        ("action", "assign"),
        True,
        ("a",),
    )
    assert [dependency.key for dependency in scope.dependencies] == ["lab"]


def test_with_its_scope_an_edited_blok_is_told_what_it_reads_wrong() -> None:
    (literal,) = blok_literals(
        'x = \'<Blok kind="action" context="panel"><Text>{args.missing}</Text></Blok>\'\n'
    )

    assert live_findings(literal) == [], (
        "mounted nowhere, what the mount provides waits for the check"
    )
    assert any(
        f.code.startswith("args") or "missing" in f.message
        for f in live_findings(literal, SCOPE)
    )
