"""The language server: ``arkitekt check`` on save, the BSX of each blok checked as it is typed.

It runs in the app's own environment (the editor starts that environment's ``arkitekt-lsp``):

- **On save** of a Python file it runs ``arkitekt check --json-out`` for the app the file belongs
  to, in a child process: a check imports the app, and an import that hangs, crashes or leaves
  modules behind must not take the server with it. A newer save stops the older check.
- **What the check found** is published at the BSX it is about (:mod:`.placement`), in every file
  it names, and follows its blok as the code around it is edited.
- **A blok edited since** is checked as it is typed (:mod:`.live`), in process and without the app,
  against the scope the check said it has (:mod:`.scopes`): its literal stays anchored to its blok.
- ``bsx/regions`` hands a client the blok literals of a document, each with its blok's scope when a
  check has seen it, for what it does in them itself (an editor's completion).

All of that is local. Only with ``--connect`` (``arkitekt.server.connect``) does it also ask rekuest,
read-only, who would satisfy each dependency the app declares (:mod:`.connected`): a count above the
protocol, a warning when nobody does, the agents on hover.

Which app a file belongs to: the file itself when it declares one (``App(``), else the
``target`` setting, else the project's default (``ARKITEKT_APP``, or ``app.py``). A project is the
nearest directory with a ``pyproject.toml``.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path
from pygls.workspace import TextDocument

from arkitekt_lsp.connected import (
    DEFAULT_URL,
    Availability,
    Connection,
    class_span,
    declared_protocols,
    dependency_key_at,
    forget_login,
    hover_markdown,
    lens_title,
    problem,
)
from arkitekt_lsp.declare import (
    CannotDeclare,
    action_missing,
    class_name_for,
    class_names,
    import_line,
    known_structures,
    signature,
    state_missing,
    write_dependency,
)
from arkitekt_lsp.live import live_findings
from arkitekt_lsp.placement import Span, place
from arkitekt_lsp.regions import BlokLiteral, blok_literals
from arkitekt_lsp.scopes import Anchors, anchor_bloks, blok_of

logger = logging.getLogger(__name__)

CHECK_SOURCE = "arkitekt check"
"""The source of what the check found: as the app sees it."""
LIVE_SOURCE = "bsx"
"""The source of what an edited blok says about itself: as typed."""
REKUEST_SOURCE = "rekuest"
"""The source of what the server said about a dependency: connected only."""
REKUEST_STATUS = "arkitekt/rekuest"
"""Told to the client whenever the connection changes."""
REKUEST_REFRESH = "arkitekt/rekuest/refresh"
REKUEST_LOGIN = "arkitekt/rekuest/login"
"""Taken from the client: log in now, at ``url`` if it names one."""
REKUEST_LOGOUT = "arkitekt/rekuest/logout"
"""Asked by the client: leave, revoke the login and forget it."""
REKUEST_OFFERS = "arkitekt/rekuest/offers"
"""Asked by the client: the agents, and what each offers."""
REKUEST_DECLARE = "arkitekt/rekuest/declare"
"""Asked by the client: write a dependency on what it picked of one agent into a file."""
REKUEST_CHALLENGE = "arkitekt/rekuest/challenge"
"""Told to a client that shows the login itself: what to approve, and where."""
STATUS = "arkitekt/status"
"""Told to the client whenever a check starts or ends."""
REGIONS = "bsx/regions"
CHECK = "arkitekt/check"

_DECLARES_APP = re.compile(r"^[^#\n]*\b(?:Qt)?App\s*(?:\[[^\]\n]*\])?\s*\(", re.MULTILINE)
_SEVERITY = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
}
_LIVE_DELAY = 0.3

try:
    __version__ = version("arkitekt-lsp")
except PackageNotFoundError:  # pragma: no cover - a source checkout
    __version__ = "0.0.0"


@dataclass
class Settings:
    """What the client configures (``arkitekt.*``); initialization options, then configuration."""

    check_on_save: bool = True
    timeout: float = 120.0
    target: str | None = None
    connect: bool = False
    """Ask rekuest about dependencies. Off, the server talks to nothing."""
    server_url: str = DEFAULT_URL
    server_from_flags: bool = False
    """``--connect`` or ``--server`` was given: what the editor configures does not move it."""
    client_picks_dependencies: bool = False
    """The client said so at initialization: a connected file offers it the way in, above its app."""
    client_shows_login: bool = False
    """The client said so at initialization: it gets the challenge, not a plain message."""

    def update(self, options: Any) -> None:  # noqa: ANN401 - LSPAny
        if not isinstance(options, dict):
            return
        check = options.get("check")
        if isinstance(check, dict):
            options = {**options, "checkOnSave": check.get("onSave"), "timeout": check.get("timeout")}
        if isinstance(options.get("checkOnSave"), bool):
            self.check_on_save = options["checkOnSave"]
        if isinstance(options.get("timeout"), (int, float)) and options["timeout"] > 0:
            self.timeout = float(options["timeout"])
        if "target" in options:
            self.target = options["target"] or None
        if options.get("clientPicksDependencies") is True:
            self.client_picks_dependencies = True
        if options.get("clientShowsLogin") is True:
            self.client_shows_login = True
        server = options.get("server")
        if isinstance(server, dict) and not self.server_from_flags:
            if isinstance(server.get("connect"), bool):
                self.connect = server["connect"]
            if isinstance(server.get("url"), str):
                self.server_url = server["url"].strip() or DEFAULT_URL


@dataclass(frozen=True)
class CheckKey:
    """One app to check: its project and the target ``arkitekt check`` takes (None: the default)."""

    root: str
    target: str | None

    def __str__(self) -> str:
        return self.target or f"{Path(self.root).name} (default app)"


@dataclass
class CheckState:
    report: dict[str, Any] | None = None
    """The last report: what is wrong now."""
    last_good: dict[str, Any] | None = None
    """The last report of a check that got through: what the app declares. A check that fails
    knows no bloks, and a broken save is when a blok's scope is needed most."""
    located: dict[str, set[str]] | None = None
    """Per file (real path), the bloks whose source was one of its literals when the report came."""
    anchors: dict[str, Anchors] | None = None
    """Per file (real path), the blok each of its literals held when the report came."""
    availability: dict[str, Availability] = field(default_factory=dict[str, Availability])
    """Per protocol the report lists, by name, who the server said would satisfy it."""
    process: asyncio.subprocess.Process | None = None
    generation: int = 0


