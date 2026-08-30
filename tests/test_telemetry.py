"""Unit tests for the in-memory telemetry ledger.

These cover the invariants that make the ledger *useful* rather than merely present:

* framework and model latency are accumulated and percentiled **separately** — the whole point
  is that you can tell our regressions from the provider's variance;
* decode throughput uses ``stream_ms`` only, never total or wait;
* the cost estimator takes prices as a parameter and hardcodes nothing;
* boundaries are safe — an empty ledger and all-zero tokens do not divide by zero.

No network, no I/O. TurnResult/TurnTiming objects are built directly.
"""

from __future__ import annotations

import pytest

from pikachu.core.types import TurnResult, TurnTiming
from pikachu.telemetry import Prices, TokenLedger


# --------------------------------------------------------------------------------------
# Helpers — build a result with a real phase-split timing
# --------------------------------------------------------------------------------------


def _result(
    *,
    setup: int = 1,
    wait: int = 2000,
    stream: int = 200,
    finalize: int = 1,
    total: int | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 100,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: int = 0,
    iterations: int = 1,
) -> TurnResult:
    if total is None:
        total = setup + wait + stream + finalize
    timing = TurnTiming(
        setup_ms=setup,
        wait_ms=wait,
        stream_ms=stream,
        finalize_ms=finalize,
        total_ms=total,
        streaming_measured=True,
    )
    return TurnResult(
        text="ok",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_credits=cost,
        iterations=iterations,
        latency_ms=total,
        timing=timing,
    )


# --------------------------------------------------------------------------------------
# Empty state
# --------------------------------------------------------------------------------------


def test_fresh_ledger_is_empty() -> None:
    ledger = TokenLedger()
    assert ledger.is_empty
    totals = ledger.totals()
    assert totals.turns == 0
    # Every ratio is boundary-safe on an empty ledger — no ZeroDivisionError.
    assert totals.cache_hit_ratio == 0.0
    assert totals.framework_share == 0.0
    assert totals.cost_per_turn_credits == 0.0
    assert totals.tokens_per_second == 0.0


def test_empty_percentiles_are_zero_with_zero_n() -> None:
    totals = TokenLedger().totals()
    for pct in (totals.latency_pct, totals.framework_pct, totals.model_pct):
        assert (pct.p50, pct.p90, pct.p99, pct.n) == (0.0, 0.0, 0.0, 0)


# --------------------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------------------


def test_record_accumulates_tokens_and_cost() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=1000, output_tokens=100, cost=35))
    ledger.record(_result(input_tokens=500, output_tokens=50, cost=15))
    totals = ledger.totals()
    assert totals.turns == 2
    assert totals.input_tokens == 1500
    assert totals.output_tokens == 150
    assert totals.cost_credits == 50


def test_phase_sums_split_framework_from_model() -> None:
    ledger = TokenLedger()
    ledger.record(_result(setup=3, wait=2000, stream=200, finalize=2))
    totals = ledger.totals()
    # framework = setup + finalize; model = wait + stream. Never blended.
    assert totals.framework_ms == 5
    assert totals.model_ms == 2200
    assert totals.setup_ms == 3
    assert totals.finalize_ms == 2
    assert totals.wait_ms == 2000
    assert totals.stream_ms == 200


def test_framework_share_is_tiny_at_measured_reality() -> None:
    # Calibrated to docs/23: ~0.3 ms framework in a ~2,900 ms turn is ~0.01%.
    ledger = TokenLedger()
    ledger.record(_result(setup=0, finalize=1, wait=2900, stream=0, total=2901))
    assert ledger.totals().framework_share < 0.01


# --------------------------------------------------------------------------------------
# Percentiles are over SEPARATE samples
# --------------------------------------------------------------------------------------


def test_latency_framework_model_percentiles_are_independent() -> None:
    ledger = TokenLedger()
    # Three turns where framework is constant-small but model swings wildly — exactly the
    # scenario where a blended percentile would be dominated by provider variance.
    ledger.record(_result(setup=1, finalize=1, wait=2345, stream=100))
    ledger.record(_result(setup=1, finalize=1, wait=2907, stream=100))
    ledger.record(_result(setup=1, finalize=1, wait=3336, stream=100))
    totals = ledger.totals()
    # Framework percentile is flat (2 ms everywhere) — our code did not move.
    assert totals.framework_pct.p50 == pytest.approx(2.0)
    assert totals.framework_pct.p99 == pytest.approx(2.0)
    # Model percentile spans the provider's real variance — that is the noisy axis.
    assert totals.model_pct.p50 == pytest.approx(3007.0)  # 2907 + 100
    assert totals.model_pct.p99 > totals.model_pct.p50
    # And they are genuinely different samples.
    assert totals.framework_pct.p50 != totals.model_pct.p50


def test_percentiles_carry_sample_count() -> None:
    ledger = TokenLedger()
    for _ in range(5):
        ledger.record(_result())
    assert ledger.totals().latency_pct.n == 5


def test_single_sample_percentiles_are_that_sample() -> None:
    ledger = TokenLedger()
    ledger.record(_result(wait=2000, stream=200, setup=1, finalize=1, total=2202))
    p = ledger.totals().latency_pct
    # One sample: p50 == p99 == the value, honestly reported with n=1.
    assert p.p50 == p.p90 == p.p99 == 2202.0
    assert p.n == 1


