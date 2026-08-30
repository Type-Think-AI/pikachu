"""Shared fixtures. Reserved file — lanes must not edit this.

Need a fixture that is not here? Write ``HANDOFF-<LANE>.md`` with the exact code.

House rules enforced here:
  * no network, ever — not even "it's just an embedding call"
  * deterministic — no wall-clock or random dependence in assertions
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

from pikachu.core.types import (
    AgentSpec,
    Lineage,
    Skill,
    SkillStatus,
    Taint,
    ToolSpec,
    TrustTier,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hard-fail any test that opens a socket.

    Autouse and non-negotiable. A test suite that quietly reaches the network is one that
    fails in CI for reasons unrelated to the code under test.
    """
    import socket

    def _blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "network access is not allowed in tests - inject a fake instead"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


class StubEmbedder:
    """Deterministic embedder. Same text always yields the same vector, no network.

    Hash-based, so it carries no real semantics: two texts that MEAN the same thing get
    unrelated vectors. Use it to test plumbing and thresholds, never to assert that a
    semantic judgement is correct.
    """

    dimensions = 16

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        out: list[tuple[float, ...]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [digest[i] / 255.0 for i in range(self.dimensions)]
            norm = sum(v * v for v in raw) ** 0.5 or 1.0
            out.append(tuple(v / norm for v in raw))
        return tuple(out)


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def fixed_allowlist() -> tuple[str, ...]:
    """A representative host allowlist. The ONLY source of tool authority."""
    return ("web_search", "generate_image", "read_canvas", "write_canvas")


@pytest.fixture
def builtin_skill() -> Skill:
    """A trusted in-repo skill that may declare tools."""
    return Skill(
        name="brand-palette",
        description="Apply the house colour palette to a generated image.",
        body="# Brand palette\n\nNever use pure black. Never crop tighter than 16:9.\n",
        declared_tools=("generate_image",),
        status=SkillStatus.ACTIVE,
        trust=TrustTier.BUILTIN,
    )


@pytest.fixture
def foreign_skill() -> Skill:
    """An untrusted third-party skill.

    Declares no tools — the model validator makes that structurally impossible at this
    trust tier, so any test wanting a tool-declaring foreign skill is testing that the
    validator rejects it.
    """
    return Skill(
        name="community-sticker-sheet",
        description="Produce a sticker sheet from a subject photo.",
        body="# Sticker sheet\n\nCut out the subject and tile it six times.\n",
        status=SkillStatus.CANDIDATE,
        trust=TrustTier.UNTRUSTED,
        lineage=Lineage.clean().with_taint(Taint.FOREIGN_SKILL, "catalog:community"),
    )


@pytest.fixture
def agent() -> AgentSpec:
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        instructions="Match the brand palette. Never invent a new palette.",
        skill_tags=("colour", "grade"),
        allowed_tools=("generate_image", "read_canvas"),
    )


@pytest.fixture
def metered_tool() -> ToolSpec:
    return ToolSpec(
        name="generate_image",
        description="Generate an image. Costs credits.",
        cost_credits=35,
    )
