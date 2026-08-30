"""Tests for the media-tool registry surface — the four properties the lane requires.

The surface under test is ``pikachu.tools`` (Lane 1). What each block proves:

  * an async media tool registers and is invocable, producing an ``Artifact``;
  * a tool NOT in the guard-narrowed set is never invoked (denied through ``admit``);
  * kind enforcement rejects registering/reaching the wrong tool;
  * a raising generator surfaces as a FAILED ``ToolOutcome``, not an exception that crashes
    the turn;
  * identity args supplied by the model are stripped and re-supplied from trusted context.

Offline and deterministic: the generators here are local async fakes, no network, no model.
"""

from __future__ import annotations

import pytest

from pikachu.core.types import ArtifactKind, ToolOutcome
from pikachu.tools import (
    MediaContext,
    MediaKind,
    MediaResult,
    MediaToolDenied,
    MediaToolRegistry,
)


# --------------------------------------------------------------------------------------
# Fakes — local async generators standing in for the host's real media pipeline
# --------------------------------------------------------------------------------------


async def fake_generate_image(*, prompt: str = "", user_id: str = "", **_: object) -> MediaResult:
    """A well-behaved image generator. Echoes the prompt into the payload reference."""
    return MediaResult(
        payload_ref=f"r2://img/{prompt or 'blank'}",
        prompt=prompt,
        model="seedream-5-lite",
        cost_credits=15,
    )


async def fake_generate_video(*, prompt: str = "", user_id: str = "", **_: object) -> MediaResult:
    return MediaResult(payload_ref=f"r2://vid/{prompt or 'blank'}", prompt=prompt)


async def raising_generator(**_: object) -> MediaResult:
    raise RuntimeError("upstream provider 503")


def sync_generator(**_: object) -> MediaResult:  # not async — must be rejected at register
    return MediaResult(payload_ref="r2://never")


@pytest.fixture
def context() -> MediaContext:
    return MediaContext(user_id="u-42", artifact_id="art-1", agent_name="colourist")


# --------------------------------------------------------------------------------------
# 1. An async media tool registers and is invocable, and its output becomes an Artifact
# --------------------------------------------------------------------------------------


async def test_async_tool_registers_and_invokes(
    fixed_allowlist: tuple[str, ...], context: MediaContext
) -> None:
    registry = MediaToolRegistry(fixed_allowlist=fixed_allowlist)
    registry.register("generate_image", MediaKind.IMAGE, fake_generate_image)
    assert registry.registered_names() == ("generate_image",)

    invocation = await registry.invoke(
        "generate_image", context=context, args={"prompt": "a red fox"}
    )

    assert invocation.outcome is ToolOutcome.SUCCESS
    assert invocation.error is None
    art = invocation.artifact
    assert art is not None
    assert art.id == "art-1"
    assert art.kind is ArtifactKind.IMAGE
    assert art.payload_ref == "r2://img/a red fox"
    assert art.provenance.prompt == "a red fox"
    assert art.provenance.model == "seedream-5-lite"
    # cost is provenance-only, copied verbatim — Pikachu never charged it.
    assert art.provenance.cost_credits == 15
    assert art.provenance.produced_by == "colourist"


async def test_video_tool_produces_video_artifact(
    fixed_allowlist: tuple[str, ...], context: MediaContext
) -> None:
    allowlist = (*fixed_allowlist, "generate_video")
    registry = MediaToolRegistry(fixed_allowlist=allowlist)
    registry.register("generate_video", MediaKind.VIDEO, fake_generate_video)

    invocation = await registry.invoke(
        "generate_video", context=context, args={"prompt": "clouds"}
    )
    assert invocation.outcome is ToolOutcome.SUCCESS
    assert invocation.artifact is not None
    assert invocation.artifact.kind is ArtifactKind.VIDEO


# --------------------------------------------------------------------------------------
# 2. A tool NOT in the guard-narrowed set is never invoked
# --------------------------------------------------------------------------------------


async def test_tool_absent_from_allowlist_is_denied_not_invoked(
    context: MediaContext,
) -> None:
    """The allowlist does NOT contain generate_video. Registering is fine (the registry is a
    catalogue), but invoking is denied by the guard before the generator can run."""
    called = False

    async def spy(**_: object) -> MediaResult:
        nonlocal called
        called = True
        return MediaResult(payload_ref="r2://should-never-exist")

    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    registry.register("generate_video", MediaKind.VIDEO, spy)

    invocation = await registry.invoke("generate_video", context=context, args={})

    assert invocation.outcome is ToolOutcome.DENIED
    assert invocation.artifact is None
    assert called is False, "a denied tool's generator must never execute"


async def test_backend_registry_only_exposes_generators_but_invoke_still_guards(
    context: MediaContext,
) -> None:
    """as_tool_registry exposes every registered generator by name, but each one re-checks
    admission on invoke — so even if the backend called a denied name, it would be denied."""
    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    registry.register("generate_video", MediaKind.VIDEO, fake_generate_video)
    registry.register("generate_image", MediaKind.IMAGE, fake_generate_image)

    mapping = registry.as_tool_registry(context=context)
    # both are present as callables (the backend, not the map, does the narrowing lookup)
    assert set(mapping) == {"generate_video", "generate_image"}

    # invoking the denied one through the adapter returns a denial string, no artifact
    denied_text = await mapping["generate_video"](prompt="x")
    assert "denied" in denied_text
    allowed_text = await mapping["generate_image"](prompt="x")
    assert "artifact" in allowed_text


# --------------------------------------------------------------------------------------
# 3. Kind enforcement rejects the wrong tool
# --------------------------------------------------------------------------------------


