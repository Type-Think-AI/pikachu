"""MCP server-mode tests — in-memory transport only, NO sockets.

The autouse ``_no_network`` fixture in ``conftest.py`` hard-fails any test that opens a
socket, so everything here drives :class:`~pikachu.mcp.server.MCPServer.handle` directly with
plain request dicts. That is itself the point of the statelessness proof: there is nothing to
connect to and no handshake to perform — a request in, a typed result out.

The assertions map one-to-one onto the lane's acceptance list:

* ``server/discover`` works with no handshake at all (statelessness)
* the advertised revision is ``2026-07-28``, not the SDK default
* every response carries a ``resultType``
* ★ a tool outside the agent's allowlist is never advertised and cannot be invoked even if
  named directly (privilege-escalation defence)
* a malformed request yields a typed error and no partial application
* an ``input_required`` result is representable and distinguishable from an error
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pikachu.core.types import AgentSpec, ToolSpec
from pikachu.mcp.client import ResultType
from pikachu.mcp.server import (
    ADVERTISED_PROTOCOL_VERSION,
    MCPServer,
    ServerResult,
    request_more_input,
)

# The SDK's own default negotiated revision — the one a naive server would silently serve.
# Named here so the trap assertion can prove we are NOT on it, without importing the SDK.
SDK_DEFAULT_NEGOTIATED_VERSION = "2025-03-26"


# --------------------------------------------------------------------------------------
# Builders — an in-memory server with a representative agent and offered tools
# --------------------------------------------------------------------------------------


def _agent(allowed: tuple[str, ...] = ("web_search", "generate_image")) -> AgentSpec:
    return AgentSpec(
        name="server-agent",
        role="Answer questions and make images.",
        allowed_tools=allowed,
    )


def _tool(name: str, *, cost: int = 0, approval: bool = False) -> ToolSpec:
    return ToolSpec(name=name, description=f"the {name} tool", cost_credits=cost, requires_approval=approval)


async def _ok_invoker(args: dict[str, Any]) -> dict[str, Any]:
    """A trivial tool that echoes its arguments back."""
    return {"echo": args}


def _server(
    *,
    allowed: tuple[str, ...] = ("web_search", "generate_image"),
    offered: tuple[ToolSpec, ...] | None = None,
    invokers: dict[str, Any] | None = None,
) -> MCPServer:
    if offered is None:
        offered = (_tool("web_search"), _tool("generate_image"))
    if invokers is None:
        invokers = {t.name: _ok_invoker for t in offered}
    return MCPServer(_agent(allowed), offered_tools=offered, invokers=invokers)


def _handle(server: MCPServer, request: object) -> ServerResult:
    """Drive one request through the server synchronously (no event loop management noise)."""
    return asyncio.run(server.handle(request))


# --------------------------------------------------------------------------------------
# server/discover works with NO handshake — statelessness
# --------------------------------------------------------------------------------------


def test_discover_needs_no_handshake() -> None:
    """A bare discover request — no _meta, no prior initialize — returns a full discovery.

    This is the statelessness proof: there is no session to establish, so the very first
    request the server ever sees can be a productive one.
    """
    server = _server()
    result = _handle(server, {"method": "server/discover"})

    assert result.result_type is ResultType.COMPLETE
    assert not result.is_error
    assert result.payload["protocolVersion"] == "2026-07-28"
    assert "capabilities" in result.payload
    assert {t["name"] for t in result.payload["tools"]} == {"web_search", "generate_image"}


def test_discover_carries_meta_version_when_supplied() -> None:
    """Statelessness also means the caller MAY state its version on the request itself.

    A matching version rides in _meta and is accepted; there was still no handshake.
    """
    server = _server()
    result = _handle(
        server,
        {"method": "server/discover", "_meta": {"protocolVersion": "2026-07-28"}},
    )
    assert result.result_type is ResultType.COMPLETE
    assert not result.is_error


# --------------------------------------------------------------------------------------
# ★ The revision trap: advertised is 2026-07-28, not the SDK default
# --------------------------------------------------------------------------------------


def test_advertised_revision_is_2026_not_sdk_default() -> None:
    assert ADVERTISED_PROTOCOL_VERSION == "2026-07-28"
    assert ADVERTISED_PROTOCOL_VERSION != SDK_DEFAULT_NEGOTIATED_VERSION

    server = _server()
    assert server.advertised_protocol_version == "2026-07-28"
    # And it is what discover reports on the wire, not an SDK-defaulted older revision.
    result = _handle(server, {"method": "server/discover"})
    assert result.payload["protocolVersion"] == "2026-07-28"


def test_caller_pinning_old_revision_is_refused_not_downgraded() -> None:
    """A caller pinning 2025-03-26 is refused loudly, not silently served the old shape."""
    server = _server()
    result = _handle(
        server,
        {"method": "server/discover", "_meta": {"protocolVersion": SDK_DEFAULT_NEGOTIATED_VERSION}},
    )
    assert result.is_error
    assert result.result_type is ResultType.COMPLETE  # errors are well-formed complete results
    assert result.payload["error"]["data"]["advertised"] == "2026-07-28"


# --------------------------------------------------------------------------------------
# Every response carries a resultType
# --------------------------------------------------------------------------------------


def test_every_response_carries_result_type() -> None:
    """Discover, a good call, a denied call, a malformed request, and an input_required
    outcome — all five must expose a resultType and serialise one on the wire."""
    server = _server(
        offered=(_tool("web_search"),),
        invokers={
            "web_search": _ok_invoker,
        },
    )
    requests: list[object] = [
        {"method": "server/discover"},
        {"method": "tools/call", "params": {"name": "web_search", "arguments": {}}},
        {"method": "tools/call", "params": {"name": "generate_image", "arguments": {}}},  # denied
        {"method": "nonsense"},  # method not found
        "not even an object",  # parse error
    ]
    for req in requests:
        result = _handle(server, req)
        assert isinstance(result.result_type, ResultType)
        assert result.as_dict()["resultType"] in {"complete", "input_required"}


def test_result_as_dict_always_has_result_type() -> None:
    server = _server()
    good = _handle(server, {"method": "tools/call", "params": {"name": "web_search"}})
    assert good.as_dict()["resultType"] == "complete"
    assert good.payload["content"]["echo"] == {}


# --------------------------------------------------------------------------------------
# ★ Privilege escalation: a tool outside the allowlist is never advertised nor invocable
# --------------------------------------------------------------------------------------


def test_tool_outside_allowlist_is_never_advertised() -> None:
    """The server OFFERS a tool the agent's allowlist does not contain; it must vanish.

    Serving is not a licence to widen: the offered set is narrowed through the guard, so an
    offered-but-not-allowed tool appears in neither advertised_tools nor discovery.
    """
    server = _server(
        allowed=("web_search",),
        offered=(_tool("web_search"), _tool("delete_everything")),
        invokers={"web_search": _ok_invoker, "delete_everything": _ok_invoker},
    )
    assert {t.name for t in server.advertised_tools} == {"web_search"}

    discovery = _handle(server, {"method": "server/discover"})
    assert {t["name"] for t in discovery.payload["tools"]} == {"web_search"}


def test_tool_outside_allowlist_cannot_be_invoked_even_if_named_directly() -> None:
    """★ The core escalation defence. Naming the un-advertised tool directly is denied.

    Even though an invoker for ``delete_everything`` was wired, the guard removed it from the
    advertised set, so tools/call refuses it — there is no name passthrough to the invoker.
    """
    called: list[str] = []

    async def _tracking_invoker(args: dict[str, Any]) -> dict[str, Any]:
        called.append("delete_everything")
        return {"deleted": True}

    server = _server(
        allowed=("web_search",),
        offered=(_tool("web_search"), _tool("delete_everything")),
        invokers={"web_search": _ok_invoker, "delete_everything": _tracking_invoker},
    )
    result = _handle(
        server,
        {"method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}},
    )

    assert result.is_error
    assert result.payload["error"]["data"]["name"] == "delete_everything"
    # The invoker was NEVER reached — denial happens before dispatch.
    assert called == []


def test_dangerous_tool_is_stripped_even_if_allowlisted() -> None:
    """A dangerous tool (bash) is stripped by the guard even when the allowlist names it,
    so the server can never advertise or invoke it. The guard rule holds server-side too."""
    server = _server(
        allowed=("bash", "web_search"),
        offered=(_tool("bash"), _tool("web_search")),
        invokers={"bash": _ok_invoker, "web_search": _ok_invoker},
    )
    assert {t.name for t in server.advertised_tools} == {"web_search"}
    result = _handle(server, {"method": "tools/call", "params": {"name": "bash", "arguments": {}}})
    assert result.is_error


def test_unknown_tool_name_is_denied_no_fallthrough() -> None:
    server = _server()
    result = _handle(
        server,
        {"method": "tools/call", "params": {"name": "never_heard_of_it"}},
    )
    assert result.is_error
    assert result.payload["error"]["data"]["name"] == "never_heard_of_it"


# --------------------------------------------------------------------------------------
# Malformed request -> typed error, no partial application
# --------------------------------------------------------------------------------------


def test_non_object_request_is_typed_error() -> None:
    server = _server()
    result = _handle(server, ["not", "an", "object"])
    assert result.is_error
    assert result.result_type is ResultType.COMPLETE


def test_missing_method_is_typed_error() -> None:
    server = _server()
    result = _handle(server, {"params": {"name": "web_search"}})
    assert result.is_error


def test_unknown_method_is_method_not_found_not_success() -> None:
    server = _server()
    result = _handle(server, {"method": "tasks/get", "params": {}})
    assert result.is_error
    assert result.payload["error"]["data"]["method"] == "tasks/get"


def test_tools_call_bad_params_is_typed_error_no_invocation() -> None:
    """A malformed tools/call (arguments not an object) is rejected before any invocation,
    so nothing is partially applied."""
    called: list[str] = []

    async def _tracking(args: dict[str, Any]) -> dict[str, Any]:
        called.append("web_search")
        return {}

    server = _server(offered=(_tool("web_search"),), invokers={"web_search": _tracking})
    result = _handle(
        server,
        {"method": "tools/call", "params": {"name": "web_search", "arguments": "oops"}},
    )
    assert result.is_error
    assert called == []


def test_bad_meta_is_typed_error() -> None:
    server = _server()
    result = _handle(server, {"method": "server/discover", "_meta": "not an object"})
    assert result.is_error


# --------------------------------------------------------------------------------------
# input_required is representable and distinct from an error
# --------------------------------------------------------------------------------------


def test_input_required_is_distinct_from_error_and_success() -> None:
    """A tool that raises request_more_input yields an input_required result — not an error,
    not a completed success. All three are distinguishable by resultType / is_error."""

    async def _needs_more(args: dict[str, Any]) -> dict[str, Any]:
        raise request_more_input(prompt="which size?")

    server = _server(offered=(_tool("web_search"),), invokers={"web_search": _needs_more})
    result = _handle(
        server,
        {"method": "tools/call", "params": {"name": "web_search", "arguments": {}}},
    )

    assert result.result_type is ResultType.INPUT_REQUIRED
    assert not result.is_error  # crucially NOT an error
    assert result.payload["prompt"] == "which size?"
    assert result.as_dict()["resultType"] == "input_required"


def test_three_outcomes_have_three_distinct_shapes() -> None:
    """complete-success, complete-error and input_required are structurally separable."""

    async def _needs_more(args: dict[str, Any]) -> dict[str, Any]:
        raise request_more_input(prompt="?")

    server = _server(
        allowed=("web_search",),
        offered=(_tool("web_search"),),
        invokers={"web_search": _needs_more},
    )
    parked = _handle(server, {"method": "tools/call", "params": {"name": "web_search"}})
    denied = _handle(server, {"method": "tools/call", "params": {"name": "generate_image"}})
    discover = _handle(server, {"method": "server/discover"})

    assert parked.result_type is ResultType.INPUT_REQUIRED and not parked.is_error
    assert denied.result_type is ResultType.COMPLETE and denied.is_error
    assert discover.result_type is ResultType.COMPLETE and not discover.is_error


# --------------------------------------------------------------------------------------
# A successful call reaches the invoker with validated arguments
# --------------------------------------------------------------------------------------


def test_successful_call_passes_validated_arguments() -> None:
    seen: list[dict[str, Any]] = []

    async def _capture(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(args)
        return {"ok": True}

    server = _server(offered=(_tool("web_search"),), invokers={"web_search": _capture})
    result = _handle(
        server,
        {"method": "tools/call", "params": {"name": "web_search", "arguments": {"q": "cats"}}},
    )
    assert result.result_type is ResultType.COMPLETE
    assert not result.is_error
    assert result.payload["content"] == {"ok": True}
    assert seen == [{"q": "cats"}]


def test_advertised_tool_without_invoker_is_typed_error_not_crash() -> None:
    """Advertised but unwired: a denial, not an exception across the boundary."""
    server = MCPServer(_agent(("web_search",)), offered_tools=(_tool("web_search"),), invokers={})
    result = _handle(server, {"method": "tools/call", "params": {"name": "web_search"}})
    assert result.is_error


def test_empty_agent_advertises_nothing() -> None:
    """An agent with an empty allowlist advertises no tools, whatever is offered."""
    server = MCPServer(
        _agent(()),
        offered_tools=(_tool("web_search"), _tool("generate_image")),
        invokers={"web_search": _ok_invoker, "generate_image": _ok_invoker},
    )
    assert server.advertised_tools == ()
    discovery = _handle(server, {"method": "server/discover"})
    assert discovery.payload["tools"] == []


@pytest.mark.parametrize("method", ["server/discover", "tools/call"])
def test_supported_methods_never_raise(method: str) -> None:
    """Neither supported method raises across the boundary, even with empty params."""
    server = _server()
    result = _handle(server, {"method": method})
    assert isinstance(result, ServerResult)
    assert isinstance(result.result_type, ResultType)
