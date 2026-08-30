#!/usr/bin/env python
"""Isolate Pikachu's own overhead from the model's time.

Two measurements, and the distinction is the whole point:

**Offline (no network, no model).** Every hot path exercised against fakes. Whatever time this
takes is *ours* — it is the floor Pikachu adds to every turn regardless of which model is
behind it. If this number grows, we regressed, and no provider change will hide it.

**Live (optional, needs a key).** The same turn against the real model, phase-resolved into
setup / wait / stream / finalize. Confirms the offline floor against reality and shows how
small our share actually is.

    .venv/bin/python scripts/bench.py              # offline only
    .venv/bin/python scripts/bench.py --live       # adds real model calls
    .venv/bin/python scripts/bench.py --live -n 5  # 5 samples, for variance

Why this exists: a blended latency number moves when the model changes AND when our code
changes, so on its own it can never tell you which happened. Separating them is what makes the
number actionable.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest  # noqa: E402
from pikachu.config import DEFAULT_MODEL, get_api_key  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402
from pikachu.skills.frontmatter import parse_frontmatter  # noqa: E402
from pikachu.skills.loader import load_metadata, load_skill  # noqa: E402
from pikachu.skills.scanner import scan  # noqa: E402

# A whole SKILL.md document, for load_skill / load_metadata.
SKILL_DOC = """---
name: house-palette
description: Apply the house colour palette.
allowed-tools: [generate_image, read_canvas]
---

# House palette

Signal amber #FFB300. Ink #101014. Bone #F4F1EA. Never use pure black.
"""

# Just the frontmatter block, WITHOUT the --- delimiters: parse_frontmatter takes the block,
# not the document. load_skill is the whole-document entry point.
FM_BLOCK = """name: house-palette
description: Apply the house colour palette.
allowed-tools: [generate_image, read_canvas]
"""


def _bench(label: str, fn: Callable[[], object], *, iterations: int) -> tuple[str, float, float]:
    """Time ``fn`` ``iterations`` times, returning (label, mean_us, p95_us).

    Reports microseconds because these operations are fast enough that milliseconds would
    round most of them to zero — which is itself the finding.
    """
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return label, statistics.mean(samples), p95


def offline_bench(iterations: int) -> None:
    print("\nFRAMEWORK OVERHEAD — offline, no network, no model")
    print("  This is the floor Pikachu adds to every turn. If it grows, WE regressed.\n")

    agent = AgentSpec(
        name="colourist",
        instructions="Grade to the house look.",
        skill_tags=("colour",),
        allowed_tools=("generate_image", "read_canvas"),
    )
    skill = Skill(
        name="house-palette",
        body=SKILL_DOC,
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
        declared_tools=("generate_image",),
    )
    allow = ("generate_image", "read_canvas", "web_search")

    rows = [
        _bench("parse frontmatter block", lambda: parse_frontmatter(FM_BLOCK),
               iterations=iterations),
        _bench("load_metadata (no body read)", lambda: load_metadata(SKILL_DOC),
               iterations=iterations),
        _bench("load_skill (full document)",
               lambda: load_skill(SKILL_DOC, trust=TrustTier.BUILTIN, source="repo:builtin"),
               iterations=iterations),
        _bench("scan skill body for injection", lambda: scan(SKILL_DOC), iterations=iterations),
        _bench("guard: effective_tools (P3)",
               lambda: effective_tools(allow, ("generate_image", "bash")),
               iterations=iterations),
        _bench("build AgentSpec", lambda: AgentSpec(name="x", allowed_tools=allow),
               iterations=iterations),
        _bench("build TurnRequest",
               lambda: TurnRequest(message="hi", agent=agent, skill=skill,
                                   effective_tools=allow),
               iterations=iterations),
    ]

    print(f"  {'operation':34s} {'mean':>11s} {'p95':>11s}")
    print(f"  {'-' * 34} {'-' * 11} {'-' * 11}")
    total_mean = 0.0
    for label, mean_us, p95_us in rows:
        total_mean += mean_us
        print(f"  {label:34s} {mean_us:>8.1f} µs {p95_us:>8.1f} µs")
    print(f"  {'-' * 34} {'-' * 11} {'-' * 11}")
    print(f"  {'TOTAL per turn (framework)':34s} {total_mean:>8.1f} µs "
          f"= {total_mean / 1000:.3f} ms")
    print(f"\n  Measured over {iterations} iterations each.")
    print("  For context: a single model call in this project measures 1,900-9,300 ms, so the")
    print(f"  framework is roughly {total_mean / 1000 / 2000 * 100:.4f}% of a 2s turn.")


async def live_bench(samples: int) -> None:
    key = get_api_key()
    if not key:
        print("\nLIVE — skipped, no OPENROUTER_API_KEY found")
        return

    from pikachu.backends.pydantic_ai import PydanticAIBackend

    print(f"\nLIVE PHASES — {DEFAULT_MODEL}, {samples} sample(s)")
    print("  setup/finalize are OURS. wait/stream are the provider's.\n")

    backend = PydanticAIBackend(api_key=key)
    agent = AgentSpec(name="pikachu-agent", instructions="Answer in one short sentence.")
    rows: list[tuple[int, int, int, int, int, int]] = []
    try:
        for i in range(samples):
            result = await backend.run_turn(
                TurnRequest(message="Name the three primary additive colours.", agent=agent)
            )
            t = result.timing
            rows.append(
                (i + 1, t.setup_ms, t.wait_ms, t.stream_ms, t.finalize_ms, t.total_ms)
            )
    finally:
        await backend.aclose()

    print(f"  {'#':>2s} {'setup':>7s} {'wait':>8s} {'stream':>8s} {'final':>7s} {'TOTAL':>8s}"
          f" {'ours%':>7s}")
    print(f"  {'-' * 2} {'-' * 7} {'-' * 8} {'-' * 8} {'-' * 7} {'-' * 8} {'-' * 7}")
    for n, setup, wait, stream, final, total in rows:
        ours = (setup + final) / total * 100 if total else 0.0
        print(f"  {n:>2d} {setup:>6d}m {wait:>7d}m {stream:>7d}m {final:>6d}m {total:>7d}m"
              f" {ours:>6.2f}%")

    waits = [r[2] for r in rows]
    setups = [r[1] for r in rows]
    print()
    print(f"  wait:  min {min(waits)} ms · median {int(statistics.median(waits))} ms · "
          f"max {max(waits)} ms")
    if len(setups) > 1:
        print(f"  setup: first {setups[0]} ms · rest {setups[1:]}")
        print("         The first call pays one-time cost (imports, TLS handshake, client")
        print("         construction). Later calls are near-zero, so setup is a STARTUP cost,")
        print("         not a per-turn cost - which is the distinction that matters when")
        print("         judging whether the framework is slow.")
    if max(waits) > 2 * min(waits) and min(waits) > 0:
        print(f"\n  wait varies {max(waits) / min(waits):.1f}x across identical requests.")
        print("  That spread is provider QUEUEING, not model capability - the same prompt to")
        print("  the same model. It is the single largest and least controllable term, and no")
        print("  amount of framework optimisation touches it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also run real model calls")
    parser.add_argument("-n", "--samples", type=int, default=3, help="live samples (default 3)")
    parser.add_argument("-i", "--iterations", type=int, default=2000,
                        help="offline iterations per op (default 2000)")
    args = parser.parse_args()

    offline_bench(args.iterations)
    if args.live:
        asyncio.run(live_bench(args.samples))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
