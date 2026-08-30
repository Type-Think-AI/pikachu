"""Confusability detection between skill descriptions.

WHY THIS EXISTS
---------------
Skill-selection accuracy does not degrade gracefully as a skill library grows. The observed
shape is a plateau followed by a cliff: accuracy stays roughly stable as skills are added,
then drops sharply once the selectable set contains descriptions that are semantically close
to one another. The driver of the cliff is **semantic confusability among similar skills**,
not library size on its own — a large library of well-separated skills selects fine, while a
small library with two near-duplicate descriptions can already mis-select.

The failure mode is **silent**. When two descriptions are confusable the model picks the
wrong skill and nothing errors: no exception, no log line, just a subtly worse answer. That
silence is exactly why this check runs at *authoring* time — the only cheap moment to catch
it is before the second confusable skill is admitted to the partition.

WHAT WE DO ABOUT IT
-------------------
We **warn, we do not reject.** A human may have a legitimate reason for two similar skills
(e.g. a general and a specialised variant that a router disambiguates on other signals).
Rejecting would substitute the tool's judgement for the author's, and would be wrong often
enough to get the check disabled. So :func:`check_new_skill` returns a report with a
``breaches_threshold`` flag; acting on it is the caller's call.

SCOPE: everything here is scoped to a **partition** — the set of skills a given agent
actually selects from. Confusability across partitions is irrelevant because the model never
chooses between them, so the caller passes only the descriptions in the relevant partition.

STATUS OF THE NUMBERS
---------------------
The mechanism above is drawn from a **single-author preliminary technical report**. Treat it
as *plausible* rather than settled: the qualitative claim (a sharp, confusability-driven
cliff) is the useful part; the specific threshold at which it triggers is **unconfirmed**.
The ``0.85`` default in :func:`check_new_skill` is **our own choice**, not a published
figure — a starting point to tune against real selection data, not a validated constant.

NETWORK
-------
This module never makes a network call. It accepts an :class:`~pikachu.core.protocols.Embedder`
as a parameter and calls it; whether that embedder is local or remote is the caller's
concern, and in tests it is the deterministic hash-based stub. No provider is hardcoded.
"""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.protocols import Embedder

__all__ = [
    "ConfusabilityReport",
    "SimilarityPair",
    "cosine_similarity",
    "max_pairwise_similarity",
    "check_new_skill",
]


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity of two vectors, in ``[-1.0, 1.0]``.

    Returns ``0.0`` if either vector is all-zero (undefined direction) — a safe, non-warning
    answer rather than a division by zero. Raises ``ValueError`` on a length mismatch, since
    comparing vectors from different embedders is a bug, not a low score.
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


class SimilarityPair(BaseModel):
    """A pair of descriptions and their similarity. Indices refer to the input sequence."""

    model_config = ConfigDict(frozen=True)

    index_a: int
    index_b: int
    text_a: str
    text_b: str
    score: float


class ConfusabilityReport(BaseModel):
    """The outcome of checking one new description against an existing partition.

    A report is advisory. ``breaches_threshold`` being ``True`` is a WARNING that a human
    should look, never an instruction to reject the skill.
    """

    model_config = ConfigDict(frozen=True)

    new_description: str
    partition: str | None = None
    threshold: float

    nearest_description: str | None = None
    """The existing description most similar to the new one, or ``None`` when the partition
    was empty (the first skill in a partition can never be confusable with anything)."""

    nearest_score: float = 0.0
    breaches_threshold: bool = False


async def _embed_all(
    embedder: Embedder, texts: tuple[str, ...]
) -> tuple[tuple[float, ...], ...]:
    """One batched embed call. Batching is the caller-visible contract of the protocol."""
    if not texts:
        return ()
    return await embedder.embed(texts)


async def max_pairwise_similarity(
    descriptions: tuple[str, ...],
    *,
    embedder: Embedder,
) -> SimilarityPair | None:
    """Most-similar pair within a partition, or ``None`` for fewer than two descriptions.

    Embeds every description once (a single batched call) and compares all
    ``n * (n - 1) / 2`` pairs. Intended for an authoring-time audit of a whole partition; it
    is ``O(n^2)`` in the comparison step, which is fine for the partition sizes a single
    agent selects from and is not meant for a whole-library sweep.
    """
    if len(descriptions) < 2:
        return None
    vectors = await _embed_all(embedder, descriptions)
    best: SimilarityPair | None = None
    for i in range(len(descriptions)):
        for j in range(i + 1, len(descriptions)):
            score = cosine_similarity(vectors[i], vectors[j])
            if best is None or score > best.score:
                best = SimilarityPair(
                    index_a=i,
                    index_b=j,
                    text_a=descriptions[i],
                    text_b=descriptions[j],
                    score=score,
                )
    return best


async def check_new_skill(
    new_description: str,
    existing_descriptions: tuple[str, ...],
    *,
    embedder: Embedder,
    threshold: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.85,
    partition: str | None = None,
) -> ConfusabilityReport:
    """Check one new description against the descriptions already in its partition.

    Returns the nearest existing description, its cosine score, and whether that score meets
    or exceeds ``threshold``. An empty ``existing_descriptions`` yields a non-breaching
    report with no nearest match — the first skill in a partition is confusable with nothing.

    ``threshold`` defaults to ``0.85``, which is **our own default, not a published number**
    (see the module docstring). Tune it against real selection data. A breach WARNS; it never
    rejects — that decision belongs to a human.
    """
    if not existing_descriptions:
        return ConfusabilityReport(
            new_description=new_description,
            partition=partition,
            threshold=threshold,
            nearest_description=None,
            nearest_score=0.0,
            breaches_threshold=False,
        )

    # One batched call: the new description first, then the partition.
    vectors = await _embed_all(embedder, (new_description, *existing_descriptions))
    new_vec = vectors[0]

    nearest_idx = 0
    nearest_score = -math.inf
    for offset, vec in enumerate(vectors[1:]):
        score = cosine_similarity(new_vec, vec)
        if score > nearest_score:
            nearest_score = score
            nearest_idx = offset

    return ConfusabilityReport(
        new_description=new_description,
        partition=partition,
        threshold=threshold,
        nearest_description=existing_descriptions[nearest_idx],
        nearest_score=nearest_score,
        breaches_threshold=nearest_score >= threshold,
    )
