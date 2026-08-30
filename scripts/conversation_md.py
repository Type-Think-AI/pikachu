#!/usr/bin/env python
"""Run a live turn and print the full agent CONVERSATION as markdown.

The demo log shows the outcome; this shows the transcript — every message that flowed
between the user, the model, and the tool, in order, rendered to a `.md` file you can read
or share.

    .venv/bin/python scripts/conversation_md.py

Writes scripts/logs/conversation-<timestamp>.md and echoes the path.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest  # noqa: E402
from pikachu.config import DEFAULT_MODEL, get_api_key  # noqa: E402
from pikachu.guard import effective_tools  # noqa: E402
from pikachu.skills.loader import load_skill  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"

SKILL_MD = """---
name: house-colourist
description: Answer palette questions using the house palette tool.
allowed-tools: [brand_palette]
---

# House colourist

The palette is NOT in your head. When asked about a house colour, call `brand_palette`
and quote the exact hex it returns.
"""


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "House palette — signal amber #FFB300, ink #101014, bone #F4F1EA."


def _render_part(part: object) -> list[str]:
    """Turn one message part into markdown lines, by its kind."""
    kind = type(part).__name__
    out: list[str] = []
    if kind == "UserPromptPart":
        out.append(f"> {getattr(part, 'content', '')}")
    elif kind in ("SystemPromptPart",):
        content = str(getattr(part, "content", ""))
        out.append("```text")
        out.append(content.strip())
        out.append("```")
    elif kind == "TextPart":
        out.append(str(getattr(part, "content", "")).strip())
    elif kind == "ToolCallPart":
        name = getattr(part, "tool_name", "?")
        args = getattr(part, "args", "")
        out.append(f"**calls tool** `{name}`" + (f" with `{args}`" if args else ""))
    elif kind == "ToolReturnPart":
        name = getattr(part, "tool_name", "?")
        content = str(getattr(part, "content", "")).strip()
        out.append(f"**tool** `{name}` **returned:** {content}")
    else:
        out.append(f"_({kind})_")
    return out


def _speaker(message: object) -> str:
    kind = type(message).__name__
    if kind == "ModelRequest":
        # a request may hold the system prompt, the user prompt, or a tool return
        parts = getattr(message, "parts", ())
        names = {type(p).__name__ for p in parts}
        if "ToolReturnPart" in names:
            return "🔧 Tool → Model"
        if "SystemPromptPart" in names and "UserPromptPart" not in names:
            return "⚙️ System"
        return "🧑 User → Model"
    if kind == "ModelResponse":
        return "🤖 Model"
    return kind


async def main() -> int:
    key = get_api_key()
    if not key:
        print("No OPENROUTER_API_KEY found; cannot run a live conversation.")
        return 1

    from pikachu.backends.pydantic_ai import PydanticAIBackend

    agent = AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Answer using only the house palette. Be concise.",
        skill_tags=("colour",),
        allowed_tools=("brand_palette",),
    )
    skill: Skill = load_skill(SKILL_MD, trust=TrustTier.BUILTIN, source="demo:inline")
    narrowed = effective_tools(agent.allowed_tools, skill.declared_tools)
    question = "What is our house signal colour? Give the exact hex."

    backend = PydanticAIBackend(api_key=key, tool_registry={"brand_palette": brand_palette})
    request = TurnRequest(
        message=question, agent=agent, skill=skill, effective_tools=narrowed.tools
    )

    # run_turn records the full message history; grab it off the backend by re-running through
    # the agent so we can walk every message. We use the backend's own build + call path.
    built = backend._build_agent(request)  # noqa: SLF001 - demo introspection, intentional
    result_run = await built.run(question)
    messages: list[Any] = list(result_run.all_messages())
    await backend.aclose()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"conversation-{stamp}.md"

    lines: list[str] = [
        "# Agent conversation",
        "",
        f"**When:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}  ",
        f"**Model:** `{DEFAULT_MODEL}`  ",
        f"**Agent:** `{agent.name}` — {agent.role}  ",
        f"**Skill:** `{skill.name}` (declares `{', '.join(skill.declared_tools)}`)  ",
        f"**Tools the guard allowed:** `{', '.join(narrowed.tools) or 'none'}`  ",
        f"**User asked:** {question}",
        "",
        "---",
        "",
        "## Transcript",
        "",
        "Every message in order — system prompt, the user's question, the model deciding to "
        "call a tool, the tool's reply, and the model's final answer.",
        "",
    ]

    for i, message in enumerate(messages, start=1):
        lines.append(f"### {i}. {_speaker(message)}")
        lines.append("")
        for part in getattr(message, "parts", ()):
            lines.extend(_render_part(part))
            lines.append("")
        lines.append("")

    lines += [
        "---",
        "",
        "## Final answer",
        "",
        f"> {str(result_run.output).strip()}",
        "",
        "This is the whole loop: the skill body told the model to call the tool, the guard had "
        "already confirmed the tool was allowed, the model called it, read the real palette "
        "back, and answered with the exact hex rather than guessing.",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nConversation written to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
