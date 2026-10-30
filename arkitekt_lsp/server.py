"""The language server: ``arkitekt check`` on save, the BSX of each blok checked as it is typed.

It runs in the app's own environment (the editor starts that environment's ``arkitekt-lsp``):

- **On save** of a Python file it runs ``arkitekt check --json-out`` for the app the file belongs
  to, in a child process: a check imports the app, and an import that hangs, crashes or leaves
  modules behind must not take the server with it. A newer save stops the older check.
- **What the check found** is published at the BSX it is about (:mod:`.placement`), in every file
  it names, and follows its blok as the code around it is edited.
- **A blok edited since** is checked as it is typed (:mod:`.live`), in process and without the app.
- ``bsx/regions`` hands a client the blok literals of a document, for what it does in them itself
  (an editor's completion).

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
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path
from pygls.workspace import TextDocument

from arkitekt_lsp.live import live_findings
from arkitekt_lsp.placement import Span, place
from arkitekt_lsp.regions import blok_literals

logger = logging.getLogger(__name__)

CHECK_SOURCE = "arkitekt check"
"""The source of what the check found: as the app sees it."""
LIVE_SOURCE = "bsx"
"""The source of what an edited blok says about itself: as typed."""
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
    located: dict[str, set[str]] | None = None
    """Per file (real path), the bloks whose source was one of its literals when the report came."""
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
        self.refresh_all()
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
                for literal in literals:
                    if literal.value in checked:
                        continue  # as the check saw it: the check has said it
                    for finding in live_findings(literal):
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
        """The blok literals of ``uri``, in client positions: where a client may treat text as BSX."""
        document = self.workspace.get_text_document(uri)
        found: list[dict[str, object]] = []
        for literal in blok_literals(document.source):
            start = document.client_position_at_offset(literal.start)
            end = document.client_position_at_offset(literal.end)
            found.append(
                {
                    "start": {"line": start.line, "character": start.character},
                    "end": {"line": end.line, "character": end.character},
                    "value": literal.value,
                    "root": literal.root,
                    "context": literal.context,
                    "kind": literal.kind,
                    "exact": literal.exact,
                }
            )
        return found


server = ArkitektLanguageServer()


@server.feature(types.INITIALIZE)
def initialize(ls: ArkitektLanguageServer, params: types.InitializeParams) -> None:
    ls.settings.update(params.initialization_options)


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(ls: ArkitektLanguageServer, params: types.DidChangeConfigurationParams) -> None:
    settings = params.settings
    ls.settings.update(settings.get("arkitekt") if isinstance(settings, dict) else None)


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
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server.start_io()


__all__ = ["ArkitektLanguageServer", "CheckKey", "Settings", "main", "project_root", "server"]
