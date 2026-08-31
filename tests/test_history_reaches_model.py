"""Conversation history must actually reach the model.

Regression cover for a production bug: ``TurnRequest.history`` was accepted, stored and
then **dropped**. ``message_history`` appeared nowhere in the backend except a docstring,
so ``agent.run(message)`` was called with the new prompt only. Every turn arrived at the
provider as a first turn and the agent had no memory of the conversation, while the host
(picx-studio) was passing history correctly the whole time.

The lesson these tests encode: asserting "a turn succeeded" cannot catch a dropped
history, because a context-free turn succeeds perfectly well. The assertion has to be on
what was handed to the model.
"""

from __future__ import annotations

import pytest

from pikachu.backends.pydantic_ai import PydanticAIBackend, _to_message_history

HISTORY = (
    {"role": "user", "content": "my cat is called Mochi"},
    {"role": "assistant", "content": "Nice to meet Mochi!"},
)


class TestConversion:
    def test_maps_roles_to_request_and_response(self) -> None:
        msgs = _to_message_history(HISTORY)
        assert [m.kind for m in msgs] == ["request", "response"]
        assert msgs[0].parts[0].content == "my cat is called Mochi"
        assert msgs[1].parts[0].content == "Nice to meet Mochi!"

    def test_preserves_order_oldest_first(self) -> None:
        msgs = _to_message_history(
            ({"role": "user", "content": "one"}, {"role": "user", "content": "two"})
        )
        assert [m.parts[0].content for m in msgs] == ["one", "two"]

    def test_accepts_alias_roles(self) -> None:
        msgs = _to_message_history(
            ({"role": "human", "content": "a"}, {"role": "model", "content": "b"})
        )
        assert [m.kind for m in msgs] == ["request", "response"]

    @pytest.mark.parametrize(
        "item",
        [
            {"role": "system", "content": "you are helpful"},  # belongs to AgentSpec
            {"role": "tool", "content": "{}"},  # invalid without its call id
            {"role": "user", "content": "   "},  # blank -> provider validation error
            {"role": "user", "content": None},
            {"role": "user"},
            "not-a-mapping",
        ],
    )
    def test_drops_what_must_not_be_replayed(self, item) -> None:
        assert _to_message_history((item,)) == []

    def test_empty_and_none_are_safe(self) -> None:
        assert _to_message_history(()) == []
        assert _to_message_history(None) == []


class TestThreading:
    @pytest.mark.asyncio
    async def test_call_forwards_history_to_the_agent(self, monkeypatch) -> None:
        """The bug was here: history existed but never became `message_history`."""
        seen: dict[str, object] = {}

        class _Result:
            output = "ok"

            def all_messages(self):
                return []

        class _Agent:
            async def run(self, message, *, message_history=None):
                seen["message"] = message
                seen["message_history"] = message_history
                return _Result()

        backend = PydanticAIBackend(api_key="test-key", measure_streaming=False)
        history = _to_message_history(HISTORY)

        await backend._call(_Agent(), "what is my cat called?", history)

        assert seen["message"] == "what is my cat called?"
        assert seen["message_history"] == history, "history must reach the model"
        assert len(seen["message_history"]) == 2

    @pytest.mark.asyncio
    async def test_no_history_passes_none_not_empty_list(self, monkeypatch) -> None:
        # An empty list is not the same as absent for every provider; keep the
        # first-turn request shape identical to what it was before history existed.
        seen: dict[str, object] = {}

        class _Result:
            output = "ok"

            def all_messages(self):
                return []

        class _Agent:
            async def run(self, message, *, message_history=None):
                seen["message_history"] = message_history
                return _Result()

        backend = PydanticAIBackend(api_key="test-key", measure_streaming=False)
        await backend._call(_Agent(), "hi", [])
        assert seen["message_history"] is None
