"""MCP client tests — fake transport only, no network, no SDK required.

The most important test in the file is :func:`test_negotiated_revision_is_2026_and_not_sdk_default`.
The failure mode this lane guards against is a *silent* downgrade to the SDK's
``DEFAULT_NEGOTIATED_VERSION`` of ``2025-03-26``: no error is raised, statelessness /
``server/discover`` / ``resultType`` are simply lost. So it must be asserted, never assumed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pikachu.core.errors import PikachuError
from pikachu.core.types import Taint
from pikachu.mcp import (
    REQUESTED_PROTOCOL_VERSION,
    DiscoveredTool,
    InputRequired,
    MCPClient,
    MCPProtocolError,
    MCPResult,
    MCPTransport,
    ResultType,
)

# The SDK's own default — the revision a naive client silently negotiates. Named here so the
# trap test can assert we are NOT on it, without importing the (optional) SDK.
SDK_DEFAULT_NEGOTIATED_VERSION = "2025-03-26"


# --------------------------------------------------------------------------------------
# Fake transport
# --------------------------------------------------------------------------------------


class FakeTransport:
    """A scripted MCP transport. Records requests, returns canned responses. No wire.

    ``discover_response`` is returned for ``server/discover``. ``echo_version`` (default True)
    makes discover echo back exactly the protocol version the client asked for in ``_meta`` —
    a well-behaved server. Set it False and supply ``discover_response['protocolVersion']`` to
    simulate a server that downgrades.
    """

    def __init__(
        self,
        discover_response: dict[str, Any] | None = None,
        *,
        echo_version: bool = True,
    ) -> None:
        self.discover_response = discover_response if discover_response is not None else {}
        self.echo_version = echo_version
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "server/discover":
            resp = dict(self.discover_response)
            if self.echo_version:
                requested = params.get("_meta", {}).get("protocolVersion")
                resp.setdefault("protocolVersion", requested)
            return resp
        raise AssertionError(f"unexpected method {method!r}")


def _discover_body(tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"capabilities": {"tools": {}}, "tools": tools if tools is not None else []}


def _client(
    transport: MCPTransport,
    *,
    allowlist: tuple[str, ...] = ("web_search", "generate_image"),
) -> MCPClient:
    return MCPClient(transport, fixed_allowlist=allowlist, server_name="fixture")


# --------------------------------------------------------------------------------------
# ★ The trap: negotiated revision must be 2026-07-28, not the SDK default
# --------------------------------------------------------------------------------------


def test_requested_version_is_2026() -> None:
    assert REQUESTED_PROTOCOL_VERSION == "2026-07-28"


def test_negotiated_revision_is_2026_and_not_sdk_default() -> None:
    """The single most important assertion in the lane.

    A well-behaved server echoes the version the client requested. The client must have
    requested 2026-07-28 (not relied on the SDK default 2025-03-26), so the negotiated
    revision it exposes must be 2026-07-28 and must NOT be the SDK default.
    """
    transport = FakeTransport(_discover_body())
    client = _client(transport)

    assert client.negotiated_version is None  # not yet discovered
    asyncio.run(client.discover())

    assert client.negotiated_version == "2026-07-28"
    assert client.negotiated_version != SDK_DEFAULT_NEGOTIATED_VERSION

    # And the wire proves WHY: the request carried 2026-07-28 in _meta. Statelessness means
    # the version rides on the request itself, not on a prior initialize handshake.
    method, params = transport.requests[0]
    assert method == "server/discover"
    assert params["_meta"]["protocolVersion"] == "2026-07-28"


def test_server_downgrade_is_rejected_not_silently_accepted() -> None:
    """If a server tries to negotiate the old revision, we refuse loudly.

    This is the silent-downgrade failure mode made explicit: a server answering with
    2025-03-26 must raise, not quietly set negotiated_version to the old revision.
    """
    body = _discover_body()
    body["protocolVersion"] = SDK_DEFAULT_NEGOTIATED_VERSION
    transport = FakeTransport(body, echo_version=False)
    client = _client(transport)

    with pytest.raises(MCPProtocolError):
        asyncio.run(client.discover())
    # State untouched — nothing partially applied.
    assert client.negotiated_version is None


def test_sdk_default_is_actually_behind_when_installed() -> None:
    """When the real ``mcp`` SDK is installed, prove the trap is real, not folklore.

    Confirms LATEST=2026-07-28 while DEFAULT=2025-03-26. Skipped when the package is absent —
    the whole lane is designed to work without it.
    """
    mcp_types = pytest.importorskip("mcp.types")
    assert mcp_types.LATEST_PROTOCOL_VERSION == "2026-07-28"
    assert mcp_types.DEFAULT_NEGOTIATED_VERSION == SDK_DEFAULT_NEGOTIATED_VERSION
    # The client requests LATEST explicitly rather than trusting DEFAULT.
    assert REQUESTED_PROTOCOL_VERSION == mcp_types.LATEST_PROTOCOL_VERSION


# --------------------------------------------------------------------------------------
# server/discover
# --------------------------------------------------------------------------------------


def test_discover_calls_server_discover_and_parses() -> None:
    tools = [
        {"name": "web_search", "description": "search the web"},
        {"name": "unrelated", "description": "something else"},
    ]
    transport = FakeTransport(_discover_body(tools))
    client = _client(transport)

    discovery = asyncio.run(client.discover())

    assert transport.requests[0][0] == "server/discover"
    assert discovery.protocol_version == "2026-07-28"
    assert discovery.capabilities == {"tools": {}}
    # Discovery reports everything the server advertised, before allowlist narrowing.
    assert {t.spec.name for t in discovery.tools} == {"web_search", "unrelated"}


# --------------------------------------------------------------------------------------
# P3 across the MCP boundary — a server cannot widen authority
# --------------------------------------------------------------------------------------


def test_ten_advertised_two_allowed_yields_exactly_two() -> None:
    """A server advertising 10 tools to an agent that allows 2 yields exactly 2.

    The MCP boundary is subject to P3 exactly like any other tool source: the server's list is
    a request, the fixed allowlist is the grant.
    """
    advertised = [
        {"name": f"tool_{i}", "description": f"remote tool {i}"} for i in range(10)
    ]
    # Two of the advertised names are in the allowlist; the other eight are not.
    advertised[3]["name"] = "web_search"
    advertised[7]["name"] = "generate_image"
    transport = FakeTransport(_discover_body(advertised))
    client = _client(transport, allowlist=("web_search", "generate_image"))

    asyncio.run(client.discover())
    granted = client.list_tools()

    assert {t.spec.name for t in granted} == {"web_search", "generate_image"}
    assert len(granted) == 2


def test_server_cannot_smuggle_dangerous_tool() -> None:
    """Even if a server advertises a dangerous tool AND the allowlist contains it, it is
    stripped. The guard's dangerous-tool rule holds across the MCP boundary."""
    advertised = [
        {"name": "bash", "description": "run a shell command"},
        {"name": "web_search", "description": "search"},
    ]
    transport = FakeTransport(_discover_body(advertised))
    client = _client(transport, allowlist=("bash", "web_search"))

    asyncio.run(client.discover())
    granted = client.list_tools()

    assert {t.spec.name for t in granted} == {"web_search"}


