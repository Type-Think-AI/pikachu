#!/usr/bin/env python
"""Whole-package hot-path profiler — the Lane R measurement half.

``scripts/bench.py`` profiles the seven turn-assembly primitives in isolation. This goes
wider: it exercises every hot path a real turn touches — skill load, injection scan, guard
narrowing, canvas append + traverse, memory recall, SQLite read/search/write, the toolset
cache lookup, and a full turn assembled and run against ``FakeBackend`` — then ranks them by
mean cost so the single most expensive operation is obvious at a glance.

Everything runs offline: no network, no model, no wall-clock inside anything an assertion
depends on. The number this prints is the floor Pikachu adds to a turn regardless of which
model is behind it; if it grows, we regressed and no provider change hides it.

    .venv/bin/python scripts/profile_all.py               # default iterations
    .venv/bin/python scripts/profile_all.py -i 5000       # more iterations, tighter p95

Known baselines it compares against (docs/23-framework-comparison.md, 2026-08-30):

    framework total (parse+scan+guard+build)   ~96 us  (before the toolset cache)
    cached toolset lookup                        0.24 us
    SQLite search (FTS5)                          7.5 us
    SQLite read-by-key                            5.0 us
    agent construction (toolset cache)           24.5 us

A row whose mean exceeds its baseline by more than a tolerance factor is flagged REGRESSED.
The baselines are order-of-magnitude anchors measured on the author's machine, so the
tolerance is generous (3x) — this catches a real regression (a lost cache, an O(n^2) walk
that used to be O(n)), not machine-to-machine jitter. Absolute microseconds are not
comparable across machines; the RANKING and the ratios are the portable signal.
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

from pikachu import (  # noqa: E402
    AgentSpec,
    Artifact,
    ArtifactKind,
    MemoryRecord,
    MemoryScope,
    Run,
    Skill,
    SkillStatus,
    ToolOutcome,
    ToolSpec,
    TrustTier,
    TurnRequest,
)
from pikachu.backends.fake import FakeBackend, ScriptedToolCall, ScriptedTurn  # noqa: E402
from pikachu.canvas.graph import CanvasGraph  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402
from pikachu.memory.store import InMemoryMemoryStore  # noqa: E402
from pikachu.skills.loader import load_skill  # noqa: E402
from pikachu.skills.scanner import scan  # noqa: E402
from pikachu.storage.sqlite import SqliteStorage  # noqa: E402

SKILL_DOC = """---
name: house-palette
description: Apply the house colour palette.
allowed-tools: [generate_image, read_canvas]
---

# House palette

