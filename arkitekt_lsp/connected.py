"""The flagged part of the server: who would satisfy a dependency, asked of rekuest, read-only.

Everything else the server does is local. Only with ``--connect`` (or ``arkitekt.server.connect``)
does it log in, at ``https://go.arkitekt.live`` unless told otherwise (``--server``), as an app of
its own: not the app being checked, and with a client that asks for ``rekuest_read`` and nothing
else. It registers no agent. A dependency needs nothing registered either: what the check says the
app declares (the report's ``protocols``, a blok's ``scope``) is a set of demands, and
``agents(filters: {actionDemands, stateDemands})`` answers them. An agent has to meet every demand,
each possibly with a different implementation, which is what a dependency asks for.

Whatever goes wrong here (a login nobody approves, a server that is gone) leaves the server local.

With ``arkitekt.server.actions`` as well, it logs in as a second app that may also start tasks and
read mikro (:mod:`.rekuest_call`), for the editor's action panel. Queries that panel forwards arrive
as data, so :func:`read_only_problem` keeps them to reads here, whatever the page says it sent.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arkitekt_spec.actions import ActionDemandInput, StateDemandInput

if TYPE_CHECKING:
    from arkitekt import DeviceCodeChallenge

    from arkitekt_lsp.rekuest_read import RekuestRead

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://go.arkitekt.live"
"""The coordination server asked when none is named."""
LOGIN_TIMEOUT = 300.0
QUERY_TIMEOUT = 10.0
FRESH_FOR = 30.0
"""How long an answer about a dependency holds; a manual refresh drops it."""
AGENT_LIMIT = 100
"""How many agents are listed per dependency: a count of this many reads as "at least"."""

POLL_FROM = 0.3
POLL_TO = 3.0
"""How often a task's events are read: at first, and once it has run for a while."""
FINAL_EVENTS = frozenset({"COMPLETED", "FAILED", "CRITICAL", "CANCELLED", "DENIED", "INTERRUPTED", "LOST"})
"""The event kinds after which a task reports nothing more."""


def read_only_problem(document: str) -> str | None:
    """Why ``document`` may not run as a forwarded read, if so: exactly one query, nothing else."""
    from graphql import GraphQLError, OperationDefinitionNode, parse

    try:
        definitions = parse(document).definitions
    except GraphQLError:
        return "That is not a GraphQL document."
    operations = [node for node in definitions if isinstance(node, OperationDefinitionNode)]
    if any(operation.operation.value != "query" for operation in operations):
        return "Only queries can run here: a change goes through an action, never a mutation."
    if len(operations) != 1:
        return "The document must hold exactly one query."
    return None


# -- what to ask


def _demand(demand: Any, model: Any) -> dict[str, Any]:  # noqa: ANN401
    """A report's demand (snake_case) as the server's input spells it."""
    return model.model_validate(demand).model_dump(mode="json", by_alias=True, exclude_none=True)


