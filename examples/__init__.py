"""Runnable examples that PROVE success criteria, rather than asserting them.

Each module here exercises a claim from the PRD end to end so that a reader can run it and
watch the property hold, and so that ``tests/test_examples.py`` can drive the same code path
offline in CI. An example that only works live is an example that rots, so every example
runs on :class:`pikachu.backends.fake.FakeBackend` with no network and takes a ``--live``
flag for an optional real run.

    canvas_handoff            S5 — blackboard coordination without argument passing
    create_agent_at_runtime   S4 — an end user creates and invokes an agent, no code change

``scripts/measure_cache.py`` covers S1 (prompt-cache floor) as a script rather than an
example, because it costs real money and must never run in CI.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
