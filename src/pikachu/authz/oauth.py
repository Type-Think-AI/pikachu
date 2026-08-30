"""OAuth 2.1 for MCP — our side of the client auth dance, offline and dependency-light.

Our MCP server is a **resource server**, never an authenticator. The 2026-07-28 auth flow, as
this module implements the *client* half of it:

1. A request to a protected MCP resource with no (or an invalid) token gets a **401**. The 401
   points at the resource's **protected-resource metadata (RFC 9728)** via a
   ``WWW-Authenticate: Bearer resource_metadata="…"`` challenge.
2. The client fetches that metadata to learn which **authorization server** issues tokens for
   this resource. :func:`discover_from_challenge` parses the challenge; :func:`parse_protected_resource_metadata`
   parses the document.
3. The client runs the authorization-code flow with **PKCE (S256 — required)** and an
   **RFC 8707 ``resource`` indicator** on both the authorization and token requests, so the
   issued token is *audience-bound* to this resource. :func:`build_pkce_pair`,
   :func:`build_authorization_request`, :func:`build_token_request`.
4. On every call, the resource server validates that a presented token's **audience matches
   the resource indicator**. A token issued for a *different* resource is rejected — that is
   the whole point of RFC 8707, and it stops a token minted for resource A being replayed
   against resource B. :func:`validate_token_audience`.

★ **Client registration: CIMD is the PRIMARY path.** 2026-07-28 **deprecated Dynamic Client
Registration** in favour of **Client ID Metadata Documents** — the client *is* a URL that
resolves to its own metadata, no registration round-trip. Back-compat for DCR is expected for
≥12 months, so DCR is kept as a **documented fallback only**, never the default.
:func:`preferred_client_registration` returns CIMD when a metadata URL is available and only
falls back to DCR (with a recorded reason) when it is not.

No network, no crypto library dependency, no token *minting*. This module builds the request
artefacts (URLs, PKCE challenges, parameter sets) and validates responses/claims a caller
already has in hand. The caller owns the actual HTTP and any signature verification. Tests fake
the HTTP layer entirely. Imported lazily so an agent that never authenticates to an MCP server
pays nothing.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final
from urllib.parse import urlencode

from pikachu.core.errors import PikachuError

__all__ = [
    "PKCE_METHOD",
    "AuthDiscoveryError",
    "ClientRegistrationMode",
    "PkcePair",
    "ProtectedResourceMetadata",
    "TokenAudienceError",
    "build_authorization_request",
    "build_pkce_pair",
    "build_token_request",
    "discover_from_challenge",
    "parse_protected_resource_metadata",
    "preferred_client_registration",
    "validate_token_audience",
]

#: PKCE is REQUIRED and the challenge method MUST be S256. ``plain`` is not acceptable.
PKCE_METHOD: Final[str] = "S256"

#: The RFC 9728 challenge parameter naming the metadata document.
_RESOURCE_METADATA_PARAM: Final[str] = "resource_metadata"

#: Pulls a quoted or bare parameter value out of a ``WWW-Authenticate`` challenge.
_CHALLENGE_PARAM_RE: Final[re.Pattern[str]] = re.compile(
    r'(?P<key>[a-zA-Z0-9_-]+)\s*=\s*(?:"(?P<qval>[^"]*)"|(?P<bval>[^\s,]+))'
)


class AuthDiscoveryError(PikachuError):
    """A 401 challenge or protected-resource metadata document was malformed.

    Raised rather than tolerated: a client that guesses an authorization server from a broken
    challenge is a client that can be steered to an attacker's token endpoint.
    """


class TokenAudienceError(PikachuError):
    """A token's audience did not match the resource indicator it was presented against.

    This is the RFC 8707 check. A token minted for resource A replayed against resource B fails
    here, which is exactly what audience-binding exists to prevent. Rejection is total.
    """

    def __init__(self, message: str, *, expected: str, actual: tuple[str, ...]) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class ClientRegistrationMode(str, Enum):
    """How a client identifies itself to the authorization server.

    ``CIMD`` (Client ID Metadata Documents) is the PRIMARY, modern path: the client id is a URL
    that resolves to the client's own metadata, so there is no registration round-trip. ``DCR``
    (Dynamic Client Registration) is DEPRECATED as of 2026-07-28 and kept only for back-compat
    (expected ≥12 months). Never default to DCR.
    """

    CIMD = "cimd"
    DCR = "dcr"


@dataclass(frozen=True)
class PkcePair:
    """A PKCE verifier/challenge pair. Method is always S256.

    ``verifier`` is the secret held by the client; ``challenge`` is its S256 transform, sent on
    the authorization request. The token request later presents the ``verifier``; the server
    recomputes the challenge and compares. ``plain`` is never produced.
    """

    verifier: str
    challenge: str
    method: str = PKCE_METHOD


@dataclass(frozen=True)
class ProtectedResourceMetadata:
    """RFC 9728 protected-resource metadata — where a client learns its authorization server.

    ``resource`` is the canonical resource identifier (also the RFC 8707 resource indicator to
    send). ``authorization_servers`` is the non-empty list of issuers that mint tokens for it.
    """

    resource: str
    authorization_servers: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict)


def build_pkce_pair(*, verifier: str | None = None) -> PkcePair:
    """Build an S256 PKCE pair. PKCE is required; the method is never ``plain``.

    :param verifier: An optional caller-supplied verifier (tests pass a fixed one for
        determinism). When omitted, a fresh high-entropy verifier is generated. The verifier is
        constrained to the RFC 7636 unreserved character set and 43–128 chars.
    :returns: A :class:`PkcePair` whose ``challenge`` is ``BASE64URL(SHA256(verifier))`` with
        padding stripped, and whose ``method`` is ``S256``.
    """
    if verifier is None:
        # 32 bytes -> 43 base64url chars, within the RFC 7636 43..128 range.
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    if not 43 <= len(verifier) <= 128:
        raise AuthDiscoveryError(
            f"PKCE verifier length {len(verifier)} is outside the required 43..128 range"
        )
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(verifier=verifier, challenge=challenge, method=PKCE_METHOD)


def discover_from_challenge(www_authenticate: str) -> str:
    """Extract the protected-resource metadata URL from a 401 ``WWW-Authenticate`` challenge.

    A 401 from our resource server carries ``Bearer resource_metadata="https://…"`` (RFC 9728).
    This returns that URL, which the caller then fetches (its HTTP, not ours).

    :raises AuthDiscoveryError: if the challenge is not a Bearer challenge or carries no
        ``resource_metadata`` parameter — a client must not guess an authorization server.
    """
    if not www_authenticate or "bearer" not in www_authenticate.lower():
        raise AuthDiscoveryError(
            "401 challenge is not a Bearer challenge; cannot begin RFC 9728 discovery"
        )
    params = {
        m.group("key").lower(): (m.group("qval") if m.group("qval") is not None else m.group("bval"))
        for m in _CHALLENGE_PARAM_RE.finditer(www_authenticate)
    }
    url = params.get(_RESOURCE_METADATA_PARAM)
    if not url:
        raise AuthDiscoveryError(
            "401 Bearer challenge has no resource_metadata parameter (RFC 9728); "
            "refusing to guess an authorization server"
        )
    return url


def parse_protected_resource_metadata(document: dict[str, Any]) -> ProtectedResourceMetadata:
    """Parse an RFC 9728 protected-resource metadata document.

    :raises AuthDiscoveryError: if the document is not an object, has no ``resource``, or lists
        no authorization servers. A metadata document that names no issuer is unusable, and
        proceeding from it would mean inventing an authorization server.
    """
    if not isinstance(document, dict):
        raise AuthDiscoveryError(
            f"protected-resource metadata is {type(document).__name__}, expected object"
        )
    resource = document.get("resource")
    if not isinstance(resource, str) or not resource:
        raise AuthDiscoveryError("protected-resource metadata missing a resource identifier")
    servers = document.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise AuthDiscoveryError(
            "protected-resource metadata lists no authorization_servers"
        )
    if not all(isinstance(s, str) and s for s in servers):
        raise AuthDiscoveryError("authorization_servers must be a list of non-empty strings")
    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=tuple(servers),
        raw=document,
    )


def build_authorization_request(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    resource: str,
    pkce: PkcePair,
    state: str,
    scope: str = "",
) -> str:
    """Build the authorization-code request URL, carrying PKCE (S256) AND the resource indicator.

    Both are mandatory:

    * ``code_challenge`` / ``code_challenge_method=S256`` — PKCE is required.
    * ``resource`` — the RFC 8707 resource indicator, so the issued token is audience-bound to
      this resource and cannot be replayed against another.

    :raises AuthDiscoveryError: if the PKCE method is not S256, or the resource indicator is
        blank — both are required and a request missing either is not built.
    """
    if pkce.method != PKCE_METHOD:
        raise AuthDiscoveryError(
            f"PKCE method must be {PKCE_METHOD!r}, got {pkce.method!r}; plain is not acceptable"
        )
    if not resource:
        raise AuthDiscoveryError("RFC 8707 resource indicator is required and must be non-empty")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
        "resource": resource,
        "state": state,
    }
    if scope:
        params["scope"] = scope
    return f"{authorization_endpoint}?{urlencode(params)}"


def build_token_request(
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    resource: str,
    pkce: PkcePair,
) -> dict[str, str]:
    """Build the token-exchange request body, carrying the PKCE verifier AND the resource indicator.

    The ``resource`` indicator is sent again (RFC 8707 requires it on *both* the authorization
    and token requests), and the PKCE ``code_verifier`` is presented for the server to check
    against the challenge it saw earlier.

    :raises AuthDiscoveryError: if the resource indicator is blank.
    """
    if not resource:
        raise AuthDiscoveryError("RFC 8707 resource indicator is required on the token request")
    return {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": pkce.verifier,
        "resource": resource,
    }


def validate_token_audience(claims: dict[str, Any], *, resource: str) -> None:
    """Validate a token's audience against the resource indicator — the RFC 8707 check.

    Our server is the resource server; it must reject any token *not* issued for it. The token's
    ``aud`` claim (a string or a list of strings) must contain ``resource``.

    :raises TokenAudienceError: if ``aud`` is missing, malformed, or does not contain
        ``resource``. A token for another resource is rejected outright — no partial acceptance.
    """
    aud = claims.get("aud")
    if aud is None:
        raise TokenAudienceError(
            "token has no aud claim; cannot confirm it was issued for this resource",
            expected=resource,
            actual=(),
        )
    if isinstance(aud, str):
        audiences: tuple[str, ...] = (aud,)
    elif isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        audiences = tuple(aud)
    else:
        raise TokenAudienceError(
            f"token aud claim is malformed ({type(aud).__name__}); expected string or list",
            expected=resource,
            actual=(),
        )
    if resource not in audiences:
        raise TokenAudienceError(
            f"token audience {audiences} does not include this resource {resource!r}; "
            "a token issued for another resource is rejected (RFC 8707)",
            expected=resource,
            actual=audiences,
        )


@dataclass(frozen=True)
class _RegistrationDecision:
    """Result of :func:`preferred_client_registration`: the mode chosen and why."""

    mode: ClientRegistrationMode
    client_id: str
    reason: str


def preferred_client_registration(
    *,
    client_metadata_url: str | None,
    registration_endpoint: str | None = None,
) -> _RegistrationDecision:
    """Choose client-registration mode — CIMD FIRST, DCR only as a documented fallback.

    2026-07-28 deprecated DCR in favour of Client ID Metadata Documents. So:

    * If ``client_metadata_url`` is present, use **CIMD**: the client id *is* that URL. No
      registration round-trip. This is the primary path and is preferred even when a DCR
      ``registration_endpoint`` is also offered.
    * Only if there is no metadata URL do we fall back to **DCR**, and only when a
      ``registration_endpoint`` exists. The returned reason records that this is a deprecated
      fallback, so a caller/operator sees the downgrade rather than it being silent.

    :raises AuthDiscoveryError: if neither a metadata URL nor a registration endpoint is
        available — there is then no way to identify the client at all.
    """
    if client_metadata_url:
        return _RegistrationDecision(
            mode=ClientRegistrationMode.CIMD,
            client_id=client_metadata_url,
            reason="Client ID Metadata Document (primary path; DCR deprecated 2026-07-28)",
        )
    if registration_endpoint:
        return _RegistrationDecision(
            mode=ClientRegistrationMode.DCR,
            client_id="",
            reason=(
                "Dynamic Client Registration — DEPRECATED fallback (no client metadata URL "
                "available); kept for >=12 months back-compat only"
            ),
        )
    raise AuthDiscoveryError(
        "no client identification available: neither a CIMD metadata URL nor a DCR "
        "registration endpoint was provided"
    )
