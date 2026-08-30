"""A2A Agent Card tests — fake signer/verifier only, no network, no crypto library.

The signature layer is faked entirely: :class:`FakeSigner` produces a deterministic tag over
the canonical bytes and :class:`FakeVerifier` recomputes it. That is enough to exercise every
branch the module owns — emit, round-trip, tamper-detection, rejection, taint, and P3 across
the boundary — without a real key or the network.
"""

from __future__ import annotations

import hashlib

import pytest

from pikachu.a2a import (
    WELL_KNOWN_CARD_PATH,
    AgentCard,
    CardVerificationError,
    PeerAgent,
    consume_card,
    emit_card,
    peer_effective_tools,
)
from pikachu.core.errors import PikachuError
from pikachu.core.types import AgentSpec, Taint


# --------------------------------------------------------------------------------------
# Fake crypto — deterministic, offline
# --------------------------------------------------------------------------------------


class FakeSigner:
    """A deterministic signer: HMAC-ish tag = sha256(key_id + payload). No real key."""

    def __init__(self, key_id: str = "test-key-1") -> None:
        self._key_id = key_id

    def sign(self, payload: bytes) -> str:
        return hashlib.sha256(self._key_id.encode() + payload).hexdigest()

    @property
    def key_id(self) -> str:
        return self._key_id


class FakeVerifier:
    """Verifies FakeSigner's tag. Recomputes over the SAME canonical bytes it is handed."""

    def verify(self, payload: bytes, signature: str, *, key_id: str) -> bool:
        expected = hashlib.sha256(key_id.encode() + payload).hexdigest()
        return signature == expected


class RejectingVerifier:
    """Always says no — the unreachable-key / hostile-signature case."""

    def verify(self, payload: bytes, signature: str, *, key_id: str) -> bool:
        return False


def _agent() -> AgentSpec:
    return AgentSpec(
        name="colourist",
        role="Grade stills to the house look.",
        allowed_tools=("generate_image", "read_canvas"),
    )


# --------------------------------------------------------------------------------------
# Emit + round-trip
# --------------------------------------------------------------------------------------


def test_emit_and_round_trip_a_card() -> None:
    """Emit our card, then consume it back and get an equivalent verified peer."""
    signer = FakeSigner()
    card = emit_card(_agent(), signer, endpoint="https://us.example/agents/colourist")

    assert isinstance(card, AgentCard)
    assert card.payload["name"] == "colourist"
    assert card.payload["capabilities"] == ["generate_image", "read_canvas"]

    peer = consume_card(card.to_document(), FakeVerifier())
    assert isinstance(peer, PeerAgent)
    assert peer.name == "colourist"
    assert peer.role == "Grade stills to the house look."
    assert peer.advertised_tools == ("generate_image", "read_canvas")
    assert peer.key_id == signer.key_id


def test_round_trip_via_json_string() -> None:
    """A card consumed from its served JSON string verifies identically to the mapping."""
    signer = FakeSigner()
    card = emit_card(_agent(), signer)
    peer = consume_card(card.to_json(), FakeVerifier())
    assert peer.name == "colourist"


def test_well_known_path_is_the_a2a_convention() -> None:
    assert WELL_KNOWN_CARD_PATH == "/.well-known/agent-card.json"


# --------------------------------------------------------------------------------------
# ★ A bad signature is REJECTED — never degrade-and-continue
# --------------------------------------------------------------------------------------


def test_bad_signature_is_rejected() -> None:
    """A verifier that rejects the signature => CardVerificationError, no PeerAgent."""
    card = emit_card(_agent(), FakeSigner())
    with pytest.raises(CardVerificationError):
        consume_card(card.to_document(), RejectingVerifier())


def test_tampered_capability_breaks_verification() -> None:
    """Tampering with an advertised tool after signing invalidates the signature.

    The signature covers the canonical payload bytes, so adding a capability the signer never
    saw means consume_card recomputes different bytes and the (real) FakeVerifier rejects it.
    """
    signer = FakeSigner()
    card = emit_card(_agent(), signer)
    doc = card.to_document()
    # Attacker injects a powerful tool into the advertised capabilities post-signature.
    doc["payload"]["capabilities"].append("bash")

    with pytest.raises(CardVerificationError):
        consume_card(doc, FakeVerifier())


