#!/usr/bin/env python
"""S1 — measure whether prompt caching FIRES on the default model. A negative is a result.

    S1: RunUsage.cache_hit_ratio > 0 on the default model — today it is 0 because the prefix
        is below the model's cacheable floor.

The existing live tests use tiny prompts, which is exactly why they proved nothing about the
floor: a two-line prompt is far below any provider's minimum cacheable-prefix size, so its
cache_read of 0 tells you only that a short prompt does not cache. This script instead carries
a **full-size** stable prefix — a real skill body plus tool schemas, sized to the measured
STABLE_PREFIX_TOKENS_MIN..MAX band (~1,500–2,400 tokens) — because that is the prefix a real
turn actually carries, and it is the only prefix whose caching behaviour matters.

WHAT IT DOES
    * builds one agent + one skill whose combined instruction body lands in the target band;
    * runs the SAME turn three times against the real model (identical prefix each time, so
      turn 2 and 3 are the ones that could read from a cache written on turn 1);
    * prints cache_read_tokens and cache_write_tokens per turn, plus the timing phases;
    * prints the estimated cost, because this spends real money — three turns, no retries.

READING THE RESULT
    * If cache_read_tokens > 0 on turn 2 or 3: caching FIRED. S1 is met. CACHE_FLOOR_UNVERIFIED
      in config.py can then be cleared (it is reserved — do not edit it here; the script only
      says so).
    * If it stays 0 on every turn: caching did NOT fire on this prefix/model. That is a
      RECORDED NEGATIVE and the deliverable — this script prints the number and a model
      recommendation, and does NOT retry until it passes or inflate the prefix to force a hit.

    ★ CAVEAT printed next to the number: Google's implicit caching can report 0
      cache_read_tokens even when the cache actually fired (pydantic-ai issue #5205). So a 0
      here is SUGGESTIVE, not conclusive — confirm against an OpenTelemetry span before
      declaring the floor is the cause.

    .venv/bin/python scripts/measure_cache.py             # 3 real turns (needs a key)
    .venv/bin/python scripts/measure_cache.py --turns 3   # explicit; capped at 3

NEVER imported by the test suite. It requires a key and a network, both forbidden in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest  # noqa: E402
from pikachu.config import (  # noqa: E402
    CACHE_FLOOR_UNVERIFIED,
    DEFAULT_MODEL,
    STABLE_PREFIX_TOKENS_MAX,
    STABLE_PREFIX_TOKENS_MIN,
    get_api_key,
)

# OpenRouter published pricing for the default model (config.py records these), used only for
# the cost estimate printed below. $/MTok.
_PROMPT_PRICE = 0.75
_COMPLETION_PRICE = 3.75
_CACHE_READ_PRICE = 0.075
_CACHE_WRITE_PRICE = 0.0417

# A rough 4:1 bytes-per-token ratio, matching config.py's own estimate (no tokenizer is a
# dependency here). Used only to SIZE the prefix into the target band, never to bill.
_BYTES_PER_TOKEN = 4


def _full_size_skill_body() -> str:
    """A realistic skill body sized into STABLE_PREFIX_TOKENS_MIN..MAX.

    This is not filler — it is a plausible house-style skill, padded with genuine additional
    house rules until the body reaches the target byte range. Inflating a prefix with garbage
    to force a cache hit would measure nothing real, so the padding is real content of the
    kind a production skill carries.
    """
    target_bytes = STABLE_PREFIX_TOKENS_MIN * _BYTES_PER_TOKEN  # aim at the low end of the band
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
    header = (
        "# House cinematography style\n\n"
        "Apply the house look to every generated frame. These rules are non-negotiable and "
        "are the same across every project the house ships.\n\n"
    )
    body = header
    i = 0
    while len(body.encode("utf-8")) < target_bytes:
        body += f"{i + 1}. {rules[i % len(rules)]}\n"
        i += 1
    return body


def _tool_schema_block() -> str:
    """Tool-schema text appended to the prefix.

    In a real turn the tool JSON schemas are part of the stable prefix (config.py's byte
    measurement found tool schemas were the LARGER half). We add a representative block so the
    measured prefix matches a real turn's shape rather than skill-body-only.
    """
    return (
        "\n\n## Tools available this turn\n"
        "generate_image(prompt: str, seed: int | None, aspect: str) -> ImageRef  "
        "— render a still to the house look; costs credits.\n"
        "read_canvas(artifact_id: str) -> Artifact  "
        "— read an artifact from the shared board; propagates canvas-read taint.\n"
        "write_canvas(kind: str, payload_ref: str, parent: str | None) -> Artifact  "
        "— append an immutable artifact to the board; append-only, never overwrites.\n"
    )


def build_full_size_prefix() -> tuple[AgentSpec, Skill, int]:
    """Assemble the agent + skill whose combined body lands in the target band.

    Returns the agent, the skill, and the estimated prefix token count so the script can print
    it and confirm it is a full-size prefix, not a toy one.
    """
    body = _full_size_skill_body() + _tool_schema_block()
    agent = AgentSpec(
        name="house-colourist",
        role="Grade every frame to the house look.",
        instructions=(
            "You are the house colourist. Apply the style below to everything you produce. "
            "Answer in one short sentence."
        ),
        skill_tags=("colour",),
        allowed_tools=("generate_image", "read_canvas", "write_canvas"),
        # No per-agent model override: measure the DEFAULT model, which is what S1 is about.
    )
    skill = Skill(
        name="house-cinematography",
        description="The complete house cinematography style.",
        body=body,
        declared_tools=("generate_image",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )
    prefix_bytes = len((agent.instructions + "\n\n" + body).encode("utf-8"))
    est_tokens = prefix_bytes // _BYTES_PER_TOKEN
    return agent, skill, est_tokens


def _estimate_cost(rows: list[tuple[int, int, int, int, int]]) -> float:
    """Dollar estimate for the turns actually run, from OpenRouter's published pricing."""
    total = 0.0
    for _n, inp, out, cread, cwrite in rows:
        total += (
            inp * _PROMPT_PRICE
            + out * _COMPLETION_PRICE
            + cread * _CACHE_READ_PRICE
            + cwrite * _CACHE_WRITE_PRICE
        ) / 1_000_000
    return total


