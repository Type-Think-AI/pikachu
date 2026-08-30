"""OAuth 2.1 for MCP tests — artefact building + claim validation, no network.

The whole flow is exercised offline: parse a 401 challenge, parse metadata, build the PKCE
pair and the authorization/token requests, and validate a token's audience — plus the
CIMD-over-DCR registration preference. No HTTP is performed; the caller owns that.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from pikachu.authz import (
    PKCE_METHOD,
    AuthDiscoveryError,
    ClientRegistrationMode,
    TokenAudienceError,
    build_authorization_request,
    build_pkce_pair,
    build_token_request,
    discover_from_challenge,
    parse_protected_resource_metadata,
    preferred_client_registration,
    validate_token_audience,
)
from pikachu.core.errors import PikachuError

RESOURCE = "https://mcp.example.com/"


# --------------------------------------------------------------------------------------
# ★ 401 -> RFC 9728 protected-resource metadata discovery
# --------------------------------------------------------------------------------------


def test_401_challenge_yields_metadata_url() -> None:
    challenge = (
        'Bearer resource_metadata='
        '"https://mcp.example.com/.well-known/oauth-protected-resource"'
    )
    url = discover_from_challenge(challenge)
    assert url == "https://mcp.example.com/.well-known/oauth-protected-resource"


def test_non_bearer_challenge_is_rejected() -> None:
    with pytest.raises(AuthDiscoveryError):
        discover_from_challenge('Basic realm="x"')


def test_bearer_without_resource_metadata_is_rejected() -> None:
    """A client must not guess an authorization server from a challenge with no metadata URL."""
    with pytest.raises(AuthDiscoveryError):
        discover_from_challenge('Bearer error="invalid_token"')


def test_metadata_parse_extracts_authorization_server() -> None:
    doc = {
        "resource": RESOURCE,
        "authorization_servers": ["https://auth.example.com"],
    }
    meta = parse_protected_resource_metadata(doc)
    assert meta.resource == RESOURCE
    assert meta.authorization_servers == ("https://auth.example.com",)


def test_metadata_without_servers_is_rejected() -> None:
    with pytest.raises(AuthDiscoveryError):
        parse_protected_resource_metadata({"resource": RESOURCE, "authorization_servers": []})


def test_metadata_non_object_is_rejected() -> None:
    with pytest.raises(AuthDiscoveryError):
        parse_protected_resource_metadata([])  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# ★ PKCE S256 challenge present + correct
# --------------------------------------------------------------------------------------


def test_pkce_pair_is_s256_and_correct() -> None:
    verifier = "a" * 43
    pair = build_pkce_pair(verifier=verifier)
    assert pair.method == PKCE_METHOD == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
        b"="
    ).decode()
    assert pair.challenge == expected


def test_generated_verifier_is_valid_length() -> None:
    pair = build_pkce_pair()
    assert 43 <= len(pair.verifier) <= 128
    assert pair.method == "S256"


def test_short_verifier_is_rejected() -> None:
    with pytest.raises(AuthDiscoveryError):
        build_pkce_pair(verifier="tooshort")


def test_authorization_request_carries_pkce_s256() -> None:
    pair = build_pkce_pair(verifier="a" * 43)
    url = build_authorization_request(
        authorization_endpoint="https://auth.example.com/authorize",
        client_id="https://client.example.com/metadata",
        redirect_uri="https://client.example.com/cb",
        resource=RESOURCE,
        pkce=pair,
        state="xyz",
    )
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [pair.challenge]
    assert q["response_type"] == ["code"]


# --------------------------------------------------------------------------------------
# ★ RFC 8707 resource indicator sent on BOTH authorization and token requests
# --------------------------------------------------------------------------------------


def test_resource_indicator_on_authorization_request() -> None:
    pair = build_pkce_pair(verifier="b" * 43)
    url = build_authorization_request(
        authorization_endpoint="https://auth.example.com/authorize",
        client_id="cid",
        redirect_uri="https://c/cb",
        resource=RESOURCE,
        pkce=pair,
        state="s",
    )
    q = parse_qs(urlparse(url).query)
    assert q["resource"] == [RESOURCE]


def test_resource_indicator_on_token_request() -> None:
    pair = build_pkce_pair(verifier="c" * 43)
    body = build_token_request(
        client_id="cid",
        code="authcode",
        redirect_uri="https://c/cb",
        resource=RESOURCE,
        pkce=pair,
    )
    assert body["resource"] == RESOURCE
    assert body["code_verifier"] == pair.verifier
    assert body["grant_type"] == "authorization_code"


def test_blank_resource_indicator_is_rejected() -> None:
    pair = build_pkce_pair(verifier="d" * 43)
    with pytest.raises(AuthDiscoveryError):
        build_authorization_request(
            authorization_endpoint="https://a/authorize",
            client_id="cid",
            redirect_uri="https://c/cb",
            resource="",
            pkce=pair,
            state="s",
        )
    with pytest.raises(AuthDiscoveryError):
        build_token_request(
            client_id="cid", code="x", redirect_uri="https://c/cb", resource="", pkce=pair
        )


def test_non_s256_pkce_is_rejected_on_authorization() -> None:
    """A plain PKCE method must never build a request — S256 is required."""
    from pikachu.authz.oauth import PkcePair

    plain = PkcePair(verifier="e" * 43, challenge="e" * 43, method="plain")
    with pytest.raises(AuthDiscoveryError):
        build_authorization_request(
            authorization_endpoint="https://a/authorize",
            client_id="cid",
            redirect_uri="https://c/cb",
            resource=RESOURCE,
            pkce=plain,
            state="s",
        )


# --------------------------------------------------------------------------------------
# ★ Token audience mismatch is rejected
# --------------------------------------------------------------------------------------


def test_token_for_this_resource_is_accepted() -> None:
    validate_token_audience({"aud": RESOURCE}, resource=RESOURCE)  # no raise
    validate_token_audience({"aud": ["other", RESOURCE]}, resource=RESOURCE)  # no raise


def test_token_for_other_resource_is_rejected() -> None:
    with pytest.raises(TokenAudienceError):
        validate_token_audience({"aud": "https://other.example.com/"}, resource=RESOURCE)


def test_token_without_aud_is_rejected() -> None:
    with pytest.raises(TokenAudienceError):
        validate_token_audience({"sub": "user"}, resource=RESOURCE)


def test_token_with_malformed_aud_is_rejected() -> None:
    with pytest.raises(TokenAudienceError):
        validate_token_audience({"aud": 42}, resource=RESOURCE)  # type: ignore[dict-item]


def test_audience_error_is_pikachu_error() -> None:
    assert issubclass(TokenAudienceError, PikachuError)
    assert issubclass(AuthDiscoveryError, PikachuError)


# --------------------------------------------------------------------------------------
# ★ CIMD is preferred over DCR
# --------------------------------------------------------------------------------------


def test_cimd_is_preferred_when_metadata_url_present() -> None:
    """Even when a DCR endpoint is also offered, CIMD wins — DCR is deprecated."""
    decision = preferred_client_registration(
        client_metadata_url="https://client.example.com/metadata",
        registration_endpoint="https://auth.example.com/register",
    )
    assert decision.mode is ClientRegistrationMode.CIMD
    assert decision.client_id == "https://client.example.com/metadata"


def test_dcr_is_only_a_fallback() -> None:
    """DCR is used only when no CIMD metadata URL exists, and the reason records the downgrade."""
    decision = preferred_client_registration(
        client_metadata_url=None,
        registration_endpoint="https://auth.example.com/register",
    )
    assert decision.mode is ClientRegistrationMode.DCR
    assert "deprecated" in decision.reason.lower()


def test_no_registration_path_at_all_is_an_error() -> None:
    with pytest.raises(AuthDiscoveryError):
        preferred_client_registration(client_metadata_url=None, registration_endpoint=None)
