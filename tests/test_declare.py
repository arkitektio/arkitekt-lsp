"""A dependency written from what an agent offers is the one declared by hand: same demand, runnable."""

import runpy
from pathlib import Path
from typing import Any

from arkitekt import App

from arkitekt_lsp.connected import offer_of
from arkitekt_lsp.declare import write_dependency

SOURCE = 'from arkitekt import App\n\napp = App("mine", "1.0.0")\n\n\n@app.action\ndef run(a: int) -> int:\n    return a\n'

AGENT = {
    "id": "1",
    "name": "stage",
    "connected": True,
    "app": {"identifier": "microscope-stage"},
    "release": {"version": "0.1.0"},
    "implementations": [
        {
            "action": {
                "key": "move_to",
                "name": "Move To",
                "kind": "FUNCTION",
                "description": "Drives the stage there.\nAnd more.",
                "args": [
                    {"key": "x_um", "kind": "FLOAT", "nullable": False},
                    {"key": "tags", "kind": "LIST", "nullable": True, "children": [{"key": "tag", "kind": "STRING", "nullable": False}]},
                    {"key": "z_um", "kind": "FLOAT", "nullable": False, "default": 0.0},
                ],
                "returns": [{"key": "return0", "kind": "FLOAT", "nullable": False}],
            }
        },
        {
            "action": {
                "key": "snap",
                "name": "Snap",
                "kind": "FUNCTION",
                "args": [],
                "returns": [{"key": "return0", "kind": "STRUCTURE", "nullable": False, "identifier": "@mikro/image"}],
            }
        },
    ],
    "states": [
        {"interface": "StagePosition", "key": None, "definition": {"ports": [{"key": "x_um", "kind": "FLOAT", "nullable": False}]}}
    ],
}


def _applied(source: str, **kwargs: Any) -> str:
    written = write_dependency(source, **kwargs)
    lines = source.splitlines(keepends=True)
    lines.insert(written.line, written.source)
    last_import = max(i for i, line in enumerate(source.splitlines()) if line.startswith(("from ", "import "))) + 1
    lines.insert(last_import, "".join(f"{line}\n" for line in written.imports))
    result = "".join(lines)
    assert result.splitlines()[written.class_line].startswith("class Stage(")
    return result


def test_what_is_written_declares_the_demand_it_was_read_from(tmp_path: Path) -> None:
    offer = offer_of(AGENT)
    file = tmp_path / "written.py"
    file.write_text(_applied(SOURCE, app=offer["app"], class_name="Stage", actions=offer["actions"], states=offer["states"]))

    namespace = runpy.run_path(str(file), run_name="written")

    app: App[Any] = namespace["app"]
    protocol = app.registry.structure_registry.protocol_for(namespace["Stage"])
    dependency = protocol.to_dependency_input("stage")
    assert dependency.app == "microscope-stage"
    (move,) = dependency.action_dependencies
    assert (move.demand.app, move.demand.key) == ("microscope-stage", "move_to")
    (state,) = dependency.state_dependencies
    assert (state.key, state.demand.key) == ("StagePosition", "StagePosition")


def test_a_port_only_the_app_can_name_is_a_comment_saying_so_never_a_guess() -> None:
    offer = offer_of(AGENT)

    written = write_dependency(SOURCE, app=offer["app"], class_name="Stage", actions=offer["actions"])

    assert "def move_to(self, x_um: float, tags: Optional[list[str]] = None, z_um: float = 0.0) -> float:" in written.source
    assert "# snap() → @mikro/image: needs @mikro/image" in written.source
    assert "def snap" not in written.source
