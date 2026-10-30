"""Choosing an app: what a client is offered, and that a pick reaches a file with several Apps."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from lsprotocol import types
from pygls.workspace import Workspace

from arkitekt_lsp.apps import find_apps
from arkitekt_lsp.server import ArkitektLanguageServer, CheckKey

TWO_APPS = 'one = App("one", "1.0")\ntwo: App = App("two", "1.0")\n'
ONE_APP = 'api = App("api", "1.0")\n'


def test_a_file_with_several_apps_is_offered_once_per_app(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "many.py").write_text(TWO_APPS)
    (tmp_path / "single.py").write_text(ONE_APP)
    (tmp_path / "helper.py").write_text("x = 1\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "installed.py").write_text(ONE_APP)

    assert [(app["target"], app["line"]) for app in find_apps([tmp_path.resolve()])] == [
        ("many.py:one", 0),
        ("many.py:two", 1),
        ("single.py", 0),
    ]


def test_a_target_naming_the_file_wins_over_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    ls = ArkitektLanguageServer()
    workspace = Workspace(tmp_path.as_uri(), types.TextDocumentSyncKind.Incremental)
    monkeypatch.setattr(ls.protocol, "_workspace", workspace, raising=False)
    monkeypatch.setattr(type(ls.protocol), "client_capabilities", SimpleNamespace(workspace=None), raising=False)
    root = str(tmp_path.resolve())
    uris = {}
    for name, text in (("many.py", TWO_APPS), ("single.py", ONE_APP), ("helper.py", "x = 1\n")):
        (tmp_path / name).write_text(text)
        uris[name] = (tmp_path / name).as_uri()
        workspace.put_text_document(types.TextDocumentItem(uri=uris[name], language_id="python", version=1, text=text))

    ls.settings.target = "many.py:two"

    assert ls.check_key(uris["many.py"]) == CheckKey(root, "many.py:two")
    assert ls.check_key(uris["helper.py"]) == CheckKey(root, "many.py:two")
    assert ls.check_key(uris["single.py"]) == CheckKey(root, "single.py")