def test_missing_signature_envelope_is_rejected() -> None:
    card = emit_card(_agent(), FakeSigner())
    doc = card.to_document()
    del doc["signature"]
    with pytest.raises(CardVerificationError):
        consume_card(doc, FakeVerifier())


def test_blank_signature_value_is_rejected() -> None:
    card = emit_card(_agent(), FakeSigner())
    doc = card.to_document()
    doc["signature"]["value"] = ""
    with pytest.raises(CardVerificationError):
        consume_card(doc, FakeVerifier())


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(CardVerificationError):
        consume_card("{not valid json", FakeVerifier())


def test_non_object_document_is_rejected() -> None:
    with pytest.raises(CardVerificationError):
        consume_card("[1, 2, 3]", FakeVerifier())


def test_payload_without_name_is_rejected() -> None:
    """Even a correctly-signed card with no usable name is rejected."""
    signer = FakeSigner()
    # Sign a payload that has no name.
    payload = {"a2a_version": "1.0", "role": "x", "capabilities": []}
    sig = signer.sign(AgentCard.canonical_payload_bytes(payload))
    doc = {"payload": payload, "signature": {"key_id": signer.key_id, "value": sig}}
    with pytest.raises(CardVerificationError):
        consume_card(doc, FakeVerifier())


def test_verification_error_is_pikachu_error() -> None:
    assert issubclass(CardVerificationError, PikachuError)


# --------------------------------------------------------------------------------------
# ★ A peer card is TAINTED
# --------------------------------------------------------------------------------------


def test_consumed_peer_is_tainted() -> None:
    """A verified peer is still untrusted: signature proves identity, not content safety."""
    card = emit_card(_agent(), FakeSigner())
    peer = consume_card(card.to_document(), FakeVerifier())

    assert not peer.lineage.is_clean
    assert Taint.USER_UNVERIFIED in peer.lineage.taints
    assert any("a2a:" in s for s in peer.lineage.sources)


# --------------------------------------------------------------------------------------
# ★ P3 across the boundary — a peer advertising 10, we allow 2, yields 2
# --------------------------------------------------------------------------------------


def test_peer_advertising_ten_we_allow_two_yields_two() -> None:
    """A remote agent cannot widen our authority: its list is a request, ours is the grant."""
    signer = FakeSigner()
    # Peer advertises ten tools; two of them happen to be in OUR allowlist.
    ten = [f"remote_{i}" for i in range(10)]
    ten[2] = "generate_image"
    ten[6] = "web_search"
    peer_spec = AgentSpec(name="peer", role="stranger", allowed_tools=tuple(ten))
    card = emit_card(peer_spec, signer)
    peer = consume_card(card.to_document(), FakeVerifier())
    assert len(peer.advertised_tools) == 10

    our_allowlist = ("generate_image", "web_search")
    narrowed = peer_effective_tools(peer, our_allowlist)

    assert set(narrowed.tools) == {"generate_image", "web_search"}
    assert len(narrowed.tools) == 2


def test_peer_cannot_smuggle_dangerous_tool() -> None:
    """A dangerous tool a peer advertises is stripped even if our allowlist somehow lists it."""
    signer = FakeSigner()
    peer_spec = AgentSpec(name="peer", role="x", allowed_tools=("bash", "web_search"))
    card = emit_card(peer_spec, signer)
    peer = consume_card(card.to_document(), FakeVerifier())

    narrowed = peer_effective_tools(peer, ("bash", "web_search"))
    assert set(narrowed.tools) == {"web_search"}


def test_peer_advertising_nothing_grants_nothing() -> None:
    signer = FakeSigner()
    peer_spec = AgentSpec(name="peer", role="x", allowed_tools=())
    card = emit_card(peer_spec, signer)
    peer = consume_card(card.to_document(), FakeVerifier())
    narrowed = peer_effective_tools(peer, ("generate_image",))
    assert narrowed.tools == ()


def test_malformed_capabilities_type_is_rejected() -> None:
    """A capabilities value of the wrong type is malformed input, not silently coerced."""
    signer = FakeSigner()
    payload = {
        "a2a_version": "1.0",
        "name": "peer",
        "role": "x",
        "capabilities": "generate_image",  # string, not a list
    }
    sig = signer.sign(AgentCard.canonical_payload_bytes(payload))
    doc = {"payload": payload, "signature": {"key_id": signer.key_id, "value": sig}}
    with pytest.raises(CardVerificationError):
        consume_card(doc, FakeVerifier())
