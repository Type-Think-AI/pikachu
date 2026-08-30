"""Earth badge — Giovanni, Ground: grounded in measurement.

This is the test that catches *us* regressing our own code, independent of which model is in
use. It is a latency **budget**, not a benchmark: it asserts the part of a turn that Pikachu is
responsible for stays small, and that the phase arithmetic that lets us make that claim is
coherent.

Everything here builds ``TurnTiming`` / ``TurnResult`` objects directly. **No network, no
model, no backend** — a latency-budget gate that needed a provider round trip would be gating
on the provider's variance (2,345–3,336 ms on identical requests, per docs/23), which is the
exact number this badge exists to hold separate.
"""

from __future__ import annotations

import pytest

from pikachu.core.types import TurnResult, TurnTiming

pytestmark = pytest.mark.earth


# The framework-latency gate. Measured framework overhead is ~0.3 ms of a ~2,900 ms turn
# (docs/23-framework-comparison.md). The gate is set at 5 ms — over 16× the measured value — on
# purpose:
#
#   * it catches a GENUINE regression (e.g. rebuilding a tool schema per turn instead of using
#     the cached toolset would push framework time up by hundreds of µs to ms), while
#   * staying well clear of flakiness on a loaded CI box, where scheduling jitter alone can add
#     a millisecond or two to a "0.3 ms" operation.
#
# DO NOT tighten this toward the measured 0.3 ms. A gate pinned near the mean fails on a busy
# machine for reasons that have nothing to do with our code, gets marked flaky, and then gets
# disabled — which loses the regression signal entirely. 5 ms is the deliberate slack.
FRAMEWORK_BUDGET_MS = 5


# --------------------------------------------------------------------------------------
# The budget itself
# --------------------------------------------------------------------------------------


@pytest.mark.earth
def test_framework_ms_within_budget_at_measured_reality() -> None:
    """A turn built at measured phase values keeps framework_ms under the 5 ms budget."""
    # Measured: setup ~0.2 ms, finalize ~0.1 ms, provider ~2,900 ms. Rounded to the ms grain
    # TurnTiming stores, framework is ~0-1 ms.
    timing = TurnTiming(setup_ms=0, wait_ms=2700, stream_ms=200, finalize_ms=0, total_ms=2900)
    assert timing.framework_ms <= FRAMEWORK_BUDGET_MS


@pytest.mark.earth
def test_framework_budget_catches_a_regression() -> None:
    """A turn that spends real time in our own code busts the budget — the gate has teeth.

    This is the failure the badge is meant to catch: if setup climbs (e.g. per-turn schema
    regeneration), framework_ms crosses 5 ms and this assertion would fire in CI.
    """
    regressed = TurnTiming(setup_ms=8, wait_ms=2700, stream_ms=200, finalize_ms=1, total_ms=2909)
    assert regressed.framework_ms > FRAMEWORK_BUDGET_MS


# --------------------------------------------------------------------------------------
# Phase arithmetic is coherent
# --------------------------------------------------------------------------------------


@pytest.mark.earth
def test_framework_plus_model_never_exceeds_total() -> None:
    """framework_ms + model_ms <= total_ms. Attribution cannot invent time."""
    timing = TurnTiming(setup_ms=1, wait_ms=2000, stream_ms=200, finalize_ms=1, total_ms=2300)
    assert timing.framework_ms + timing.model_ms <= timing.total_ms


@pytest.mark.earth
def test_unattributed_ms_stays_small_and_nonnegative() -> None:
    """The gap the phases did not account for is small and never negative."""
    timing = TurnTiming(setup_ms=1, wait_ms=2000, stream_ms=200, finalize_ms=1, total_ms=2205)
    # 1 + 2000 + 200 + 1 = 2202; total 2205 → 3 ms unattributed.
    assert timing.unattributed_ms == 3
    assert timing.unattributed_ms >= 0


@pytest.mark.earth
def test_unattributed_never_negative_when_phases_exceed_total() -> None:
    """A total smaller than the phase sum clamps unattributed at 0, not a negative number."""
    timing = TurnTiming(setup_ms=1, wait_ms=2000, stream_ms=200, finalize_ms=1, total_ms=100)
    assert timing.unattributed_ms == 0


@pytest.mark.earth
def test_framework_ms_is_setup_plus_finalize() -> None:
    timing = TurnTiming(setup_ms=3, finalize_ms=2, wait_ms=0, stream_ms=0, total_ms=5)
    assert timing.framework_ms == 5


@pytest.mark.earth
def test_model_ms_is_wait_plus_stream() -> None:
    timing = TurnTiming(setup_ms=0, finalize_ms=0, wait_ms=2000, stream_ms=200, total_ms=2200)
    assert timing.model_ms == 2200


# --------------------------------------------------------------------------------------
# cache_hit_ratio at boundaries, including all-zero (no ZeroDivisionError)
# --------------------------------------------------------------------------------------


@pytest.mark.earth
def test_cache_hit_ratio_all_zero_tokens_is_zero_not_error() -> None:
    """A turn with no tokens at all computes a 0.0 ratio rather than dividing by zero."""
    result = TurnResult(text="", input_tokens=0, cache_read_tokens=0)
    assert result.cache_hit_ratio == 0.0


@pytest.mark.earth
def test_cache_hit_ratio_full_hit() -> None:
    result = TurnResult(text="", input_tokens=0, cache_read_tokens=1000)
    assert result.cache_hit_ratio == pytest.approx(1.0)


@pytest.mark.earth
def test_cache_hit_ratio_no_hit() -> None:
    result = TurnResult(text="", input_tokens=1000, cache_read_tokens=0)
    assert result.cache_hit_ratio == 0.0


@pytest.mark.earth
def test_cache_hit_ratio_partial() -> None:
    result = TurnResult(text="", input_tokens=800, cache_read_tokens=1200)
    assert result.cache_hit_ratio == pytest.approx(1200 / 2000)


# --------------------------------------------------------------------------------------
# tokens_per_second uses stream_ms, not total
# --------------------------------------------------------------------------------------


@pytest.mark.earth
def test_tokens_per_second_uses_stream_ms() -> None:
    """Decode throughput divides by stream time, so provider queue does not dilute it."""
    # 490 tokens in 100 ms of decode = 4,900 tok/s (the measured decode rate). A large wait
    # must not be part of the denominator.
    timing = TurnTiming(setup_ms=1, wait_ms=9000, stream_ms=100, finalize_ms=1, total_ms=9102)
    assert timing.tokens_per_second(490) == pytest.approx(4900.0)


@pytest.mark.earth
def test_tokens_per_second_not_diluted_by_wait() -> None:
    """Same output, same decode time, wildly different wait → identical tok/s."""
    fast_queue = TurnTiming(stream_ms=100, wait_ms=500, total_ms=600)
    slow_queue = TurnTiming(stream_ms=100, wait_ms=9000, total_ms=9100)
    assert fast_queue.tokens_per_second(490) == slow_queue.tokens_per_second(490)


@pytest.mark.earth
def test_tokens_per_second_zero_stream_is_zero_not_error() -> None:
    """No decode time → 0.0, not a ZeroDivisionError."""
    timing = TurnTiming(stream_ms=0, wait_ms=2000, total_ms=2000)
    assert timing.tokens_per_second(100) == 0.0