def _identity(dependency: Mapping[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {"distinct": True}
    if dependency.get("app"):
        filters["appIdentifier"] = dependency["app"]
    # As an assign resolves it: no version, an empty one or `*` is any version, else exactly that.
    if dependency.get("version") not in (None, "", "*"):
        filters["versionNumber"] = dependency["version"]
    return filters


def _slots(dependency: Mapping[str, Any]) -> list[tuple[str, str, bool, dict[str, Any]]]:
    """Each slot with a demand: its key, what it is (``actionDemands``/``stateDemands``), whether it
    is optional, and its demand. A slot without a demand asks nothing (and the server refuses one)."""
    slots: list[tuple[str, str, bool, dict[str, Any]]] = []
    for slot in dependency.get("action_dependencies") or ():
        if slot.get("demand"):
            demand = _demand(slot["demand"], ActionDemandInput)
            slots.append((slot["key"], "actionDemands", bool(slot.get("optional")), demand))
    for slot in dependency.get("state_dependencies") or ():
        if slot.get("demand"):
            demand = _demand(slot["demand"], StateDemandInput)
            slots.append((slot["key"], "stateDemands", bool(slot.get("optional")), demand))
    return slots


def filters_for(dependency: Mapping[str, Any]) -> dict[str, Any]:
    """The ``AgentFilter`` of the agents that satisfy ``dependency``: every slot it requires."""
    filters = _identity(dependency)
    for _, field, optional, demand in _slots(dependency):
        if not optional:
            filters.setdefault(field, []).append(demand)
    return filters


def slot_filters(dependency: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per slot, the ``AgentFilter`` of the agents that meet that slot alone: what says which is unmet."""
    return {key: {**_identity(dependency), field: [demand]} for key, field, _, demand in _slots(dependency)}


# -- what it answered


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    connected: bool
    app: str | None = None
    version: str | None = None

    @classmethod
    def of(cls, agent: Mapping[str, Any]) -> "AgentInfo":
        return cls(
            id=str(agent["id"]),
            name=str(agent["name"]),
            connected=bool(agent["connected"]),
            app=(agent.get("app") or {}).get("identifier"),
            version=(agent.get("release") or {}).get("version"),
        )


@dataclass(frozen=True)
class Availability:
    """Who would satisfy a dependency, as the server said it just now."""

    agents: tuple[AgentInfo, ...]
    slots: Mapping[str, int]
    """Per slot with a demand, how many agents meet it alone."""
    optional_slots: frozenset[str] = frozenset()

    @property
    def connected(self) -> int:
        return sum(1 for agent in self.agents if agent.connected)


def _count(count: int) -> str:
    return f"{count}+" if count >= AGENT_LIMIT else str(count)


def lens_title(availability: Availability) -> str:
    """One line above the protocol: ``3 agents · 2 connected``, and the slots nobody meets."""
    total = len(availability.agents)
    if total == 0:
        title = "no agents"
    else:
        title = f"{_count(total)} agent{'' if total == 1 else 's'} · {_count(availability.connected)} connected"
    unmet = [key for key, count in availability.slots.items() if count == 0 and key not in availability.optional_slots]
    if unmet:
        title += " · nobody offers " + ", ".join(unmet)
    return title


def action_lens_title(action: Mapping[str, Any] | None) -> str:
    """One line above an action: run it, and who would run it; or what is missing for that."""
    if action is None:
        return "$(play) Not on the server yet: arkitekt run dev"
    agents = [i["agent"] for i in action.get("implementations") or () if i.get("agent")]
    connected = sum(1 for agent in agents if agent.get("connected"))
    if not connected:
        return "$(play) Run · no agent connected: arkitekt run dev"
    return f"$(play) Run · {connected} agent{'' if connected == 1 else 's'}"


def problem(dependency: Mapping[str, Any], availability: Availability) -> tuple[str, str] | None:
    """The level and message when too few agents satisfy ``dependency``; never an error, because
    what is missing may simply not be running yet."""
    needed = dependency.get("min_viable_instances") or 1
    total = len(availability.agents)
    if total >= needed:
        return None
    unmet = [key for key, count in availability.slots.items() if count == 0 and key not in availability.optional_slots]
    where = f" Nobody offers: {', '.join(unmet)}." if unmet else ""
    level = "info" if dependency.get("optional") else "warning"
    if total == 0:
        return level, f"No agent satisfies this dependency.{where}"
    return (
        level,
        f"{total} agent{'' if total == 1 else 's'} satisfy this dependency, it asks for at least {needed}.{where}",
    )


def hover_markdown(name: str, availability: Availability) -> str:
    """What hovering a dependency tells: the agents, then each slot."""
    lines = [f"**{name}**: {lens_title(availability)}", ""]
    for agent in availability.agents[:10]:
        at = f" ({agent.app}{f'@{agent.version}' if agent.version else ''})" if agent.app else ""
        lines.append(f"- {'●' if agent.connected else '○'} `{agent.name}`{at}")
    if len(availability.agents) > 10:
        lines.append(f"- … {_count(len(availability.agents))} in all")
    if availability.slots:
        lines.extend(("", "| slot | agents |", "|---|---|"))
        lines.extend(
            f"| `{key}`{' (optional)' if key in availability.optional_slots else ''} | {_count(count)} |"
            for key, count in availability.slots.items()
        )
    return "\n".join(lines)


def class_span(source: str, name: str) -> tuple[int, int] | None:
    """Where ``class <name>`` names it in ``source`` as it reads now (the report's line may have moved)."""
    match = re.search(rf"^[ \t]*class[ \t]+({re.escape(name.rsplit('.', 1)[-1])})\b", source, re.MULTILINE)
    return match.span(1) if match else None


def dependency_key_at(text: str, index: int) -> str | None:
    """The ``<key>`` of the ``deps.<key>`` at ``index`` of a blok's BSX, if the cursor is on one."""
    for match in re.finditer(r"\bdeps\.([A-Za-z_]\w*)", text):
        if match.start() <= index <= match.end():
            return match.group(1)
    return None


def offer_of(agent: Mapping[str, Any]) -> dict[str, Any]:
    """An agent of the server's answer, flat: its actions, and its states with their ports."""
    actions = {str(i["action"]["key"]): dict(i["action"]) for i in agent.get("implementations") or ()}
    states = {
        str(state.get("key") or state["interface"]): {
            "key": str(state.get("key") or state["interface"]),
            "ports": list((state.get("definition") or {}).get("ports") or ()),
        }
        for state in agent.get("states") or ()
    }
    return {
        "id": str(agent["id"]),
        "name": str(agent["name"]),
        "connected": bool(agent["connected"]),
        "app": (agent.get("app") or {}).get("identifier"),
        "version": (agent.get("release") or {}).get("version"),
        "actions": list(actions.values()),
        "states": list(states.values()),
    }


# -- the connection


Status = Callable[..., None]
Login = Callable[["DeviceCodeChallenge"], Awaitable[None]]


class ActionsUnavailable(Exception):
    """What the panel should say instead of an answer."""


def assign_input(
    action_hash: str,
    args: Mapping[str, Any],
    reference: str,
    implementation: str | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What an assign takes. Without ``implementation`` the server picks the agent, as ``arkitekt call
    remote`` does. Dependencies belong to an implementation, so pins only travel with one."""
    input_: dict[str, Any] = {"actionHash": action_hash, "args": dict(args), "capture": False, "reference": reference}
    if implementation:
        input_["implementation"] = implementation
        if dependencies:
            input_["dependencies"] = [dict(pin) for pin in dependencies]
    return input_


def app_for(version: str, actions: bool) -> Any:  # noqa: ANN401 - App loads only when connecting
    """The app to log in as: the read-only one, or the one that may also call."""
    if actions:
        from arkitekt_lsp.rekuest_call import actions_app

        return actions_app(version)
    from arkitekt_lsp.rekuest_read import lsp_app

    return lsp_app(version)


class Connection:
    """One login at ``url``, and what it has answered lately.

    Args:
        url: The coordination server.
        status: Told every change: ``status(state, message=...)``, a state of ``connecting``,
            ``login``, ``connected`` or ``offline``.
        login: Shows the user a device code challenge.
    """

    def __init__(self, url: str, status: Status, login: Login, version: str = "0.0.0", actions: bool = False) -> None:
        self.url = url
        self.actions = actions
        """Log in as the app that may also call actions and read mikro."""
        self.status = status
        self.login = login
        self.version = version
        self.runtime: Any = None
        self.client: RekuestRead | None = None
        self.pending: asyncio.Future[Any] | None = None
        """The login under way: what giving up on it cancels."""
        self.answers: dict[str, tuple[float, Availability]] = {}

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def _show_login(self, challenge: "DeviceCodeChallenge") -> None:
        self.status("login", message=f"Approve this editor, code {challenge.user_code}")
        await self.login(challenge)

    async def start(self) -> None:
        """Log in and connect; on any failure, stay local and say why."""
        self.status("connecting")
        try:
            # Only here: a server that is not asked to connect never loads any of this.
            from arkitekt import connect

            from arkitekt_lsp.rekuest_read import RekuestRead

            runtime = connect(
                app_for(self.version, self.actions),
                url=self.url,
                headless=True,
                device_code_hook=self._show_login,
                mesh=False,
            )
            self.pending = asyncio.ensure_future(asyncio.wait_for(runtime.__aenter__(), LOGIN_TIMEOUT))
            await self.pending
        except asyncio.CancelledError:
            if self.pending is None:  # stop() gave up on it
                return
            raise
        except Exception as error:  # noqa: BLE001 - whatever it was, the server stays local
            logger.warning("Could not connect to %s: %s", self.url, error)
            self.pending = None
            self.status("offline", message=f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
            return
        self.pending = None
        self.runtime = runtime
        self.client = runtime.require(RekuestRead)
        self.status("connected")

    async def stop(self) -> None:
        runtime, self.runtime, self.client = self.runtime, None, None
        pending, self.pending = self.pending, None
        self.answers.clear()
        if pending is not None:
            pending.cancel()
        if runtime is not None:
            try:
                await runtime.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - leaving is best effort
                logger.exception("Leaving the connection failed")

    def forget(self) -> None:
        """Drop what it knows, so the next question reaches the server."""
        self.answers.clear()

    def _client(self, service: str) -> Any:  # noqa: ANN401 - the clients load only when connecting
        """The client for ``service``; raises what the panel should say when there is none."""
        if self.runtime is None or not self.actions:
            raise ActionsUnavailable("Actions are off: set arkitekt.server.actions and connect to the server.")
        from arkitekt_lsp.rekuest_call import SERVICES

        kind = SERVICES.get(service)
        if kind is None:
            raise ActionsUnavailable(f"The editor cannot ask the {service} service.")
        return self.runtime.require(kind)

    async def query(self, service: str, document: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """A read a widget or a blok carries as text, on ``service``."""
        problem_ = read_only_problem(document)
        if problem_ is not None:
            raise ActionsUnavailable(problem_)
        return await asyncio.wait_for(self._client(service).aquery(document, variables), QUERY_TIMEOUT)

    async def action(self, action_hash: str) -> dict[str, Any] | None:
        """The action the server knows by ``action_hash``, with its ports; None when it knows none."""
        return await asyncio.wait_for(self._client("rekuest").aaction(action_hash), QUERY_TIMEOUT)

    async def call(
        self,
        action_hash: str,
        args: Mapping[str, Any],
        reference: str,
        implementation: str | None = None,
        dependencies: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Start the action as a root task; see :func:`assign_input` for who runs it."""
        input_ = assign_input(action_hash, args, reference, implementation, dependencies)
        return await asyncio.wait_for(self._client("rekuest").aassign(input_), QUERY_TIMEOUT)

    async def cancel(self, task: str) -> dict[str, Any]:
        return await asyncio.wait_for(self._client("rekuest").acancel(task), QUERY_TIMEOUT)

    async def follow(self, task: str, told: Callable[[list[dict[str, Any]], bool], None]) -> None:
        """Tell ``told(new events, done)`` until the task ends or this is cancelled."""
        seen: set[str] = set()
        wait = POLL_FROM
        while True:
            events = await asyncio.wait_for(self._client("rekuest").aevents(task), QUERY_TIMEOUT)
            new = [event for event in events if event["id"] not in seen]
            seen.update(event["id"] for event in new)
            done = any(event["kind"] in FINAL_EVENTS for event in events)
            if new or done:
                told(new, done)
            if done:
                return
            wait = POLL_FROM if new else min(wait * 1.5, POLL_TO)
            await asyncio.sleep(wait)

    async def offers(self) -> dict[str, dict[str, Any]] | None:
        """Per agent id, what it offers, as a picker and a dependency read it; None without an answer."""
        client = self.client
        if client is None:
            return None
        try:
            agents = await asyncio.wait_for(client.aoffers(AGENT_LIMIT), QUERY_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - no answer is an answer: nothing to offer
            logger.warning("Asking %s what agents offer failed: %s", self.url, error)
            self.status("offline", message=f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
            return None
        return {str(agent["id"]): offer_of(agent) for agent in agents}

    async def availability(self, dependency: Mapping[str, Any]) -> Availability | None:
        """Who would satisfy ``dependency`` now; None while there is no answer (offline, too slow)."""
        client = self.client
        if client is None:
            return None
        key = json.dumps(dependency, sort_keys=True, default=str)
        known = self.answers.get(key)
        if known is not None and time.monotonic() - known[0] < FRESH_FOR:
            return known[1]
        slots = slot_filters(dependency)
        try:
            agents, *counts = await asyncio.wait_for(
                asyncio.gather(
                    client.aagents(filters_for(dependency), AGENT_LIMIT),
                    *(client.aagents(filters, AGENT_LIMIT) for filters in slots.values()),
                ),
                QUERY_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001 - no answer is an answer: nothing to say
            logger.warning("Asking %s about a dependency failed: %s", self.url, error)
            self.status("offline", message=f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
            return None
        self.status("connected")
        answer = Availability(
            agents=tuple(AgentInfo.of(agent) for agent in agents),
            slots={slot: len(found) for slot, found in zip(slots, counts)},
            optional_slots=frozenset(slot for slot, _, optional, _ in _slots(dependency) if optional),
        )
        self.answers[key] = (time.monotonic(), answer)
        return answer


def forget_login(url: str, version: str = "0.0.0", actions: bool = False) -> bool:
    """Revoke the login at ``url`` and forget it here; whether there was one. Blocks: a thread's work."""
    from arkitekt import logout

    return logout(app_for(version, actions), url=url)


def declared_protocols(report: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The report's dependency protocols; a version 1 report has none."""
    return report.get("protocols") or ()


__all__ = [
    "DEFAULT_URL",
    "ActionsUnavailable",
    "AgentInfo",
    "Availability",
    "Connection",
    "action_lens_title",
    "assign_input",
    "class_span",
    "declared_protocols",
    "dependency_key_at",
    "filters_for",
    "forget_login",
    "hover_markdown",
    "lens_title",
    "offer_of",
    "problem",
    "read_only_problem",
    "slot_filters",
]
