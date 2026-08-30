"""WebMCP — expose an agent's tools to a browser page, in a shape a host cannot get wrong.

WebMCP puts tools on ``document.modelContext`` so an in-page agent can call them. Every
constraint encoded here has caused a real, silent failure elsewhere on this project — so each
is made *structurally* impossible rather than merely documented:

★ **The content envelope is mandatory.** A tool's ``execute()`` must return
``{content: [{type: 'text', text: ...}]}`` and **never a bare string**. Chrome tolerates a
bare string, so it *looks* like it works — but any host that unwraps ``content[]`` (the demo's
agent panel, DevTools Tool Activity, an MCP-shaped agent) gets an opaque blob and cannot
express ``isError``. :func:`to_text_envelope` / :func:`with_text_envelope` are the only way to
build a result here, so a new tool **cannot** ship in the wrong shape.

**Declarative form attributes are bare.** ``toolname``, ``tooldescription``,
``toolparamdescription``, ``toolautosubmit`` — never ``data-``-prefixed. The ``data-`` prefix
is a React/JSX TypeScript workaround; a browser ignores ``data-``-prefixed attributes, so the
tools never register. :func:`render_declarative_form` emits the bare names and there is no code
path that emits a ``data-`` one.

**A non-autosubmit form needs a real submit button.** A declarative form without
``toolautosubmit`` requires a ``<button type="submit">``; a ``type="button"`` makes Chrome
throw *"No submit button was found"* when an agent invokes it. :func:`render_declarative_form`
refuses to emit such a form.

**Response headers are load-bearing.** ``Origin-Agent-Cluster: ?1`` is a *hard requirement* —
``?0`` disables WebMCP outright (origin-keyed agent cluster is a documented prerequisite). And
``tools=()`` must never appear in ``Permissions-Policy`` — it kills WebMCP; the ``tools``
policy defaults to ``self``, which is what WebMCP wants. :func:`webmcp_response_headers`
produces a correct set and :func:`assert_headers_enable_webmcp` refuses a hostile one.

**The character budget is measured on the UNWRAPPED text.** A per-tool budget must be checked
against the human-readable ``text``, not the JSON wire form — charging a compliant response for
``{"content":[{"type":"text","text":...}]}`` punctuation would false-fail a response that is
actually within budget. :func:`WebMcpTextResult.within_budget` measures the right string.

Lazy import (:pep:`562`): ``import pikachu.webmcp`` imports nothing heavy until referenced. An
agent that never exposes tools to a page pays nothing. There is no browser dependency — this
module *builds and validates the artefacts* (envelopes, form HTML, headers) that the page then
uses; it does not itself run in a browser.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pikachu.core.errors import PikachuError
from pikachu.core.types import Lineage
from pikachu.guard.untrusted import Admission, SourceKind, admit

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

#: The per-tool character budget, measured on the UNWRAPPED text. A tool response longer than
#: this is likely to blow the host agent's context; the exact number is a policy default a
#: caller can override per tool. Named after the recorded worst case (~1,470 text chars for
#: get_doodle_article) — 1,500 gives a small margin without inviting bloat.
DEFAULT_TEXT_BUDGET: Final[int] = 1_500

#: ``Origin-Agent-Cluster: ?1`` is a HARD requirement for WebMCP. ``?0`` disables it outright.
REQUIRED_ORIGIN_AGENT_CLUSTER: Final[str] = "?1"

#: The bare declarative-form attribute names. ``data-``-prefixed variants are a React/JSX
#: workaround the browser ignores, so they are never emitted. Kept as a constant so a test can
#: assert exactly this set appears and no ``data-`` name does.
_BARE_FORM_ATTRS: Final[frozenset[str]] = frozenset(
    {"toolname", "tooldescription", "toolparamdescription", "toolautosubmit"}
)


class WebMcpResultError(PikachuError):
    """A WebMCP tool result was built in the wrong shape, or exceeds its budget."""


class DeclarativeFormError(PikachuError):
    """A declarative WebMCP form was requested in a shape the browser would reject.

    The headline case: a non-autosubmit form with no real ``<button type="submit">``. Chrome
    throws *"No submit button was found"* at agent-invocation time — a runtime failure in the
    browser that this class turns into a construction-time refusal instead.
    """


def to_text_envelope(text: str) -> dict[str, Any]:
    """Build the ONE correct WebMCP result shape: the content envelope.

    Returns ``{"content": [{"type": "text", "text": text}]}`` — never a bare string. This is
    the only constructor for a text result in this module, so a tool physically cannot return
    the wrong shape by going through it. A non-string ``text`` is a programming error and
    raises, rather than being coerced into something that looks fine in Chrome and opaque
    everywhere else.
    """
    if not isinstance(text, str):
        raise WebMcpResultError(
            f"WebMCP text must be a str, got {type(text).__name__}; "
            "a bare/typed value cannot be wrapped in the content envelope safely"
        )
    return {"content": [{"type": "text", "text": text}]}


@dataclass(frozen=True)
class WebMcpTextResult:
    """A budget-aware text result. The envelope is the ONLY way out.

    Construct with the human-readable text; :meth:`envelope` returns the mandatory content
    envelope and :meth:`within_budget` / :meth:`unwrapped_length` measure the **unwrapped**
    text — never the JSON wire form. Measuring the wire form would charge a compliant response
    for the envelope's own punctuation and false-fail it.
    """

    text: str
    budget: int = DEFAULT_TEXT_BUDGET

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise WebMcpResultError(
                f"WebMcpTextResult.text must be a str, got {type(self.text).__name__}"
            )
        if self.budget <= 0:
            raise WebMcpResultError(f"budget must be positive, got {self.budget}")

    def envelope(self) -> dict[str, Any]:
        """The mandatory content envelope for this result."""
        return to_text_envelope(self.text)

    def unwrapped_length(self) -> int:
        """Length of the UNWRAPPED text — the string the budget is measured against."""
        return len(self.text)

    def within_budget(self) -> bool:
        """True iff the unwrapped text fits the budget.

        Measured on ``self.text``, NOT on ``json.dumps(self.envelope())``. The envelope adds
        ~40 characters of ``{"content":[{"type":"text","text":...}]}`` punctuation; charging a
        response for that is a false-fail.
        """
        return self.unwrapped_length() <= self.budget

    def wire_length(self) -> int:
        """Length of the serialised envelope — provided only to make the distinction explicit.

        This is deliberately NOT what :meth:`within_budget` uses. It exists so a caller (or a
        test) can see how far the wire form overshoots the text, and confirm the budget is not
        being charged for JSON punctuation.
        """
        return len(json.dumps(self.envelope(), separators=(",", ":")))


def with_text_envelope(text: str, *, budget: int = DEFAULT_TEXT_BUDGET) -> dict[str, Any]:
    """Build a budget-checked content envelope in one call — the registration-site helper.

    Wraps :class:`WebMcpTextResult`: raises :class:`WebMcpResultError` if the unwrapped text
    exceeds ``budget``, otherwise returns the envelope. Apply this at the single point where a
    tool's ``execute`` result is produced, so no tool can ship a bare string or an over-budget
    blob — the same pattern (``withTextEnvelope`` at the one registration site) that fixed the
    real incident.
    """
    result = WebMcpTextResult(text=text, budget=budget)
    if not result.within_budget():
        raise WebMcpResultError(
            f"WebMCP text result is {result.unwrapped_length()} chars, over the "
            f"{budget}-char budget (measured on unwrapped text, not the "
            f"{result.wire_length()}-char wire form)"
        )
    return result.envelope()


def webmcp_response_headers(
    *,
    extra_permissions_policy: str = "",
) -> dict[str, str]:
    """Produce a header set that ENABLES WebMCP, with the two footguns pre-disarmed.

    * ``Origin-Agent-Cluster: ?1`` — the hard requirement. Never ``?0``.
    * ``Permissions-Policy`` that does **not** contain ``tools=()``. The ``tools`` policy
      defaults to ``self`` (what WebMCP wants); we leave it alone. ``extra_permissions_policy``
      lets a caller add unrelated directives — it is validated to make sure it does not sneak
      ``tools=()`` back in.

    :raises DeclarativeFormError: never — headers do not raise here; hostile *input* is caught
        by :func:`assert_headers_enable_webmcp`. This function only builds a correct set.
    """
    headers: dict[str, str] = {"Origin-Agent-Cluster": REQUIRED_ORIGIN_AGENT_CLUSTER}
    pp = extra_permissions_policy.strip()
    if pp:
        # Guard the caller against re-adding the footgun through the extra field.
        if _permissions_policy_kills_webmcp(pp):
            raise WebMcpResultError(
                "extra Permissions-Policy contains a tools=() directive, which disables "
                "WebMCP; the tools policy must stay at its default of self"
            )
        headers["Permissions-Policy"] = pp
    return headers


def assert_headers_enable_webmcp(headers: dict[str, str]) -> None:
    """Refuse a header set that would silently disable WebMCP.

    Raises :class:`WebMcpResultError` if ``Origin-Agent-Cluster`` is anything other than ``?1``
    (``?0`` disables WebMCP outright; a missing header is also wrong because the prerequisite is
    origin-keyed clustering), or if ``Permissions-Policy`` contains a ``tools=()`` directive.
    Header lookups are case-insensitive, because HTTP header names are.
    """
    lower = {k.lower(): v for k, v in headers.items()}

    oac = lower.get("origin-agent-cluster")
    if oac is None:
        raise WebMcpResultError(
            "Origin-Agent-Cluster header is absent; WebMCP requires it set to '?1' "
            "(origin-keyed agent cluster is a documented hard prerequisite)"
        )
    if oac.strip() != REQUIRED_ORIGIN_AGENT_CLUSTER:
        raise WebMcpResultError(
            f"Origin-Agent-Cluster is {oac!r}; WebMCP requires '?1'. "
            "'?0' disables WebMCP outright."
        )

    pp = lower.get("permissions-policy", "")
    if _permissions_policy_kills_webmcp(pp):
        raise WebMcpResultError(
            "Permissions-Policy contains a tools=() directive, which disables WebMCP; "
            "leave the tools policy at its default of self"
        )


def _permissions_policy_kills_webmcp(policy: str) -> bool:
    """Whether a Permissions-Policy string neutralises the ``tools`` feature.

    ``tools=()`` (empty allowlist) turns the feature off for every origin, which kills WebMCP.
    Match tolerantly of whitespace inside the parens (``tools=( )``) so a formatting variant
    does not slip past.
    """
    normalised = policy.replace(" ", "").lower()
    return "tools=()" in normalised


@dataclass(frozen=True)
class FormParam:
    """One parameter of a declarative WebMCP form.

    ``name`` is the input's name; ``description`` becomes its ``toolparamdescription`` (bare
    attribute). ``required`` toggles the HTML ``required`` attribute.
    """

    name: str
    description: str = ""
    required: bool = False


def render_declarative_form(
    *,
    tool_name: str,
    tool_description: str,
    params: tuple[FormParam, ...] = (),
    autosubmit: bool = False,
    action: str = "",
    method: str = "post",
) -> str:
    """Render a declarative WebMCP ``<form>`` with the BARE attribute names, safely.

    Emits ``toolname`` / ``tooldescription`` / ``toolparamdescription`` / ``toolautosubmit`` —
    never ``data-``-prefixed, because the browser ignores ``data-`` attributes and the tool
    would silently never register.

    ★ **A non-autosubmit form MUST contain a real ``<button type="submit">``.** This function
    always emits one for a non-autosubmit form and never emits a ``type="button"`` in its
    place. Passing ``autosubmit=True`` sets ``toolautosubmit`` and omits the button, which is
    the only shape where no submit button is required.

    :raises DeclarativeFormError: if ``tool_name`` is blank — an unnamed tool cannot register.
    """
    name = tool_name.strip()
    if not name:
        raise DeclarativeFormError("declarative form tool_name is blank; the tool cannot register")

    attrs = [
        f'toolname="{html.escape(name, quote=True)}"',
        f'tooldescription="{html.escape(tool_description, quote=True)}"',
    ]
    if autosubmit:
        # The autosubmit form does NOT need a submit button — Chrome submits it for the agent.
        attrs.append("toolautosubmit")
    if action:
        attrs.append(f'action="{html.escape(action, quote=True)}"')
    attrs.append(f'method="{html.escape(method, quote=True)}"')

    lines = [f"<form {' '.join(attrs)}>"]
    for p in params:
        pname = html.escape(p.name, quote=True)
        pdesc = html.escape(p.description, quote=True)
        req = " required" if p.required else ""
        lines.append(
            f'  <input name="{pname}" toolparamdescription="{pdesc}"{req} />'
        )

    if not autosubmit:
        # REQUIRED: a real submit button. A type="button" here makes an agent-invoked submit
        # throw "No submit button was found", so we hard-code type="submit" and never expose a
        # way to make it anything else.
        lines.append('  <button type="submit">Submit</button>')

    lines.append("</form>")
    return "\n".join(lines)


def admit_page_tools(
    origin: str,
    *,
    requested_tools: Sequence[str] | None,
    fixed_allowlist: Sequence[str],
    lineage: Lineage | None = None,
) -> Admission:
    """Narrow the tools a browser page may expose, through the SINGLE guard admission path.

    A WebMCP page is untrusted input: what it asks to put on ``document.modelContext`` is a
    *request*, never a grant. This routes that request through
    :func:`pikachu.guard.untrusted.admit` — the exact same path an MCP server, a plugin and a
    foreign skill go through — so a page can never advertise a tool outside the agent's fixed
    allowlist. Before this, ``webmcp/`` had no guard reference at all; that gap is precisely
    what success criterion S2 requires closed, and closed on the *shared* path rather than by a
    third bespoke mechanism.

    :param origin: The page origin, recorded in the admitted lineage for audit.
    :param requested_tools: Tool names the page wants to expose. ``None`` means it requested
        nothing specific and inherits the (dangerous-filtered) allowlist; an empty sequence
        yields no tools. ``admit`` (via :func:`~pikachu.guard.effective_tools`) honours the
        distinction.
    :param fixed_allowlist: The agent's fixed allowlist — the only source of authority.
    :param lineage: Any existing lineage to merge the web-page taint into.
    :returns: An :class:`Admission` whose ``tools`` are the page-exposable set and whose
        ``lineage`` carries the web-page taint. Never raises for a denied tool — it omits.
    """
    return admit(
        origin,
        declared_tools=requested_tools,
        fixed_allowlist=fixed_allowlist,
        lineage=lineage,
        kind=SourceKind.WEB_PAGE,
    )
