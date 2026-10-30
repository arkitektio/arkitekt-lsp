"""The server's connected paths, with what a connection would have answered: no network."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lsprotocol import types
from pygls.workspace import Workspace

from arkitekt_lsp.connected import DEFAULT_URL, AgentInfo, Availability
from arkitekt_lsp.server import ArkitektLanguageServer, CheckKey, CheckState, Settings, code_lens, hover

SOURCE = (
    "class Lab(Protocol):\n"
    "    def ping(self, n: int) -> int: ...\n"
    "\n"
    "app.blok('panel', '<Html><Text text={deps.lab.state} /></Html>')\n"
)
DEPENDENCY = {"key": "lab", "action_dependencies": [{"key": "ping", "demand": {"key": "ping"}}]}


class FakeConnection:
    url = "https://example"

    def __init__(self, answer: Availability | None) -> None:
        self.answer = answer
        self.asked: list[dict[str, Any]] = []

    async def availability(self, dependency: dict[str, Any]) -> Availability | None:
        self.asked.append(dependency)
        return self.answer


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ArkitektLanguageServer, str, list[Any]]:
    file = tmp_path / "app.py"
    file.write_text(SOURCE)
    uri = file.as_uri()
    ls = ArkitektLanguageServer()
    workspace = Workspace(tmp_path.as_uri(), types.TextDocumentSyncKind.Incremental)
    workspace.put_text_document(types.TextDocumentItem(uri=uri, language_id="python", version=1, text=SOURCE))
    published: list[Any] = []
    monkeypatch.setattr(ls.protocol, "_workspace", workspace, raising=False)
    monkeypatch.setattr(ls, "text_document_publish_diagnostics", published.append)
    monkeypatch.setattr(
        type(ls.protocol),
        "client_capabilities",
        SimpleNamespace(workspace=None),
        raising=False,
    )
    report = {
        "bloks": [
            {
                "blok": "panel",
                "source": "<Html><Text text={deps.lab.state} /></Html>",
                "file": str(file),
                "call_line": 4,
                "scope": {"kind": "agent", "context": "HTML", "dependencies": [DEPENDENCY], "args": []},
            }
        ],
        "protocols": [{"name": "Lab", "file": str(file), "line": 1, "dependency": DEPENDENCY}],
    }
    state = CheckState(report=report, last_good=report)
    state.anchors = ls.anchor(report)
    ls.checks[CheckKey(str(tmp_path), None)] = state
    return ls, uri, published


NOBODY = Availability(agents=(), slots={"ping": 0})
TWO = Availability(
    agents=(
        AgentInfo(id="1", name="a", connected=True, app="lab", version="1"),
        AgentInfo(id="2", name="b", connected=False),
    ),
    slots={"ping": 2},
)


def _state(ls: ArkitektLanguageServer) -> CheckState:
    return next(iter(ls.checks.values()))


def _lens_params(uri: str) -> types.CodeLensParams:
    return types.CodeLensParams(text_document=types.TextDocumentIdentifier(uri=uri))


def test_without_a_connection_there_is_no_lens_and_no_hover(served: Any) -> None:
    ls, uri, _ = served

    assert code_lens(ls, _lens_params(uri)) == []
    params = types.HoverParams(
        text_document=types.TextDocumentIdentifier(uri=uri), position=types.Position(line=0, character=7)
    )
    assert asyncio.run(hover(ls, params)) is None


def test_asking_puts_a_count_above_the_protocol(served: Any) -> None:
    ls, uri, _ = served
    ls.connection = FakeConnection(TWO)

    asyncio.run(ls.ask_about(_state(ls)))

    (lens,) = code_lens(ls, _lens_params(uri))
    assert lens.command.title == "2 agents · 1 connected"
    assert lens.range.start.line == 0


def test_nobody_satisfying_it_is_a_warning_at_its_name(served: Any) -> None:
    ls, uri, published = served
    ls.connection = FakeConnection(NOBODY)

    asyncio.run(ls.ask_about(_state(ls)))

    told = [d for params in published for d in params.diagnostics if d.source == "rekuest"]
    assert told, "the warning reached the editor"
    assert told[-1].severity == types.DiagnosticSeverity.Warning
    assert (told[-1].range.start.line, told[-1].range.start.character) == (0, 6)


def test_a_check_that_fails_keeps_what_the_last_good_one_said(served: Any) -> None:
    ls, uri, _ = served
    ls.connection = FakeConnection(TWO)
    state = _state(ls)
    asyncio.run(ls.ask_about(state))

    state.report = {"ok": False, "bloks": [], "protocols": [], "diagnostics": []}

    assert code_lens(ls, _lens_params(uri)), "the lens stays"
    character = SOURCE.splitlines()[3].index("deps.lab") + 6
    params = types.HoverParams(
        text_document=types.TextDocumentIdentifier(uri=uri), position=types.Position(line=3, character=character)
    )
    assert asyncio.run(hover(ls, params)) is not None, "the blok keeps its scope"


def test_flags_outrank_what_the_editor_configures() -> None:
    settings = Settings(connect=True, server_from_flags=True, server_url="https://mine")

    settings.update({"server": {"connect": False, "url": "https://theirs"}})

    assert (settings.connect, settings.server_url) == (True, "https://mine")


def test_the_editor_configures_the_server_otherwise() -> None:
    settings = Settings()

    settings.update({"server": {"connect": True, "url": " "}})

    assert (settings.connect, settings.server_url) == (True, DEFAULT_URL)


def test_hovering_the_protocol_lists_the_agents(served: Any) -> None:
    ls, uri, _ = served
    ls.connection = FakeConnection(TWO)
    asyncio.run(ls.ask_about(_state(ls)))

    params = types.HoverParams(
        text_document=types.TextDocumentIdentifier(uri=uri), position=types.Position(line=0, character=7)
    )
    answer = asyncio.run(hover(ls, params))

    assert answer is not None and "`a`" in answer.contents.value


def test_hovering_a_dependency_in_a_blok_asks_about_what_its_scope_says(served: Any) -> None:
    ls, uri, _ = served
    connection = FakeConnection(TWO)
    ls.connection = connection

    character = SOURCE.splitlines()[3].index("deps.lab") + 6
    params = types.HoverParams(
        text_document=types.TextDocumentIdentifier(uri=uri), position=types.Position(line=3, character=character)
    )
    answer = asyncio.run(hover(ls, params))

    assert answer is not None and "deps.lab" in answer.contents.value
    assert connection.asked == [DEPENDENCY]
