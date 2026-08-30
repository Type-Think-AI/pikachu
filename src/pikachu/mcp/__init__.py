"""MCP client — connect to a remote Model Context Protocol server and import its tools.

An MCP server is a **stranger**. Everything it says is a *request*, never a grant:

* Its advertised tool list is routed through :func:`pikachu.guard.effective_tools` exactly
  like any other declared toolset, so a server can only ever expose tools the agent's fixed
  allowlist already permits. A remote server can never widen an agent's authority.
* Every tool it advertises carries :class:`~pikachu.core.types.Taint`, because a tool
  descriptor is foreign input — its name and description are attacker-controllable text.

Protocol note (the reason this lane needs care): the ``mcp`` SDK's
``DEFAULT_NEGOTIATED_VERSION`` is ``2025-03-26``, three revisions behind the
``LATEST_PROTOCOL_VERSION`` of ``2026-07-28`` it also supports. A client that does not
*explicitly* request ``2026-07-28`` silently negotiates the old revision and loses
statelessness, required ``server/discover`` and per-result ``resultType``. The downgrade is
silent — no error — so :class:`MCPClient` requests ``2026-07-28`` explicitly and exposes the
negotiated revision as a readable property that a test and an operator can both check.

Lazy import: this module re-exports its symbols through :pep:`562` ``__getattr__`` so that
``import pikachu.mcp`` does not import :mod:`client` (and, transitively, the ``mcp`` SDK)
until something is actually referenced. An agent with no MCP servers pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pikachu.mcp.client import (
        REQUESTED_PROTOCOL_VERSION,
        DiscoveredTool,
        InputRequired,
        MCPClient,
        MCPDiscovery,
        MCPProtocolError,
        MCPResult,
        MCPTransport,
        ResultType,
    )


def __getattr__(name: str) -> Any:
    if name in __all__:
        from pikachu.mcp import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