def project_root(path: Path, folders: list[Path]) -> Path:
    """The nearest directory of ``path`` with a ``pyproject.toml``, within a workspace folder."""
    folder = next((folder for folder in folders if path.is_relative_to(folder)), None)
    for directory in path.parents:
        if (directory / "pyproject.toml").exists():
            return directory
        if folder is not None and directory == folder:
            return folder
    return folder or path.parent


class ArkitektLanguageServer(LanguageServer):
    """See the module: the check, its placement and the live layer, for one editor."""

    def __init__(self) -> None:
        super().__init__("arkitekt-lsp", __version__, text_document_sync_kind=types.TextDocumentSyncKind.Incremental)
        self.settings = Settings()
        self.checks: dict[CheckKey, CheckState] = {}
        self.published: dict[str, str] = {}
        """The documents told something, by unquoted URI: told again (if only that it is clean) on
        every refresh."""
        self.pending: dict[str, asyncio.TimerHandle] = {}
        self.one_at_a_time = asyncio.Semaphore(1)
        """Checks import whole apps (torch, CUDA): opening many app files queues them, newest kept."""
        self.connection: Connection | None = None
        self.offers: dict[str, dict[str, Any]] = {}
        """What the agents offered, when last asked: what a pick of them is read against."""
        """The read-only login, when connecting was asked for."""

    # -- which app

    def folders(self) -> list[Path]:
        folders = [Path(to_fs_path(folder.uri) or "") for folder in self.workspace.folders.values()]
        if not folders and self.workspace.root_path:
            folders = [Path(self.workspace.root_path)]
        return [folder.resolve() for folder in folders]

    def check_key(self, uri: str) -> CheckKey | None:
        """The app the file at ``uri`` belongs to, or None when it belongs to none this knows of."""
        fs_path = to_fs_path(uri)
        if fs_path is None or not fs_path.endswith(".py"):
            return None
        path = Path(fs_path).resolve()
        root = project_root(path, self.folders())
        try:
            text = self.workspace.get_text_document(uri).source
        except OSError:
            return None
        if _DECLARES_APP.search(text) and path.is_relative_to(root):
            return CheckKey(str(root), path.relative_to(root).as_posix())
        if self.settings.target:
            return CheckKey(str(root), self.settings.target)
        if os.environ.get("ARKITEKT_APP") or (root / "app.py").exists():
            return CheckKey(str(root), None)
        return None

    # -- the check

    def status(self, state: str, key: CheckKey, **fields: object) -> None:
        self.protocol.notify(STATUS, {"state": state, "app": str(key), **fields})

    def is_open(self, uri: str) -> bool:
        """Whether the client has ``uri`` open (pygls keys documents by the unquoted URI)."""
        return unquote(uri) in self.workspace.text_documents

    async def run_check(self, key: CheckKey) -> None:
        """Check ``key``'s app in a child process and publish what it found; a newer run stops this one.

        One check runs at a time; a run still queued when a newer one for the same app is asked
        for never starts.
        """
        state = self.checks.setdefault(key, CheckState())
        state.generation += 1
        generation = state.generation
        if state.process is not None and state.process.returncode is None:
            state.process.kill()
        async with self.one_at_a_time:
            if state.generation == generation:
                await self._check(key, state, generation)

    async def _check(self, key: CheckKey, state: CheckState, generation: int) -> None:
        self.status("checking", key)
        with tempfile.TemporaryDirectory(prefix="arkitekt-lsp-") as directory:
            out = Path(directory) / "report.json"
            args = ["--work-dir", key.root, "check", "--json-out", str(out)]
            if key.target is not None:
                args.append(key.target)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "arkitekt.cli.main",
                *args,
                cwd=key.root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
            state.process = process
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), self.settings.timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                if state.generation == generation:
                    self.status(
                        "failed", key, message=f"The check took longer than {self.settings.timeout:g}s and was stopped."
                    )
                return
            if state.generation != generation:
                return  # a newer save superseded it
            if not out.exists():
                said = stderr.decode(errors="replace").strip()
                logger.warning("arkitekt check wrote no report for %s: %s", key, said)
                self.status("failed", key, message=said or "The check ended without a report.")
                return
            report = json.loads(out.read_text())
        state.report = report
        state.located = self.locate(report)
        if report.get("ok"):
            state.last_good = report
            state.anchors = self.anchor(report)
        self.refresh_all()
        if self.connection is not None:
            asyncio.ensure_future(self.ask_about(state))
        diagnostics = report.get("diagnostics", [])
        unplaced = [d["message"] for d in diagnostics if not d.get("file") or not Path(d["file"]).exists()]
        self.status(
            "ok" if report.get("ok") else "failed",
            key,
            errors=sum(1 for d in diagnostics if d.get("level") == "error"),
            warnings=sum(1 for d in diagnostics if d.get("level") == "warning"),
            message=unplaced[0] if unplaced else None,
        )

    def locate(self, report: dict[str, Any]) -> dict[str, set[str]]:
        """Per file the report names, the bloks whose source is one of its literals as it reads now."""
        bloks = {blok["blok"]: blok["source"] for blok in report.get("bloks", []) if "source" in blok}
        located: dict[str, set[str]] = {}
        for found in report.get("diagnostics", []):
            file, blok = found.get("file"), found.get("blok")
            if not file or blok not in bloks:
                continue
            path = os.path.realpath(file)
            if path not in located:
                uri = from_fs_path(file)
                try:
                    text = self.workspace.get_text_document(uri).source if uri else Path(file).read_text()
                except OSError:
                    continue
                values = {literal.value for literal in blok_literals(text)}
                located[path] = {key for key, source in bloks.items() if source in values}
        return located

    def anchor(self, report: dict[str, Any]) -> dict[str, Anchors]:
        """Per file the report's bloks were declared in, the blok each of its literals holds now."""
        anchors: dict[str, Anchors] = {}
        for blok in report.get("bloks", []):
            file = blok.get("file")
            if not isinstance(file, str) or "scope" not in blok:
                continue
            path = os.path.realpath(file)
            if path in anchors:
                continue
            uri = from_fs_path(file)
            try:
                text = self.workspace.get_text_document(uri).source if uri else Path(file).read_text()
            except OSError:
                continue
            anchors[path] = anchor_bloks(text, blok_literals(text), path, report)
        return anchors

    def scope_of(self, path: str, literals: list[BlokLiteral], index: int) -> tuple[str, dict[str, Any]] | None:
        """The blok the ``index``-th literal of the file at real ``path`` is and its scope, if a check saw it."""
        for state in self.checks.values():
            if state.last_good is None:
                continue
            records = {blok["blok"]: blok for blok in state.last_good.get("bloks", []) if "scope" in blok}
            sources = {key: blok["source"] for key, blok in records.items() if "source" in blok}
            key = blok_of(literals, index, (state.anchors or {}).get(path), sources)
            if key is not None and key in records:
                return key, records[key]["scope"]
        return None

    # -- the server (connected only)

    def connection_status(self, state: str, message: str | None = None) -> None:
        url = self.connection.url if self.connection is not None else self.settings.server_url
        self.protocol.notify(REKUEST_STATUS, {"state": state, "url": url, "message": message})

    async def show_login(self, challenge: Any) -> None:  # noqa: ANN401 - fakts' DeviceCodeChallenge
        """Offer the login in the editor; opening it is the user's click, and waiting is not on them."""
        if self.settings.client_shows_login:
            url = self.connection.url if self.connection is not None else self.settings.server_url
            self.protocol.notify(
                REKUEST_CHALLENGE,
                {
                    "url": url,
                    "code": challenge.user_code,
                    "link": challenge.verification_uri_complete,
                    "expiresIn": challenge.expires_in,
                },
            )
            return

        async def offer() -> None:
            chosen = await self.window_show_message_request_async(
                types.ShowMessageRequestParams(
                    type=types.MessageType.Info,
                    message=f"Arkitekt: approve read-only access for this editor (code {challenge.user_code}).",
                    actions=[types.MessageActionItem(title="Open")],
                )
            )
            if chosen is not None:
                await self.window_show_document_async(
                    types.ShowDocumentParams(uri=challenge.verification_uri_complete, external=True)
                )

        asyncio.ensure_future(offer())

    async def connect_as_configured(self) -> None:
        """Make the connection what the settings say: none, or one to their server."""
        wanted = self.settings.server_url if self.settings.connect else None
        current = self.connection
        if current is not None and current.url == wanted:
            return
        if current is not None:
            self.connection = None
            await current.stop()
            for state in self.checks.values():
                state.availability = {}
            self.refresh_all()
            self.connection_status("off")
        if wanted is None:
            return
        connection = Connection(wanted, self.connection_status, self.show_login, __version__)
        self.connection = connection
        await connection.start()
        if self.connection is not connection:  # turned off or moved while the login was pending
            await connection.stop()
            return
        for state in self.checks.values():
            await self.ask_about(state)

    def structures_known_to(self, uri: str) -> dict[str, Any]:
        """The classes the app of ``uri`` has for identifiers, as its last good check said."""
        key = self.check_key(uri)
        state = self.checks.get(key) if key is not None else None
        return known_structures(state.last_good if state is not None else None)

    async def log_in(self, url: str | None = None) -> None:
        """Connect because the user asked, also where the settings alone would change nothing: a
        connection that failed keeps its url, and only asking again gives it another go."""
        self.settings.connect = True
        if url:
            self.settings.server_url = url.strip() or DEFAULT_URL
        current = self.connection
        if current is not None and current.url == self.settings.server_url:
            if current.connected:
                self.connection_status("connected")
                return
            if current.pending is not None:
                return
            self.connection = None
            await current.stop()
        await self.connect_as_configured()

    async def log_out(self) -> bool:
        """Leave, then revoke and forget the login; whether there was one. In that order: a run still
        going would write the session back when its token rotates."""
        url = self.connection.url if self.connection is not None else self.settings.server_url
        self.settings.connect = False
        await self.connect_as_configured()
        forgotten = await asyncio.to_thread(forget_login, url, __version__)
        self.connection_status("off")
        return forgotten

    async def ask_about(self, state: CheckState) -> None:
        """Ask the server about every protocol ``state``'s report lists, and tell what it said."""
        connection = self.connection
        if connection is None or state.last_good is None:
            return
        answers: dict[str, Availability] = {}
        for protocol in declared_protocols(state.last_good):
            answer = await connection.availability(protocol["dependency"])
            if answer is not None:
                answers[protocol["name"]] = answer
        state.availability = answers
        self.refresh_all()
        capabilities = self.protocol.client_capabilities.workspace
        if capabilities is not None and capabilities.code_lens is not None and capabilities.code_lens.refresh_support:
            self.workspace_code_lens_refresh(None)

    def about_protocols(self, path: str, text: str) -> list[tuple[tuple[int, int], dict[str, Any], Availability]]:
        """The protocols declared in the file at real ``path``, where each is now and what was said."""
        found: list[tuple[tuple[int, int], dict[str, Any], Availability]] = []
        for state in self.checks.values():
            for protocol in declared_protocols(state.last_good or {}):
                file, answer = protocol.get("file"), state.availability.get(protocol["name"])
                if not isinstance(file, str) or answer is None or os.path.realpath(file) != path:
                    continue
                span = class_span(text, protocol["name"])
                if span is not None:
                    found.append((span, dict(protocol), answer))
        return found

    def schedule_check(self, uri: str) -> None:
        key = self.check_key(uri)
        if key is not None:
            asyncio.ensure_future(self.run_check(key))

    # -- publishing

    def diagnostic(
        self,
        document: TextDocument,
        span: Span,
        level: str,
        code: str | None,
        message: str,
        hint: str | None,
        source: str,
    ) -> types.Diagnostic:
        return types.Diagnostic(
            range=types.Range(
                start=document.client_position_at_offset(span[0]), end=document.client_position_at_offset(span[1])
            ),
            severity=_SEVERITY.get(level, types.DiagnosticSeverity.Information),
            code=code,
            source=source,
            message=f"{message}\n{hint}" if hint else message,
        )

    def refresh(self, uri: str) -> None:
        """Publish what the checks found in ``uri``, and what its edited bloks say, as it reads now."""
        fs_path = to_fs_path(uri)
        if fs_path is None:
            return
        document = self.workspace.get_text_document(uri)
        try:
            text = document.source
        except OSError:
            text = None
        diagnostics: list[types.Diagnostic] = []
        if text is not None:
            path = os.path.realpath(fs_path)
            literals = blok_literals(text)
            told: set[tuple[str | None, Span]] = set()
            checked: set[str] = set()
            for state in self.checks.values():
                if state.report is None:
                    continue
                bloks = {blok["blok"]: blok["source"] for blok in state.report.get("bloks", []) if "source" in blok}
                checked.update(bloks.values())
                for found in state.report.get("diagnostics", []):
                    if not found.get("file") or os.path.realpath(found["file"]) != path:
                        continue
                    span = place(found, text, literals, bloks, (state.located or {}).get(path, ()))
                    if span is None:
                        continue
                    told.add((found.get("code"), span))
                    diagnostics.append(
                        self.diagnostic(
                            document,
                            span,
                            found.get("level", "warning"),
                            found.get("code"),
                            found["message"],
                            found.get("hint"),
                            CHECK_SOURCE,
                        )
                    )
            if self.is_open(uri):
                for index, literal in enumerate(literals):
                    if literal.value in checked:
                        continue  # as the check saw it: the check has said it
                    known = self.scope_of(path, literals, index)
                    for finding in live_findings(literal, known[1] if known is not None else None):
                        if (finding.code, finding.span) in told:
                            continue
                        diagnostics.append(
                            self.diagnostic(
                                document,
                                finding.span,
                                finding.level,
                                finding.code,
                                finding.message,
                                finding.hint,
                                LIVE_SOURCE,
                            )
                        )
            for span, protocol, answer in self.about_protocols(path, text):
                said = problem(protocol["dependency"], answer)
                if said is not None:
                    diagnostics.append(
                        self.diagnostic(document, span, said[0], "rekuest.dependency", said[1], None, REKUEST_SOURCE)
                    )
        if not diagnostics and unquote(uri) not in self.published:
            return
        self.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics))
        if diagnostics:
            self.published[unquote(uri)] = uri
        else:
            self.published.pop(unquote(uri), None)

    def refresh_all(self) -> None:
        """Republish every file a check named, every file told before, and every open one."""
        uris = dict(self.published)
        uris.update({unquote(document.uri): document.uri for document in self.workspace.text_documents.values()})
        for state in self.checks.values():
            for found in (state.report or {}).get("diagnostics", []):
                if found.get("file"):
                    uri = from_fs_path(found["file"])
                    if uri is not None:
                        uris.setdefault(unquote(uri), uri)
        for uri in uris.values():
            self.refresh(uri)

    def refresh_soon(self, uri: str) -> None:
        """Refresh ``uri`` once typing pauses."""
        pending = self.pending.pop(uri, None)
        if pending is not None:
            pending.cancel()
        self.pending[uri] = asyncio.get_running_loop().call_later(_LIVE_DELAY, self.refresh, uri)

    # -- regions

    def regions(self, uri: str) -> list[dict[str, object]]:
        """The blok literals of ``uri``, in client positions: where a client may treat text as BSX.

        A literal a check has seen carries its ``blok`` and ``scope`` (``BlokCheckScope``), what
        its references complete against; one no check has seen carries neither.
        """
        document = self.workspace.get_text_document(uri)
        fs_path = to_fs_path(uri)
        path = os.path.realpath(fs_path) if fs_path is not None else None
        literals = blok_literals(document.source)
        found: list[dict[str, object]] = []
        for index, literal in enumerate(literals):
            start = document.client_position_at_offset(literal.start)
            end = document.client_position_at_offset(literal.end)
            region: dict[str, object] = {
                "start": {"line": start.line, "character": start.character},
                "end": {"line": end.line, "character": end.character},
                "value": literal.value,
                "root": literal.root,
                "context": literal.context,
                "kind": literal.kind,
                "exact": literal.exact,
            }
            known = self.scope_of(path, literals, index) if path is not None else None
            if known is not None:
                region["blok"], region["scope"] = known
            found.append(region)
        return found