Signal amber #FFB300. Ink #101014. Bone #F4F1EA. Never use pure black.
"""


# Baselines: (mean_us, "source"). None means "no published baseline, report only".
_BASELINES: dict[str, float | None] = {
    "skill: load_skill (full document)": 15.0,
    "scan skill body for injection": 55.0,
    "guard: effective_tools (P3)": 6.0,
    "canvas: append artifact": None,
    "canvas: descendants traverse (depth 8)": None,
    "memory: recall (in-memory, budgeted)": None,
    "sqlite: read skill by key": 5.0,
    "sqlite: search skills (LIKE)": 7.5,
    "sqlite: search memory (FTS5)": 7.5,
    "sqlite: write one skill": None,
    "toolset cache: lookup (warm)": 0.24,
    "full turn assembly (FakeBackend)": 96.0,
}
_TOLERANCE = 3.0  # a mean above baseline * tolerance is flagged REGRESSED


def _bench(label: str, fn: Callable[[], object], *, iterations: int) -> tuple[str, float, float]:
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return label, statistics.mean(samples), p95


def _abench(
    label: str, coro_fn: Callable[[], object], *, iterations: int, loop: object, floor_us: float = 0.0
) -> tuple[str, float, float]:
    """Time an async ``coro_fn`` inside ONE shared event loop, minus the loop-step floor.

    Timing ``asyncio.run(coro())`` measures event-loop *construction* (~100 us here), which
    swamps a 5 us SQLite read and turns every async row into a false REGRESSED. Driving the
    coroutine on a single already-running loop via ``run_until_complete`` removes construction,
    but each call still pays a fixed scheduling step (~30 us here). ``floor_us`` is that step,
    measured once against a no-op coroutine and subtracted, so the reported number is the
    OPERATION — comparable to the synchronous baselines in docs/23, which were taken without a
    loop. The subtraction is clamped at 0.
    """
    run = loop.run_until_complete  # type: ignore[attr-defined]
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        run(coro_fn())  # type: ignore[arg-type]
        samples.append(max(0.0, (time.perf_counter() - t0) * 1e6 - floor_us))
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return label, statistics.mean(samples), p95


def _loop_step_floor_us(loop: object, iterations: int = 2000) -> float:
    """Measure the fixed per-call cost of ``run_until_complete`` on a no-op coroutine.

    This is the scheduling overhead the async rows must not be charged for. Subtracting it is
    what makes an async SQLite read comparable to the synchronous 5 us baseline.
    """
    run = loop.run_until_complete  # type: ignore[attr-defined]

    async def _noop() -> None:
        return None

    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        run(_noop())
        samples.append((time.perf_counter() - t0) * 1e6)
    return statistics.median(samples)


def profile(iterations: int) -> tuple[list[tuple[str, float, float]], float]:
    rows: list[tuple[str, float, float]] = []
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    floor = _loop_step_floor_us(loop)

    # --- pure sync hot paths ---------------------------------------------------------
    allow = ("generate_image", "read_canvas", "web_search")
    rows.append(
        _bench(
            "skill: load_skill (full document)",
            lambda: load_skill(SKILL_DOC, trust=TrustTier.BUILTIN, source="repo:builtin"),
            iterations=iterations,
        )
    )
    rows.append(_bench("scan skill body for injection", lambda: scan(SKILL_DOC), iterations=iterations))
    rows.append(
        _bench(
            "guard: effective_tools (P3)",
            lambda: effective_tools(allow, ("generate_image", "bash")),
            iterations=iterations,
        )
    )

    # --- canvas: append + traverse ---------------------------------------------------
    # append: time the coroutine only; the CanvasGraph itself is cheap to build so the
    # measured cost is the append, not construction.
    def _canvas_append() -> object:
        g = CanvasGraph()
        return g.append(Artifact(id="a0", kind=ArtifactKind.TEXT, payload_ref="r"))

    rows.append(_abench("canvas: append artifact", _canvas_append, iterations=iterations, loop=loop, floor_us=floor))

    chain = CanvasGraph()

    async def _build_chain() -> None:
        prev: str | None = None
        for i in range(8):
            await chain.append(
                Artifact(id=f"n{i}", kind=ArtifactKind.TEXT, payload_ref="r", parent=prev)
            )
            prev = f"n{i}"

    run(_build_chain())
    rows.append(
        _abench(
            "canvas: descendants traverse (depth 8)",
            lambda: chain.descendants("n0"),
            iterations=iterations,
            loop=loop,
            floor_us=floor,
        )
    )

    # --- memory recall (in-memory reference store) -----------------------------------
    mem = InMemoryMemoryStore(tenant="house")

    async def _seed_mem() -> None:
        for i in range(50):
            await mem.remember(
                MemoryRecord(key=f"brand-{i}", value=f"amber tone {i}", scope=MemoryScope.LONG)
            )

    run(_seed_mem())
    rows.append(
        _abench(
            "memory: recall (in-memory, budgeted)",
            lambda: mem.recall("amber", limit=5),
            iterations=iterations,
            loop=loop,
            floor_us=floor,
        )
    )

    # --- SQLite read / search / write ------------------------------------------------
    store = SqliteStorage()
    seed_skill = Skill(
        name="house-palette",
        description="Apply the house colour palette",
        body="amber ink bone",
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
        partition="colour",
    )
    run(store.skills.put(seed_skill))
    run(store.memory.remember(MemoryRecord(key="brand", value="amber ink bone")))

    rows.append(
        _abench(
            "sqlite: read skill by key",
            lambda: store.skills.get("house-palette"),
            iterations=iterations,
            loop=loop,
            floor_us=floor,
        )
    )
    rows.append(
        _abench(
            "sqlite: search skills (LIKE)",
            lambda: store.skills.find("palette", limit=5),
            iterations=iterations,
            loop=loop,
            floor_us=floor,
        )
    )
    rows.append(
        _abench(
            "sqlite: search memory (FTS5)",
            lambda: store.memory.recall("amber", limit=5),
            iterations=iterations,
            loop=loop,
            floor_us=floor,
        )
    )
    # Write is timed with a distinct name each iteration to avoid a REPLACE no-op.
    _write_seq = iter(range(1_000_000))

    def _sqlite_write() -> object:
        n = next(_write_seq)
        return store.skills.put(
            Skill(name=f"s{n}", body="x", status=SkillStatus.DRAFT, trust=TrustTier.BUILTIN)
        )

    rows.append(
        _abench("sqlite: write one skill", _sqlite_write, iterations=min(iterations, 400), loop=loop, floor_us=floor)
    )

    # --- toolset cache warm lookup ---------------------------------------------------
    from pikachu.backends.pydantic_ai import PydanticAIBackend

    def generate_image() -> str:
        """Generate an image."""
        return "ok"

    def read_canvas() -> str:
        """Read the canvas."""
        return "ok"

    backend = PydanticAIBackend(
        api_key="sk-not-used-offline",
        tool_registry={"generate_image": generate_image, "read_canvas": read_canvas},
    )
    warm_req = TurnRequest(
        message="hi",
        agent=AgentSpec(name="colourist", allowed_tools=allow),
        effective_tools=("generate_image", "read_canvas"),
    )
    backend._toolset_for(warm_req)  # prime the cache once  # noqa: SLF001
    rows.append(
        _bench(
            "toolset cache: lookup (warm)",
            lambda: backend._toolset_for(warm_req),  # noqa: SLF001
            iterations=iterations,
        )
    )

    # --- full turn assembly against FakeBackend --------------------------------------
    turn_run = Run(id="run:profile", agent_name="colourist", max_iterations=20)
    tools = (ToolSpec(name="generate_image", cost_credits=0),)

    def _full_turn() -> object:
        fake = FakeBackend(
            [
                ScriptedTurn(
                    text="done",
                    tool_calls=(ScriptedToolCall("generate_image", ToolOutcome.SUCCESS),),
                )
            ],
            run=turn_run,
            tools=tools,
        )
        req = TurnRequest(
            message="grade this",
            agent=AgentSpec(name="colourist", allowed_tools=("generate_image",)),
            effective_tools=("generate_image",),
            run_id=turn_run.id,
        )
        return fake.run_turn(req)

    rows.append(
        _abench(
            "full turn assembly (FakeBackend)",
            _full_turn,
            iterations=min(iterations, 2000),
            loop=loop,
            floor_us=floor,
        )
    )

    store.close()
    loop.close()
    return rows, floor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--iterations", type=int, default=2000, help="iterations per op")
    args = parser.parse_args()

    rows, floor = profile(args.iterations)
    ranked = sorted(rows, key=lambda r: r[1], reverse=True)

    print("\nWHOLE-PACKAGE HOT-PATH PROFILE — offline, ranked by mean cost")
    print("  The floor Pikachu adds to a turn. If a row is flagged REGRESSED, investigate it.\n")
    print(f"  {'#':>2s}  {'operation':40s} {'mean':>10s} {'p95':>10s}  {'baseline':>9s}  status")
    print(f"  {'-' * 2}  {'-' * 40} {'-' * 10} {'-' * 10}  {'-' * 9}  {'-' * 10}")
    for i, (label, mean_us, p95_us) in enumerate(ranked, start=1):
        base = _BASELINES.get(label)
        if base is None:
            base_s, status = "     —  ", "no baseline"
        elif mean_us > base * _TOLERANCE:
            base_s, status = f"{base:>7.2f}µs", f"REGRESSED (>{_TOLERANCE:.0f}x)"
        else:
            base_s, status = f"{base:>7.2f}µs", "ok"
        print(f"  {i:>2d}  {label:40s} {mean_us:>7.2f}µs {p95_us:>7.2f}µs  {base_s}  {status}")

    print(f"\n  Measured over {args.iterations} iterations each (write/full-turn capped for speed).")
    print(f"  Async rows have the event-loop step (~{floor:.1f} µs, measured on a no-op coroutine)")
    print("  subtracted, so they compare against the SYNCHRONOUS baselines in docs/23.")
    print("  Absolute µs vary by machine; the RANKING and the baseline RATIOS are the portable")
    print("  signal. A REGRESSED row means a mean above its published baseline * tolerance —")
    print("  most often a lost cache or an accidental O(n^2) walk, not machine jitter.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
