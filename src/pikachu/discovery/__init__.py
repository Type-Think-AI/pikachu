"""Discovery — the AgentSpec registry and conservative routing.

Two responsibilities live here, and they are deliberately kept apart:

``registry``
    A runtime store of user-created :class:`~pikachu.core.types.AgentSpec` s — create,
    list, get, retire. The underserved persona is the *end user* of a product built on
    this SDK, not a developer: agents are made at runtime from six declarative fields, not
    committed as YAML in a repo that needs a checkout and a deploy.

``routing``
    Conservative, trigger-match-only selection. The default is a **single** agent; an agent
    with no triggers is never auto-selected (it stays reachable by name); and an ambiguous
    match returns its candidates rather than guessing. There is no supervisor, no delegation
    graph, and no message passing — coordination is the canvas, and re-introducing a
    topology here is the specific failure this design rejects (docs/14-multi-agent.md).

Everything is imported lazily via PEP 562 so ``import pikachu`` — and ``import
pikachu.discovery`` — never pulls the confusability/embedder machinery a caller that only
touches the registry does not need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    # registry
    "AgentRegistry",
    "DuplicateAgentError",
    "UnknownAgentError",
    "InvalidAgentName",
    "is_valid_agent_name",
    # routing
    "RouteResult",
    "RouteDecision",
    "route",
    # partition management
    "PartitionAudit",
    "audit_partition",
    "check_partition_addition",
]

if TYPE_CHECKING:
    from pikachu.discovery.registry import (
        AgentRegistry,
        DuplicateAgentError,
        InvalidAgentName,
        UnknownAgentError,
        is_valid_agent_name,
    )
    from pikachu.discovery.routing import (
        PartitionAudit,
        RouteDecision,
        RouteResult,
        audit_partition,
        check_partition_addition,
        route,
    )


_REGISTRY_NAMES = {
    "AgentRegistry",
    "DuplicateAgentError",
    "UnknownAgentError",
    "InvalidAgentName",
    "is_valid_agent_name",
}
_ROUTING_NAMES = {
    "RouteResult",
    "RouteDecision",
    "route",
    "PartitionAudit",
    "audit_partition",
    "check_partition_addition",
}


def __getattr__(name: str) -> Any:
    if name in _REGISTRY_NAMES:
        from pikachu.discovery import registry

        return getattr(registry, name)
    if name in _ROUTING_NAMES:
        from pikachu.discovery import routing

        return getattr(routing, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