server = ArkitektLanguageServer()


@server.feature(types.INITIALIZE)
def initialize(ls: ArkitektLanguageServer, params: types.InitializeParams) -> None:
    ls.settings.update(params.initialization_options)


@server.feature(types.INITIALIZED)
async def initialized(ls: ArkitektLanguageServer, params: types.InitializedParams) -> None:
    await ls.connect_as_configured()


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
async def did_change_configuration(ls: ArkitektLanguageServer, params: types.DidChangeConfigurationParams) -> None:
    settings = params.settings
    ls.settings.update(settings.get("arkitekt") if isinstance(settings, dict) else None)
    await ls.connect_as_configured()


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
async def did_open(ls: ArkitektLanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    uri = params.text_document.uri
    ls.refresh(uri)
    key = ls.check_key(uri)
    if key is not None and ls.settings.check_on_save and key not in ls.checks:
        ls.checks[key] = CheckState()  # now, so a second open of the same app does not check it twice
        asyncio.ensure_future(ls.run_check(key))


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
async def did_change(ls: ArkitektLanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    ls.refresh_soon(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
async def did_save(ls: ArkitektLanguageServer, params: types.DidSaveTextDocumentParams) -> None:
    ls.refresh(params.text_document.uri)
    if ls.settings.check_on_save:
        ls.schedule_check(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: ArkitektLanguageServer, params: types.DidCloseTextDocumentParams) -> None:
    ls.refresh(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_CODE_LENS)
def code_lens(ls: ArkitektLanguageServer, params: types.CodeLensParams) -> list[types.CodeLens]:
    """Above each protocol, how many agents would satisfy it. Nothing unless connected."""
    uri = params.text_document.uri
    fs_path = to_fs_path(uri)
    if ls.connection is None or fs_path is None:
        return []
    document = ls.workspace.get_text_document(uri)
    lenses: list[types.CodeLens] = []
    for span, _, answer in ls.about_protocols(os.path.realpath(fs_path), document.source):
        at = document.client_position_at_offset(span[0])
        lenses.append(
            types.CodeLens(
                range=types.Range(start=at, end=at),
                command=types.Command(title=lens_title(answer), command="arkitekt.rekuest.refresh"),
            )
        )
    if ls.settings.client_picks_dependencies and ls.connection.connected:
        for match in _DECLARES_APP.finditer(document.source):
            at = document.client_position_at_offset(match.start())
            lenses.append(
                types.CodeLens(
                    range=types.Range(start=at, end=at),
                    command=types.Command(title="$(add) Add a dependency", command="arkitekt.addDependency"),
                )
            )
    return lenses


@server.feature(types.TEXT_DOCUMENT_HOVER)
async def hover(ls: ArkitektLanguageServer, params: types.HoverParams) -> types.Hover | None:
    """On a protocol's name or a blok's ``deps.<key>``: the agents that would satisfy it."""
    connection = ls.connection
    fs_path = to_fs_path(params.text_document.uri)
    if connection is None or fs_path is None:
        return None
    document = ls.workspace.get_text_document(params.text_document.uri)
    offset = document.offset_at_position(params.position)
    path = os.path.realpath(fs_path)
    for span, protocol, answer in ls.about_protocols(path, document.source):
        if span[0] <= offset <= span[1]:
            return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=hover_markdown(protocol["name"], answer)))
    literals = blok_literals(document.source)
    for index, literal in enumerate(literals):
        inside = literal.to_value(offset)
        key = dependency_key_at(literal.value, inside) if inside is not None else None
        known = ls.scope_of(path, literals, index) if key is not None else None
        if known is None:
            continue
        dependency = next((d for d in known[1].get("dependencies") or () if d.get("key") == key), None)
        answer = await connection.availability(dependency) if dependency is not None else None
        if answer is not None:
            return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=hover_markdown(f"deps.{key}", answer)))
    return None


