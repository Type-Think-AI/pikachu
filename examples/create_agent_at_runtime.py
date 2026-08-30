#!/usr/bin/env python
"""S4 — an end user creates an agent at runtime and invokes it by name.

    S4: An end user creates an agent in the UI and invokes it, with no code change and no
        deploy.

This is the persona bet of the whole SDK: the person who creates an agent is P2, *a user of
a product built on this library*, not P1 the developer editing a repo. So this example does
what that user's click would do behind the UI:

  * build an :class:`~pikachu.AgentSpec` from the six declarative fields — name, role,
    instructions, skill_tags, allowed_tools, triggers — as plain data;
  * :meth:`~pikachu.discovery.registry.AgentRegistry.create` it into a live registry;
  * :meth:`~pikachu.discovery.registry.AgentRegistry.get` it back **by name** and run a turn.

No subclass. No decorator. No new module imported to make the agent exist. That last part is
the machine-checkable claim S4 rests on, and ``tests/test_examples.py`` asserts it by proving
the agent is created and invoked without importing any agent-specific module — there is no
``agents/colourist.py`` to import, because the agent is data.

A **second** agent is created with a DIFFERENT ``skill_tags`` partition, so the partition
boundary is visible: ``colourist`` selects from the colour partition, ``retoucher`` from the
retouch partition, and the two selectable sets do not overlap. The partition is a correctness
mechanism (it keeps each agent's selectable set below the confusability cliff), not tidiness.

    .venv/bin/python examples/create_agent_at_runtime.py            # offline, FakeBackend
    .venv/bin/python examples/create_agent_at_runtime.py --live     # real model
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, TurnRequest  # noqa: E402
from pikachu.backends.fake import FakeBackend, ScriptedTurn  # noqa: E402
from pikachu.discovery.registry import AgentRegistry  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402


def user_creates_colourist() -> AgentSpec:
    """What the product's UI form produces when a user defines a 'Colourist' agent.

    Purely declarative data. Nothing here is code the user wrote or a class they subclassed.
    """
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Match the brand palette. Never invent a new palette.",
        skill_tags=("colour", "grade"),  # <- the colour partition
        allowed_tools=("generate_image", "read_canvas"),
        triggers=("grade", "colour match"),
    )


def user_creates_retoucher() -> AgentSpec:
    """A second user-created agent in a DIFFERENT partition, to make the boundary visible."""
    return AgentSpec(
        name="retoucher",
        role="Clean plates and remove blemishes.",
        instructions="Remove artefacts. Never alter composition.",
        skill_tags=("retouch", "cleanup"),  # <- a disjoint partition from the colourist
        allowed_tools=("generate_image",),
        triggers=("retouch", "clean up"),
    )


async def invoke_by_name(
    registry: AgentRegistry, name: str, backend: FakeBackend, message: str
) -> str:
    """Look an agent up BY NAME and run one turn. No reference to the concrete agent in code.

    The only handle used is the string ``name`` — exactly what a UI would pass. The registry
    returns the spec, the guard narrows its tools, the backend runs the turn.
    """
    spec = registry.get(name)
    allowed = effective_tools(spec.allowed_tools, spec.allowed_tools)
    request = TurnRequest(
        message=message,
        agent=spec,
        effective_tools=allowed.tools,
        run_id=f"run:{name}",
    )
    result = await backend.run_turn(request)
    return result.text


async def run(*, live: bool = False) -> int:
    registry = AgentRegistry()

    print("=" * 70)
    print("S4 — a user creates an agent at runtime and invokes it by name, no code change")
    print("=" * 70)

    # 1. The user creates two agents. This is the entire "deploy": a create() call.
    colourist = registry.create(user_creates_colourist())
    retoucher = registry.create(user_creates_retoucher())
    print(f"\n1. created (no restart, no deploy): {[s.name for s in registry.list()]}")

    # 2. The partition boundary, made visible.
    print("\n2. partitions are disjoint (each agent's selectable set is separate):")
    print(f"   colourist.skill_tags = {colourist.skill_tags}")
    print(f"   retoucher.skill_tags = {retoucher.skill_tags}")
    overlap = set(colourist.skill_tags) & set(retoucher.skill_tags)
    print(f"   overlap = {overlap or '∅'}   (empty => the confusability boundary holds)")

    # 3. Invoke each BY NAME.
    if live:
        from pikachu.config import get_api_key

        key = get_api_key()
        if not key:
            print("\n--live requested but no OPENROUTER_API_KEY found; aborting.")
            return 2
        from pikachu.backends.pydantic_ai import PydanticAIBackend

        backend: FakeBackend | PydanticAIBackend = PydanticAIBackend(api_key=key)
    else:
        backend = FakeBackend(
            [ScriptedTurn(text="graded to the house palette")]
        )

    try:
        colour_out = await invoke_by_name(
            registry, "colourist", backend, "Grade this rooftop still."  # type: ignore[arg-type]
        )
        print(f"\n3. invoked 'colourist' by name -> {colour_out!r}")

        # A fresh scripted backend for the second turn offline (each FakeBackend runs one
        # scripted turn); live reuses the same backend.
        if not live:
            backend = FakeBackend([ScriptedTurn(text="retouched the plate")])
        retouch_out = await invoke_by_name(
            registry, "retoucher", backend, "Clean this plate."  # type: ignore[arg-type]
        )
        print(f"   invoked 'retoucher'  by name -> {retouch_out!r}")

        ok = bool(colour_out) and bool(retouch_out) and not overlap
        print("\n" + ("PASS — S4 holds: agents created as data and invoked by name."
                      if ok else "FAIL — S4 property not satisfied."))
        return 0 if ok else 1
    finally:
        aclose = getattr(backend, "aclose", None)
        if aclose is not None:
            await aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run against the real model")
    args = parser.parse_args()
    return asyncio.run(run(live=args.live))


if __name__ == "__main__":
    raise SystemExit(main())
