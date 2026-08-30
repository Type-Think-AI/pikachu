#!/usr/bin/env python
"""Round 3 — LIVE tool-calling behaviour against the real model. A handful of turns, costed.

This is the artifact behind the round-3 verdict: does live tool-calling behaviour match what
``FakeBackend`` claims offline? The fake can only prove *plumbing* — a scripted call is
authorized, threaded and metered — because a scripted call's tool name is fixed at authoring
time. It cannot prove *selection*: that the model, given a skill body and a set of tool
schemas, CHOOSES to call the right tool. Only a real model can show that, so this script runs
three live probes and prints exactly what happened.

    1. SKILL-WITH-TOOLS (2 turns): the colourist skill body says "call brand_palette for
       colour", the agent is granted brand_palette, and the task asks a colour question. Did
       the model call the tool and quote its #FFB300? Then the SAME task with the tool removed
       from the allowlist — does it degrade (answer without the tool) rather than crash?
    2. DECLARATIVE TOOL, UNPROMPTED (1 turn): a plain Python function `shot_count` is offered
       with only its docstring, and the task needs a shot count. The prompt does NOT say "use
       a tool". Did the model choose to call it on its own?
    4. CACHE, ONE HONEST LOOK (2 turns): the same full-size prefix run twice, reading
       cache_read_tokens. Confirms the S1 negative reproduces, with the Google-implicit-caching
       caveat restated.

Total live turns: 5 (cap is 6). Every turn prints setup/wait/stream/finalize, tokens,
served_by, and a running cost estimate. Safe to re-run: pure, side-effect-free tools; no
retries beyond the backend's own; a missing key exits cleanly with an UNMEASURED verdict.

    .venv/bin/python scripts/round3_live.py            # needs OPENROUTER_API_KEY (env or .env)
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest  # noqa: E402
from pikachu.config import (  # noqa: E402
    CACHE_FLOOR_UNVERIFIED,
    DEFAULT_MODEL,
    STABLE_PREFIX_TOKENS_MAX,
    STABLE_PREFIX_TOKENS_MIN,
    get_api_key,
)

# OpenRouter published $/MTok for the default model (config.py records these). Estimate only —
# nothing here bills anyone.
_PROMPT_PER_MTOK = 0.75
_COMPLETION_PER_MTOK = 3.75
_CACHE_READ_PER_MTOK = 0.075
_BYTES_PER_TOKEN = 4

# The palette the colourist tool returns; the model must quote this hex if it truly called it.
HOUSE_AMBER = "#FFB300"


# ---------------------------------------------------------------------------------------
# The tools offered live. Pure and side-effect-free on purpose — a live probe that mutates
# something outside itself is one nobody runs twice.
# ---------------------------------------------------------------------------------------


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "Brand palette: ink #101014, bone #F4F1EA, signal amber #FFB300. Never pure black."


def shot_count(scene_description: str) -> int:
    """Return how many distinct camera shots a scene of this description should be broken into."""
    return max(1, min(6, len(scene_description.split()) // 4))


def estimate_cost_usd(*, input_tokens: int, output_tokens: int, cache_read_tokens: int) -> float:
    """Dollar estimate for one turn from OpenRouter's published pricing.

    Pure arithmetic, unit-tested offline in ``tests/round_3`` so a wrong cost is caught before
    any money is spent. Cache reads bill at the discounted read rate, not the prompt rate.
    """
    return (
        input_tokens * _PROMPT_PER_MTOK
        + output_tokens * _COMPLETION_PER_MTOK
        + cache_read_tokens * _CACHE_READ_PER_MTOK
    ) / 1_000_000


# ---------------------------------------------------------------------------------------
# Outcome the caller (and the live pytest test) can assert against.
# ---------------------------------------------------------------------------------------


@dataclass
class SkillToolOutcome:
    """Result of the skill-with-tools live probe."""

    called_brand_palette: bool
    text: str
    tool_calls: tuple[dict[str, Any], ...]
    served_by: str
    iterations: int
    framework_ms: int
    model_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


def _colourist_skill() -> Skill:
    """Author the colourist palette skill: body declares the house palette lives in a tool."""
    return Skill(
        name="house-colourist",
        description="Grade every generated frame to the house colour palette.",
        body=(
            "# House colourist\n\n"
            "You grade frames to the house look. The canonical palette is NOT in your head — "
            "it is returned by the `brand_palette` tool, and it is the only authority on "
            "colour. Whenever a colour question arises, CALL `brand_palette` and quote its hex "
            "values exactly. Never invent a palette."
        ),
        declared_tools=("brand_palette",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )


def _print_turn(label: str, result: Any) -> None:
    """One turn's phase split, tokens, served_by, tool calls."""
    t = result.timing
    split = "" if t.streaming_measured else "  (wait/stream split unavailable)"
    calls = ", ".join(c.get("tool", "?") for c in result.tool_calls) or "—"
    print(f"\n  · {label}")
    print(f"      served_by : {result.served_by or '(gateway did not say)'}")
    print(f"      iterations: {result.iterations}   tool_calls: {calls}")
    print(
        f"      timing    : framework {t.framework_ms}ms · model {t.model_ms}ms "
        f"(setup {t.setup_ms} · wait {t.wait_ms} · stream {t.stream_ms} · "
        f"finalize {t.finalize_ms}){split}"
    )
    print(
        f"      tokens    : in {result.input_tokens} · out {result.output_tokens} · "
        f"cache_read {result.cache_read_tokens} · cache_write {result.cache_write_tokens}"
    )
    est = estimate_cost_usd(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )
    print(f"      est cost  : ${est:.6f}")


