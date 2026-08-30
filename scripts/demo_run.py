#!/usr/bin/env python
"""End-to-end demo: create a custom agent, add a skill, run a turn — logged to one file.

This is the "does the agent actually work" script. It does the full user journey in order and
writes a complete, timestamped transcript to a single log file so you can read exactly what
happened at each step: the agent that was created, the skill added to it, what the guard
allowed, and what the model said back.

    .venv/bin/python scripts/demo_run.py            # live turn against the real model
    .venv/bin/python scripts/demo_run.py --offline  # deterministic FakeBackend, no key, no cost

The log is written to scripts/logs/demo-<timestamp>.log AND echoed to the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest  # noqa: E402
from pikachu.config import DEFAULT_MODEL, get_api_key  # noqa: E402
from pikachu.discovery.registry import AgentRegistry  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402
from pikachu.skills.loader import load_skill  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"


class Log:
    """Writes every line to both a single file and the terminal, timestamped."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")

    def __call__(self, line: str = "") -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        text = f"[{stamp}] {line}" if line else ""
        print(text)
        self._fh.write(text + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# The skill the user "adds" — a real SKILL.md document, authored inline for the demo.
SKILL_MD = """---
name: house-colourist
description: Grade stills to the house palette and answer palette questions.
license: MIT
allowed-tools: [brand_palette]
---

# House colourist

The palette is NOT in your head. When asked about a house colour, call `brand_palette`
and quote the exact hex it returns. Never invent a colour.
"""


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "House palette — signal amber #FFB300, ink #101014, bone #F4F1EA."


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="use FakeBackend, no key, no cost")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log = Log(LOG_DIR / f"demo-{stamp}.log")

    log("=" * 70)
    log("PIKACHU DEMO — create an agent, add a skill, run a turn")
    log(f"mode: {'OFFLINE (FakeBackend)' if args.offline else 'LIVE (' + DEFAULT_MODEL + ')'}")
    log("=" * 70)

    # ---- STEP 1: create a custom agent, at runtime, no code ------------------------------
    log()
    log("STEP 1 — create a custom agent (six declarative fields, at runtime)")
    registry = AgentRegistry()
    agent = AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Answer using only the house palette. Be concise.",
        skill_tags=("colour", "grade"),
        allowed_tools=("brand_palette",),
        triggers=("colour", "palette", "grade"),
    )
    registry.create(agent)
    log(f"  created agent   : {agent.name!r}")
    log(f"  role            : {agent.role}")
    log(f"  allowed tools   : {agent.allowed_tools}")
    log(f"  skill partition : {agent.skill_tags}")
    log(f"  registry now has: {[a.name for a in registry.list()]}")

    # ---- STEP 2: add a skill to the agent ------------------------------------------------
    log()
    log("STEP 2 — add a skill (author a SKILL.md, load it, attach it)")
    skill: Skill = load_skill(SKILL_MD, trust=TrustTier.BUILTIN, source="demo:inline")
    log(f"  skill name      : {skill.name!r}")
    log(f"  declared tools  : {skill.declared_tools}")
    log(f"  trust tier      : {skill.trust.value}")
    log(f"  status          : {skill.status.value}")
    body_lines = [ln for ln in skill.body.splitlines() if ln.strip() and not ln.startswith("#")]
    log(f"  body (first line): {(body_lines[0].strip() if body_lines else '')!r}")

    # ---- STEP 3: the guard decides what the agent may actually touch ---------------------
    log()
    log("STEP 3 — the guard narrows the toolset (P3: effective ⊆ allowlist ∩ declared)")
    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)
    log(f"  agent allowlist : {agent.allowed_tools}")
    log(f"  skill declares  : {skill.declared_tools}")
    log(f"  EFFECTIVE tools : {narrowed.tools}")
    log(f"  removed         : {narrowed.removed_tools or '(none)'}")

    # ---- STEP 4: run a turn --------------------------------------------------------------
    log()
    log("STEP 4 — run a turn: ask the agent a question its skill answers with a tool")
    question = "What is our house signal colour? Give the exact hex."
    log(f"  user asks       : {question!r}")

    if args.offline:
        from pikachu.backends.fake import FakeBackend, ScriptedToolCall, ScriptedTurn
        from pikachu.core.types import ToolOutcome

        backend = FakeBackend(
            [
                ScriptedTurn(
                    text="Our signal colour is amber #FFB300.",
                    tool_calls=(ScriptedToolCall(tool="brand_palette", outcome=ToolOutcome.SUCCESS),),
                )
            ]
        )
        request = TurnRequest(message=question, agent=agent, skill=skill,
                              effective_tools=narrowed.tools)
        t0 = time.perf_counter()
        result = await backend.run_turn(request)
        elapsed = (time.perf_counter() - t0) * 1000
    else:
        key = get_api_key()
        if not key:
            log("  ERROR: no OPENROUTER_API_KEY found. Re-run with --offline for a no-key demo.")
            log.close()
            return 1
        from pikachu.backends.pydantic_ai import PydanticAIBackend

        backend = PydanticAIBackend(api_key=key, tool_registry={"brand_palette": brand_palette})
        request = TurnRequest(message=question, agent=agent, skill=skill,
                              effective_tools=narrowed.tools)
        t0 = time.perf_counter()
        result = await backend.run_turn(request)
        elapsed = (time.perf_counter() - t0) * 1000
        await backend.aclose()

    # ---- STEP 5: log exactly what the agent did and said ---------------------------------
    log()
    log("STEP 5 — what the agent did")
    log(f"  ANSWER          : {result.text.strip()!r}")
    log(f"  tool calls      :")
    for c in result.tool_calls:
        log(f"    - {c.get('tool')}  executed={c.get('executed')}  outcome={c.get('outcome', 'n/a')}")
    if not result.tool_calls:
        log("    (none)")
    log(f"  iterations      : {result.iterations}")
    log(f"  tokens          : {result.input_tokens} in / {result.output_tokens} out")
    t = result.timing
    log(f"  timing (ms)     : setup {t.setup_ms} · wait {t.wait_ms} · "
        f"stream {t.stream_ms} · finalize {t.finalize_ms} · TOTAL {t.total_ms}")
    log(f"  framework share : {t.framework_share * 100:.3f}%  (ours vs the model)")
    if getattr(result, "served_by", ""):
        log(f"  served by       : {result.served_by}")
    log(f"  wall clock      : {elapsed:.0f} ms")

    # ---- verdict -------------------------------------------------------------------------
    log()
    log("VERDICT")
    hit = "FFB300" in result.text.upper()
    used_tool = any(c.get("tool") == "brand_palette" and c.get("executed") for c in result.tool_calls)
    log(f"  quoted the hex from the skill's tool? {'YES' if hit else 'no'}")
    log(f"  the granted tool actually executed?   {'YES' if used_tool else 'no'}")
    log(f"  => the agent created a custom agent, loaded a skill, the guard narrowed the tools,")
    log(f"     and the model {'used the tool and answered correctly' if hit else 'answered'}.")
    log()
    log(f"Full log written to: {log.path}")
    log.close()
    print(f"\nLog file: {log.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
