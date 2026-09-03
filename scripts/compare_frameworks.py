#!/usr/bin/env python
"""Head-to-head: Pikachu / Pydantic AI vs Agno vs OpenAI Agents SDK.

Measures the three things that *can* differ between agent frameworks, and reports them against
the one thing that dominates a real turn:

1. **Cold import time** — a fresh interpreter importing the framework. This is real: it is paid
   at process start, and it is the largest part of our measured 236 ms first-call cost.
2. **Model object construction** — building the provider/model handle.
3. **Agent instantiation** — the number Agno markets. Matters more to us than to most, because
   invariant P7 forbids sharing an agent across turns, so we pay it *every* turn.

It does NOT measure inference speed. No framework makes a model think faster; a framework can
only add overhead to it. The comparison is therefore about overhead, and the report puts that
overhead next to the measured provider wait so the scale is unmissable.

REQUIRES a venv with all three frameworks, which is deliberately NOT pikachu's own venv —
constraint C5 allows exactly one agent framework in our dependencies:

    BENCH=$(mktemp -d)
    uv venv --python 3.13 "$BENCH/.venv"
    uv pip install --python "$BENCH/.venv/bin/python" \
        "agno==2.5.17" "pydantic-ai-slim[openai]==2.36.0" "openai-agents==0.22.0"
    "$BENCH/.venv/bin/python" scripts/compare_frameworks.py

Anything not installed is reported as absent rather than guessed at.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable

MODEL_ID = "google/gemini-3.8-flash"

# Measured in this project, for context. See scripts/bench.py and the live reports.
PIKACHU_FRAMEWORK_US = 96.1
PROVIDER_WAIT_MS_MIN = 2345
PROVIDER_WAIT_MS_MED = 2907
PROVIDER_WAIT_MS_MAX = 3336


def cold_import_ms(module: str, *, runs: int = 3) -> float | None:
    """Time ``import <module>`` in a FRESH interpreter, best of ``runs``.

    A fresh process each time is the point: importing twice in one process is free, so an
    in-process measurement would report ~0 and miss the cost that is actually paid at startup.
    Best-of rather than mean, to reduce noise from disk and scheduler.
    """
    best: float | None = None
    for _ in range(runs):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, timeout=120
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            return None
        best = elapsed if best is None else min(best, elapsed)
    return best


def bench_us(fn: Callable[[], object], *, iterations: int) -> tuple[float, float] | None:
    """Return (mean_us, p95_us), or None if the operation raises."""
    try:
        fn()
    except Exception:
        return None
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            return None
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    return statistics.mean(samples), samples[min(len(samples) - 1, int(len(samples) * 0.95))]


def probe_pydantic_ai(iterations: int) -> dict[str, object]:
    out: dict[str, object] = {"name": "Pydantic AI", "available": False}
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openrouter import OpenRouterProvider
    except ImportError:
        return out
    out["available"] = True
    provider = OpenRouterProvider(api_key="bench-not-a-real-key")

    def tool(x: str) -> str:
        """A trivial tool."""
        return x

    out["model_us"] = bench_us(
        lambda: OpenAIChatModel(MODEL_ID, provider=provider), iterations=iterations
    )
    model = OpenAIChatModel(MODEL_ID, provider=provider)
    out["agent_us"] = bench_us(
        lambda: Agent(model, instructions="be brief", tools=[tool]), iterations=iterations
    )
    return out


def probe_agno(iterations: int) -> dict[str, object]:
    out: dict[str, object] = {"name": "Agno", "available": False}
    try:
        from agno.agent import Agent
        from agno.models.openrouter import OpenRouter
    except ImportError:
        return out
    out["available"] = True

    def tool(x: str) -> str:
        """A trivial tool."""
        return x

    out["model_us"] = bench_us(lambda: OpenRouter(id=MODEL_ID), iterations=iterations)
    model = OpenRouter(id=MODEL_ID)
    out["agent_us"] = bench_us(
        lambda: Agent(model=model, instructions="be brief", tools=[tool]), iterations=iterations
    )
    return out


def probe_openai_agents(iterations: int) -> dict[str, object]:
    out: dict[str, object] = {"name": "OpenAI Agents SDK", "available": False}
    try:
        from agents import Agent
    except ImportError:
        return out
    out["available"] = True

    def tool(x: str) -> str:
        """A trivial tool."""
        return x

    # This SDK takes the model as a plain string, so there is no separate model-object step.
    out["model_us"] = None
    out["agent_us"] = bench_us(
        lambda: Agent(name="bench", instructions="be brief", model=MODEL_ID), iterations=iterations
    )
    return out


def fmt(pair: object) -> str:
    if pair is None:
        return "n/a"
    mean, p95 = pair  # type: ignore[misc]
    return f"{mean:8.2f} µs (p95 {p95:6.2f})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--iterations", type=int, default=2000)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    results = [
        probe_pydantic_ai(args.iterations),
        probe_agno(args.iterations),
        probe_openai_agents(args.iterations),
    ]
    imports = {
        "Pydantic AI": cold_import_ms("pydantic_ai"),
        "Agno": cold_import_ms("agno.agent"),
        "OpenAI Agents SDK": cold_import_ms("agents"),
    }

    if args.json:
        print(json.dumps({"results": results, "cold_import_ms": imports}, default=str, indent=2))
        return 0

    print(f"\nAGENT FRAMEWORK OVERHEAD — {args.iterations} iterations, model id {MODEL_ID!r}")
    print("  Overhead only. No framework makes inference faster; it can only add to it.\n")

    print(f"  {'framework':22s} {'cold import':>14s} {'model object':>26s} {'agent instantiation':>28s}")
    print(f"  {'-' * 22} {'-' * 14} {'-' * 26} {'-' * 28}")
    for r in results:
        name = str(r["name"])
        if not r["available"]:
            print(f"  {name:22s} {'NOT INSTALLED':>14s}")
            continue
        imp = imports.get(name)
        imp_s = f"{imp:8.1f} ms" if imp else "n/a"
        print(f"  {name:22s} {imp_s:>14s} {fmt(r.get('model_us')):>26s} "
              f"{fmt(r.get('agent_us')):>28s}")

    print("\n  Pikachu adds its own layer on top of Pydantic AI:")
    print(f"  {'Pikachu (full path)':22s} {'':>14s} {'':>26s} "
          f"{PIKACHU_FRAMEWORK_US:8.2f} µs  (parse+scan+guard+build)")

    # ---- the part that decides whether any of this matters --------------------------------
    print("\n  SCALE CHECK — measured provider wait for one real turn on this model:")
    print(f"    min {PROVIDER_WAIT_MS_MIN} ms · median {PROVIDER_WAIT_MS_MED} ms · "
          f"max {PROVIDER_WAIT_MS_MAX} ms")
    med_us = PROVIDER_WAIT_MS_MED * 1000
    print()
    for r in results:
        if not r["available"] or r.get("agent_us") is None:
            continue
        mean = r["agent_us"][0]  # type: ignore[index]
        print(f"    {str(r['name']):22s} agent instantiation is "
              f"{mean / med_us * 100:.5f}% of a median turn")
    print(f"    {'Pikachu (full path)':22s} framework total is "
          f"{PIKACHU_FRAMEWORK_US / med_us * 100:.5f}% of a median turn")

    spread = PROVIDER_WAIT_MS_MAX - PROVIDER_WAIT_MS_MIN
    print(f"\n  The provider's own run-to-run spread is {spread} ms on identical requests.")
    print(f"  That single spread is ~{spread * 1000 / max(PIKACHU_FRAMEWORK_US, 1):,.0f}x the "
          "entire framework cost, which is why")
    print("  instantiation benchmarks cannot be used to choose an agent framework for a")
    print("  network-bound workload. Choose on capability, safety and ecosystem instead.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