def _turn_cost(result: Any) -> float:
    return estimate_cost_usd(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )


async def run_skill_with_tools_live(api_key: str) -> SkillToolOutcome:
    """Task 1, granted case — used by the live pytest test too. Returns a decidable outcome."""
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(
        api_key=api_key, tool_registry={"brand_palette": brand_palette}
    )
    try:
        req = TurnRequest(
            message="What is the house signal amber colour? Answer with its hex value.",
            agent=AgentSpec(
                name="house-colourist",
                instructions="You are the house colourist. Answer in one short sentence.",
                allowed_tools=("brand_palette",),
            ),
            skill=_colourist_skill(),
            effective_tools=("brand_palette",),
            run_id="run:r3-skill-granted",
        )
        result = await backend.run_turn(req)
    finally:
        await backend.aclose()

    called = any(c.get("tool") == "brand_palette" for c in result.tool_calls)
    return SkillToolOutcome(
        called_brand_palette=called,
        text=result.text,
        tool_calls=result.tool_calls,
        served_by=result.served_by,
        iterations=result.iterations,
        framework_ms=result.timing.framework_ms,
        model_ms=result.timing.model_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )


async def _task1_skill_with_tools(api_key: str, budget: dict[str, float]) -> None:
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    print("=" * 78)
    print("TASK 1 — SKILL-WITH-TOOLS, live tool SELECTION")
    print("=" * 78)
    print("  Q: does the model actually CALL brand_palette and quote its #FFB300,")
    print("     given only a skill body that tells it to? (not scripted — its choice)")

    # 1a: granted.
    granted = await run_skill_with_tools_live(api_key)
    budget["turns"] += 1
    shim = _Shim(granted)
    _print_turn("1a granted — brand_palette on the allowlist", shim)
    budget["cost"] += _turn_cost(shim)
    print(
        f"      => called brand_palette: {granted.called_brand_palette}   "
        f"quoted {HOUSE_AMBER}: {HOUSE_AMBER in granted.text}"
    )
    print(f"      => answer: {granted.text.strip()[:200]}")

    # 1b: degraded — same task, tool removed from the allowlist.
    backend = PydanticAIBackend(api_key=api_key, tool_registry={"brand_palette": brand_palette})
    try:
        req = TurnRequest(
            message="What is the house signal amber colour? Answer with its hex value.",
            agent=AgentSpec(
                name="house-colourist",
                instructions="You are the house colourist. Answer in one short sentence.",
                allowed_tools=("generate_image",),  # brand_palette NOT granted
            ),
            skill=_colourist_skill(),
            effective_tools=(),  # guard would narrow to nothing colour-relevant
            run_id="run:r3-skill-degraded",
        )
        degraded = await backend.run_turn(req)
    finally:
        await backend.aclose()
    budget["turns"] += 1
    _print_turn("1b degraded — brand_palette REMOVED from the allowlist", degraded)
    budget["cost"] += _turn_cost(degraded)
    called_degraded = any(c.get("tool") == "brand_palette" for c in degraded.tool_calls)
    print(
        f"      => called brand_palette: {called_degraded} (must be False) · "
        f"completed without crashing: {bool(degraded.text)}"
    )
    print(f"      => answer: {degraded.text.strip()[:200]}")


