"""Thin MCP 2026-07-28 client.

Built to the modern (sessionless) shape of the protocol:

* **Stateless.** No ``initialize`` handshake. Every request carries the protocol version and
  the client's capabilities in ``_meta``; there is no session to keep alive.
* **``server/discover`` is required** and replaces capability probing — one call returns the
  server's protocol version, capabilities and advertised tools.
* **Every result carries ``resultType``**, either ``complete`` or ``input_required``.
  Multi-round-trip requests use ``input_required`` rather than server-to-client calls, so
  :class:`InputRequired` is surfaced as its own outcome and is *never* collapsed into a
  failure.
* **Roots, Sampling and Logging are deprecated (SEP-2577)** and are not adopted here.

Security posture (the non-negotiable part):

* A discovered tool is mapped into our :class:`~pikachu.core.types.ToolSpec` and then routed
  through :func:`pikachu.guard.effective_tools` against the agent's fixed allowlist. A server
  advertising ten tools to an agent that allows two yields exactly two. **A remote server
  cannot widen an agent's authority** — its tool list is a request, not a grant.
* Every discovered tool carries :class:`~pikachu.core.types.Lineage` tainted
  ``FOREIGN_SKILL`` (a remote tool descriptor is a foreign declaration, and its text is
  attacker-controllable). Taint is monotonic and never cleared.
* A malformed or hostile ``server/discover`` response raises :class:`MCPProtocolError` and is
  never partially applied.

The ``mcp`` SDK is imported **lazily**, inside the functions that need it, so an agent with
no MCP servers never pays the import cost. Tests inject a :class:`MCPTransport` fake and never
touch the SDK or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Protocol, runtime_checkable

from pikachu.core.errors import PikachuError
from pikachu.core.types import Lineage, Taint, ToolSpec
from pikachu.guard import effective_tools

__all__ = [
    "REQUESTED_PROTOCOL_VERSION",
    "DiscoveredTool",
    "InputRequired",
    "MCPClient",
    "MCPDiscovery",
    "MCPProtocolError",
    "MCPResult",
    "MCPTransport",
    "ResultType",
]


#: The revision we ALWAYS request. This is the whole point of the lane: the SDK's
#: DEFAULT_NEGOTIATED_VERSION is 2025-03-26, three revisions behind, and negotiating it
#: silently loses statelessness, required server/discover and resultType. We never rely on
#: the default — we ask for this string explicitly and then assert what we actually got.
REQUESTED_PROTOCOL_VERSION: Final[str] = "2026-07-28"

#: The taint every foreign tool descriptor carries. A tool advertised by a remote server is a
#: foreign declaration whose name and description are attacker-controllable text — the same
#: category of risk as a foreign skill, so it reuses that taint rather than inventing one.
_FOREIGN_TAINT: Final[Taint] = Taint.FOREIGN_SKILL


class MCPProtocolError(PikachuError):
    """A server response violated the MCP 2026-07-28 shape.

    Raised — never tolerated — for a missing/blank protocol version, a downgraded negotiated
    revision, a malformed tool descriptor, or an unknown ``resultType``. A hostile discover
    response is rejected whole; nothing is partially applied.
    """

    def __init__(self, message: str, *, server: str | None = None) -> None:
        super().__init__(message)
        self.server = server


class ResultType(str, Enum):
    """The ``resultType`` every 2026-07-28 result carries.

    ``INPUT_REQUIRED`` is the multi-round-trip signal and is a first-class outcome, never an
    error: the server is asking for more input, not reporting a failure.
    """

    COMPLETE = "complete"
    INPUT_REQUIRED = "input_required"


@dataclass(frozen=True)
class DiscoveredTool:
    """One tool advertised by a remote server, after mapping into our own types.

    ``spec`` is the guard-facing :class:`ToolSpec`. ``lineage`` is always tainted — a remote
    descriptor is foreign input. ``raw`` keeps the original descriptor for debugging, but no
    authority is ever derived from it.
    """

    spec: ToolSpec
    lineage: Lineage
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPDiscovery:
    """The parsed result of a ``server/discover`` call, before any allowlist narrowing.

    ``tools`` is everything the server advertised (each already tainted). Narrowing to the
    agent's fixed allowlist happens in :meth:`MCPClient.list_tools`, not here — discovery
    reports what the server *claims*, the guard decides what the agent *gets*.
    """

    protocol_version: str
    capabilities: dict[str, Any]
    tools: tuple[DiscoveredTool, ...]


@dataclass(frozen=True)
class InputRequired:
    """A ``resultType: input_required`` result — the server needs another round trip.

    A distinct type so a caller cannot accidentally treat it as either success or failure.
    """

    payload: dict[str, Any]


@dataclass(frozen=True)
class MCPResult:
    """A ``resultType: complete`` result — the request finished."""

    payload: dict[str, Any]


@runtime_checkable
class MCPTransport(Protocol):
    """The seam a test fakes and the SDK adapter fulfils.

    One async method: send a request object and return the raw response dict. The client owns
    the protocol shape (version negotiation, discover parsing, resultType routing); the
    transport owns only the wire. Keeping the split here is what lets every test run against a
    scripted fake with no SDK and no network.
    """

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one MCP request and return the raw response dict."""
        ...


