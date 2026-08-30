"""OAuth 2.1 for MCP — the client half of the auth dance, as a resource-server consumer.

Our MCP server is a resource server: a 401 points at RFC 9728 protected-resource metadata,
the client runs authorization-code with PKCE (S256, required) and an RFC 8707 resource
indicator on both requests, and every presented token has its audience validated against that
resource. Client registration prefers Client ID Metadata Documents (CIMD); Dynamic Client
Registration is a deprecated fallback only. See :mod:`pikachu.authz.oauth` for the full flow.

Lazy import (:pep:`562`): ``import pikachu.authz`` imports nothing until a symbol is
referenced. An agent that never authenticates to an MCP server pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pikachu.authz.oauth import (
        PKCE_METHOD,
        AuthDiscoveryError,
        ClientRegistrationMode,
        PkcePair,
        ProtectedResourceMetadata,
        TokenAudienceError,
        build_authorization_request,
        build_pkce_pair,
        build_token_request,
        discover_from_challenge,
        parse_protected_resource_metadata,
        preferred_client_registration,
        validate_token_audience,
    )


def __getattr__(name: str) -> Any:
    if name in __all__:
        from pikachu.authz import oauth

        return getattr(oauth, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
