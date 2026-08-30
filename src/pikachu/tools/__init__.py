"""First-class media-tool registry — the surface a HOST APP registers generators on.

Pikachu's ``PydanticAIBackend`` already accepts ``tool_registry: Mapping[str, Callable]``.
That is the raw seam: a bare name -> callable map with no notion of *what kind of thing* a
callable produces, no identity stripping, and no contract for turning an output into a
Pikachu :class:`~pikachu.core.types.Artifact`. This package is the first-class surface built
on top of it, for the one job a host most needs: long-running image and video generation
whose outputs land as artifacts.

    from pikachu.tools import MediaToolRegistry, MediaKind, MediaResult

    registry = MediaToolRegistry(fixed_allowlist=agent.allowed_tools)
    registry.register("generate_image", MediaKind.IMAGE, host_generate_image)
    backend = PydanticAIBackend(api_key=key, tool_registry=registry.as_tool_registry())

Four properties this surface guarantees, each earned from the parent system's incidents and
the lane contract (``api/PIKACHU-TOOLS-LANES.md``):

1. **Authority comes from the guard, never from the registry.** Every invocation routes
   through :func:`pikachu.guard.admit` — the single admission point — with the host's fixed
   allowlist. The registry is a *catalogue of implementations*; it grants nothing. A tool
   whose name the guard did not narrow to is never invoked (:class:`MediaToolDenied`).

2. **Kind enforcement.** A tool is registered with a declared :class:`MediaKind`. The name
   it registers under must be one the kind permits (``video`` -> ``generate_video``), so a
   video skill cannot reach the image tool by naming it. Mirrors groot's ``_enforce_kind`` /
   ``_KIND_TOOLS``.

3. **Identity cannot be overridden by the model.** The model's arguments are untrusted
   input. Before a host callable runs, the reserved keys ``session`` and ``user_id`` are
   stripped from the model-supplied args and re-supplied by the host context — the model can
   never name a different user. Mirrors groot's ``_make_handler``.

4. **A raising tool degrades, it does not crash the turn.** A host generator that raises
   surfaces as a failed :class:`~pikachu.core.types.ToolOutcome` carried on a
   :class:`MediaInvocation`, not as an exception that aborts the run.

**What this surface deliberately does NOT do: money.** The host owns money — PicX charges
before generating and refunds on failure (lane contract P5). Pikachu's job is confinement
and provenance, not billing. There is no ``charge``/``deduct``/``refund`` here. A host that
has *already* applied a cost may REPORT it via :attr:`MediaResult.cost_credits`, which is
copied verbatim into the artifact's :class:`~pikachu.core.types.Provenance` for provenance
only — reporting a number is not charging it. See :class:`MediaResult`.
"""

from __future__ import annotations

from pikachu.tools.media import (
    MEDIA_KIND_TOOLS,
    MediaContext,
    MediaInvocation,
    MediaKind,
    MediaResult,
    MediaTool,
    MediaToolDenied,
    MediaToolRegistry,
    RESERVED_IDENTITY_ARGS,
)

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