@server.feature(REKUEST_REFRESH)
async def rekuest_refresh(ls: ArkitektLanguageServer, params: Any) -> None:  # noqa: ANN401 - a custom notification
    """Ask the server again instead of waiting for the answers to age."""
    if ls.connection is None:
        return
    ls.connection.forget()
    for state in ls.checks.values():
        await ls.ask_about(state)


@server.feature(REKUEST_OFFERS)
async def rekuest_offers(ls: ArkitektLanguageServer, params: Any) -> dict[str, Any]:  # noqa: ANN401 - a custom request
    """The agents and what each offers, each already worded for a picker."""
    connection = ls.connection
    offers = await connection.offers() if connection is not None else None
    if offers is None:
        return {"error": "Not logged in, or the server did not answer."}
    ls.offers = offers
    uri = getattr(params, "uri", None)
    taken = class_names(ls.workspace.get_text_document(uri).source) if uri else []
    known = ls.structures_known_to(uri) if uri else {}
    return {
        "agents": [
            {
                "id": offer["id"],
                "name": offer["name"],
                "connected": offer["connected"],
                "app": offer["app"],
                "version": offer["version"],
                "className": class_name_for(offer["app"], taken),
                "actions": [
                    {"key": a["key"], "name": a.get("name"), "signature": signature(a, known), "missing": action_missing(a, known)}
                    for a in offer["actions"]
                ],
                "states": [{"key": s["key"], "missing": state_missing(s, known)} for s in offer["states"]],
            }
            for offer in offers.values()
        ]
    }


