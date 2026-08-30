"""Media-tool registry implementation.

Read ``pikachu.tools.__init__`` for the surface's contract and the four properties it
guarantees. This module implements them. It depends only on ``pikachu.core.types`` and
``pikachu.guard`` — the same dependency-light boundary the rest of the guard-adjacent code
keeps, so a host can register generators without importing a model framework.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import Enum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.errors import PikachuError
from pikachu.core.types import (
    Artifact,
    ArtifactKind,
    Lineage,
    Provenance,
    Taint,
    ToolOutcome,
    normalize_tool_name,
    utcnow,
)
from pikachu.guard import SourceKind, admit

__all__ = [
    "MEDIA_KIND_TOOLS",
    "RESERVED_IDENTITY_ARGS",
    "MediaContext",
    "MediaInvocation",
    "MediaKind",
    "MediaResult",
    "MediaTool",
    "MediaToolDenied",
    "MediaToolRegistry",
]


# --------------------------------------------------------------------------------------
# Kind — what a media tool produces, and which tool names that kind may reach
# --------------------------------------------------------------------------------------


class MediaKind(str, Enum):
    """The kind of media a tool produces.

    Kind is the mechanism that stops a skill reaching for the wrong tool: a skill scoped to
    ``VIDEO`` cannot invoke ``generate_image`` even if the model emits a call for it, because
    the name is not in the kind's permitted set. This mirrors groot's ``_KIND_TOOLS`` map
    exactly (``image`` -> ``generate_image``; ``image_edit`` -> ``{edit_image,
    generate_image}``; ``video`` -> ``generate_video``) so the two runtimes enforce the same
    boundary and a migration does not change behaviour.

    Each kind maps to an :class:`~pikachu.core.types.ArtifactKind` for the artifact its
    output becomes.
    """

    IMAGE = "image"
    IMAGE_EDIT = "image_edit"
    VIDEO = "video"

    @property
    def artifact_kind(self) -> ArtifactKind:
        """The artifact kind a successful output of this media kind becomes."""
        return _ARTIFACT_KIND_FOR[self]

    @property
    def permitted_tools(self) -> frozenset[str]:
        """Tool names this kind is allowed to register/invoke, normalized."""
        return MEDIA_KIND_TOOLS[self]


#: Which tool names each :class:`MediaKind` may register under and invoke. Names are stored
#: normalized so membership is tested against the same canonical form the guard uses. This
#: is the Pikachu-side twin of groot's ``_KIND_TOOLS``; ``image_edit`` deliberately permits
#: ``generate_image`` too, matching groot (an edit skill may also generate from scratch),
#: while ``video`` and ``image`` are disjoint so neither can reach the other's tool.
MEDIA_KIND_TOOLS: Final[dict[MediaKind, frozenset[str]]] = {
    MediaKind.IMAGE: frozenset({normalize_tool_name("generate_image")}),
    MediaKind.IMAGE_EDIT: frozenset(
        {normalize_tool_name("edit_image"), normalize_tool_name("generate_image")}
    ),
    MediaKind.VIDEO: frozenset({normalize_tool_name("generate_video")}),
}

_ARTIFACT_KIND_FOR: Final[dict[MediaKind, ArtifactKind]] = {
    MediaKind.IMAGE: ArtifactKind.IMAGE,
    MediaKind.IMAGE_EDIT: ArtifactKind.IMAGE,
    MediaKind.VIDEO: ArtifactKind.VIDEO,
}


#: Argument keys the model may NOT set. The model is untrusted input; identity is supplied
#: by the host context, never by the model. Stripped from model args before every call.
#: Mirrors groot's ``_make_handler`` stripping ``session`` and ``user_id``.
RESERVED_IDENTITY_ARGS: Final[frozenset[str]] = frozenset({"session", "user_id"})


# --------------------------------------------------------------------------------------
# Error
# --------------------------------------------------------------------------------------


class MediaToolDenied(PikachuError):
    """A media tool was invoked outside what the guard permitted, or for the wrong kind.

    Raised at the CALL boundary (consistent with :class:`~pikachu.core.errors.ToolDenied`):
    the guard's *filter* fails closed by omission, but once a call actually reaches a
    registered implementation with a name the guard did not admit — or a name the tool's
    declared kind does not permit — that is a denial to surface, not to swallow. It is caught
    by the registry's own invocation wrapper and reported as a failed
    :class:`~pikachu.core.types.ToolOutcome`; a host calling :meth:`MediaToolRegistry.invoke`
    directly receives it raised.
    """

    def __init__(self, tool: str, *, reason: str) -> None:
        super().__init__(f"media tool {tool!r} denied: {reason}")
        self.tool = tool
        self.reason = reason


# --------------------------------------------------------------------------------------
# Result contract — what a host callable returns
# --------------------------------------------------------------------------------------


class MediaResult(BaseModel):
    """What a host media generator returns — enough to construct an :class:`Artifact`.

    A host callable does not build the artifact itself: it returns this, and the registry
    builds the artifact with the correct kind and lineage. That keeps artifact construction
    (and its taint rules) on Pikachu's side of the boundary where they belong.

    ``cost_credits`` is provenance ONLY. The host owns money — it charges before generating
    and refunds on failure (lane contract P5); Pikachu never charges. If the host has
    ALREADY applied a cost, it may report that number here purely so the artifact's
    :class:`~pikachu.core.types.Provenance` records what a frame cost. Reporting a number is
    not charging it, and nothing in Pikachu reads this to deduct, reserve, or refund. If you
    are tempted to make this field *cause* a charge, stop — that is the host's job.
    """

    model_config = ConfigDict(frozen=True)

    payload_ref: str
    """A reference to the produced bytes (a URL, an R2 key, a canvas id) — never the bytes.
    Matches :attr:`Artifact.payload_ref`: dropping an artifact from context is lossless
    because this reference restores it."""

    prompt: str | None = None
    model: str | None = None
    seed: int | None = None

    cost_credits: Annotated[int, Field(ge=0)] = 0
    """A cost the host ALREADY applied, reported for provenance. NOT a charge. See above."""

    produced_by: str | None = None
    """Which agent produced this, for the artifact's provenance. Defaults to the invoking
    agent name when the registry knows it."""


# --------------------------------------------------------------------------------------
# Invocation record — what the registry returns from an invocation
# --------------------------------------------------------------------------------------


class MediaInvocation(BaseModel):
    """The outcome of invoking a registered media tool.

    ``outcome`` is the authoritative signal: :attr:`~pikachu.core.types.ToolOutcome.SUCCESS`
    carries a built :attr:`artifact`; :attr:`~pikachu.core.types.ToolOutcome.DENIED` and
    :attr:`~pikachu.core.types.ToolOutcome.FAILED` carry an :attr:`error` string and no
    artifact. A raising host generator becomes ``FAILED`` here rather than an exception that
    kills the turn.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    outcome: ToolOutcome
    artifact: Artifact | None = None
    error: str | None = None


