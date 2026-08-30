"""Signed Agent Cards — the A2A cross-boundary door, and *only* the cross-boundary door.

★ **A2A is for agents on the far side of a trust boundary — a different vendor, a different
organisation.** It is emphatically **not** how this runtime's own in-process crew coordinates.
Internal coordination is the canvas (``docs/15-extensibility.md``): an append-only blackboard,
not messages between agents. The temptation to reach for A2A internally ("it's already here,
and it has nice typed cards") is exactly how the message-passing topology this design rejected
comes back in through the side door — every agent addressing every other agent, a quadratic
mesh of point-to-point calls that the canvas exists to avoid. If you find yourself emitting a
card for a peer that runs in *our* process, stop: you want the canvas.

What this module does, and nothing more:

* :func:`emit_card` — turn one of *our* :class:`~pikachu.core.types.AgentSpec`\\s into an
  :class:`AgentCard`, sign it with a caller-supplied signer, and hand back the signed,
  serialisable document to publish at the well-known URI.
* :func:`consume_card` — take a *peer's* raw card document (untrusted bytes off the wire),
  verify its signature with a caller-supplied verifier, and either reject it or return a
  :class:`PeerAgent` whose every advertised tool is **tainted** and treated as a *request*.
* :func:`peer_effective_tools` — narrow a peer's advertised tools against *our* fixed
  allowlist via :func:`pikachu.guard.effective_tools`. A remote agent can never widen our
  authority; its tool list is a declaration, the allowlist is the grant. This is invariant P3
  applied across the organisational boundary.

Security posture, non-negotiable:

* **A peer card is untrusted input.** Its advertised skills/tools are attacker-controllable
  text. Everything derived from it carries :class:`~pikachu.core.types.Taint`
  (``USER_UNVERIFIED`` — it is an external declaration of unknown provenance) and its taint is
  never cleared.
* **An unverifiable or malformed signature means REJECT.** There is no degrade-and-continue.
  A card that does not verify is discarded whole; nothing is partially applied. Accepting an
  unsigned or badly-signed peer is accepting an impersonator.

The crypto is injected. :class:`CardSigner` / :class:`CardVerifier` are Protocols the caller
fulfils (Ed25519, an HSM, a KMS — this module does not care and does not import a crypto
library). Tests supply a deterministic fake. This keeps the module free of a heavy dependency
and free of the network, and it is imported lazily so an agent that never speaks A2A pays
nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from pikachu.core.errors import PikachuError
from pikachu.core.types import AgentSpec, Lineage, Taint
from pikachu.guard import EffectiveToolset, effective_tools

__all__ = [
    "A2A_CARD_VERSION",
    "WELL_KNOWN_CARD_PATH",
    "AgentCard",
    "CardSigner",
    "CardVerifier",
    "CardVerificationError",
    "PeerAgent",
    "consume_card",
    "emit_card",
    "peer_effective_tools",
]

#: A2A discovery is via a card at a well-known URI. This is the path component; a publisher
#: joins it to their own origin. Recorded as a constant so a test and an operator agree.
WELL_KNOWN_CARD_PATH: Final[str] = "/.well-known/agent-card.json"

#: The card document schema version we emit and can consume. Bumped on a breaking shape change.
A2A_CARD_VERSION: Final[str] = "1.0"

#: A peer card is an external declaration of unknown provenance — the closest existing taint is
#: ``USER_UNVERIFIED``. It is applied to everything derived from a consumed card and never
#: cleared (``Lineage`` is monotonic by construction).
_PEER_TAINT: Final[Taint] = Taint.USER_UNVERIFIED


class CardVerificationError(PikachuError):
    """A peer's Agent Card failed verification and was rejected.

    Raised — never tolerated — for a malformed document, a missing signature, or a signature
    that does not verify against the presented payload. There is no degrade-and-continue path:
    a card that does not verify is discarded whole, because accepting it is accepting an
    impersonator. The offending reason is attached for logging, not for a retry decision.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@runtime_checkable
class CardSigner(Protocol):
    """Signs the canonical bytes of a card payload. Caller-supplied; not imported here.

    Implementations own the key material (Ed25519, HSM, KMS). This module only asks for a
    signature over bytes it hands over, and never sees the private key.
    """

    def sign(self, payload: bytes) -> str:
        """Return a signature (opaque string, e.g. base64) over ``payload``."""
        ...

    @property
    def key_id(self) -> str:
        """An identifier for the signing key, embedded in the card so a verifier can select it."""
        ...


