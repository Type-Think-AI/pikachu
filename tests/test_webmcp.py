"""WebMCP tests — pure artefact construction/validation, no browser, no network.

Each test pins one of the hard-won constraints so a regression re-surfaces as a failing test
rather than a silent browser failure discovered in production.
"""

from __future__ import annotations

import json

import pytest

from pikachu.core.errors import PikachuError
from pikachu.webmcp import (
    DEFAULT_TEXT_BUDGET,
    REQUIRED_ORIGIN_AGENT_CLUSTER,
    DeclarativeFormError,
    FormParam,
    WebMcpResultError,
    WebMcpTextResult,
    assert_headers_enable_webmcp,
    render_declarative_form,
    to_text_envelope,
    webmcp_response_headers,
    with_text_envelope,
)


# --------------------------------------------------------------------------------------
# ★ The content envelope is the ONLY output shape — a bare string is impossible
# --------------------------------------------------------------------------------------


def test_envelope_shape_is_exact() -> None:
    env = to_text_envelope("hello")
    assert env == {"content": [{"type": "text", "text": "hello"}]}


def test_wrapper_produces_envelope_not_bare_string() -> None:
    env = with_text_envelope("done")
    assert isinstance(env, dict)
    assert env["content"][0]["type"] == "text"
    assert env["content"][0]["text"] == "done"
    # It is emphatically NOT a bare string.
    assert not isinstance(env, str)


def test_bare_string_cannot_be_wrapped_as_text() -> None:
    """A non-string handed to the envelope constructor raises rather than producing an opaque blob."""
    with pytest.raises(WebMcpResultError):
        to_text_envelope({"already": "wrapped"})  # type: ignore[arg-type]
    with pytest.raises(WebMcpResultError):
        WebMcpTextResult(text=123)  # type: ignore[arg-type]


def test_result_error_is_pikachu_error() -> None:
    assert issubclass(WebMcpResultError, PikachuError)
    assert issubclass(DeclarativeFormError, PikachuError)


# --------------------------------------------------------------------------------------
# ★ Budget measured on UNWRAPPED text, not the JSON wire form
# --------------------------------------------------------------------------------------


def test_budget_measured_on_unwrapped_text() -> None:
    """A text that fits the budget but whose wire form exceeds it must still pass."""
    budget = 20
    text = "x" * 20  # exactly the budget, unwrapped
    result = WebMcpTextResult(text=text, budget=budget)

    assert result.unwrapped_length() == 20
    assert result.within_budget() is True
    # The wire form is much longer because of the envelope punctuation...
    assert result.wire_length() > budget
    # ...but the budget is charged on the text, so with_text_envelope succeeds.
    env = with_text_envelope(text, budget=budget)
    assert env["content"][0]["text"] == text


def test_over_budget_unwrapped_text_is_rejected() -> None:
    with pytest.raises(WebMcpResultError):
        with_text_envelope("y" * 21, budget=20)


def test_wire_form_overshoot_does_not_false_fail() -> None:
    """Explicitly: measuring the JSON wire form would wrongly reject a compliant response."""
    text = "a" * DEFAULT_TEXT_BUDGET
    result = WebMcpTextResult(text=text)
    assert result.within_budget() is True
    # Prove the wire form would have failed a naive length check.
    wire = json.dumps(result.envelope(), separators=(",", ":"))
    assert len(wire) > DEFAULT_TEXT_BUDGET


# --------------------------------------------------------------------------------------
# ★ Declarative form: BARE attribute names, never data- prefixed
# --------------------------------------------------------------------------------------


def test_form_emits_bare_attribute_names() -> None:
    form = render_declarative_form(
        tool_name="search",
        tool_description="Search the catalogue",
        params=(FormParam(name="q", description="the query", required=True),),
    )
    assert "toolname=" in form
    assert "tooldescription=" in form
    assert "toolparamdescription=" in form
    # And crucially, NO data- prefixed variants.
    assert "data-toolname" not in form
    assert "data-tooldescription" not in form
    assert "data-toolparamdescription" not in form


def test_autosubmit_form_emits_bare_toolautosubmit_and_no_button() -> None:
    form = render_declarative_form(
        tool_name="ping",
        tool_description="ping",
        autosubmit=True,
    )
    assert "toolautosubmit" in form
    assert "data-toolautosubmit" not in form
    # An autosubmit form needs no submit button.
    assert "<button" not in form


# --------------------------------------------------------------------------------------
# ★ A non-autosubmit form MUST have a real <button type="submit">
# --------------------------------------------------------------------------------------


def test_non_autosubmit_form_has_real_submit_button() -> None:
    form = render_declarative_form(tool_name="save", tool_description="save it")
    assert '<button type="submit">' in form
    # It must NOT be a type="button", which would throw "No submit button was found".
    assert 'type="button"' not in form


def test_blank_tool_name_form_is_rejected() -> None:
    with pytest.raises(DeclarativeFormError):
        render_declarative_form(tool_name="   ", tool_description="x")


# --------------------------------------------------------------------------------------
# ★ Headers: Origin-Agent-Cluster ?1 required; tools=() forbidden
# --------------------------------------------------------------------------------------


def test_generated_headers_enable_webmcp() -> None:
    headers = webmcp_response_headers()
    assert headers["Origin-Agent-Cluster"] == REQUIRED_ORIGIN_AGENT_CLUSTER == "?1"
    # No tools=() footgun in the generated set.
    assert_headers_enable_webmcp(headers)  # does not raise


def test_origin_agent_cluster_zero_is_rejected() -> None:
    """?0 disables WebMCP outright, so a header set carrying it is refused."""
    with pytest.raises(WebMcpResultError):
        assert_headers_enable_webmcp({"Origin-Agent-Cluster": "?0"})


def test_missing_origin_agent_cluster_is_rejected() -> None:
    with pytest.raises(WebMcpResultError):
        assert_headers_enable_webmcp({"Permissions-Policy": "geolocation=()"})


def test_tools_disabled_permissions_policy_is_rejected() -> None:
    with pytest.raises(WebMcpResultError):
        assert_headers_enable_webmcp(
            {
                "Origin-Agent-Cluster": "?1",
                "Permissions-Policy": "tools=(), geolocation=()",
            }
        )


def test_tools_disabled_with_whitespace_still_caught() -> None:
    with pytest.raises(WebMcpResultError):
        assert_headers_enable_webmcp(
            {"Origin-Agent-Cluster": "?1", "Permissions-Policy": "tools=( )"}
        )


def test_header_check_is_case_insensitive() -> None:
    """HTTP header names are case-insensitive; the check must handle lowercased names."""
    assert_headers_enable_webmcp(
        {"origin-agent-cluster": "?1", "permissions-policy": "geolocation=()"}
    )


def test_extra_permissions_policy_cannot_reintroduce_tools_footgun() -> None:
    with pytest.raises(WebMcpResultError):
        webmcp_response_headers(extra_permissions_policy="tools=()")


def test_extra_permissions_policy_is_carried() -> None:
    headers = webmcp_response_headers(extra_permissions_policy="geolocation=()")
    assert headers["Permissions-Policy"] == "geolocation=()"
    assert_headers_enable_webmcp(headers)