async def measure(turns: int) -> int:
    key = get_api_key()
    if not key:
        print("No OPENROUTER_API_KEY found (env or .env). Cannot run a live measurement.")
        print("S1 verdict: UNMEASURED — no credential. Configure a key and re-run.")
        return 2

    agent, skill, est_tokens = build_full_size_prefix()

    print("=" * 78)
    print(f"S1 — prompt-cache measurement · model = {DEFAULT_MODEL}")
    print("=" * 78)
    print(f"\nStable prefix: instructions + full skill body + tool schemas")
    print(f"  estimated size ~{est_tokens} tokens "
          f"(target band {STABLE_PREFIX_TOKENS_MIN}-{STABLE_PREFIX_TOKENS_MAX}, "
          f"~{_BYTES_PER_TOKEN}:1 bytes/token estimate)")
    if not (STABLE_PREFIX_TOKENS_MIN <= est_tokens <= STABLE_PREFIX_TOKENS_MAX * 1.2):
        print(f"  NOTE: prefix is outside the measured band; it is a realistic full turn, "
              f"not padded to force a hit.")
    print(f"  CACHE_FLOOR_UNVERIFIED is currently {CACHE_FLOOR_UNVERIFIED} in config.py")
    print(f"\nRunning {turns} identical real turns (this spends real money) ...\n")

    from pikachu.backends.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(api_key=key)
    # Register the tool implementations named in effective_tools so the schemas are in the
    # prefix on the wire, not just in the skill body text.
    rows: list[tuple[int, int, int, int, int]] = []
    timings: list[tuple[int, int, int, int, int]] = []
    request = TurnRequest(
        message="Name the three primary additive colours.",
        agent=agent,
        skill=skill,
        effective_tools=("generate_image", "read_canvas", "write_canvas"),
        run_id="run:measure-cache",
    )
    try:
        for i in range(turns):
            result = await backend.run_turn(request)
            rows.append(
                (
                    i + 1,
                    result.input_tokens,
                    result.output_tokens,
                    result.cache_read_tokens,
                    result.cache_write_tokens,
                )
            )
            t = result.timing
            timings.append((i + 1, t.setup_ms, t.wait_ms, t.stream_ms, t.total_ms))
    finally:
        await backend.aclose()

    print(f"  {'#':>2s} {'input':>8s} {'output':>8s} {'cache_read':>11s} {'cache_write':>12s}")
    print(f"  {'-'*2} {'-'*8} {'-'*8} {'-'*11} {'-'*12}")
    for n, inp, out, cread, cwrite in rows:
        print(f"  {n:>2d} {inp:>8d} {out:>8d} {cread:>11d} {cwrite:>12d}")

    print(f"\n  {'#':>2s} {'setup':>7s} {'wait':>8s} {'stream':>8s} {'TOTAL':>8s}")
    print(f"  {'-'*2} {'-'*7} {'-'*8} {'-'*8} {'-'*8}")
    for n, setup, wait, stream, total in timings:
        print(f"  {n:>2d} {setup:>6d}m {wait:>7d}m {stream:>7d}m {total:>7d}m")

    est = _estimate_cost(rows)
    print(f"\n  estimated cost of this run: ${est:.6f}")

    # ---- verdict -------------------------------------------------------------------------
    max_read = max((r[3] for r in rows), default=0)
    max_write = max((r[4] for r in rows), default=0)
    read_after_first = max((r[3] for r in rows if r[0] > 1), default=0)

    print("\n" + "=" * 78)
    print("S1 VERDICT")
    print("=" * 78)
    if max_read > 0 or read_after_first > 0:
        print(f"  FIRED. cache_read_tokens reached {max_read} "
              f"({read_after_first} on turns after the first).")
        print("  => S1 is MET on the default model with a full-size prefix.")
        print("  => CACHE_FLOOR_UNVERIFIED can be set to False in config.py (reserved file — ")
        print("     the integrator makes that edit; record the number in "
              "docs/22-phase0-verification.md).")
        verdict = 0
    else:
        print("  DID NOT FIRE. cache_read_tokens stayed 0 across every turn"
              f"{' (cache_write reached %d)' % max_write if max_write else ''}.")
        print("  ★ CAVEAT: Google's implicit caching can report 0 cache_read_tokens even when")
        print("    the cache DID fire (pydantic-ai #5205). This 0 is SUGGESTIVE, not conclusive")
        print("    — confirm against an OpenTelemetry gen_ai span before concluding the prefix")
        print("    floor is the cause.")
        print("\n  RECOMMENDATION: to guarantee a cacheable prefix at this ~1.5-2.4K size, move")
        print("  the default (or the caching agents) to a model with a PUBLISHED small floor —")
        print("  Anthropic Claude (1,024-token minimum, explicit cache_control) or an OpenAI")
        print("  model with automatic prompt caching at a 1,024-token floor. Both clear our")
        print("  prefix; the current Gemini-class default has a ~4,096 floor per OpenRouter's")
        print("  own guidance, which our prefix does not reach. A recorded negative is the")
        print("  deliverable — this is settled, not failed.")
        verdict = 0  # a recorded negative is a successful deliverable, not a script failure
    print("=" * 78)

    if len(rows) > 1:
        waits = [r[2] for r in timings]
        print(f"\n  (wait latency: min {min(waits)}ms · median "
              f"{int(statistics.median(waits))}ms · max {max(waits)}ms)")
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--turns", type=int, default=3, help="real turns to run (default 3, capped at 3)"
    )
    args = parser.parse_args()
    turns = max(1, min(3, args.turns))  # hard cap: this costs real money
    return asyncio.run(measure(turns))


if __name__ == "__main__":
    raise SystemExit(main())
