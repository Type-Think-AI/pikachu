"""Live-test configuration: real network, real model, real money.

Three things this file does that the main ``tests/conftest.py`` deliberately does not:

1. **Re-enables the network.** The parent conftest installs an autouse socket block, which is
   correct for the offline suite and fatal here, so it is overridden for this directory only.
2. **Skips instead of failing when there is no credential.** A contributor without an
   OpenRouter key should see "skipped, no key", never a red suite.
3. **Writes a markdown report every run** to ``tests/live/reports/``, recording per-task
   status, wall-clock duration, tokens, cache reads and the model's actual response — which is
   the artifact you read to decide what to improve.

Run it explicitly; it is excluded from the default suite:

    .venv/bin/python -m pytest tests/live -v
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pikachu.config import CACHE_FLOOR_UNVERIFIED, DEFAULT_MODEL, get_api_key

REPORT_DIR = Path(__file__).parent / "reports"


@dataclass
class TaskRecord:
    """One live task's outcome, as it will appear in the report."""

    name: str
    description: str
    status: str = "not run"
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    iterations: int = 0
    model: str = ""
    response: str = ""
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    def cost_usd(self, *, prompt_per_mtok: float, completion_per_mtok: float,
                 cache_read_per_mtok: float) -> float:
        return (
            self.input_tokens / 1e6 * prompt_per_mtok
            + self.output_tokens / 1e6 * completion_per_mtok
            + self.cache_read_tokens / 1e6 * cache_read_per_mtok
        )


class Collector:
    """Accumulates records across the session so one report covers the whole run."""

    # Published OpenRouter prices for the default model, 2026-08-30. Used only to report an
    # estimate; nothing here bills anyone.
    PROMPT_PER_MTOK = 0.75
    COMPLETION_PER_MTOK = 3.75
    CACHE_READ_PER_MTOK = 0.075

    def __init__(self) -> None:
        self.records: list[TaskRecord] = []
        self.started = datetime.now(timezone.utc)

    def add(self, record: TaskRecord) -> TaskRecord:
        self.records.append(record)
        return record

    # -- report ------------------------------------------------------------------------
    def total_cost_usd(self) -> float:
        return sum(
            r.cost_usd(
                prompt_per_mtok=self.PROMPT_PER_MTOK,
                completion_per_mtok=self.COMPLETION_PER_MTOK,
                cache_read_per_mtok=self.CACHE_READ_PER_MTOK,
            )
            for r in self.records
        )

    def write_markdown(self) -> Path | None:
        if not self.records:
            return None
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = self.started.strftime("%Y%m%d-%H%M%S")
        path = REPORT_DIR / f"live-report-{stamp}.md"

        passed = [r for r in self.records if r.status == "PASS"]
        failed = [r for r in self.records if r.status == "FAIL"]
        skipped = [r for r in self.records if r.status == "SKIP"]
        total_ms = sum(r.duration_ms for r in self.records)
        cache_reads = sum(r.cache_read_tokens for r in self.records)

        lines: list[str] = [
            "# Pikachu Agent — live test report",
            "",
            f"**Run:** {self.started.isoformat(timespec='seconds')}  ",
            f"**Model:** `{DEFAULT_MODEL}`  ",
            f"**Result:** {len(passed)} passed · {len(failed)} failed · {len(skipped)} skipped "
            f"(of {len(self.records)})  ",
            f"**Wall clock:** {total_ms / 1000:.2f}s  ",
            f"**Estimated cost:** ${self.total_cost_usd():.6f}",
            "",
            "## Summary",
            "",
            "| Task | Status | Time | In | Out | Cache read | Iters |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for r in self.records:
            mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}.get(r.status, "·")
            lines.append(
                f"| {r.name} | {mark} {r.status} | {r.duration_ms} ms | {r.input_tokens} | "
                f"{r.output_tokens} | {r.cache_read_tokens} | {r.iterations} |"
            )

        # The headline finding: did prompt caching actually engage?
        lines += [
            "",
            "## Prompt caching — the open question",
            "",
        ]
        if cache_reads > 0:
            lines += [
                f"**Caching FIRED.** {cache_reads} cached prompt tokens were read across this "
                "run, so the model's minimum-prefix floor is at or below our prefix size.",
                "",
                "Action: set `CACHE_FLOOR_UNVERIFIED = False` in `src/pikachu/config.py`, record "
                "the measured number in `docs/22-phase0-verification.md`, and success criterion "
                "S1 (`cache_hit_ratio > 0`) is **met**.",
            ]
        else:
            lines += [
                "**Caching did NOT fire.** `cache_read_tokens` is 0 across every task.",
                "",
                "Two candidate causes, and they need different responses:",
                "",
                "1. **The prefix is below the model's floor.** Our stable prefix is ~1,500–2,400 "
                "tokens and Gemini's blanket documented minimum is 4,096. If this is the cause, "
                "caching is enabled and doing nothing, and S1 is unreachable on this model.",
                "2. **The counter is not reported.** Google's implicit caching returns 0 for "
                "cache reads through some paths even when it fired "
                "([pydantic-ai #5205](https://github.com/pydantic/pydantic-ai/issues/5205)). "
                "Confirm against the OpenTelemetry span before concluding cause 1.",
                "",
                "Note these test prompts are short — well under the prefix size a real turn "
                "carries — so a 0 here is **expected** and is not yet evidence about the floor. "
                "The floor question needs a turn with a full skill body and tool schemas loaded.",
                "",
                f"`CACHE_FLOOR_UNVERIFIED` remains `{CACHE_FLOOR_UNVERIFIED}`.",
            ]

        lines += ["", "## Per-task detail", ""]
        for r in self.records:
            lines += [
                f"### {r.name} — {r.status}",
                "",
                f"{r.description}",
                "",
                f"- **Duration:** {r.duration_ms} ms",
                f"- **Model:** `{r.model or DEFAULT_MODEL}`",
                f"- **Tokens:** {r.input_tokens} in / {r.output_tokens} out / "
                f"{r.cache_read_tokens} cache-read / {r.cache_write_tokens} cache-write",
                f"- **Iterations:** {r.iterations}",
            ]
            if r.notes:
                lines.append("- **Notes:**")
                lines += [f"  - {n}" for n in r.notes]
            if r.detail:
                lines += ["", f"**Outcome:** {r.detail}"]
            if r.response:
                snippet = r.response if len(r.response) < 1200 else r.response[:1200] + " …"
                lines += ["", "**Model response:**", "", "```text", snippet, "```"]
            lines.append("")

        if failed:
            lines += ["## What to improve", ""]
            for r in failed:
                lines.append(f"- **{r.name}** — {r.detail or 'failed, see detail above'}")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path