@server.feature(REKUEST_DECLARE)
async def rekuest_declare(ls: ArkitektLanguageServer, params: Any) -> dict[str, Any]:  # noqa: ANN401 - a custom request
    """Write the dependency into the file, as an edit the editor applies: undoable like any other."""
    offer = ls.offers.get(str(params.agent))
    if offer is None:
        return {"error": "That agent is gone: pick it again."}
    picked_actions, picked_states = set(params.actions or ()), set(params.states or ())
    source = ls.workspace.get_text_document(params.uri).source
    try:
        written = write_dependency(
            source,
            app=offer["app"],
            class_name=params.className,
            actions=[a for a in offer["actions"] if a["key"] in picked_actions],
            states=[s for s in offer["states"] if s["key"] in picked_states],
            known=ls.structures_known_to(params.uri),
        )
        line = import_line(source)
    except CannotDeclare as error:
        return {"error": str(error)}
    at_end = written.line >= len(source.splitlines()) and not source.endswith("\n")
    place = types.Position(line=written.line, character=0)
    edits = [
        types.TextEdit(range=types.Range(start=place, end=place), new_text=("\n" if at_end else "") + written.source)
    ]
    if written.imports:
        at = types.Position(line=line, character=0)
        edits.insert(0, types.TextEdit(range=types.Range(start=at, end=at), new_text="\n".join(written.imports) + "\n"))
    answer = await ls.workspace_apply_edit_async(
        types.ApplyWorkspaceEditParams(edit=types.WorkspaceEdit(changes={params.uri: edits}), label="Add a dependency")
    )
    if not answer.applied:
        return {"error": answer.failure_reason or "The editor did not apply the edit."}
    return {"line": written.class_line}