@runtime_checkable
class CardVerifier(Protocol):
    """Verifies a signature against a card payload. Caller-supplied; not imported here."""

    def verify(self, payload: bytes, signature: str, *, key_id: str) -> bool:
        """Return True iff ``signature`` is a valid signature over ``payload`` for ``key_id``."""
        ...


@dataclass(frozen=True)
class AgentCard:
    """A signed Agent Card for one of *our* agents, ready to publish at the well-known URI.

    ``payload`` is the signed body (the exact bytes the signature covers, as a JSON-safe
    mapping). ``signature`` and ``key_id`` are the envelope. :meth:`to_document` produces the
    full published document; :meth:`canonical_payload_bytes` reproduces the exact bytes that
    were signed, so a verifier checks the same bytes the signer saw.
    """

    payload: dict[str, Any]
    signature: str
    key_id: str

    @staticmethod
    def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
        """The exact bytes a signature covers.

        Canonical = ``json.dumps`` with sorted keys and no incidental whitespace, UTF-8
        encoded. Signer and verifier MUST both go through this function; a signature over
        "the payload" is meaningless unless both sides serialise it identically.
        """
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_document(self) -> dict[str, Any]:
        """The full published card document: the signed payload plus its signature envelope."""
        return {
            "payload": self.payload,
            "signature": {"key_id": self.key_id, "value": self.signature},
        }

    def to_json(self) -> str:
        """The document as a JSON string, ready to serve at the well-known URI."""
        return json.dumps(self.to_document(), sort_keys=True)


@dataclass(frozen=True)
class PeerAgent:
    """A verified peer agent, consumed from its card. Everything here is TAINTED.

    A peer lives across a trust boundary. Its ``advertised_tools`` are a *request*, never a
    grant — route them through :func:`peer_effective_tools` before any of them is offered to
    one of our agents. ``lineage`` is always tainted ``USER_UNVERIFIED`` and never clean; the
    signature proved *identity*, not *trustworthiness of content*.
    """

    name: str
    role: str
    advertised_tools: tuple[str, ...]
    key_id: str
    lineage: Lineage
    raw: dict[str, Any] = field(default_factory=dict)


def emit_card(
    agent: AgentSpec,
    signer: CardSigner,
    *,
    endpoint: str = "",
) -> AgentCard:
    """Emit a signed Agent Card for one of *our* agents.

    :param agent: The local :class:`AgentSpec` to advertise across the boundary. Its
        ``allowed_tools`` become the card's advertised capabilities — we publish what the agent
        can actually do, not an aspirational list.
    :param signer: Caller-supplied signer that owns the key material.
    :param endpoint: Optional URL where a peer reaches this agent. Part of the signed payload.
    :returns: A signed :class:`AgentCard` to publish at :data:`WELL_KNOWN_CARD_PATH`.

    The signature covers the canonical bytes of the payload (:meth:`AgentCard.canonical_payload_bytes`),
    so any later tampering with an advertised tool invalidates the signature.
    """
    payload: dict[str, Any] = {
        "a2a_version": A2A_CARD_VERSION,
        "name": agent.name,
        "role": agent.role,
        # We advertise what the agent may actually do. The peer treats this the way we treat
        # theirs: as a request, subject to their own allowlist.
        "capabilities": list(agent.allowed_tools),
        "endpoint": endpoint,
    }
    signed_bytes = AgentCard.canonical_payload_bytes(payload)
    signature = signer.sign(signed_bytes)
    return AgentCard(payload=payload, signature=signature, key_id=signer.key_id)