# --------------------------------------------------------------------------------------
# Decode throughput uses stream_ms only
# --------------------------------------------------------------------------------------


def test_tokens_per_second_uses_stream_not_total() -> None:
    ledger = TokenLedger()
    # 490 output tokens in 100 ms of decode = 4,900 tok/s, matching the measured decode rate.
    # A huge wait must NOT drag this down.
    ledger.record(_result(output_tokens=490, stream=100, wait=9000, setup=0, finalize=0))
    tps = ledger.totals().tokens_per_second
    assert tps == pytest.approx(4900.0)
    # Sanity: if it had used total (9100 ms) it would be ~54 tok/s, wildly wrong.
    assert tps > 1000


def test_tokens_per_second_zero_when_no_stream_time() -> None:
    ledger = TokenLedger()
    ledger.record(_result(output_tokens=100, stream=0, wait=2000, total=2000))
    assert ledger.totals().tokens_per_second == 0.0


# --------------------------------------------------------------------------------------
# Cache-hit ratio boundaries
# --------------------------------------------------------------------------------------


def test_cache_hit_ratio_computed() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=800, cache_read=1200))
    # 1200 cached / (800 + 1200) prompt = 0.6
    assert ledger.totals().cache_hit_ratio == pytest.approx(0.6)


def test_cache_hit_ratio_all_zero_tokens_no_division_error() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=0, cache_read=0, output_tokens=0))
    assert ledger.totals().cache_hit_ratio == 0.0


# --------------------------------------------------------------------------------------
# Cost estimator takes prices as a parameter
# --------------------------------------------------------------------------------------


def test_cost_estimator_uses_supplied_prices() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=1_000_000, output_tokens=1_000_000,
                          cache_read=1_000_000, cache_write=1_000_000))
    prices = Prices(
        prompt_per_mtok=0.75,
        completion_per_mtok=3.75,
        cache_read_per_mtok=0.075,
        cache_write_per_mtok=0.0417,
    )
    # 1 MTok of each class = sum of the four per-MTok prices.
    expected = 0.75 + 3.75 + 0.075 + 0.0417
    assert ledger.estimated_cost_usd(prices) == pytest.approx(expected)


def test_cost_estimator_different_prices_give_different_answers() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0))
    cheap = Prices(prompt_per_mtok=0.10, completion_per_mtok=0.0)
    dear = Prices(prompt_per_mtok=10.0, completion_per_mtok=0.0)
    assert ledger.estimated_cost_usd(dear) > ledger.estimated_cost_usd(cheap)
    # No hidden constant: the answer is exactly the price we passed.
    assert ledger.estimated_cost_usd(cheap) == pytest.approx(0.10)


def test_cost_estimator_zero_prices_zero_cost() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=5000, output_tokens=5000))
    assert ledger.estimated_cost_usd(Prices(prompt_per_mtok=0.0, completion_per_mtok=0.0)) == 0.0


# --------------------------------------------------------------------------------------
# Per-agent breakdown
# --------------------------------------------------------------------------------------


def test_per_agent_totals_separated() -> None:
    ledger = TokenLedger()
    ledger.record(_result(cost=35), agent_name="colourist")
    ledger.record(_result(cost=15), agent_name="colourist")
    ledger.record(_result(cost=20), agent_name="writer")
    agents = ledger.agent_totals()
    assert set(agents) == {"colourist", "writer"}
    assert agents["colourist"].turns == 2
    assert agents["colourist"].cost_credits == 50
    assert agents["writer"].turns == 1
    assert agents["writer"].cost_credits == 20


def test_per_agent_cache_ratio_boundary_safe() -> None:
    ledger = TokenLedger()
    ledger.record(_result(input_tokens=0, cache_read=0), agent_name="x")
    assert ledger.agent_totals()["x"].cache_hit_ratio == 0.0


def test_unnamed_agent_bucketed_under_empty_string() -> None:
    ledger = TokenLedger()
    ledger.record(_result())
    assert "" in ledger.agent_totals()


# --------------------------------------------------------------------------------------
# record_timing — timing without a full result
# --------------------------------------------------------------------------------------


def test_record_timing_counts_a_turn_with_zero_tokens() -> None:
    ledger = TokenLedger()
    ledger.record_timing(TurnTiming(setup_ms=1, wait_ms=2000, stream_ms=200, finalize_ms=1,
                                    total_ms=2202, streaming_measured=True))
    totals = ledger.totals()
    assert totals.turns == 1
    assert totals.framework_ms == 2
    assert totals.model_ms == 2200
    assert totals.output_tokens == 0
    assert totals.tokens_per_second == 0.0


# --------------------------------------------------------------------------------------
# Fallback: blended-only result (no measured phases) still counts
# --------------------------------------------------------------------------------------


def test_result_without_phases_falls_back_to_latency_ms() -> None:
    ledger = TokenLedger()
    blended = TurnResult(text="ok", latency_ms=2900, timing=TurnTiming())
    ledger.record(blended)
    totals = ledger.totals()
    assert totals.turns == 1
    # total came from latency_ms since phases were all zero.
    assert totals.total_ms == 2900
    # framework/model are honestly zero — nothing was attributed.
    assert totals.framework_ms == 0
    assert totals.model_ms == 0
