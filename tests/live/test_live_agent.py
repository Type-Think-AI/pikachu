"""Live tests: the Pikachu Agent against a real model.

Each test records a ``TaskRecord`` so the markdown report has timing, tokens and the actual
response — the report is the deliverable, the assertions are the gate.

    .venv/bin/python -m pytest tests/live -v

Costs real money. At the default model's $0.75/MTok input these prompts are fractions of a
cent, and the report prints the estimate.
"""

from __future__ import annotations

import time

import pytest

from pikachu import AgentSpec, Skill, SkillStatus, TrustTier, TurnRequest
from pikachu.config import DEFAULT_MODEL
from pikachu.guard import effective_tools

from .conftest import Collector, TaskRecord

# `filterwarnings = ["error"]` in pyproject.toml is correct for the offline suite and wrong
# here: the async HTTP stack (httpx/anyio) leaves a socket to be closed at GC time, which
# surfaces as a ResourceWarning and, escalated to an error, fails a test whose actual API call
# succeeded. The leak is inside the client library, not this package, so it is downgraded for
# the live directory only rather than globally.
pytestmark = [
    pytest.mark.live,
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


def _record(result: object, record: TaskRecord) -> None:
    """Copy metrics off a TurnResult onto the report record."""
    for attr in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "iterations",
    ):
        setattr(record, attr, getattr(result, attr, 0))
    record.duration_ms = getattr(result, "latency_ms", 0)
    record.response = getattr(result, "text", "")


async def test_01_agent_answers_at_all(live_backend, collector: Collector) -> None:
    """The most basic thing: does a turn complete against the real model?"""
    rec = collector.add(
        TaskRecord(
            name="01 · basic turn",
            description="A single-shot turn with no skill and no tools. Proves the backend, "
            "provider, credential and model string all line up.",
            model=DEFAULT_MODEL,
        )
    )
    agent = AgentSpec(name="pikachu-agent", role="A concise assistant.",
                      instructions="Answer in one short sentence.")
    request = TurnRequest(message="Name the three primary additive colours.", agent=agent)

    result = await live_backend.run_turn(request)
    _record(result, rec)

    try:
        assert result.text.strip(), "model returned empty text"
        assert result.output_tokens > 0, "no output tokens reported"
        rec.status = "PASS"
        rec.detail = f"Model responded in {rec.duration_ms} ms."
    except AssertionError as exc:
        rec.status, rec.detail = "FAIL", str(exc)
        raise


async def test_02_instructions_are_obeyed(live_backend, collector: Collector) -> None:
    """Instructions must actually steer output — the basis of every skill we ship."""
    rec = collector.add(
        TaskRecord(
            name="02 · instructions steer output",
            description="Instructs the agent to reply with exactly one word. Verifies that "
            "AgentSpec.instructions reaches the model rather than being dropped.",
            model=DEFAULT_MODEL,
        )
    )
    agent = AgentSpec(
        name="pikachu-agent",
        instructions="Reply with EXACTLY one word, lowercase, no punctuation.",
    )
    result = await live_backend.run_turn(
        TurnRequest(message="What colour is a clear midday sky?", agent=agent)
    )
    _record(result, rec)

    words = result.text.strip().split()
    try:
        assert len(words) <= 3, f"expected ~1 word, got {len(words)}: {result.text!r}"
        rec.status = "PASS"
        rec.detail = f"Returned {len(words)} word(s): {result.text.strip()!r}"
    except AssertionError as exc:
        # A soft instruction-following miss is worth reporting, not hiding.
        rec.status, rec.detail = "FAIL", str(exc)
        rec.notes.append("Instruction following is a model-quality signal, not a code defect.")
        raise


async def test_03_skill_body_reaches_the_model(live_backend, collector: Collector) -> None:
    """A skill body must be injected into the prompt and change the answer."""
    rec = collector.add(
        TaskRecord(
            name="03 · skill body applied",
            description="Loads a BUILTIN skill whose body defines a house palette, then asks a "
            "question only answerable from that body. Proves skill injection works end to end.",
            model=DEFAULT_MODEL,
        )
    )
    skill = Skill(
        name="house-palette",
        description="The house colour palette.",
        body=(
            "# House palette\n\n"
            "The signal colour is amber #FFB300.\n"
            "Never use pure black; use ink #101014 instead.\n"
        ),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )
    agent = AgentSpec(name="colourist", instructions="Answer using only the house palette.")
    result = await live_backend.run_turn(
        TurnRequest(message="What is our signal colour? Give the hex code.", agent=agent,
                    skill=skill)
    )
    _record(result, rec)

    try:
        assert "FFB300" in result.text.upper(), (
            f"skill body did not reach the model; response: {result.text!r}"
        )
        rec.status = "PASS"
        rec.detail = "Model quoted the hex code from the skill body, so injection works."
    except AssertionError as exc:
        rec.status, rec.detail = "FAIL", str(exc)
        raise


