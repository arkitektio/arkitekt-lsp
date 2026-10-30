"""What the server asks rekuest and what it makes of the answer; nothing here connects."""

import asyncio
import subprocess
import sys

from arkitekt_lsp.connected import (
    DEFAULT_URL,
    AgentInfo,
    Availability,
    Connection,
    class_span,
    dependency_key_at,
    filters_for,
    hover_markdown,
    lens_title,
    problem,
    slot_filters,
)

DEPENDENCY = {
    "key": "Lab",
    "app": "lab",
    "action_dependencies": [
        {"key": "ping", "demand": {"key": "ping", "arg_matches": [{"key": "n", "kind": "INT"}]}},
        {"key": "extra", "optional": True, "demand": {"key": "extra"}},
        {"key": "free"},
    ],
    "state_dependencies": [{"key": "status", "demand": {"key": "status"}}],
}


def _agent(name: str, connected: bool = True) -> dict[str, object]:
    return {"id": name, "name": name, "connected": connected, "app": {"identifier": "lab"}, "release": {"version": "1"}}


def test_the_filter_asks_for_every_required_slot_as_the_server_spells_it() -> None:
    filters = filters_for(DEPENDENCY)

    assert filters["appIdentifier"] == "lab" and filters["distinct"] is True
    assert filters["actionDemands"] == [{"key": "ping", "argMatches": [{"key": "n", "kind": "INT"}]}]
    assert filters["stateDemands"] == [{"key": "status"}]


def test_a_version_filters_as_an_assign_resolves_it() -> None:
    assert filters_for({**DEPENDENCY, "version": "1.2.0"})["versionNumber"] == "1.2.0"
    assert "versionNumber" not in filters_for({**DEPENDENCY, "version": "*"})
    assert "versionNumber" not in filters_for({**DEPENDENCY, "version": ""})


def test_each_slot_with_a_demand_can_be_asked_alone() -> None:
    assert list(slot_filters(DEPENDENCY)) == ["ping", "extra", "status"]
    assert slot_filters(DEPENDENCY)["extra"]["actionDemands"] == [{"key": "extra"}]


def test_too_few_agents_is_a_warning_never_an_error() -> None:
    nobody = Availability(agents=(), slots={"ping": 0, "extra": 0}, optional_slots=frozenset({"extra"}))

    level, message = problem(DEPENDENCY, nobody) or ("", "")
    assert level == "warning" and "ping" in message and "extra" not in message
    assert problem({**DEPENDENCY, "optional": True}, nobody)[0] == "info"  # type: ignore[index]

    one = Availability(agents=(AgentInfo.of(_agent("a")),), slots={})
    assert problem(DEPENDENCY, one) is None
    assert problem({**DEPENDENCY, "min_viable_instances": 2}, one)[0] == "warning"  # type: ignore[index]


def test_the_lens_counts_agents_and_names_the_slots_nobody_offers() -> None:
    answer = Availability(
        agents=(AgentInfo.of(_agent("a")), AgentInfo.of(_agent("b", connected=False))), slots={"ping": 2}
    )

    assert lens_title(answer) == "2 agents · 1 connected"
    assert lens_title(Availability(agents=(), slots={"ping": 0})) == "no agents · nobody offers ping"
    assert "`a`" in hover_markdown("Lab", answer) and "`ping`" in hover_markdown("Lab", answer)


def test_a_protocol_is_found_where_it_is_now() -> None:
    source = "# moved\n\nclass Lab(Protocol):\n    ...\n"

    start, end = class_span(source, "Lab") or (0, 0)
    assert source[start:end] == "Lab"
    assert class_span(source, "Other") is None


def test_a_dependency_is_read_off_the_cursor() -> None:
    text = "<Html><Text>{deps.lab.state}</Text></Html>"

    assert dependency_key_at(text, text.index("lab")) == "lab"
    assert dependency_key_at(text, 2) is None


class _Client:
    def __init__(self) -> None:
        self.asked: list[dict[str, object]] = []

    async def aagents(self, filters: dict[str, object], limit: int) -> list[dict[str, object]]:
        self.asked.append(filters)
        return [_agent("a")]


def _connection(client: object | None) -> tuple[Connection, list[tuple[str, str | None]]]:
    said: list[tuple[str, str | None]] = []

    async def login(challenge: object) -> None:
        raise AssertionError("no login here")

    connection = Connection(DEFAULT_URL, lambda state, message=None: said.append((state, message)), login)
    connection.client = client  # type: ignore[assignment]
    return connection, said


def test_an_answer_holds_until_it_is_forgotten() -> None:
    client = _Client()
    connection, _ = _connection(client)

    async def ask() -> None:
        answer = await connection.availability(DEPENDENCY)
        assert answer is not None and answer.slots == {"ping": 1, "extra": 1, "status": 1}
        assert len(client.asked) == 4, "the dependency and each slot"
        await connection.availability(DEPENDENCY)
        assert len(client.asked) == 4
        connection.forget()
        await connection.availability(DEPENDENCY)
        assert len(client.asked) == 8

    asyncio.run(ask())


def test_a_server_that_fails_leaves_no_answer_and_says_so() -> None:
    class Broken:
        async def aagents(self, filters: object, limit: int) -> list[object]:
            raise ConnectionError("gone")

    connection, said = _connection(Broken())

    assert asyncio.run(connection.availability(DEPENDENCY)) is None
    assert said[-1][0] == "offline" and "gone" in (said[-1][1] or "")


def test_without_a_connection_nothing_is_asked() -> None:
    connection, said = _connection(None)

    assert asyncio.run(connection.availability(DEPENDENCY)) is None
    assert said == []


def test_the_login_asks_for_reading_rekuest_and_nothing_else() -> None:
    from arkitekt_lsp.rekuest_read import lsp_app

    app = lsp_app()

    assert app.scopes == ["openid", "rekuest_read"]
    assert app.services == ["rekuest_read"]


def test_a_local_server_loads_nothing_it_would_connect_with() -> None:
    code = (
        "import sys, arkitekt_lsp.server\n"
        "print([m for m in ('arkitekt', 'fakts', 'rath', 'koil', 'arkitekt_lsp.rekuest_read') if m in sys.modules])"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "[]"
