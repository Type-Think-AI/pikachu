#!/usr/bin/env python
"""Author a skill, give it a tool, run a turn — the whole loop, readable top to bottom.

This is a teaching artifact. Read it to learn the shape of the Pikachu API:

    1. Author a SKILL.md (frontmatter + body) and load it.
    2. Write a plain Python function — its docstring becomes the tool description.
    3. Build an agent whose fixed allowlist decides what the skill may actually use.
    4. Run one turn and read the result.

By default this runs entirely OFFLINE on FakeBackend: deterministic, no network, no
model, no money. Pass --live to run the same turn against the real model — that path is
guarded so it never fires in CI (the offline test suite's socket block would fail it).

    .venv/bin/python examples/skill_with_tools.py           # offline, deterministic
    .venv/bin/python examples/skill_with_tools.py --live    # real model (needs a key)

The point the example makes: authority comes from the agent's allowlist, never from the
skill. The skill can *ask* for a tool; the allowlist decides. Change ALLOWLIST below to
() and re-run — the tool vanishes and the turn still completes, degraded not crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pikachu import AgentSpec, Run, SkillStatus, ToolSpec, TrustTier, TurnRequest
from pikachu.backends.fake import FakeBackend, ScriptedToolCall, ScriptedTurn
from pikachu.guard import effective_tools
from pikachu.skills.loader import load_metadata, load_skill

# --------------------------------------------------------------------------------------
# 1. Author a skill. This is exactly what lands in a SKILL.md file on disk — frontmatter
#    (the catalogue-visible metadata) then a body (the instructions the model reads).
# --------------------------------------------------------------------------------------

COLOURIST_SKILL = """\
---
name: colourist-palette
description: Grade stills to the house palette, using brand_palette for the exact hex values.
allowed-tools:
  - brand_palette
---

# Colourist palette

You are a colourist. Call the `brand_palette` tool for the exact house colours and quote
the hex codes it returns — never invent a colour. Never use pure black; use the ink colour
the tool reports. If the tool is unavailable, grade to a neutral palette and say so.
"""

# The host's fixed allowlist. THIS is the only source of tool authority. Set it to () and
# re-run to watch the guard narrow the skill's declared tool away — the turn still runs.
ALLOWLIST: tuple[str, ...] = ("brand_palette",)


# --------------------------------------------------------------------------------------
# 2. A declarative function tool. The function name is the tool name; the docstring is the
#    description the model sees. Keep the docstring a crisp instruction — it is prompt.
# --------------------------------------------------------------------------------------


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "Brand palette: ink #101014, bone #F4F1EA, signal amber #FFB300. Never pure black."


# --------------------------------------------------------------------------------------
# 3. Progressive disclosure: a catalogue lists 400 skills by reading only frontmatter,
#    never a single body. load_metadata proves it — there is no body to read here.
# --------------------------------------------------------------------------------------


def show_metadata_is_cheap() -> None:
    meta = load_metadata(COLOURIST_SKILL)
    print(f"  catalogue sees: {meta.name!r} — declares {meta.declared_tools}")
    assert not hasattr(meta, "body"), "metadata must not carry the body"


# --------------------------------------------------------------------------------------
# 4. The offline run — the whole loop on a deterministic FakeBackend.
# --------------------------------------------------------------------------------------


async def run_offline() -> None:
    print("OFFLINE run (FakeBackend, deterministic, no network)\n")
    show_metadata_is_cheap()

    # Full load: frontmatter + body -> a Skill. A user promotes it to ACTIVE to use it.
    skill = load_skill(COLOURIST_SKILL, trust=TrustTier.BUILTIN, source="example").model_copy(
        update={"status": SkillStatus.ACTIVE}
    )

    agent = AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Use brand_palette for exact hex values. Never invent a colour.",
        allowed_tools=ALLOWLIST,
    )

    # The guard narrows: effective = fixed allowlist ∩ declared, dangerous stripped.
    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)
    print(f"  guard: declared {skill.declared_tools} ∩ allowlist {agent.allowed_tools}")
    print(f"       → effective {narrowed.tools}"
          + (f"  (removed {narrowed.removed_tools})" if narrowed.removed_tools else ""))

    tool = ToolSpec(name="brand_palette", description="House palette.", cost_credits=0)
    run = Run(id="example-run", agent_name=agent.name, max_iterations=20)

    if narrowed.tools:
        # The tool is granted: script the model to call it, then answer from its output.
        script = [
            ScriptedTurn(tool_calls=(ScriptedToolCall("brand_palette"),)),
            ScriptedTurn(text="The signal colour is amber #FFB300, per brand_palette."),
        ]
    else:
        # Degraded: the tool was narrowed away. The skill body says fall back gracefully.
        script = [ScriptedTurn(text="No palette tool available — grading to a neutral scheme.")]

    backend = FakeBackend(script, run=run, tools=(tool,))
    request = TurnRequest(
        message="What is our signal colour?",
        agent=agent,
        skill=skill,
        effective_tools=narrowed.tools,
        run_id=run.id,
    )
    result = await backend.run_turn(request)

    called = [c["tool"] for c in result.tool_calls]
    print(f"\n  tools called : {called or '(none — degraded)'}")
    print(f"  answer       : {result.text}")
    print(f"  iterations   : {result.iterations}")


# --------------------------------------------------------------------------------------
# 5. The live run — same skill, same tool, real model. Guarded so CI never calls it.
# --------------------------------------------------------------------------------------


async def run_live() -> int:
    from pikachu.config import DEFAULT_MODEL, get_api_key

    key = get_api_key()
    if not key:
        print("--live needs OPENROUTER_API_KEY in the environment or a .env — skipping.")
        return 1

    from pikachu.backends.pydantic_ai import PydanticAIBackend

    print(f"LIVE run against {DEFAULT_MODEL} (real network, costs a fraction of a cent)\n")
    skill = load_skill(COLOURIST_SKILL, trust=TrustTier.BUILTIN, source="example").model_copy(
        update={"status": SkillStatus.ACTIVE}
    )
    agent = AgentSpec(
        name="colourist",
        instructions="Use brand_palette for exact hex values. Never invent a colour.",
        allowed_tools=ALLOWLIST,
    )
    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)

    backend = PydanticAIBackend(api_key=key, tool_registry={"brand_palette": brand_palette})
    try:
        result = await backend.run_turn(
            TurnRequest(
                message="Call brand_palette and tell me the signal colour hex.",
                agent=agent,
                skill=skill,
                effective_tools=narrowed.tools,
            )
        )
    finally:
        await backend.aclose()

    called = [c["tool"] for c in result.tool_calls]
    print(f"  served by    : {result.served_by or '(not reported)'}")
    print(f"  tools called : {called or '(none)'}")
    print(f"  answer       : {result.text.strip()}")
    print(f"  timing       : framework {result.timing.framework_ms} ms · "
          f"model {result.timing.model_ms} ms")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run against the real model")
    args = parser.parse_args()
    if args.live:
        return asyncio.run(run_live())
    asyncio.run(run_offline())
    return 0


if __name__ == "__main__":
    sys.exit(main())
