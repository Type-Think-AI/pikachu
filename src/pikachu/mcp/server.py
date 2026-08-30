"""Expose a Pikachu agent **as** an MCP 2026-07-28 server.

The client (:mod:`pikachu.mcp.client`) connects Pikachu *out* to a stranger's server. This
module is the mirror: it lets a stranger's agent call *us*. Everything below is written to
the modern, sessionless shape of the protocol and reuses the client's protocol constants
(:data:`~pikachu.mcp.client.REQUESTED_PROTOCOL_VERSION`, :class:`~pikachu.mcp.client.ResultType`,
:class:`~pikachu.mcp.client.MCPProtocolError`) rather than redefining them — one revision
string, one error type, one ``resultType`` enum across both directions.

Protocol posture (MCP 2026-07-28, the modern/stateless era):

* **Stateless.** There is no ``initialize`` handshake. Every request carries the protocol
  version and the caller's capabilities in ``_meta``; :meth:`MCPServer.handle` reads them off
  each request and never keeps a session.
* **``server/discover`` is required** and replaces capability probing. It returns our
  advertised protocol version, capabilities and — critically — only the tools this agent may
  actually use.
* **Every result carries ``resultType``**, ``complete`` or ``input_required``. A tool that
  needs another round trip returns ``input_required``; we never call back out to the client
  (server-to-client calls are the deprecated pattern).
* **Roots, Sampling and Logging are deprecated (SEP-2577)** and are not implemented.
* **tasks** are the ``io.modelcontextprotocol/tasks`` extension and are out of scope here.
  Nothing below forecloses adding them: unknown methods produce a typed
  ``method_not_found`` error rather than a crash, so a task-aware subclass can extend
  :meth:`handle` cleanly.

Security posture — the part that matters most:

* **Serving is not a licence to widen.** The set of tools this server advertises is run
  through :func:`pikachu.guard.effective_tools` against the agent's fixed allowlist at
  construction time. A tool the agent could not itself call is **never** advertised and can
  **never** be invoked, even if a caller names it directly. A server that advertised more
  than its own agent may use would be a privilege-escalation surface pointed at our own
  users.
* **An inbound request is untrusted input.** It is validated before anything is applied; a
  malformed request yields a typed :class:`~pikachu.mcp.client.MCPProtocolError`-derived
  error result and nothing is partially applied.
* **No tool selection outside the advertised list.** ``tools/call`` resolves a name only
  against the guard-narrowed advertised set. There is no wildcard and no name passthrough:
  an unknown or denied name is an error, never a silent fallthrough.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pikachu.core.types import AgentSpec, ToolSpec
from pikachu.guard import effective_tools

# Reuse the client's protocol constants and error type — do NOT redefine them. One source of
# truth for the revision string, the resultType enum, and the protocol error across both the
# inbound (server) and outbound (client) directions.
from pikachu.mcp.client import (
    REQUESTED_PROTOCOL_VERSION,
    ResultType,
)

__all__ = [
    "ADVERTISED_PROTOCOL_VERSION",
    "MCPServer",
    "ServerResult",
    "ToolInvoker",
]


#: The revision this server advertises. It is exactly the revision the client requests —
#: reused, not re-typed — so both directions agree on 2026-07-28 by construction. Advertised
#: explicitly for the same reason the client requests it explicitly: the SDK's
#: DEFAULT_NEGOTIATED_VERSION is 2025-03-26, three revisions behind, and a server that let the
#: default stand would silently downgrade a caller that trusted it.
ADVERTISED_PROTOCOL_VERSION: Final[str] = REQUESTED_PROTOCOL_VERSION


#: A tool implementation: given a validated argument dict, produce a payload. The return is
#: typed ``object`` rather than ``dict`` on purpose — an invoker is host-supplied code, and
#: "inbound is untrusted" applies to what it hands back too. The server validates that the
#: value is a dict before wrapping it, so a misbehaving invoker yields a typed error result
#: rather than a malformed envelope. To ask for another round trip a tool raises
#: :class:`_InputRequiredSignal` (via :func:`request_more_input`); it never crafts the
#: envelope itself, so ``resultType`` is always the server's to set.
ToolInvoker = Callable[[dict[str, Any]], Awaitable[object]]


class _InputRequiredSignal(Exception):
    """Internal: a tool signalling it needs another round trip.

    Not a public error — it never escapes :meth:`MCPServer.handle`, which converts it into an
    ``input_required`` result. Distinct from :class:`MCPProtocolError`, which is a genuine
    protocol violation.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("input required")
        self.payload = payload


def request_more_input(**payload: Any) -> _InputRequiredSignal:
    """Return the signal a :data:`ToolInvoker` raises to ask for more input.

    Usage inside a tool implementation::

        raise request_more_input(prompt="which size?")

    The server turns this into a ``resultType: input_required`` result — distinguishable from
    both success and error — without the tool ever touching the envelope.
    """
    return _InputRequiredSignal(dict(payload))


