"""In-memory telemetry ledger — the number that tells us whether *we* regressed.

The whole point of this module is stated in one line: **a single blended latency number is
useless for tuning**, because it moves when the *model* changes and when *our code* changes and
you cannot tell which. So nothing here reports a blended latency without also splitting it into
``framework_ms`` (ours — setup + finalize) and ``model_ms`` (the provider's — wait + stream),
using the attribution already baked into ``TurnTiming``.

Measured reality this is calibrated against (docs/23-framework-comparison.md, 2026-08-30):

* framework overhead is ~0.3 ms of a ~2,900 ms turn — about **0.01%**
* provider ``wait`` varies **2,345–3,336 ms** on *identical* requests
* decode runs at **~4,900 tok/s**

So a percentile taken over blended latency is dominated by provider queue variance and says
nothing about our code. Percentiles are therefore computed **separately** over latency,
``framework_ms`` and ``model_ms`` — mixing them defeats the purpose.

Design rules:

* **Pure in-memory. No I/O, no network.** Persistence is Lane L's job (``storage/``); this
  object is fed ``TurnResult`` values and answers questions about them, nothing more.
* **Prices are parameters, never constants.** Per-MTok prices change, and a stale constant is
  worse than no constant because it lies with confidence. The cost estimator takes prices as
  arguments; this module ships no price table.
* **Decode throughput uses ``stream_ms`` only**, so provider queue time (``wait_ms``) does not
  dilute a tokens/sec figure into meaninglessness.
* **Lazy imports.** Nothing heavy at module scope; ``statistics`` is stdlib and cheap, imported
  at first use anyway to keep the module-import cost near zero per the wave rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pikachu.core.types import TurnResult, TurnTiming

__all__ = [
    "AgentTotals",
    "LedgerTotals",
    "Percentiles",
    "Prices",
    "TokenLedger",
]


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prices:
    """Per-MTok prices, supplied by the caller. **Not** a constant table.

    Prices change and differ per model, and a hardcoded price silently misreports cost the day
    a provider re-tiers. The caller owns the current numbers (config documents the default
    model's published prices with a date, but this module never reaches for them).

    Units: dollars per million tokens. Cache write is billed as storage on the default model
    (Google, ~0.056x prompt) rather than a premium, so it is a real cost line, not zero.
    """

    prompt_per_mtok: float
    completion_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0


@dataclass(frozen=True)
class Percentiles:
    """p50 / p90 / p99 over some sample. Zeroed when there is no data.

    ``n`` is carried so a reader can tell a real p99 from one computed over three points — a
    percentile over a handful of samples is not yet a percentile, and hiding ``n`` would let a
    trend line imply a confidence it has not earned.
    """

    p50: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    n: int = 0


@dataclass(frozen=True)
class AgentTotals:
    """Accumulated counters for one agent."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_credits: int = 0
    iterations: int = 0
    framework_ms: int = 0
    model_ms: int = 0
    total_ms: int = 0

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of prompt tokens served from cache. Boundary-safe at all-zero.

        Denominator is ``input_tokens + cache_read_tokens`` — the total prompt-side tokens —
        matching ``TurnResult.cache_hit_ratio``. Returns 0.0 rather than raising when there is
        nothing to divide, because a fresh ledger with no tokens must not be a ZeroDivisionError.
        """
        total = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / total if total else 0.0


@dataclass(frozen=True)
class LedgerTotals:
    """The whole ledger rolled up. What ``report.py`` renders and ``--json`` serialises."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_credits: int = 0
    iterations: int = 0

    framework_ms: int = 0
    model_ms: int = 0
    total_ms: int = 0
    setup_ms: int = 0
    wait_ms: int = 0
    stream_ms: int = 0
    finalize_ms: int = 0
    unattributed_ms: int = 0

    # Percentiles are computed OVER DIFFERENT SAMPLES on purpose — never one blended number.
    latency_pct: Percentiles = field(default_factory=Percentiles)
    framework_pct: Percentiles = field(default_factory=Percentiles)
    model_pct: Percentiles = field(default_factory=Percentiles)

    @property
    def cache_hit_ratio(self) -> float:
        total = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / total if total else 0.0

    @property
    def framework_share(self) -> float:
        """Fraction of wall clock spent in our own code, 0.0-1.0. The regression signal."""
        return self.framework_ms / self.total_ms if self.total_ms else 0.0

    @property
    def cost_per_turn_credits(self) -> float:
        return self.cost_credits / self.turns if self.turns else 0.0

    @property
    def tokens_per_second(self) -> float:
        """Decode throughput across the whole ledger, on ``stream_ms`` ONLY.

        Uses stream time, not total and not wait, so provider queueing cannot drag a real
        ~4,900 tok/s decode down to a number that looks like a model problem when it is a
        network problem. Returns 0.0 when no decode time was measured.
        """
        return self.output_tokens / (self.stream_ms / 1000) if self.stream_ms else 0.0

    def estimated_cost_usd(self, prices: Prices) -> float:
        """Dollar cost from token counts and caller-supplied prices. No constant table.

        Cache reads are cheaper than fresh input and cache writes are a separate line, so all
        four token classes are priced independently. Pass the current numbers — a stale price
        here is a confident lie.
        """
        return (
            self.input_tokens / 1e6 * prices.prompt_per_mtok
            + self.output_tokens / 1e6 * prices.completion_per_mtok
            + self.cache_read_tokens / 1e6 * prices.cache_read_per_mtok
            + self.cache_write_tokens / 1e6 * prices.cache_write_per_mtok
        )


# --------------------------------------------------------------------------------------
# Percentile helper
# --------------------------------------------------------------------------------------


def _percentiles(samples: list[int]) -> Percentiles:
    """p50/p90/p99 over an integer sample, using linear interpolation.

    ``statistics.quantiles`` needs at least two points and cuts into fixed buckets, which does
    not give an arbitrary percentile cleanly, so a small explicit interpolation is used. Empty
    -> all zeros; a single point -> that point at every percentile (honest: with one sample the
    p50 and the p99 genuinely are the same number).
    """
    if not samples:
        return Percentiles()
    ordered = sorted(samples)
    n = len(ordered)

    def pick(q: float) -> float:
        if n == 1:
            return float(ordered[0])
        # Rank on [0, n-1]; interpolate between neighbours.
        rank = q * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

    return Percentiles(p50=pick(0.50), p90=pick(0.90), p99=pick(0.99), n=n)


# --------------------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------------------


class TokenLedger:
    """Accumulates per-run and per-agent telemetry from ``TurnResult`` values.

    Fully in-memory. Feed it results with :meth:`record`; ask it for rolled-up
    :meth:`totals` or :meth:`agent_totals`. It never touches disk or the network — a caller that
    wants persistence hands the results to Lane L's storage as well.
    """

    def __init__(self) -> None:
        # Raw per-turn samples, kept so percentiles can be recomputed over the right axis.
        self._latency: list[int] = []
        self._framework: list[int] = []
        self._model: list[int] = []

        # Scalar accumulators.
        self._turns = 0
        self._input = 0
        self._output = 0
        self._cache_read = 0
        self._cache_write = 0
        self._cost = 0
        self._iterations = 0
        self._setup = 0
        self._wait = 0
        self._stream = 0
        self._finalize = 0
        self._unattributed = 0

        # Per-agent breakdown. Agent name -> mutable running counters (kept as a small dict of
        # ints, frozen into an AgentTotals only on read).
        self._by_agent: dict[str, dict[str, int]] = {}

    # -- ingestion ---------------------------------------------------------------------

    def record(self, result: TurnResult, *, agent_name: str = "") -> None:
        """Fold one turn's result into the running totals.

        ``agent_name`` is optional so the ledger works with results that do not carry one; an
        empty name accumulates under the ``""`` bucket, which the report labels honestly rather
        than pretending it is a single agent.
        """
        timing = result.timing
        fw = timing.framework_ms
        md = timing.model_ms
        # Prefer the phase-summed total; fall back to latency_ms when phases are all zero (a
        # result built without a measured TurnTiming), so a blended-only turn still counts.
        total = timing.total_ms or result.latency_ms

        self._latency.append(total)
        self._framework.append(fw)
        self._model.append(md)

        self._turns += 1
        self._input += result.input_tokens
        self._output += result.output_tokens
        self._cache_read += result.cache_read_tokens
        self._cache_write += result.cache_write_tokens
        self._cost += result.cost_credits
        self._iterations += result.iterations
        self._setup += timing.setup_ms
        self._wait += timing.wait_ms
        self._stream += timing.stream_ms
        self._finalize += timing.finalize_ms
        self._unattributed += timing.unattributed_ms

        bucket = self._by_agent.setdefault(
            agent_name,
            {
                "turns": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "cost": 0, "iterations": 0, "framework_ms": 0, "model_ms": 0, "total_ms": 0,
            },
        )
        bucket["turns"] += 1
        bucket["input"] += result.input_tokens
        bucket["output"] += result.output_tokens
        bucket["cache_read"] += result.cache_read_tokens
        bucket["cache_write"] += result.cache_write_tokens
        bucket["cost"] += result.cost_credits
        bucket["iterations"] += result.iterations
        bucket["framework_ms"] += fw
        bucket["model_ms"] += md
        bucket["total_ms"] += total

    def record_timing(self, timing: TurnTiming) -> None:
        """Fold a bare ``TurnTiming`` in, for callers that have timing but no full result.

        Timing-only turns contribute to the latency/framework/model percentiles and phase sums
        but carry no tokens or cost — recorded honestly as a turn with zero token counters
        rather than skipped.
        """
        self._latency.append(timing.total_ms)
        self._framework.append(timing.framework_ms)
        self._model.append(timing.model_ms)
        self._turns += 1
        self._setup += timing.setup_ms
        self._wait += timing.wait_ms
        self._stream += timing.stream_ms
        self._finalize += timing.finalize_ms
        self._unattributed += timing.unattributed_ms

    # -- queries -----------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """True when no turns have been recorded. A fresh install is empty, not zeroed."""
        return self._turns == 0

    def totals(self) -> LedgerTotals:
        """Roll everything up. Percentiles are computed over three separate samples."""
        return LedgerTotals(
            turns=self._turns,
            input_tokens=self._input,
            output_tokens=self._output,
            cache_read_tokens=self._cache_read,
            cache_write_tokens=self._cache_write,
            cost_credits=self._cost,
            iterations=self._iterations,
            framework_ms=sum(self._framework),
            model_ms=sum(self._model),
            total_ms=sum(self._latency),
            setup_ms=self._setup,
            wait_ms=self._wait,
            stream_ms=self._stream,
            finalize_ms=self._finalize,
            unattributed_ms=self._unattributed,
            latency_pct=_percentiles(self._latency),
            framework_pct=_percentiles(self._framework),
            model_pct=_percentiles(self._model),
        )

    def agent_totals(self) -> dict[str, AgentTotals]:
        """Per-agent counters, name -> totals. Sorted by name for stable output."""
        out: dict[str, AgentTotals] = {}
        for name in sorted(self._by_agent):
            b = self._by_agent[name]
            out[name] = AgentTotals(
                turns=b["turns"],
                input_tokens=b["input"],
                output_tokens=b["output"],
                cache_read_tokens=b["cache_read"],
                cache_write_tokens=b["cache_write"],
                cost_credits=b["cost"],
                iterations=b["iterations"],
                framework_ms=b["framework_ms"],
                model_ms=b["model_ms"],
                total_ms=b["total_ms"],
            )
        return out

    def estimated_cost_usd(self, prices: Prices) -> float:
        """Total dollar cost at the supplied prices. See ``LedgerTotals.estimated_cost_usd``."""
        return self.totals().estimated_cost_usd(prices)
