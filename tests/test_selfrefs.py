from arkitekt_lsp.selfrefs import find, self_path_at, target_of

SOURCE = '''UM = "µm"


@app.state(name="scope")
@dataclass
class ScopeState:
    """The stage."""

    #: Where it stands, in µm.
    position: float = 0.0
    chart: Chart | None = None


@app.action
async def move(to: float) -> None:
    """Moves the stage."""
'''

REPORT = {
    "states": [{"key": "scope", "name": "ScopeState", "file": "/app.py"}],
    "actions": [{"interface": "move", "file": "/app.py", "line": 14}],
}


def test_a_self_path_resolves_to_the_name_that_declares_it() -> None:
    blok = "<Plot><Mount value={self.scope.position} /><Button on={self.move(to: 1)} /></Plot>"

    def at(word: str) -> tuple[str, str]:
        path = self_path_at(blok, blok.index(word) + 1)
        assert path is not None
        target = target_of(REPORT, path)
        assert target is not None
        found = find(SOURCE, target)
        assert found is not None
        return SOURCE[found.start : found.end], found.hover

    assert at("scope")[0] == "ScopeState"
    # Offsets are code points: the µ above must not shift them.
    name, hover = at("position")
    assert name == "position" and "float" in hover and "Where it stands" in hover
    assert at("move")[0] == "move"
    assert self_path_at(blok, blok.index("self") + 1) is None