def test_list_tools_before_discover_raises() -> None:
    client = _client(FakeTransport(_discover_body()))
    with pytest.raises(MCPProtocolError):
        client.list_tools()


# --------------------------------------------------------------------------------------
# Taint — discovered tools are foreign input
# --------------------------------------------------------------------------------------


def test_discovered_tools_carry_taint() -> None:
    tools = [{"name": "web_search", "description": "search"}]
    transport = FakeTransport(_discover_body(tools))
    client = _client(transport)

    discovery = asyncio.run(client.discover())
    (tool,) = discovery.tools

    assert isinstance(tool, DiscoveredTool)
    assert not tool.lineage.is_clean
    assert Taint.FOREIGN_SKILL in tool.lineage.taints
    # The source records which server tainted it, so lineage stays auditable.
    assert any("mcp:" in s for s in tool.lineage.sources)


def test_taint_survives_allowlist_narrowing() -> None:
    """Narrowing removes tools; it never launders the survivors."""
    tools = [
        {"name": "web_search", "description": "search"},
        {"name": "dropped", "description": "not allowed"},
    ]
    transport = FakeTransport(_discover_body(tools))
    client = _client(transport, allowlist=("web_search",))

    asyncio.run(client.discover())
    (granted,) = client.list_tools()

    assert granted.spec.name == "web_search"
    assert Taint.FOREIGN_SKILL in granted.lineage.taints


