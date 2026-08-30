"""Confusability plumbing and threshold tests.

The embedder here is the hash-based ``StubEmbedder`` from conftest. It carries NO semantics:
two strings that mean the same thing get unrelated vectors. So these tests exercise the
plumbing and the threshold logic only, and never assert that a semantic judgement is
correct — asserting "these two similar sentences are near each other" against a hash stub
would be testing the stub's hash, not the code.

Network is blocked by the autouse ``_no_network`` fixture in conftest; the explicit test at
the bottom documents that an embedder that reaches out fails loudly.
"""

from __future__ import annotations

import pytest

from pikachu.skills.confusability import (
    ConfusabilityReport,
    SimilarityPair,
    check_new_skill,
    cosine_similarity,
    max_pairwise_similarity,
)

# StubEmbedder is defined in conftest; import it for the no-network probe test.
from tests.conftest import StubEmbedder


# --------------------------------------------------------------------------------------
# cosine_similarity — pure math, no embedder
# --------------------------------------------------------------------------------------


def test_cosine_identical_vectors_is_one() -> None:
    v = (0.1, 0.2, 0.3, 0.4)
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero() -> None:
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one() -> None:
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero_not_error() -> None:
    # Undefined direction -> safe, non-warning 0.0 rather than a ZeroDivisionError.
    assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0


def test_cosine_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0))


# --------------------------------------------------------------------------------------
# Identical descriptions score ~1.0 through the real embed path
# --------------------------------------------------------------------------------------


async def test_identical_descriptions_score_near_one(embedder: StubEmbedder) -> None:
    # Same text -> same stub vector -> cosine ~1.0. This is a plumbing fact, not semantics.
    report = await check_new_skill(
        "Apply the house palette to a still.",
        ("Apply the house palette to a still.",),
        embedder=embedder,
    )
    assert report.nearest_score == pytest.approx(1.0)
    assert report.breaches_threshold is True
    assert report.nearest_description == "Apply the house palette to a still."


async def test_distinct_texts_do_not_falsely_breach(embedder: StubEmbedder) -> None:
    # Two DIFFERENT strings get unrelated hash vectors; with a high threshold they must not
    # breach. We assert the threshold plumbing, not that the stub "understands" difference.
    report = await check_new_skill(
        "aaaaaaaa",
        ("zzzzzzzz",),
        embedder=embedder,
        threshold=0.999,
    )
    assert report.breaches_threshold is False
    assert report.nearest_description == "zzzzzzzz"


# --------------------------------------------------------------------------------------
# check_new_skill — partition and threshold behaviour
# --------------------------------------------------------------------------------------


async def test_empty_partition_never_breaches(embedder: StubEmbedder) -> None:
    report = await check_new_skill("first skill in its partition", (), embedder=embedder)
    assert isinstance(report, ConfusabilityReport)
    assert report.nearest_description is None
    assert report.nearest_score == 0.0
    assert report.breaches_threshold is False


async def test_threshold_boundary_is_inclusive(embedder: StubEmbedder) -> None:
    # A threshold of exactly the identical-score (1.0) must count as a breach (>=).
    report = await check_new_skill(
        "same", ("same",), embedder=embedder, threshold=1.0
    )
    assert report.breaches_threshold is True


async def test_nearest_is_selected_from_partition(embedder: StubEmbedder) -> None:
    # The nearest match to "same" is the identical entry, not the others.
    report = await check_new_skill(
        "same",
        ("other-a", "same", "other-b"),
        embedder=embedder,
    )
    assert report.nearest_description == "same"
    assert report.nearest_score == pytest.approx(1.0)


async def test_partition_label_round_trips(embedder: StubEmbedder) -> None:
    report = await check_new_skill(
        "x", ("y",), embedder=embedder, partition="colourist"
    )
    assert report.partition == "colourist"


# --------------------------------------------------------------------------------------
# max_pairwise_similarity
# --------------------------------------------------------------------------------------


async def test_max_pairwise_none_for_fewer_than_two(embedder: StubEmbedder) -> None:
    assert await max_pairwise_similarity((), embedder=embedder) is None
    assert await max_pairwise_similarity(("only",), embedder=embedder) is None


async def test_max_pairwise_finds_the_duplicate(embedder: StubEmbedder) -> None:
    # Two identical entries are the most-similar pair (~1.0) regardless of the third.
    pair = await max_pairwise_similarity(
        ("dup", "unrelated", "dup"), embedder=embedder
    )
    assert pair is not None
    assert isinstance(pair, SimilarityPair)
    assert pair.score == pytest.approx(1.0)
    assert {pair.text_a, pair.text_b} == {"dup"}
    assert pair.index_a < pair.index_b


# --------------------------------------------------------------------------------------
# No network — a reaching embedder fails loudly under the autouse socket block
# --------------------------------------------------------------------------------------


async def test_no_network_socket_block_is_active() -> None:
    # Prove the conftest autouse block is live in this file: any real socket connect fails.
    import socket

    with pytest.raises(RuntimeError):
        socket.create_connection(("127.0.0.1", 9))


async def test_stub_embedder_makes_no_network_call(embedder: StubEmbedder) -> None:
    # The whole check runs to completion using only the injected stub — no provider, no
    # socket. If confusability ever hardcoded a client, this would trip the socket block.
    report = await check_new_skill("a", ("b", "c"), embedder=embedder)
    assert isinstance(report, ConfusabilityReport)