async def test_04_tool_is_called_and_guard_narrows_it(
    live_backend, collector: Collector
) -> None:
    """The guard's decision must be what the model actually sees.

    Two tools exist in the registry; the guard permits one. The permitted tool must be
    callable and the denied one must be absent — this is P3 proven against a live model
    rather than against a fake.
    """
    rec = collector.add(
        TaskRecord(
            name="04 · tool call + guard narrowing",
            description="Registry has brand_palette and shot_count. The allowlist permits only "
            "brand_palette. Verifies the model can call the permitted tool and never sees the "
            "denied one — P3 enforced against a real model.",
            model=DEFAULT_MODEL,
        )
    )
    permitted = effective_tools(("brand_palette",), ("brand_palette", "shot_count"))
    rec.notes.append(f"guard returned {permitted.tools}, removed {permitted.removed_tools}")

    agent = AgentSpec(
        name="colourist",
        instructions="Use the brand_palette tool to answer questions about colour.",
        allowed_tools=("brand_palette",),
    )
    result = await live_backend.run_turn(
        TurnRequest(
            message="Call the brand_palette tool and tell me the signal colour hex.",
            agent=agent,
            effective_tools=permitted.tools,
        )
    )
    _record(result, rec)
    called = [c["tool"] for c in result.tool_calls]
    rec.notes.append(f"tools actually called: {called or 'none'}")

    try:
        assert permitted.tools == ("brand_palette",), permitted.tools
        assert "shot_count" not in called, "model called a tool the guard denied"
        assert "FFB300" in result.text.upper() or "brand_palette" in called, (
            f"tool appears not to have been used; response: {result.text!r}"
        )
        rec.status = "PASS"
        rec.detail = f"Guard narrowed 2 tools to 1; model called {called or ['none']}."
    except AssertionError as exc:
        rec.status, rec.detail = "FAIL", str(exc)
        raise


async def test_05_multi_turn_timing_and_cache(live_backend, collector: Collector) -> None:
    """Three turns sharing one prefix — the closest thing to the caching question.

    Reports per-turn latency and any cache reads. A 0 here is expected with prompts this
    small; the value is the timing baseline and the fact that repeated turns are stable.
    """
    rec = collector.add(
        TaskRecord(
            name="05 · repeated turns, shared prefix",
            description="Runs the same instructions and skill three times to measure latency "
            "spread and whether any cached prompt tokens are reported on turns 2 and 3.",
            model=DEFAULT_MODEL,
        )
    )
    skill = Skill(
        name="house-palette",
        body="# House palette\n\nSignal amber #FFB300. Ink #101014. Bone #F4F1EA.\n" * 4,
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )
    agent = AgentSpec(name="colourist", instructions="Answer in one short sentence.")

    timings: list[int] = []
    reads: list[int] = []
    started = time.perf_counter()
    for i, q in enumerate(
        ("Name the ink colour.", "Name the bone colour.", "Name the signal colour."), start=1
    ):
        result = await live_backend.run_turn(
            TurnRequest(message=q, agent=agent, skill=skill)
        )
        timings.append(result.latency_ms)
        reads.append(result.cache_read_tokens)
        rec.input_tokens += result.input_tokens
        rec.output_tokens += result.output_tokens
        rec.cache_read_tokens += result.cache_read_tokens
        rec.cache_write_tokens += result.cache_write_tokens
        rec.iterations += result.iterations
        rec.response += f"turn {i}: {result.text.strip()}\n"

    rec.duration_ms = int((time.perf_counter() - started) * 1000)
    rec.notes.append(f"per-turn latency: {timings} ms")
    rec.notes.append(f"per-turn cache_read_tokens: {reads}")
    if sum(reads) == 0:
        rec.notes.append(
            "No cache reads. Expected at this prompt size - the floor question needs a "
            "full-size prefix, and Google's implicit caching may report 0 regardless."
        )

    try:
        assert len(timings) == 3 and all(t > 0 for t in timings)
        rec.status = "PASS"
        rec.detail = (
            f"3 turns in {rec.duration_ms} ms (mean {sum(timings) // 3} ms). "
            f"Cache reads: {sum(reads)}."
        )
    except AssertionError as exc:
        rec.status, rec.detail = "FAIL", str(exc)
        raise