class MCPClient:
    """A thin wrapper over an :class:`MCPTransport` that speaks MCP 2026-07-28.

    Construct with a transport (a fake in tests, the SDK adapter in production) and the agent
    whose authority bounds anything this server can expose. Call :meth:`discover` once, then
    :meth:`list_tools` to get the guard-narrowed, tainted toolset.
    """

    def __init__(
        self,
        transport: MCPTransport,
        *,
        fixed_allowlist: tuple[str, ...],
        server_name: str = "",
    ) -> None:
        self._transport = transport
        self._fixed_allowlist = fixed_allowlist
        self._server_name = server_name
        self._negotiated_version: str | None = None
        self._discovery: MCPDiscovery | None = None

    # -- readable protocol state -------------------------------------------------------

    @property
    def requested_version(self) -> str:
        """The revision this client asks for — always ``2026-07-28``."""
        return REQUESTED_PROTOCOL_VERSION

    @property
    def negotiated_version(self) -> str | None:
        """The revision the server actually agreed to, or ``None`` before :meth:`discover`.

        Exposed as a plain readable property precisely so a conformance test AND a human
        operator can both confirm the client did not silently downgrade to the SDK default.
        """
        return self._negotiated_version

    # -- discovery ---------------------------------------------------------------------

    async def discover(self) -> MCPDiscovery:
        """Call ``server/discover`` and parse it into an :class:`MCPDiscovery`.

        Requests ``2026-07-28`` explicitly in ``_meta``. Rejects — with
        :class:`MCPProtocolError` — a response that omits the protocol version, that
        negotiated a revision other than the one we required, or whose tool list is
        malformed. Nothing is applied partially: a bad response leaves this client's state
        untouched.
        """
        params = self._meta_params()
        raw = await self._transport.request("server/discover", params)
        discovery = self._parse_discovery(raw)
        # Commit only after the whole response parses cleanly.
        self._negotiated_version = discovery.protocol_version
        self._discovery = discovery
        return discovery

    def _meta_params(self) -> dict[str, Any]:
        """The stateless ``_meta`` block carried on every request.

        There is no ``initialize`` handshake in 2026-07-28, so the version and capabilities
        travel on every request instead of being negotiated once.
        """
        return {
            "_meta": {
                "protocolVersion": REQUESTED_PROTOCOL_VERSION,
                "capabilities": {},
            }
        }

    def _parse_discovery(self, raw: dict[str, Any]) -> MCPDiscovery:
        if not isinstance(raw, dict):
            raise MCPProtocolError(
                f"server/discover returned {type(raw).__name__}, expected object",
                server=self._server_name,
            )

        version = raw.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise MCPProtocolError(
                "server/discover response missing a protocolVersion",
                server=self._server_name,
            )
        if version != REQUESTED_PROTOCOL_VERSION:
            # A silent downgrade is exactly the failure mode this lane exists to catch. We
            # asked for 2026-07-28; anything else is a downgrade and is refused loudly rather
            # than accepted quietly.
            raise MCPProtocolError(
                f"server negotiated {version!r}, refusing downgrade from "
                f"{REQUESTED_PROTOCOL_VERSION!r}",
                server=self._server_name,
            )

        capabilities = raw.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise MCPProtocolError(
                "server/discover capabilities must be an object",
                server=self._server_name,
            )

        raw_tools = raw.get("tools", [])
        if not isinstance(raw_tools, list):
            raise MCPProtocolError(
                "server/discover tools must be an array",
                server=self._server_name,
            )

        tools = tuple(self._map_tool(entry) for entry in raw_tools)
        return MCPDiscovery(
            protocol_version=version,
            capabilities=capabilities,
            tools=tools,
        )

    def _map_tool(self, entry: object) -> DiscoveredTool:
        """Map one raw server tool descriptor into a tainted :class:`DiscoveredTool`.

        A descriptor that is not an object, or that has no usable name, is a malformed
        response and raises rather than being skipped — a partially-applied discover is worse
        than a rejected one.
        """
        if not isinstance(entry, dict):
            raise MCPProtocolError(
                f"tool descriptor is {type(entry).__name__}, expected object",
                server=self._server_name,
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MCPProtocolError(
                "tool descriptor missing a name",
                server=self._server_name,
            )
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise MCPProtocolError(
                f"tool {name!r} description must be a string",
                server=self._server_name,
            )

        # A foreign descriptor is tainted the moment it enters. The source records which
        # server it came from, so lineage stays auditable across the boundary.
        source = f"mcp:{self._server_name}" if self._server_name else "mcp"
        lineage = Lineage.clean().with_taint(_FOREIGN_TAINT, source)

        # ToolSpec's own validator normalizes and rejects an empty name. A remote tool is
        # never assigned a credit cost or approval flag from its own descriptor — cost and
        # approval are host decisions, not something a stranger declares for itself.
        spec = ToolSpec(name=name, description=description)
        return DiscoveredTool(spec=spec, lineage=lineage, raw=entry)

    # -- guard-narrowed tool list ------------------------------------------------------

    def list_tools(self) -> tuple[DiscoveredTool, ...]:
        """The discovered tools **narrowed to the agent's fixed allowlist** (invariant P3).

        Routes the server's advertised tool names through
        :func:`pikachu.guard.effective_tools`. A tool the agent's allowlist does not contain
        is dropped, even though the server advertised it. This is the single line that stops a
        remote server widening authority: its list is treated as a *declaration*, narrowed the
        same way a skill's declared tools are.

        Order and multiplicity follow the guard's contract. Requires :meth:`discover` first.
        """
        if self._discovery is None:
            raise MCPProtocolError(
                "list_tools called before discover",
                server=self._server_name,
            )

        by_name: dict[str, DiscoveredTool] = {t.spec.name: t for t in self._discovery.tools}
        declared = tuple(by_name)
        narrowed = effective_tools(self._fixed_allowlist, declared)
        # effective_tools returns names the agent may actually use; re-attach the tainted
        # DiscoveredTool for each. Every survivor still carries its taint — narrowing removes
        # tools, it never launders them.
        return tuple(by_name[name] for name in narrowed.tools if name in by_name)

    # -- result routing ----------------------------------------------------------------

    def route_result(self, raw: dict[str, Any]) -> MCPResult | InputRequired:
        """Split a result on its ``resultType``.

        ``complete`` -> :class:`MCPResult`. ``input_required`` -> :class:`InputRequired`, a
        distinct type that a caller cannot mistake for success or failure. Any other value is
        a protocol violation and raises — an unknown ``resultType`` must not be silently
        treated as complete.
        """
        if not isinstance(raw, dict):
            raise MCPProtocolError(
                f"result is {type(raw).__name__}, expected object",
                server=self._server_name,
            )
        rt = raw.get("resultType")
        if rt == ResultType.COMPLETE.value:
            return MCPResult(payload=raw)
        if rt == ResultType.INPUT_REQUIRED.value:
            return InputRequired(payload=raw)
        raise MCPProtocolError(
            f"unknown or missing resultType {rt!r}",
            server=self._server_name,
        )