def consume_card(
    document: dict[str, Any] | str,
    verifier: CardVerifier,
) -> PeerAgent:
    """Consume and verify a *peer's* Agent Card, returning a tainted :class:`PeerAgent`.

    :param document: The peer's raw card — the JSON string served at their well-known URI, or
        the already-parsed mapping. Untrusted input.
    :param verifier: Caller-supplied verifier that checks the signature.
    :raises CardVerificationError: if the document is malformed, is missing a signature, or the
        signature does not verify. **Rejection is total** — no PeerAgent is returned and nothing
        is partially applied.
    :returns: A :class:`PeerAgent` whose advertised tools are a request (not a grant) and whose
        lineage is tainted ``USER_UNVERIFIED``.

    The verifier is handed the *exact* canonical bytes of the presented payload
    (:meth:`AgentCard.canonical_payload_bytes`). A card that re-serialises to different bytes
    than were signed therefore fails — tampering with any advertised field breaks verification.
    """
    doc = _parse_document(document)

    payload = doc.get("payload")
    if not isinstance(payload, dict):
        raise CardVerificationError(
            "peer card missing an object payload", reason="malformed_payload"
        )

    sig_env = doc.get("signature")
    if not isinstance(sig_env, dict):
        raise CardVerificationError(
            "peer card missing a signature envelope", reason="missing_signature"
        )
    signature = sig_env.get("value")
    key_id = sig_env.get("key_id")
    if not isinstance(signature, str) or not signature:
        raise CardVerificationError(
            "peer card signature value is missing or blank", reason="missing_signature"
        )
    if not isinstance(key_id, str) or not key_id:
        raise CardVerificationError(
            "peer card signature key_id is missing or blank", reason="missing_key_id"
        )

    # Verify against the SAME canonical bytes the signer covered. If the peer tampered with any
    # advertised field after signing, these bytes differ from what was signed and verify fails.
    signed_bytes = AgentCard.canonical_payload_bytes(payload)
    if not verifier.verify(signed_bytes, signature, key_id=key_id):
        # Unverifiable => REJECT. Never degrade-and-continue.
        raise CardVerificationError(
            f"peer card signature did not verify for key_id {key_id!r}",
            reason="bad_signature",
        )

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CardVerificationError(
            "peer card payload has no usable name", reason="malformed_payload"
        )
    role = payload.get("role", "")
    if not isinstance(role, str):
        raise CardVerificationError(
            "peer card role must be a string", reason="malformed_payload"
        )

    advertised = _extract_capabilities(payload)

    # Signature proved WHO sent it, not that its CONTENT is safe. Taint everything derived from
    # the card; the taint is monotonic and can never be cleared.
    lineage = Lineage.clean().with_taint(_PEER_TAINT, f"a2a:{name}")

    return PeerAgent(
        name=name,
        role=role,
        advertised_tools=advertised,
        key_id=key_id,
        lineage=lineage,
        raw=payload,
    )


def peer_effective_tools(
    peer: PeerAgent,
    fixed_allowlist: tuple[str, ...],
) -> EffectiveToolset:
    """Narrow a peer's advertised tools against *our* fixed allowlist — invariant P3 at the boundary.

    A remote agent advertising ten tools to one of our agents that allows two yields exactly
    two. The peer's list is a *declaration*; the fixed allowlist is the *grant*. This is the
    single call that stops a peer widening our authority, and it reuses the exact same
    :func:`pikachu.guard.effective_tools` engine every other tool source is routed through —
    the boundary is not special-cased, which is the point.
    """
    return effective_tools(fixed_allowlist, peer.advertised_tools)


def _parse_document(document: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(document, str):
        try:
            parsed = json.loads(document)
        except (ValueError, TypeError) as exc:
            raise CardVerificationError(
                f"peer card is not valid JSON: {exc}", reason="malformed_json"
            ) from exc
    else:
        parsed = document
    if not isinstance(parsed, dict):
        raise CardVerificationError(
            f"peer card is {type(parsed).__name__}, expected object",
            reason="malformed_document",
        )
    return parsed


def _extract_capabilities(payload: dict[str, Any]) -> tuple[str, ...]:
    """Pull the advertised tool names out of a peer payload, defensively.

    A missing capabilities list is a peer advertising nothing — an empty tuple, not an error.
    A capabilities value of the wrong type, or a non-string entry, IS malformed input and is
    rejected: silently coercing it would let a hostile card smuggle a non-string that later
    code mis-handles.
    """
    caps = payload.get("capabilities", [])
    if caps == []:
        return ()
    if not isinstance(caps, list):
        raise CardVerificationError(
            "peer card capabilities must be an array", reason="malformed_capabilities"
        )
    out: list[str] = []
    for entry in caps:
        if not isinstance(entry, str):
            raise CardVerificationError(
                f"peer card capability {entry!r} is not a string",
                reason="malformed_capabilities",
            )
        out.append(entry)
    return tuple(out)