# --------------------------------------------------------------------------------------
# resultType input_required is distinguishable from an error
# --------------------------------------------------------------------------------------


def test_input_required_is_distinct_outcome() -> None:
    client = _client(FakeTransport(_discover_body()))
    routed = client.route_result(
        {"resultType": "input_required", "prompt": "which size?"}
    )
    assert isinstance(routed, InputRequired)
    assert not isinstance(routed, MCPResult)
    # And crucially it is NOT an error — routing did not raise.
    assert routed.payload["prompt"] == "which size?"


def test_complete_result_is_mcp_result() -> None:
    client = _client(FakeTransport(_discover_body()))
    routed = client.route_result({"resultType": "complete", "content": "done"})
    assert isinstance(routed, MCPResult)
    assert not isinstance(routed, InputRequired)


def test_input_required_not_collapsed_into_failure() -> None:
    """The two outcomes are structurally different types, so a caller physically cannot
    collapse input_required into the failure path by mistake."""
    client = _client(FakeTransport(_discover_body()))
    complete = client.route_result({"resultType": "complete"})
    parked = client.route_result({"resultType": "input_required"})
    assert type(complete) is not type(parked)
    assert {ResultType.COMPLETE.value, ResultType.INPUT_REQUIRED.value} == {
        "complete",
        "input_required",
    }


def test_unknown_result_type_raises() -> None:
    client = _client(FakeTransport(_discover_body()))
    with pytest.raises(MCPProtocolError):
        client.route_result({"resultType": "totally_made_up"})
    with pytest.raises(MCPProtocolError):
        client.route_result({})  # missing resultType


# --------------------------------------------------------------------------------------
# Malformed / hostile discover — typed error, never partial application
# --------------------------------------------------------------------------------------


def test_discover_missing_version_raises() -> None:
    transport = FakeTransport({"capabilities": {}, "tools": []}, echo_version=False)
    client = _client(transport)
    with pytest.raises(MCPProtocolError):
        asyncio.run(client.discover())
    assert client.negotiated_version is None


def test_discover_malformed_tool_rejected_whole() -> None:
    """A single malformed tool descriptor rejects the entire discover — no partial state.

    A partially-applied discover (good tools kept, bad one skipped) is worse than a rejected
    one: it lets a hostile server slip a tool past validation by pairing it with valid ones.
    """
    tools = [
        {"name": "web_search", "description": "ok"},
        {"description": "no name at all"},  # malformed
    ]
    transport = FakeTransport(_discover_body(tools))
    client = _client(transport)

    with pytest.raises(MCPProtocolError):
        asyncio.run(client.discover())
    # Nothing applied: state untouched, list_tools still refuses.
    assert client.negotiated_version is None
    with pytest.raises(MCPProtocolError):
        client.list_tools()


def test_discover_non_object_tools_rejected() -> None:
    body = {"protocolVersion": "2026-07-28", "capabilities": {}, "tools": "not a list"}
    transport = FakeTransport(body, echo_version=False)
    client = _client(transport)
    with pytest.raises(MCPProtocolError):
        asyncio.run(client.discover())


def test_discover_tool_with_blank_name_rejected() -> None:
    tools = [{"name": "   ", "description": "whitespace name"}]
    transport = FakeTransport(_discover_body(tools))
    client = _client(transport)
    with pytest.raises(MCPProtocolError):
        asyncio.run(client.discover())


def test_protocol_error_is_pikachu_error() -> None:
    """A host can catch everything from this package with one clause."""
    assert issubclass(MCPProtocolError, PikachuError)
