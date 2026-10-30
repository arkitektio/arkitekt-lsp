"""A blok keeps its scope while it is edited: the literal is anchored to it, not its text."""

import os

from arkitekt_lsp.live import live_findings
from arkitekt_lsp.regions import blok_literals
from arkitekt_lsp.scopes import anchor_bloks, blok_of, to_blok_scope

SOURCE = (
    "from arkitekt import App\n"
    "app = App('a')\n"
    "app.blok('panel', '<Html><Card /></Html>')\n"
    "@app.workflow(bloks='<Html><Text>one</Text></Html>')\n"
    "def run(lab, a: int) -> int:\n"
    "    return a\n"
)
PATH = os.path.realpath("/app.py")
"""As the server passes it: resolved, which on Windows gives it a drive."""
SCOPE = {"kind": "action", "context": "HTML", "dependencies": [{"key": "lab"}], "args": ["a"], "catalogs": []}
REPORT = {
    "bloks": [
        {"blok": "panel", "source": "<Html><Card /></Html>", "file": "/app.py", "call_line": 3, "scope": {"kind": "agent"}},
        {"blok": "run:html", "source": "<Html><Text>one</Text></Html>", "file": "/app.py", "call_line": 4, "scope": SCOPE},
    ]
}


def test_each_literal_is_anchored_to_the_blok_the_check_found_there() -> None:
    literals = blok_literals(SOURCE)

    assert anchor_bloks(SOURCE, literals, PATH, REPORT) == ("panel", "run:html")


def test_a_blok_keeps_its_anchor_while_its_text_changes() -> None:
    anchors = anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, REPORT)
    edited = blok_literals(SOURCE.replace("<Text>one</Text>", "<Text>{deps.lab.x}</Text>"))

    assert blok_of(edited, 1, anchors, {}) == "run:html"


def test_a_new_literal_voids_the_anchors_and_only_a_checked_text_is_known() -> None:
    anchors = anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, REPORT)
    source = SOURCE + "app.blok('other', '<Html><Card /></Html>')\napp.blok('new', '<Html><Text>new</Text></Html>')\n"
    literals = blok_literals(source)

    assert len(literals) == 4
    assert blok_of(literals, 3, anchors, {"panel": "<Html><Card /></Html>"}) is None
    assert blok_of(literals, 2, anchors, {"panel": "<Html><Card /></Html>"}) == "panel"


def test_a_report_without_scopes_anchors_nothing() -> None:
    report = {"bloks": [{"blok": "panel", "source": "<Html><Card /></Html>", "file": "/app.py", "call_line": 3}]}

    assert anchor_bloks(SOURCE, blok_literals(SOURCE), PATH, report) == (None, None)


def test_a_report_scope_reads_as_the_validators() -> None:
    scope = to_blok_scope(SCOPE)

    assert (scope.kind, scope.context, scope.args) == ("action", "HTML", ("a",))
    assert [dependency.key for dependency in scope.dependencies] == ["lab"]


def test_with_its_scope_an_edited_blok_is_told_what_it_reads_wrong() -> None:
    (literal,) = blok_literals("x = '<Html><Text>{args.missing}</Text></Html>'\n")

    assert live_findings(literal) == [], "without a scope, a read waits for the check"
    assert any(f.code.startswith("args") or "missing" in f.message for f in live_findings(literal, SCOPE))
