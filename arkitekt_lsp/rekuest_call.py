"""The clients behind the editor's action panel: calling rekuest, and reading mikro for its pickers.

Loaded only when ``arkitekt.server.actions`` asks for them (:mod:`.connected`). They log in as an app
of their own (:data:`ACTIONS_IDENTIFIER`), so the read-only login never holds these scopes and turning
the setting on is a login of its own. Neither can register an agent. A task's events are read by
polling over http: no socket has to stay open for an editor that may sit idle for hours.
"""

from collections.abc import Mapping
from importlib import resources
from typing import Annotated, Any, ClassVar, Self

from arkitekt import App, client
from fakts import Alias, Require, TokenLoader
from koil.composition import Composition
from rath.rath import Rath

from arkitekt_lsp.rekuest_read import IDENTIFIER, RekuestRead, http_rath

ACTIONS_IDENTIFIER = f"{IDENTIFIER}.actions"

_TASK = """
    id
    latestEventKind
"""

_ASSIGN = """
mutation LspAssign($input: AssignInput!) {
  assign(input: $input) {TASK}
}
""".replace("TASK", _TASK)

_CANCEL = """
mutation LspCancel($input: CancelInput!) {
  cancel(input: $input) {TASK}
}
""".replace("TASK", _TASK)

_EVENTS = """
query LspTaskEvents($id: ID!) {
  task(id: $id) {
    id
    events(ordering: [{createdAt: ASC}]) {
      id
      kind
      returns
      message
      progress
      createdAt
    }
  }
}
"""


class QueriedClient(Composition):
    """A service, asked what a blok or a widget carries as text. :mod:`.connected` keeps it to reads."""

    rath: Rath

    async def aquery(
        self, document: str, variables: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = await self.rath.aquery(document, dict(variables))
        return dict(result.data)


@client()
class RekuestCall(QueriedClient):
    """Rekuest, for the panel: read an action, start it as a root task and follow it, nothing else."""

    __scopes__: ClassVar[tuple[str, ...]] = ("rekuest_read", "rekuest_root_call")

    @classmethod
    def from_fakts(
        cls,
        rekuest: Annotated[
            Alias,
            Require("live.arkitekt.rekuest", "Where your apps' actions are called"),
        ],
        tokens: TokenLoader,
    ) -> Self:
        return cls(rath=http_rath(rekuest, tokens))

    async def aaction(self, action_hash: str) -> dict[str, Any] | None:
        document = (
            resources.files("arkitekt_lsp")
            .joinpath("action_query.graphql")
            .read_text(encoding="utf-8")
        )
        return (await self.aquery(document, {"hash": action_hash})).get("action")

    async def aassign(self, input_: Mapping[str, Any]) -> dict[str, Any]:
        return (await self.rath.aquery(_ASSIGN, {"input": dict(input_)})).data["assign"]

    async def acancel(self, task: str) -> dict[str, Any]:
        return (await self.rath.aquery(_CANCEL, {"input": {"task": task}})).data[
            "cancel"
        ]

    async def aevents(self, task: str) -> list[dict[str, Any]]:
        answer = (await self.rath.aquery(_EVENTS, {"id": task})).data["task"]
        return list(answer["events"])


@client()
class MikroRead(QueriedClient):
    """Mikro, read-only: what a picker lists."""

    __scopes__: ClassVar[tuple[str, ...]] = ("mikro_read",)

    @classmethod
    def from_fakts(
        cls,
        mikro: Annotated[
            Alias,
            Require(
                "live.arkitekt.mikro", "Where the images an action's pickers list are"
            ),
        ],
        tokens: TokenLoader,
    ) -> Self:
        return cls(rath=http_rath(mikro, tokens))


SERVICES: dict[str, type[QueriedClient]] = {"rekuest": RekuestCall, "mikro": MikroRead}
"""What a query may name, by the service key a widget's ``ward`` or a blok's read says."""


def actions_app(version: str = "0.0.0") -> App[Any]:
    """The app the language server logs in as when it may call: still no agent, nothing it offers."""
    return App(
        ACTIONS_IDENTIFIER, version, clients=[RekuestRead, RekuestCall, MikroRead]
    )


__all__ = [
    "ACTIONS_IDENTIFIER",
    "SERVICES",
    "MikroRead",
    "QueriedClient",
    "RekuestCall",
    "actions_app",
]