@dataclass(frozen=True)
class ServerResult:
    """A single MCP result, tagged with its ``resultType``.

    Every result this server emits is one of these. ``result_type`` is never absent and never
    implicit: ``complete`` for a finished request, ``input_required`` for a multi-round-trip
    parking point. Errors are carried as a ``complete`` result whose ``payload`` holds an
    ``error`` object (JSON-RPC-shaped), so a caller always gets a well-formed, typed envelope
    — never a partially-applied one and never a bare exception across the wire.
    """

    result_type: ResultType
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        """True when this is a ``complete`` result carrying an ``error`` object."""
        return self.result_type is ResultType.COMPLETE and "error" in self.payload

    def as_dict(self) -> dict[str, Any]:
        """The wire form: the payload with ``resultType`` always present."""
        out = dict(self.payload)
        out["resultType"] = self.result_type.value
        return out


# JSON-RPC-style error codes, kept small and named so tests assert on the name not the number.
_ERR_PARSE: Final[int] = -32700
_ERR_INVALID_REQUEST: Final[int] = -32600
_ERR_METHOD_NOT_FOUND: Final[int] = -32601
_ERR_INVALID_PARAMS: Final[int] = -32602
_ERR_TOOL_DENIED: Final[int] = -32000


def _error_result(code: int, message: str, *, data: dict[str, Any] | None = None) -> ServerResult:
    """A ``complete`` result carrying a typed error. Never raises across the boundary."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return ServerResult(result_type=ResultType.COMPLETE, payload={"error": err})


class MCPServer:
    """Serve a single Pikachu :class:`AgentSpec` as an MCP 2026-07-28 endpoint.

    Construct with the agent (its ``allowed_tools`` is the fixed allowlist — the only source
    of authority), the tools you *offer*, and their implementations. At construction the
    offered set is narrowed through :func:`effective_tools`, so ``advertised_tools`` can only
    ever be a subset of what the agent may call. Anything the guard removes is dropped from
    both discovery and invocation in one place — a caller cannot reach a removed tool by any
    route.

    The server is stateless. :meth:`handle` takes one request dict and returns one
    :class:`ServerResult`; there is nothing to open, close, or keep alive.
    """

    def __init__(
        self,
        agent: AgentSpec,
        *,
        offered_tools: Sequence[ToolSpec] = (),
        invokers: dict[str, ToolInvoker] | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self._agent = agent
        self._capabilities = dict(capabilities) if capabilities is not None else {"tools": {}}

        # ---- the security seam: narrow the offered set through the guard ----------------
        # The agent's fixed allowlist is the grant. What we OFFER is a request, narrowed the
        # same way a skill's declared tools are. effective_tools also strips dangerous tools
        # and normalises names, so the advertised names are canonical and safe by the same
        # rule the whole system uses. Serving does not widen authority.
        offered_by_name: dict[str, ToolSpec] = {t.name: t for t in offered_tools}
        narrowed = effective_tools(agent.allowed_tools, tuple(offered_by_name))
        # Preserve the guard's order/multiplicity, but advertise each distinct tool once with
        # its real spec. A name the guard kept but that we have no spec for cannot happen,
        # since we derived the declared set from offered_by_name's own keys.
        advertised: list[ToolSpec] = []
        seen: set[str] = set()
        for name in narrowed.tools:
            if name in seen:
                continue
            seen.add(name)
            advertised.append(offered_by_name[name])
        self._advertised: tuple[ToolSpec, ...] = tuple(advertised)
        self._advertised_names: frozenset[str] = frozenset(seen)

        # Invokers are keyed by the SAME canonical name. An invoker for a tool the guard
        # removed is simply unreachable — it is never in _advertised_names, so tools/call can
        # never dispatch to it.
        self._invokers: dict[str, ToolInvoker] = dict(invokers or {})

    # -- readable state ----------------------------------------------------------------

    @property
    def advertised_protocol_version(self) -> str:
        """The revision this server advertises — always ``2026-07-28``."""
        return ADVERTISED_PROTOCOL_VERSION

    @property
    def advertised_tools(self) -> tuple[ToolSpec, ...]:
        """The guard-narrowed toolset. A strict subset of what the agent may call.

        Exposed so a test (and an operator) can confirm no tool outside the agent's authority
        was ever advertised, without going through the wire.
        """
        return self._advertised

    # -- the one entry point -----------------------------------------------------------

    async def handle(self, request: object) -> ServerResult:
        """Handle one inbound request statelessly. Always returns a typed result.

        The request is untrusted input: its shape is validated before anything is applied, and
        any failure produces a typed error result rather than a partial application or a raised
        exception. There is no handshake — ``_meta`` on the request carries the protocol
        version, which is validated per request.

        Dispatch is closed: ``server/discover`` and ``tools/call`` are the only methods; any
        other method (including a future ``tasks/*``) is a typed ``method_not_found`` error,
        never a silent success.
        """
        if not isinstance(request, dict):
            return _error_result(
                _ERR_PARSE,
                f"request is {type(request).__name__}, expected object",
            )

        meta_err = self._validate_meta(request)
        if meta_err is not None:
            return meta_err

        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _error_result(_ERR_INVALID_REQUEST, "request missing a method")

        if method == "server/discover":
            return self._discover()
        if method == "tools/call":
            return await self._tools_call(request)
        return _error_result(
            _ERR_METHOD_NOT_FOUND,
            f"method {method!r} is not supported",
            data={"method": method},
        )

    # -- _meta validation (statelessness) ----------------------------------------------

    def _validate_meta(self, request: dict[str, Any]) -> ServerResult | None:
        """Validate the stateless ``_meta`` block. Returns an error result, or None if OK.

        Statelessness means the caller states its protocol version on every request. We accept
        a request that omits ``_meta`` entirely (a caller that speaks our advertised revision
        by default), but if a version IS stated it must match what we advertise — a caller
        pinning an older revision is refused loudly rather than silently downgraded.
        """
        meta = request.get("_meta")
        if meta is None:
            return None
        if not isinstance(meta, dict):
            return _error_result(_ERR_INVALID_PARAMS, "_meta must be an object")
        version = meta.get("protocolVersion")
        if version is None:
            return None
        if not isinstance(version, str):
            return _error_result(_ERR_INVALID_PARAMS, "_meta.protocolVersion must be a string")
        if version != ADVERTISED_PROTOCOL_VERSION:
            return _error_result(
                _ERR_INVALID_REQUEST,
                f"protocol version {version!r} not supported; this server serves "
                f"{ADVERTISED_PROTOCOL_VERSION!r}",
                data={"advertised": ADVERTISED_PROTOCOL_VERSION},
            )
        return None

    # -- server/discover ---------------------------------------------------------------

    def _discover(self) -> ServerResult:
        """The required capability-probe replacement. Works with no handshake at all.

        Returns our advertised protocol version, capabilities and the guard-narrowed tool
        list. The tool list is derived from :attr:`advertised_tools`, so discovery literally
        cannot report a tool the agent may not use.
        """
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "requiresApproval": spec.requires_approval,
            }
            for spec in self._advertised
        ]
        return ServerResult(
            result_type=ResultType.COMPLETE,
            payload={
                "protocolVersion": ADVERTISED_PROTOCOL_VERSION,
                "capabilities": self._capabilities,
                "tools": tools,
            },
        )

    # -- tools/call --------------------------------------------------------------------

    async def _tools_call(self, request: dict[str, Any]) -> ServerResult:
        """Invoke one advertised tool. Name resolution is closed against the advertised set.

        A caller selects a tool ONLY by a name in the guard-narrowed advertised list. There is
        no wildcard and no passthrough: an unknown name, or a name the guard removed, is a
        typed denial, never a fallthrough to some default. Arguments must be an object.
        """
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error_result(_ERR_INVALID_PARAMS, "params must be an object")

        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error_result(_ERR_INVALID_PARAMS, "tools/call requires a tool name")

        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error_result(_ERR_INVALID_PARAMS, "tool arguments must be an object")

        # ★ The single gate that stops privilege escalation via the server. The name is only
        # ever checked against the guard-narrowed set. A tool outside the allowlist was never
        # advertised, so it is never in _advertised_names, so it is denied here — even if the
        # caller names it directly and even if an invoker for it happens to exist.
        if name not in self._advertised_names:
            return _error_result(
                _ERR_TOOL_DENIED,
                f"tool {name!r} is not advertised by this server",
                data={"name": name},
            )

        invoker = self._invokers.get(name)
        if invoker is None:
            # Advertised but no implementation wired: a typed error, not a crash.
            return _error_result(
                _ERR_TOOL_DENIED,
                f"tool {name!r} has no implementation",
                data={"name": name},
            )

        try:
            payload = await invoker(dict(arguments))
        except _InputRequiredSignal as parked:
            # A first-class multi-round-trip outcome, NOT an error. Distinguishable from both
            # success and failure by its resultType.
            return ServerResult(
                result_type=ResultType.INPUT_REQUIRED,
                payload={"name": name, **parked.payload},
            )

        if not isinstance(payload, dict):
            return _error_result(
                _ERR_INVALID_PARAMS,
                f"tool {name!r} returned {type(payload).__name__}, expected object",
                data={"name": name},
            )
        return ServerResult(
            result_type=ResultType.COMPLETE,
            payload={"name": name, "content": payload},
        )