def test_registering_wrong_name_for_kind_is_rejected() -> None:
    """A VIDEO-kind tool may not register under generate_image — fails at registration."""
    registry = MediaToolRegistry(fixed_allowlist=("generate_image", "generate_video"))
    with pytest.raises(MediaToolDenied) as excinfo:
        registry.register("generate_image", MediaKind.VIDEO, fake_generate_image)
    assert excinfo.value.tool == "generate_image"


def test_image_edit_kind_permits_both_edit_and_generate() -> None:
    """Mirrors groot's _KIND_TOOLS: image_edit -> {edit_image, generate_image}."""
    registry = MediaToolRegistry(
        fixed_allowlist=("edit_image", "generate_image")
    )
    # both names accepted under IMAGE_EDIT
    registry.register("edit_image", MediaKind.IMAGE_EDIT, fake_generate_image)
    registry.register("generate_image", MediaKind.IMAGE_EDIT, fake_generate_image)
    assert set(registry.registered_names()) == {"edit_image", "generate_image"}


def test_image_kind_cannot_register_video_tool() -> None:
    registry = MediaToolRegistry(fixed_allowlist=("generate_video",))
    with pytest.raises(MediaToolDenied):
        registry.register("generate_video", MediaKind.IMAGE, fake_generate_video)


# --------------------------------------------------------------------------------------
# 4. A raising tool surfaces as a failed ToolOutcome, not a crash
# --------------------------------------------------------------------------------------


async def test_raising_generator_becomes_failed_outcome(
    context: MediaContext,
) -> None:
    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    registry.register("generate_image", MediaKind.IMAGE, raising_generator)

    invocation = await registry.invoke("generate_image", context=context, args={})

    assert invocation.outcome is ToolOutcome.FAILED
    assert invocation.artifact is None
    assert invocation.error is not None
    assert "RuntimeError" in invocation.error
    assert "503" in invocation.error


async def test_generator_returning_wrong_type_fails_closed(
    context: MediaContext,
) -> None:
    async def bad(**_: object) -> MediaResult:
        return "not a MediaResult"  # type: ignore[return-value]

    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    registry.register("generate_image", MediaKind.IMAGE, bad)
    invocation = await registry.invoke("generate_image", context=context, args={})
    assert invocation.outcome is ToolOutcome.FAILED
    assert invocation.artifact is None


# --------------------------------------------------------------------------------------
# Registration-time guarantees
# --------------------------------------------------------------------------------------


def test_sync_generator_rejected_at_registration() -> None:
    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    with pytest.raises(TypeError):
        registry.register("generate_image", MediaKind.IMAGE, sync_generator)  # type: ignore[arg-type]


async def test_unregistered_name_raises_not_denied(context: MediaContext) -> None:
    """An unregistered name is a wiring bug, not a turn outcome — it raises."""
    registry = MediaToolRegistry(fixed_allowlist=("generate_image",))
    with pytest.raises(MediaToolDenied):
        await registry.invoke("generate_image", context=context, args={})


# --------------------------------------------------------------------------------------
# Identity cannot be overridden by the model
# --------------------------------------------------------------------------------------


async def test_model_supplied_identity_is_stripped_and_replaced(
    fixed_allowlist: tuple[str, ...],
) -> None:
    """The model tries to pass user_id and session; both are stripped and replaced by the
    trusted context. The generator sees only the host's identity."""
    seen: dict[str, object] = {}

    async def capture(**kwargs: object) -> MediaResult:
        seen.update(kwargs)
        return MediaResult(payload_ref="r2://img/x")

    registry = MediaToolRegistry(fixed_allowlist=fixed_allowlist)
    registry.register("generate_image", MediaKind.IMAGE, capture)

    context = MediaContext(
        user_id="real-owner", artifact_id="art-9", session="sess-real"
    )
    await registry.invoke(
        "generate_image",
        context=context,
        args={"prompt": "p", "user_id": "attacker", "session": "sess-forged"},
    )

    assert seen["user_id"] == "real-owner"
    assert seen["session"] == "sess-real"
    assert seen["prompt"] == "p"  # non-identity args pass through untouched


async def test_session_omitted_when_context_has_none(
    fixed_allowlist: tuple[str, ...],
) -> None:
    seen: dict[str, object] = {}

    async def capture(**kwargs: object) -> MediaResult:
        seen.update(kwargs)
        return MediaResult(payload_ref="r2://img/x")

    registry = MediaToolRegistry(fixed_allowlist=fixed_allowlist)
    registry.register("generate_image", MediaKind.IMAGE, capture)
    context = MediaContext(user_id="u", artifact_id="a")  # no session
    await registry.invoke(
        "generate_image", context=context, args={"session": "forged"}
    )
    assert "session" not in seen
    assert seen["user_id"] == "u"


# --------------------------------------------------------------------------------------
# as_tool_registry threads artifacts to a host sink
# --------------------------------------------------------------------------------------


async def test_sink_collects_successful_invocations(
    fixed_allowlist: tuple[str, ...], context: MediaContext
) -> None:
    from pikachu.tools import MediaInvocation

    collected: list[MediaInvocation] = []
    registry = MediaToolRegistry(fixed_allowlist=fixed_allowlist)
    registry.register("generate_image", MediaKind.IMAGE, fake_generate_image)

    mapping = registry.as_tool_registry(context=context, sink=collected.append)
    await mapping["generate_image"](prompt="a cat")

    assert len(collected) == 1
    assert collected[0].outcome is ToolOutcome.SUCCESS
    assert collected[0].artifact is not None
    assert collected[0].artifact.payload_ref == "r2://img/a cat"