@pytest.fixture(scope="session")
def collector(request: pytest.FixtureRequest) -> Iterator[Collector]:
    c = Collector()
    # Stashed on the session so pytest_runtest_makereport can reconcile status against
    # pytest's real verdict rather than trusting each test body to set it.
    request.session._pikachu_collector = c  # type: ignore[attr-defined]
    yield c
    written = c.write_markdown()
    if written is not None:
        # Printed so the path is visible in the pytest output without -s.
        print(f"\n\nLive report written: {written}\n")


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[None]:
    """Override the parent conftest's autouse socket block for this directory only.

    The name MUST match the parent fixture exactly — pytest overrides by name, so calling this
    ``_allow_network`` silently overrides nothing and both fixtures run, leaving sockets
    blocked and every live test failing with a bare "Connection error".
    """
    yield


@pytest.fixture(scope="session")
def api_key() -> str:
    key = get_api_key()
    if not key:
        pytest.skip("no OPENROUTER_API_KEY in the environment or any .env on the search path")
    return key


@pytest.fixture(scope="session")
def live_backend(api_key: str) -> Iterator[Any]:
    """Session-scoped backend, closed deterministically at the end.

    Closing matters: an un-closed async transport raises ResourceWarning whenever the garbage
    collector gets to it, which under a strict warning policy is reported as a *teardown
    failure on an unrelated test*. Deterministic shutdown keeps failures attributable.
    """
    import asyncio

    from pikachu.backends.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(api_key=api_key, tool_registry=_TOOL_REGISTRY)
    yield backend
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(backend.aclose())
    except Exception:  # noqa: BLE001 - never fail a suite on cleanup
        pass


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """Let pytest be the authority on status, not the test body.

    A test can raise *before* reaching its own ``rec.status`` assignment — or fail during
    teardown, after it — leaving the record reading "not run" while the suite reports a
    failure. That produced a report claiming "0 failed" on a run with three failures, which is
    worse than no report. This reconciles the record against pytest's actual verdict.
    """
    if call.when != "call":
        return
    collector: Collector | None = getattr(item.session, "_pikachu_collector", None)
    if collector is None:
        return
    index = getattr(item, "callspec", None)
    del index  # parametrised live tests are not used yet; name match is sufficient
    number = item.name.split("_")[1] if "_" in item.name else ""
    for record in collector.records:
        if record.name.startswith(number):
            if call.excinfo is not None and record.status in ("not run", "PASS"):
                record.status = "FAIL"
                if not record.detail:
                    record.detail = f"{call.excinfo.typename}: {call.excinfo.exconly()[:300]}"
            elif call.excinfo is None and record.status == "not run":
                record.status = "PASS"
            break



# ---------------------------------------------------------------------------------------
# Tools offered to the live agent. Deliberately pure and side-effect-free: a live test that
# can mutate something outside itself is a live test nobody will run twice.
# ---------------------------------------------------------------------------------------


def brand_palette() -> str:
    """Return the house colour palette that all output must conform to."""
    return "Brand palette: ink #101014, bone #F4F1EA, signal amber #FFB300. Never pure black."


def shot_count(scene_description: str) -> int:
    """Return how many shots a scene should be broken into."""
    return max(1, min(6, len(scene_description.split()) // 4))


_TOOL_REGISTRY: dict[str, Any] = {
    "brand_palette": brand_palette,
    "shot_count": shot_count,
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: hits a real model over the network; costs money")


# The offline suite must never pick these up by accident.
os.environ.setdefault("PIKACHU_LIVE", "1")
