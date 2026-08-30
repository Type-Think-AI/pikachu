"""The backend seam — one method, on purpose.

``AgentBackend`` (in ``core.protocols``) is the structural contract:

    async def run_turn(self, request: TurnRequest) -> TurnResult: ...

That is the entire coupling surface to an agent framework. ``BaseBackend`` is a thin
abstract helper for concrete backends to inherit — it does not add a second abstract method,
and it deliberately does not. A second seam method is a second thing every future backend
must reimplement correctly, and the parent repo demonstrates one method is enough: the
abstraction is 379 of 8,180 lines and a second implementation already rides on it, which is
why adding a framework is a subclass rather than a rewrite. Keep it that cheap.

The one invariant every backend shares, and the reason this file exists rather than making
each backend re-derive it: **a backend must never compute its own toolset.**
``TurnRequest.effective_tools`` arrives already narrowed by the guard (P3: effective ⊆
fixed allowlist ∩ declared). A backend that re-derives, widens, or ignores that set defeats
the entire permission layer — the guard is the only source of tool authority, and a backend
is downstream of it. :meth:`BaseBackend.authorized_tools` is the only sanctioned way to read
the toolset, and it returns exactly what the guard handed over, in order.
"""

from __future__ import annotations

import abc

from pikachu.core.types import TurnRequest, TurnResult

__all__ = ["BaseBackend"]


class BaseBackend(abc.ABC):
    """Abstract base implementing the ``AgentBackend`` protocol's single method.

    Subclass this and implement :meth:`run_turn`. Do not add a second abstract method to
    the seam — the point of the abstraction is that it stays one method wide.
    """

    @abc.abstractmethod
    async def run_turn(self, request: TurnRequest) -> TurnResult:
        """Run exactly one turn and return its result.

        The implementation MUST treat ``request.effective_tools`` as authoritative and
        MUST NOT widen it. Read it via :meth:`authorized_tools`.
        """
        raise NotImplementedError

    @staticmethod
    def authorized_tools(request: TurnRequest) -> tuple[str, ...]:
        """The tools this turn is allowed to use — exactly what the guard narrowed to.

        This is the ONLY sanctioned way for a backend to learn its toolset. It returns
        ``request.effective_tools`` verbatim: same members, same order, same multiplicity.
        A backend that reaches past this to ``request.agent.allowed_tools`` or that
        re-derives tools from the skill's ``declared_tools`` has re-implemented the guard,
        badly, and re-opened the permission hole the guard exists to close.
        """
        return request.effective_tools
