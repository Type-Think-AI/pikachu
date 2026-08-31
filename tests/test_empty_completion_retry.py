"""The provider that answers HTTP 200 and says nothing.

Regression cover for a production failure on picx-studio: OpenRouter intermittently
returned a ChatCompletion with every field null except ``created``, so an ordinary "hi"
succeeded on one attempt and failed on the next. pydantic-ai surfaces that as
``UnexpectedModelBehavior`` from response validation, which ``Agent(retries=...)`` does
not cover — that budget is for tool/output validation and a malformed envelope never
reaches it.

These tests pin both halves of the contract: the empty envelope is retried, and anything
that is a real answer (refusal, auth, rate limit, other model misbehaviour) is not.
"""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from pikachu.backends.pydantic_ai import (
    _MAX_UPSTREAM_ATTEMPTS,
    PydanticAIBackend,
    _is_empty_completion,
)

# Verbatim shape of the production failure (trimmed after the first field).
EMPTY_ENVELOPE_MSG = (
    "Invalid response from openrouter chat completions endpoint: "
    "4 validation errors for ChatCompletion\n"
    "id\n  Input should be a valid string "
    "[type=string_type, input_value=None, input_type=NoneType]"
)


def _backend() -> PydanticAIBackend:
    # No network happens at construction; the key only has to be non-empty.
    return PydanticAIBackend(api_key="test-key", measure_streaming=False)


class TestPredicate:
    def test_matches_the_production_message(self) -> None:
        assert _is_empty_completion(UnexpectedModelBehavior(EMPTY_ENVELOPE_MSG))

    def test_ignores_other_model_misbehaviour(self) -> None:
        # Real UnexpectedModelBehavior that is NOT an empty envelope: retrying is not
        # obviously correct, so it must surface on the first attempt.
        assert not _is_empty_completion(
            UnexpectedModelBehavior("Exceeded maximum retries (2) for output validation")
        )

    def test_ignores_unrelated_exceptions(self) -> None:
        assert not _is_empty_completion(RuntimeError(EMPTY_ENVELOPE_MSG))
        assert not _is_empty_completion(ValueError("boom"))


class TestRetry:
    @pytest.mark.asyncio
    async def test_recovers_when_a_later_attempt_succeeds(self, monkeypatch) -> None:
        calls = {"n": 0}
        sentinel = object()

        async def flaky(_self, _agent, _message, _history=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise UnexpectedModelBehavior(EMPTY_ENVELOPE_MSG)
            return sentinel

        monkeypatch.setattr(PydanticAIBackend, "_call_once", flaky)
        monkeypatch.setattr("pikachu.backends.pydantic_ai.asyncio.sleep", _no_sleep)

        assert await _backend()._call(object(), "hi") is sentinel
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_gives_up_and_reraises_after_the_budget(self, monkeypatch) -> None:
        calls = {"n": 0}

        async def always_empty(_self, _agent, _message, _history=None):
            calls["n"] += 1
            raise UnexpectedModelBehavior(EMPTY_ENVELOPE_MSG)

        monkeypatch.setattr(PydanticAIBackend, "_call_once", always_empty)
        monkeypatch.setattr("pikachu.backends.pydantic_ai.asyncio.sleep", _no_sleep)

        # A provider that is genuinely down must still fail, not spin.
        with pytest.raises(UnexpectedModelBehavior):
            await _backend()._call(object(), "hi")
        assert calls["n"] == _MAX_UPSTREAM_ATTEMPTS

    @pytest.mark.asyncio
    async def test_does_not_retry_a_real_answer(self, monkeypatch) -> None:
        calls = {"n": 0}

        async def refused(_self, _agent, _message, _history=None):
            calls["n"] += 1
            raise UnexpectedModelBehavior("content_policy_violation")

        monkeypatch.setattr(PydanticAIBackend, "_call_once", refused)

        with pytest.raises(UnexpectedModelBehavior):
            await _backend()._call(object(), "hi")
        assert calls["n"] == 1, "a refusal must surface immediately, not be hammered"


async def _no_sleep(_seconds: float) -> None:
    """Keep the retry test at unit speed without asserting on wall clock."""
    return None
