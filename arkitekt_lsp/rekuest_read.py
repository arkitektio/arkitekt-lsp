"""The app the language server logs in as, and the one query it may ask.

Loaded only when connecting was asked for (:mod:`.connected`).
"""

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Self

from arkitekt import App, client
from fakts import Alias, Require, TokenLoader
from fakts.contrib.rath.auth import FaktsAuthLink
from koil.composition import Composition
from rath.links.aiohttp import AIOHttpLink
from rath.links.compose import compose
from rath.rath import Rath

IDENTIFIER = "live.arkitekt.lsp"

_AGENTS = """
query LspAgents($filters: AgentFilter, $pagination: OffsetPaginationInput) {
  agents(filters: $filters, pagination: $pagination) {
    id
    name
    connected
    app { identifier }
    release { version }
  }
}
"""


_PORT = """
    key
    kind
    nullable
    identifier
"""

_OFFERS = """
query LspOffers($pagination: OffsetPaginationInput) {
  agents(pagination: $pagination) {
    id
    name
    connected
    app { identifier }
    release { version }
    implementations {
      action {
        key
        name
        kind
        description
        args { default children { PORT children { PORT } } PORT }
        returns { children { PORT children { PORT } } PORT }
      }
    }
    states {
      interface
      key
      definition { ports { children { PORT children { PORT } } PORT } }
    }
  }
}
""".replace("PORT", _PORT)


def http_rath(alias: Alias, tokens: TokenLoader) -> Rath:
    """Queries over http, with the run's token."""
    return Rath(
        link=compose(
            FaktsAuthLink(token_loader=tokens),
            AIOHttpLink(
                endpoint_url=alias.to_http_path("graphql"),
                proxy=alias.proxy,
                ssl_context=alias.ssl_context(),
            ),
        )
    )


@client()
class RekuestRead(Composition):
    """All the language server asks of rekuest.

    Rekuest's own client also registers agents and writes, and says so: an app using it asks for
    those scopes at login. This one's queries need ``rekuest_read``, and that is all it says.
    """

    __scopes__: ClassVar[tuple[str, ...]] = ("rekuest_read",)

    rath: Rath

    @classmethod
    def from_fakts(
        cls,
        rekuest: Annotated[
            Alias,
            Require(
                "live.arkitekt.rekuest",
                "Where the agents that would satisfy a dependency are",
            ),
        ],
        tokens: TokenLoader,
    ) -> Self:
        """Rekuest, read-only, over http."""
        return cls(rath=http_rath(rekuest, tokens))

    async def aagents(
        self, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        result = await self.rath.aquery(
            _AGENTS, {"filters": dict(filters), "pagination": {"limit": limit}}
        )
        return list(result.data["agents"])

    async def aoffers(self, limit: int) -> list[dict[str, Any]]:
        """The agents, each with the actions and states it offers."""
        result = await self.rath.aquery(_OFFERS, {"pagination": {"limit": limit}})
        return list(result.data["agents"])


def lsp_app(version: str = "0.0.0") -> App[Any]:
    """The app the language server logs in as: rekuest's read scope, and nothing it could offer."""
    return App(IDENTIFIER, version, clients=[RekuestRead])


__all__ = ["IDENTIFIER", "RekuestRead", "lsp_app"]