# --------------------------------------------------------------------------------------
# Context — the host-supplied identity and correlation for one invocation
# --------------------------------------------------------------------------------------


class MediaContext(BaseModel):
    """Host-supplied, trusted invocation context.

    This is where identity comes from — NOT the model's arguments. The registry strips
    :data:`RESERVED_IDENTITY_ARGS` from the model-supplied args and re-supplies ``user_id``
    (and ``session``, if the host set one) from here, so the model can never name a different
    user. ``artifact_id`` is the id the built artifact receives; supply it from the host's
    own id scheme so the canvas graph and the host's records agree.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    artifact_id: str
    agent_name: str | None = None
    session: str | None = None
    parent_artifact: str | None = None
    """When set, the produced artifact records this as its ``parent`` — a revision edge."""


# --------------------------------------------------------------------------------------
# Tool registration record
# --------------------------------------------------------------------------------------


#: The type of a host media generator: an async callable taking keyword args and returning a
#: :class:`MediaResult`. Pikachu is natively async, so coroutine functions are accepted
#: directly — there is no thread hop (contrast groot's Hermes handler, which is synchronous
#: and hops to the main loop because it runs on a worker thread).
MediaGenerator = Callable[..., Awaitable["MediaResult"]]


class MediaTool:
    """A registered media generator: name + declared kind + async callable + arg schema.

    Not a Pydantic model because it holds a live callable. Constructed by
    :meth:`MediaToolRegistry.register`; the registry rejects a synchronous callable and a
    name the kind does not permit at registration time, so a misconfiguration fails at
    startup rather than mid-turn.
    """

    __slots__ = ("name", "kind", "generator", "arg_schema", "description")

    def __init__(
        self,
        name: str,
        kind: MediaKind,
        generator: MediaGenerator,
        *,
        arg_schema: type[BaseModel] | None = None,
        description: str = "",
    ) -> None:
        norm = normalize_tool_name(name)
        if not norm:
            raise ValueError(f"media tool name {name!r} normalizes to empty")
        if norm not in kind.permitted_tools:
            raise MediaToolDenied(
                norm,
                reason=(
                    f"kind {kind.value!r} permits {sorted(kind.permitted_tools)}, "
                    f"not {norm!r}"
                ),
            )
        if not inspect.iscoroutinefunction(generator):
            raise TypeError(
                f"media tool {norm!r} generator must be an async def (coroutine function); "
                f"Pikachu invokes it directly with no thread hop"
            )
        self.name = norm
        self.kind = kind
        self.generator = generator
        self.arg_schema = arg_schema
        self.description = description


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------


class MediaToolRegistry:
    """The host-facing registry of media generators.

    Bind it to a fixed allowlist (the host's, the only source of authority), register one
    async generator per tool name, then hand :meth:`as_tool_registry` to
    ``PydanticAIBackend(tool_registry=...)``. Every produced callable routes its invocation
    through :meth:`invoke`, which is the single enforcement point:

      1. **guard admission** via :func:`pikachu.guard.admit` — the tool name must survive
         narrowing against the fixed allowlist, or the call is denied;
      2. **kind enforcement** — the name must be in the tool's declared kind's permitted set;
      3. **identity stripping** — :data:`RESERVED_IDENTITY_ARGS` are removed from model args
         and re-supplied from the trusted :class:`MediaContext`;
      4. **failure confinement** — a raising generator becomes a failed
         :class:`MediaInvocation`, never an exception out of the turn.
    """

    def __init__(self, *, fixed_allowlist: Sequence[str]) -> None:
        self._fixed_allowlist: tuple[str, ...] = tuple(fixed_allowlist)
        self._tools: dict[str, MediaTool] = {}

    @property
    def fixed_allowlist(self) -> tuple[str, ...]:
        """The host allowlist this registry admits against — read-only."""
        return self._fixed_allowlist

    def register(
        self,
        name: str,
        kind: MediaKind,
        generator: MediaGenerator,
        *,
        arg_schema: type[BaseModel] | None = None,
        description: str = "",
    ) -> MediaTool:
        """Register an async ``generator`` under ``name`` with declared ``kind``.

        Fails at registration (not mid-turn) if the generator is synchronous or if ``name``
        is not one the ``kind`` permits — see :class:`MediaTool`. Registering a name twice
        replaces the prior registration.
        """
        tool = MediaTool(
            name, kind, generator, arg_schema=arg_schema, description=description
        )
        self._tools[tool.name] = tool
        return tool

    def registered_names(self) -> tuple[str, ...]:
        """Names currently registered, in registration order."""
        return tuple(self._tools)

    async def invoke(
        self,
        name: str,
        *,
        context: MediaContext,
        args: Mapping[str, Any] | None = None,
    ) -> MediaInvocation:
        """Invoke a registered media tool through the guard, returning a
        :class:`MediaInvocation`.

        This is the enforcement point. It never raises for a denied or failed call — both
        surface on the returned :class:`MediaInvocation`'s ``outcome``. It DOES raise for a
        programming error the host must fix (an unregistered name), because that is not an
        outcome of a turn, it is a bug in wiring.
        """
        tool = self._tools.get(normalize_tool_name(name))
        if tool is None:
            raise MediaToolDenied(name, reason="no such media tool registered")

        # (1) Guard admission — the single admission point. The tool's own name is what we
        #     ask the guard to narrow: if it does not survive the fixed allowlist, the host
        #     never granted it and we deny. `admit` composes P3; we never re-derive it.
        admission = admit(
            f"media:{tool.name}",
            declared_tools=(tool.name,),
            fixed_allowlist=self._fixed_allowlist,
            kind=SourceKind.FOREIGN_SKILL,
        )
        if tool.name not in admission.tools:
            reason = admission.reasons.get(tool.name, "not in fixed allowlist")
            return MediaInvocation(
                tool=tool.name, outcome=ToolOutcome.DENIED, error=reason
            )

        # (2) Kind enforcement — redundant with register() but re-checked here because a call
        #     boundary must not trust that construction-time invariants still hold.
        if tool.name not in tool.kind.permitted_tools:
            return MediaInvocation(
                tool=tool.name,
                outcome=ToolOutcome.DENIED,
                error=f"kind {tool.kind.value!r} does not permit {tool.name!r}",
            )

        # (3) Identity stripping — the model's args are untrusted. Drop the reserved keys and
        #     re-supply identity from the trusted context. The model can never name a user.
        call_args = self._strip_identity(args)
        call_args["user_id"] = context.user_id
        if context.session is not None:
            call_args["session"] = context.session

        # (4) Failure confinement — a raising generator is a failed outcome, not a crash.
        #     The result is treated as `object` at this boundary on purpose: the generator's
        #     declared return type is a host promise, and a call boundary does not trust a
        #     promise — it verifies. Widening to object keeps the isinstance guard below
        #     reachable (a host CAN return the wrong type, and the test proves it degrades).
        try:
            result: object = await tool.generator(**call_args)
        except Exception as exc:  # noqa: BLE001 - a host generator failure must degrade
            return MediaInvocation(
                tool=tool.name,
                outcome=ToolOutcome.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        if not isinstance(result, MediaResult):
            return MediaInvocation(
                tool=tool.name,
                outcome=ToolOutcome.FAILED,
                error=(
                    f"generator returned {type(result).__name__}, expected MediaResult"
                ),
            )

        artifact = self._build_artifact(tool, result, context)
        return MediaInvocation(
            tool=tool.name, outcome=ToolOutcome.SUCCESS, artifact=artifact
        )

    def as_tool_registry(
        self,
        *,
        context: MediaContext,
        sink: Callable[[MediaInvocation], None] | None = None,
    ) -> dict[str, Callable[..., Awaitable[str]]]:
        """Build the ``Mapping[str, Callable]`` for ``PydanticAIBackend(tool_registry=...)``.

        Each returned callable is a thin async adapter the model can call: it forwards the
        model's keyword args into :meth:`invoke` with the trusted ``context``, records the
        resulting :class:`MediaInvocation` via ``sink`` (so the host can collect artifacts
        for the turn's :attr:`~pikachu.core.types.TurnResult.artifacts`), and returns a short
        text the model reads. It NEVER returns the artifact object to the model — the model
        gets a reference and a status, and the artifact is threaded to the host out of band,
        keeping tool output tainted and the canvas append-only.

        The backend independently only ever calls the names the guard narrowed to (it looks
        up ``request.effective_tools`` in this map), so admission is enforced twice: once by
        the backend's lookup, once inside :meth:`invoke`. Defence in depth, by design.
        """

        def _make(tool_name: str) -> Callable[..., Awaitable[str]]:
            async def _call(**kwargs: Any) -> str:
                invocation = await self.invoke(tool_name, context=context, args=kwargs)
                if sink is not None:
                    sink(invocation)
                if invocation.outcome is ToolOutcome.SUCCESS and invocation.artifact:
                    return (
                        f"{tool_name} produced {invocation.artifact.kind.value} "
                        f"artifact {invocation.artifact.id}"
                    )
                return f"{tool_name} {invocation.outcome.value}: {invocation.error or ''}".strip()

            _call.__name__ = tool_name
            _call.__doc__ = self._tools[tool_name].description or (
                f"Generate {self._tools[tool_name].kind.value} media."
            )
            return _call

        return {name: _make(name) for name in self._tools}

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _strip_identity(args: Mapping[str, Any] | None) -> dict[str, Any]:
        """Return a mutable copy of ``args`` with reserved identity keys removed.

        The model cannot set ``session`` or ``user_id`` — the registry supplies those from
        the trusted context. Anything else the model sent (a prompt, a size, a seed) passes
        through unchanged.
        """
        if not args:
            return {}
        return {k: v for k, v in args.items() if k not in RESERVED_IDENTITY_ARGS}

    def _build_artifact(
        self, tool: MediaTool, result: MediaResult, context: MediaContext
    ) -> Artifact:
        """Construct the immutable artifact for a successful generation.

        The artifact's kind is derived from the TOOL's declared kind, not from anything the
        model or the host result claims, so a video tool cannot mint an image artifact. Tool
        output is tainted (``TOOL_OUTPUT``): a generated frame is data produced by an
        external process, and the taint is what stops it laundering into a trusted position
        later.
        """
        provenance = Provenance(
            prompt=result.prompt,
            model=result.model,
            cost_credits=result.cost_credits,
            seed=result.seed,
            produced_by=result.produced_by or context.agent_name,
            at=utcnow(),
        )
        lineage = Lineage.clean().with_taint(
            Taint.TOOL_OUTPUT,
            f"media:{tool.name}",
        )
        return Artifact(
            id=context.artifact_id,
            kind=tool.kind.artifact_kind,
            payload_ref=result.payload_ref,
            parent=context.parent_artifact,
            provenance=provenance,
            lineage=lineage,
        )