async def _task2_declarative_unprompted(api_key: str, budget: dict[str, float]) -> None:
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    print("\n" + "=" * 78)
    print("TASK 2 — DECLARATIVE FUNCTION TOOL, chosen UNPROMPTED")
    print("=" * 78)
    print("  Q: given a plain Python function `shot_count` (docstring only) and a task that")
    print("     needs it, does the model call it WITHOUT being told to? plumbing vs behaviour.")

    backend = PydanticAIBackend(api_key=api_key, tool_registry={"shot_count": shot_count})
    try:
        req = TurnRequest(
            message=(
                "A wide desert highway at dusk, a lone motorcyclist rides toward a distant "
                "storm as the sky turns amber. How many camera shots should this scene be "
                "broken into? Give just the number."
            ),
            agent=AgentSpec(
                name="director",
                instructions="You are a film director. Be concise.",
                allowed_tools=("shot_count",),
            ),
            effective_tools=("shot_count",),
            run_id="run:r3-declarative",
        )
        result = await backend.run_turn(req)
    finally:
        await backend.aclose()
    budget["turns"] += 1
    _print_turn("2 declarative — shot_count offered, not requested", result)
    budget["cost"] += _turn_cost(result)
    called = any(c.get("tool") == "shot_count" for c in result.tool_calls)
    print(f"      => chose to call shot_count UNPROMPTED: {called}")
    print(f"      => answer: {result.text.strip()[:200]}")


def _full_size_prefix_skill() -> tuple[AgentSpec, Skill, int]:
    """A realistic full-size prefix in the STABLE_PREFIX band, mirroring measure_cache.py."""
    target_bytes = STABLE_PREFIX_TOKENS_MIN * _BYTES_PER_TOKEN
    rules = [
        "Signal amber is #FFB300; ink is #101014; bone is #F4F1EA. Never use pure black.",
        "Never crop tighter than 16:9. Keep 8% safe margin on every edge.",
        "Skin tones stay within the Rec.709 gamut; do not push saturation past 1.15x.",
        "Establish with a wide shot; reserve close-ups for emotional beats only.",
        "Match cut on motion, never on a static frame.",
        "Grade shadows toward ink, highlights toward bone; midtones stay neutral.",
        "Reject any frame with clipped highlights above 2% of the histogram.",
        "Continuity: a prop introduced in a wide must persist into its close-up.",
    ]
    body = (
        "# House cinematography style\n\n"
        "Apply the house look to every generated frame. These rules are non-negotiable.\n\n"
    )
    i = 0
    while len(body.encode("utf-8")) < target_bytes:
        body += f"{i + 1}. {rules[i % len(rules)]}\n"
        i += 1
    body += (
        "\n\n## Tools available this turn\n"
        "generate_image(prompt, seed, aspect) -> ImageRef — render a still to the house look.\n"
        "read_canvas(artifact_id) -> Artifact — read an artifact from the shared board.\n"
        "write_canvas(kind, payload_ref, parent) -> Artifact — append an immutable artifact.\n"
    )
    agent = AgentSpec(
        name="house-colourist",
        role="Grade every frame to the house look.",
        instructions=(
            "You are the house colourist. Apply the style below to everything you produce. "
            "Answer in one short sentence."
        ),
        allowed_tools=("generate_image", "read_canvas", "write_canvas"),
    )
    skill = Skill(
        name="house-cinematography",
        description="The complete house cinematography style.",
        body=body,
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )
    est_tokens = len((agent.instructions + "\n\n" + body).encode("utf-8")) // _BYTES_PER_TOKEN
    return agent, skill, est_tokens


