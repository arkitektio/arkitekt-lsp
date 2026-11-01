"""The server's connected paths, with what a connection would have answered: no network."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lsprotocol import types
from pygls.workspace import Workspace

from arkitekt_lsp.connected import (
    DEFAULT_URL,
    ActionsUnavailable,
    AgentInfo,
    Availability,
    Connection,
    action_lens_title,
    assign_input,
)
from arkitekt_lsp.server import (
    ArkitektLanguageServer,
    CheckKey,
    CheckState,
    Settings,
    code_lens,
    hover,
    rekuest_declare,
    rekuest_offers,
)

SOURCE = (
    "class Lab(Protocol):\n"
    "    def ping(self, n: int) -> int: ...\n"
    "\n"
    "app.blok('panel', '<Html><Text text={deps.lab.state} /></Html>')\n"
)
DEPENDENCY = {"key": "lab", "action_dependencies": [{"key": "ping", "demand": {"key": "ping"}}]}


class FakeConnection:
    actions = False
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


class FakeLogin:
    """A connection whose login goes as the test says, and that says what was done to it."""

    actions = False
    made: list["FakeLogin"] = []

    def __init__(self, url: str, status: Any, login: Any, version: str = "0", actions: bool = False) -> None:
        self.url, self.connected, self.pending, self.stopped = url, False, None, False
        FakeLogin.made.append(self)

    async def start(self) -> None: ...

    async def stop(self) -> None:
        self.stopped = True

    async def availability(self, dependency: dict[str, Any]) -> None:
        return None


def test_logging_in_after_a_failure_tries_again_where_the_settings_alone_would_not(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ls, _, _ = served
    FakeLogin.made = []
    monkeypatch.setattr("arkitekt_lsp.server.Connection", FakeLogin)
    monkeypatch.setattr(ls.protocol, "notify", lambda *_: None)
    ls.settings.connect = True

    asyncio.run(ls.connect_as_configured())
    asyncio.run(ls.connect_as_configured())
    assert len(FakeLogin.made) == 1

    asyncio.run(ls.log_in())
    assert len(FakeLogin.made) == 2 and FakeLogin.made[0].stopped


def test_logging_out_ends_the_run_before_it_forgets_the_login(served: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ls, _, _ = served
    told: list[tuple[str, Any]] = []
    monkeypatch.setattr(ls.protocol, "notify", lambda method, params: told.append((method, params)))
    connection = FakeLogin("https://hub", None, None)
    ls.connection = connection
    ls.settings.connect, ls.settings.server_url = True, "https://hub"

    def forget(url: str, version: str, actions: bool = False) -> bool:
        assert connection.stopped, "a run still going would write the session back"
        assert url == "https://hub"
        return True

    monkeypatch.setattr("arkitekt_lsp.server.forget_login", forget)

    assert asyncio.run(ls.log_out()) is True
    assert ls.connection is None and ls.settings.connect is False
    assert told[-1][1]["state"] == "off"


def test_a_client_that_shows_the_login_is_told_the_challenge(served: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ls, _, _ = served
    told: list[tuple[str, Any]] = []
    monkeypatch.setattr(ls.protocol, "notify", lambda method, params: told.append((method, params)))
    ls.settings.update({"clientShowsLogin": True})
    challenge = SimpleNamespace(
        user_code="AB-CD", verification_uri_complete="https://hub/ok?code=AB-CD", expires_in=600
    )

    asyncio.run(ls.show_login(challenge))

    assert told == [
        (
            "arkitekt/rekuest/challenge",
            {"url": DEFAULT_URL, "code": "AB-CD", "link": "https://hub/ok?code=AB-CD", "expiresIn": 600},
        )
    ]


def test_a_pick_of_what_an_agent_offers_becomes_an_edit_of_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "app.py"
    source = 'from arkitekt import App\n\napp = App("mine")\n'
    file.write_text(source)
    ls = ArkitektLanguageServer()
    workspace = Workspace(tmp_path.as_uri(), types.TextDocumentSyncKind.Incremental)
    workspace.put_text_document(types.TextDocumentItem(uri=file.as_uri(), language_id="python", version=1, text=source))
    monkeypatch.setattr(ls.protocol, "_workspace", workspace, raising=False)
    applied: list[Any] = []

    async def apply(params: Any) -> Any:
        applied.append(params)
        return SimpleNamespace(applied=True, failure_reason=None)

    monkeypatch.setattr(ls, "workspace_apply_edit_async", apply)

    class Offering:
        async def offers(self) -> dict[str, dict[str, Any]]:
            return {
                "7": {
                    "id": "7",
                    "name": "stage",
                    "connected": True,
                    "app": "microscope-stage",
                    "version": "1",
                    "actions": [{"key": "home", "args": [], "returns": []}],
                    "states": [],
                }
            }

    ls.connection = Offering()  # type: ignore[assignment]

    listed = asyncio.run(rekuest_offers(ls, SimpleNamespace(uri=file.as_uri())))
    (agent,) = listed["agents"]
    assert (agent["className"], agent["actions"][0]["signature"]) == ("MicroscopeStage", "home()")

    pick = SimpleNamespace(uri=file.as_uri(), agent="7", className="MicroscopeStage", actions=["home"], states=[])
    answer = asyncio.run(rekuest_declare(ls, pick))

    (edit,) = applied
    texts = "".join(change.new_text for change in edit.edit.changes[file.as_uri()])
    assert "error" not in answer
    assert '@app.declare(app="microscope-stage")\nclass MicroscopeStage(Protocol):' in texts
    assert "from typing import Protocol" in texts


def test_the_f_string_warning_follows_its_setting(served: Any, tmp_path: Path) -> None:
    ls, _, published = served
    file = tmp_path / "panel.py"
    text = 'app.blok("panel", f"<Html>{parts}</Html>")\n'
    file.write_text(text)
    uri = file.as_uri()
    ls.workspace.put_text_document(types.TextDocumentItem(uri=uri, language_id="python", version=1, text=text))

    ls.refresh(uri)
    assert [diagnostic.code for diagnostic in published[-1].diagnostics] == ["blok.generated"]

    ls.settings.update({"check": {"warnOnFStrings": False}})
    ls.refresh(uri)
    assert published[-1].diagnostics == []


def test_a_forwarded_query_cannot_change_anything_whatever_the_page_sent() -> None:
    """The page is not trusted to keep to reads: the refusal is here, before any client is asked."""
    connection = Connection("https://example", lambda *_, **__: None, None, actions=True)  # type: ignore[arg-type]
    connection.runtime = object()  # any client it asked for would fail on this

    with pytest.raises(ActionsUnavailable, match="never a mutation"):
        asyncio.run(connection.query("rekuest", "mutation Go { assign { id } }", {}))


def test_the_lens_says_whether_an_action_can_run() -> None:
    assert "Not on the server" in action_lens_title(None)
    assert "no agent connected" in action_lens_title({"implementations": [{"agent": {"connected": False}}]})
    assert action_lens_title({"implementations": [{"agent": {"connected": True}}]}) == "$(play) Run · 1 agent"


def test_pins_only_travel_with_the_implementation_they_are_for() -> None:
    pins = [{"key": "stage", "autoResolve": False, "mappedAgents": [{"key": "stage", "agent": "7"}]}]

    pinned = assign_input("abc", {"x": 1}, "ref", "12", pins)
    assert pinned["implementation"] == "12" and pinned["dependencies"] == pins

    # An action target has no dependencies of its own: the server would have nothing to bind them to.
    anywhere = assign_input("abc", {"x": 1}, "ref", None, pins)
    assert "implementation" not in anywhere and "dependencies" not in anywhere
