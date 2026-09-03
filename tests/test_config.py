"""Config defaults are decisions, so they get pinned like any other invariant."""

from __future__ import annotations

import pytest

from pikachu.config import (
    CACHE_FLOOR_UNVERIFIED,
    DEFAULT_MODEL,
    STABLE_PREFIX_TOKENS_MAX,
    STABLE_PREFIX_TOKENS_MIN,
    cache_is_expected_to_fire,
)


@pytest.mark.boulder
def test_default_model_is_the_owners_choice() -> None:
    """Pinned so the default cannot drift without a visible test change."""
    assert DEFAULT_MODEL == "google/gemini-3.8-flash"


@pytest.mark.boulder
def test_prefix_range_is_sane() -> None:
    assert 0 < STABLE_PREFIX_TOKENS_MIN < STABLE_PREFIX_TOKENS_MAX


@pytest.mark.boulder
def test_cache_floor_is_still_flagged_unverified() -> None:
    """This must stay True until somebody MEASURES a real turn.

    When the floor is measured, flip the flag in config.py, record the number in
    docs/22-phase0-verification.md, and update this test in the same commit. The point is
    that clearing the flag requires a deliberate edit rather than happening by drift.
    """
    assert CACHE_FLOOR_UNVERIFIED is True


@pytest.mark.boulder
@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        (128, True),  # DeepSeek-class - fires comfortably
        (512, True),  # Claude Opus 5 class
        (1024, True),  # Sonnet 4.5 / OpenAI / Gemini 2.5 Flash
        (1500, True),  # exactly our pessimistic prefix
        (2048, False),  # Gemini 2.5 Pro / Opus 4.7 - only fires on a larger prompt
        (4096, False),  # the blanket Gemini figure - would make caching a no-op
    ],
)
def test_cache_expectation_uses_the_pessimistic_prefix_end(floor: int, expected: bool) -> None:
    """A floor only 'fires' when it clears our SMALLEST prompt, not our largest.

    2,048 returning False is the important row: our prefix range straddles it, so caching
    would fire on some turns and not others. That is worse than never firing, because the
    metric looks alive while being unreliable.
    """
    assert cache_is_expected_to_fire(DEFAULT_MODEL, floor_tokens=floor) is expected