async def _task4_cache(api_key: str, budget: dict[str, float]) -> None:
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    print("\n" + "=" * 78)
    print("TASK 4 — CACHE, one honest look (trimmed to 2 turns to save money)")
    print("=" * 78)
    agent, skill, est_tokens = _full_size_prefix_skill()
    print(
        f"  full-size prefix ~{est_tokens} tokens "
        f"(band {STABLE_PREFIX_TOKENS_MIN}-{STABLE_PREFIX_TOKENS_MAX}, ~{_BYTES_PER_TOKEN}:1 est)"
    )
    print(f"  CACHE_FLOOR_UNVERIFIED is currently {CACHE_FLOOR_UNVERIFIED} in config.py")
    print("  Running the SAME turn twice — turn 2 is the one that could read a cache write.")

    backend = PydanticAIBackend(api_key=api_key)
    reads: list[int] = []
    try:
        req = TurnRequest(
            message="Name the three primary additive colours.",
            agent=agent,
            skill=skill,
            effective_tools=(),
            run_id="run:r3-cache",
        )
        for n in (1, 2):
            result = await backend.run_turn(req)
            budget["turns"] += 1
            budget["cost"] += _turn_cost(result)
            reads.append(result.cache_read_tokens)
            _print_turn(f"4 turn {n} — identical full-size prefix", result)
    finally:
        await backend.aclose()

    read_after_first = reads[1] if len(reads) > 1 else 0
    print("\n  S1 cache verdict:")
    if max(reads) > 0:
        print(f"      FIRED — cache_read_tokens reached {max(reads)} "
              f"({read_after_first} on turn 2). S1 MET on a full-size prefix.")
    else:
        print("      DID NOT FIRE — cache_read_tokens stayed 0 on both turns.")
        print("      ★ CAVEAT: Google implicit caching can report 0 cache_read_tokens even when")
        print("        the cache DID fire (pydantic-ai #5205). This 0 is SUGGESTIVE, not")
        print("        conclusive — confirm against an OpenTelemetry span before concluding the")
        print("        ~4096-token floor is the cause. The S1 negative REPRODUCES.")


async def main() -> int:
    key = get_api_key()
    print("\n" + "#" * 78)
    print(f"# ROUND 3 — LIVE tool-calling behaviour · model = {DEFAULT_MODEL}")
    print("#" * 78)
    if not key:
        print("\nNo OPENROUTER_API_KEY (env or .env). Cannot run a live measurement.")
        print("VERDICT: UNMEASURED — no credential. Configure a key and re-run.")
        return 2

    budget: dict[str, float] = {"turns": 0, "cost": 0.0}
    await _task1_skill_with_tools(key, budget)
    await _task2_declarative_unprompted(key, budget)
    await _task4_cache(key, budget)

    print("\n" + "#" * 78)
    print(f"# TOTAL — {int(budget['turns'])} live turns · estimated cost ${budget['cost']:.6f}")
    print("#" * 78)
    if budget["turns"] > 6:
        print("  WARNING: exceeded the 6-turn cap for this round.")
    return 0


# A tiny shim so _print_turn (which expects a TurnResult-shaped object with .timing) can print
# the SkillToolOutcome returned by run_skill_with_tools_live, whose split is already flattened.
@dataclass
class _ShimTiming:
    framework_ms: int
    model_ms: int
    setup_ms: int = 0
    wait_ms: int = 0
    stream_ms: int = 0
    finalize_ms: int = 0
    streaming_measured: bool = True


@dataclass
class _Shim:
    _o: SkillToolOutcome
    timing: _ShimTiming = field(init=False)
    tool_calls: tuple[dict[str, Any], ...] = field(init=False)
    served_by: str = field(init=False)
    iterations: int = field(init=False)
    input_tokens: int = field(init=False)
    output_tokens: int = field(init=False)
    cache_read_tokens: int = field(init=False)
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        o = self._o
        # Report model_ms as wait when we do not carry the finer split back.
        self.timing = _ShimTiming(
            framework_ms=o.framework_ms, model_ms=o.model_ms, wait_ms=o.model_ms
        )
        self.tool_calls = o.tool_calls
        self.served_by = o.served_by
        self.iterations = o.iterations
        self.input_tokens = o.input_tokens
        self.output_tokens = o.output_tokens
        self.cache_read_tokens = o.cache_read_tokens


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
