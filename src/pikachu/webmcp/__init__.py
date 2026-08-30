"""WebMCP — expose an agent's tools to a browser page, in a shape a host cannot get wrong.

Every symbol here builds or validates a WebMCP artefact so a footgun becomes a
construction-time refusal rather than a silent browser failure: the mandatory content envelope
(never a bare string), bare declarative-form attributes (never ``data-``-prefixed), a required
``<button type="submit">`` on a non-autosubmit form, ``Origin-Agent-Cluster: ?1`` (never
``?0``), no ``tools=()`` in ``Permissions-Policy``, and a budget measured on unwrapped text.
See :mod:`pikachu.webmcp.tools` for the constraint-by-constraint reasoning.

Lazy import (:pep:`562`): ``import pikachu.webmcp`` imports nothing until a symbol is
referenced. An agent that never exposes tools to a page pays nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "DEFAULT_TEXT_BUDGET",
    "REQUIRED_ORIGIN_AGENT_CLUSTER",
    "DeclarativeFormError",
    "FormParam",
    "WebMcpResultError",
    "WebMcpTextResult",
    "admit_page_tools",
    "assert_headers_enable_webmcp",
    "to_text_envelope",
    "webmcp_response_headers",
    "with_text_envelope",
    "render_declarative_form",
]

if TYPE_CHECKING:
    from pikachu.webmcp.tools import (
        DEFAULT_TEXT_BUDGET,
        REQUIRED_ORIGIN_AGENT_CLUSTER,
        DeclarativeFormError,
        FormParam,
        WebMcpResultError,
        WebMcpTextResult,
        admit_page_tools,
        assert_headers_enable_webmcp,
        render_declarative_form,
        to_text_envelope,
        webmcp_response_headers,
        with_text_envelope,
    )


def __getattr__(name: str) -> Any:
    if name in __all__:
        from pikachu.webmcp import tools

        return getattr(tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
