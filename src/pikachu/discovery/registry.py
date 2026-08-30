"""AgentRegistry — a runtime store of user-created ``AgentSpec`` s.

WHY A REGISTRY, NOT CODE
------------------------
The distinguishing bet of this SDK is that an agent is *data made at runtime by the end
user*, not developer YAML committed to a repo. So the registry is the surface the product's
own users touch: create an agent, list what exists, fetch one to run it, retire one that is
no longer wanted. No import, no deploy, no restart.

RETIRE IS REVERSIBLE — NOTHING IS DELETED
-----------------------------------------
Retiring an agent hides it from :meth:`AgentRegistry.list` and from routing, but the spec is
retained and :meth:`AgentRegistry.restore` brings it back unchanged. This mirrors the
archive-never-delete rule that runs through the rest of the package (skills, memory): a user
who retires an agent by mistake, or wants last month's configuration back, must not have lost
it. There is deliberately no hard-delete method.

NAMES ARE UNIQUE, AND VALIDATED WITH THE AGENT PLUGINS PATTERN
--------------------------------------------------------------
A name identifies an agent for by-name invocation and for routing, so two agents may not
share one. We adopt the **Agent Plugins 1.0.0** name grammar verbatim
(``^(?!.*(?:--|\\.\\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$``) including its negative lookahead
forbidding ``--`` and ``..``. That lookahead is a path-traversal and confusable-name defence
— the same reason the plugins lane adopts it for skill names — so a user-supplied agent name
can never contain a ``..`` traversal fragment or a doubled dash that reads as a range.

The registry holds no embeddings and makes no network call. Confusability and partition
audits live in :mod:`pikachu.discovery.routing`, imported lazily only when a caller asks.
"""

from __future__ import annotations

import re

from pikachu.core.errors import PikachuError
from pikachu.core.types import AgentSpec

__all__ = [
    "AgentRegistry",
    "DuplicateAgentError",
    "UnknownAgentError",
    "InvalidAgentName",
    "AGENT_NAME_PATTERN",
    "is_valid_agent_name",
]


# Agent Plugins 1.0.0 name grammar, adopted verbatim (docs/14, plugins lane, phase-0 Q2).
# The leading negative lookahead forbids '--' and '..' anywhere in the name: a
# path-traversal and confusable-name defence, not cosmetics.
AGENT_NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def is_valid_agent_name(name: str) -> bool:
    """True iff ``name`` matches the Agent Plugins grammar.

    Rejects uppercase, leading/trailing separators, and — via the negative lookahead — any
    ``--`` or ``..`` fragment. A single character in ``[a-z0-9]`` is the shortest legal name.
    """
    return AGENT_NAME_PATTERN.match(name) is not None


class DuplicateAgentError(PikachuError):
    """A name already belongs to a live or retired agent in this registry.

    Retired agents still occupy their name: retire is reversible, so a retired ``designer``
    must not be silently clobbered by a new ``designer``. Restore the old one or pick a new
    name.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"agent name {name!r} is already registered")
        self.name = name


class UnknownAgentError(PikachuError):
    """No agent with this name exists in the registry (live or retired)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no agent named {name!r}")
        self.name = name


class InvalidAgentName(PikachuError):
    """A name does not match the Agent Plugins grammar."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"agent name {name!r} is invalid: must match {AGENT_NAME_PATTERN.pattern} "
            f"(lowercase alphanumerics with '.'/'-' inside, no '--' or '..')"
        )
        self.name = name


class AgentRegistry:
    """In-memory registry of user-created agents.

    Not thread-safe and not durable by design: persistence is a host concern, exactly as
    with the storage protocols. A product wiring this into Postgres wraps the registry or
    replays specs on startup; the registry itself stays a plain, boring object per the
    simplicity constraint.

    State is two maps keyed by name — one live, one retired — so that a retired name still
    reserves itself against a duplicate and can be restored intact.
    """

    def __init__(self) -> None:
        self._live: dict[str, AgentSpec] = {}
        self._retired: dict[str, AgentSpec] = {}

    # -- create -------------------------------------------------------------------------

    def create(self, spec: AgentSpec) -> AgentSpec:
        """Register a new agent and return it.

        Raises :class:`InvalidAgentName` if the name violates the Agent Plugins grammar, and
        :class:`DuplicateAgentError` if the name is already taken by a live *or* retired
        agent. ``AgentSpec`` is frozen, so the stored value is exactly what was passed.
        """
        if not is_valid_agent_name(spec.name):
            raise InvalidAgentName(spec.name)
        if spec.name in self._live or spec.name in self._retired:
            raise DuplicateAgentError(spec.name)
        self._live[spec.name] = spec
        return spec

    # -- read ---------------------------------------------------------------------------

    def get(self, name: str, *, include_retired: bool = False) -> AgentSpec:
        """Fetch one agent by name.

        Live agents resolve always. A retired agent resolves only with
        ``include_retired=True`` — by-name invocation of a retired agent is a deliberate
        choice the caller makes, not a silent fallback. Raises :class:`UnknownAgentError`
        when nothing matches.
        """
        if name in self._live:
            return self._live[name]
        if include_retired and name in self._retired:
            return self._retired[name]
        raise UnknownAgentError(name)

    def list(self, *, include_retired: bool = False) -> tuple[AgentSpec, ...]:
        """All agents, sorted by name for a stable, deterministic order.

        Live only by default. With ``include_retired=True`` the retired set is included too,
        still name-sorted, so a UI can show "retired" agents without a second call.
        """
        specs = dict(self._live)
        if include_retired:
            specs.update(self._retired)
        return tuple(specs[name] for name in sorted(specs))

    def exists(self, name: str, *, include_retired: bool = False) -> bool:
        """Whether a name is registered. Considers retired agents only when asked."""
        if name in self._live:
            return True
        return include_retired and name in self._retired

    def is_retired(self, name: str) -> bool:
        """Whether a name currently names a retired agent."""
        return name in self._retired

    # -- retire / restore ---------------------------------------------------------------

    def retire(self, name: str) -> AgentSpec:
        """Retire a live agent: hide it from ``list`` and routing, keep the spec.

        Reversible via :meth:`restore`. Nothing is deleted and the name stays reserved.
        Raises :class:`UnknownAgentError` if the name is not live (retiring an
        already-retired agent is a no-op error rather than a silent success, so a caller
        cannot mistake it for a fresh retire).
        """
        if name not in self._live:
            raise UnknownAgentError(name)
        spec = self._live.pop(name)
        self._retired[name] = spec
        return spec

    def restore(self, name: str) -> AgentSpec:
        """Bring a retired agent back, unchanged.

        Raises :class:`UnknownAgentError` if the name is not currently retired.
        """
        if name not in self._retired:
            raise UnknownAgentError(name)
        spec = self._retired.pop(name)
        self._live[name] = spec
        return spec