@server.feature(REKUEST_LOGIN)
async def rekuest_login(ls: ArkitektLanguageServer, params: Any) -> None:  # noqa: ANN401 - a custom notification
    await ls.log_in(getattr(params, "url", None))


@server.feature(REKUEST_LOGOUT)
async def rekuest_logout(ls: ArkitektLanguageServer, params: Any) -> dict[str, bool]:  # noqa: ANN401 - a custom request
    return {"forgotten": await ls.log_out()}


@server.feature(REGIONS)
def regions(ls: ArkitektLanguageServer, params: Any) -> dict[str, object]:  # noqa: ANN401 - a custom request
    return {"regions": ls.regions(params.textDocument.uri)}


@server.feature(CHECK)
async def check(ls: ArkitektLanguageServer, params: Any) -> None:  # noqa: ANN401 - a custom notification
    uri = getattr(getattr(params, "textDocument", None), "uri", None)
    key = ls.check_key(uri) if uri else None
    if key is None:
        ls.window_show_message(
            types.ShowMessageParams(
                type=types.MessageType.Info, message="This file belongs to no app arkitekt check knows of."
            )
        )
        return
    await ls.run_check(key)


def main() -> None:
    """``arkitekt-lsp``: serve over stdio, the way an editor starts a language server."""
    parser = argparse.ArgumentParser(
        prog="arkitekt-lsp", description="The Arkitekt language server: arkitekt check, in the BSX of every blok."
    )
    parser.add_argument("--stdio", action="store_true", help="Serve over stdin/stdout (the default).")
    parser.add_argument("--version", action="version", version=f"arkitekt-lsp {__version__}")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Also ask rekuest, read-only, who would satisfy each dependency the app declares. Off by default: "
        "the server then talks to nothing.",
    )
    parser.add_argument(
        "--server",
        default=None,
        metavar="URL",
        help=f"The coordination server to connect to; implies --connect (default: {DEFAULT_URL}). "
        "Either flag overrides what the editor configures.",
    )
    args = parser.parse_args()
    server.settings.connect = args.connect or bool(args.server)
    server.settings.server_from_flags = server.settings.connect
    if args.server:
        server.settings.server_url = args.server
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server.start_io()


__all__ = ["ArkitektLanguageServer", "CheckKey", "Settings", "main", "project_root", "server"]
