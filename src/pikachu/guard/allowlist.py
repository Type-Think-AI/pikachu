"""The allowlist intersection — invariant P3.

    effective_tools(fixed_allowlist, declared) ⊆ set(fixed_allowlist) ∩ set(declared)

A skill can only ever NARROW its authority. It can never widen it. This is proven for
*every* input by ``tests/properties/test_p3.py`` (hypothesis), and demonstrated on the
specific historical failure cases by ``tests/test_guard.py``.

Design rules, each one earned from a real incident in the parent system:

* **Normalize at the entry point.** Every input is run through
  :func:`~pikachu.core.types.normalize_tool_name` before any comparison. The historical bug
  was that one path matched the literal string, so ``" terminal "`` and ``"TERMINAL"``
  survived a strip that was meant to remove them. A guarantee that holds on one path and
  not another is not a guarantee — so we normalize here even though ``Skill.declared_tools``
  is already normalized by its own validator: a raw ``tuple[str, ...]`` may arrive from any
  caller, not only through a ``Skill``.

* **Preserve order and multiplicity.** ``("web", "web")`` stays ``("web", "web")``. We do
  NOT sort and do NOT dedupe. A pinned test in the parent repo depends on multiplicity and
  order surviving, and :func:`effective_tools` is a filter, not a set operation.

* **Dangerous tools are stripped, never silently dropped.** ``bash``, ``terminal``,
  ``read_file``, ``browser`` are removed into ``removed_tools`` with a recorded reason, even
  if the host allowlist somehow contains them.

* **Fail closed.** When a tool must be denied it is OMITTED from ``tools``. This function
  never raises — raising from a tool-preparation hook is unsupported by the underlying
  framework and produces a hard error instead of a graceful denial. ``ToolDenied`` is for
  the call boundary, not for this filter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.types import normalize_tool_name

__all__ = [
    "DANGEROUS_TOOLS",
    "EffectiveToolset",
    "effective_tools",
]


#: Tools that are never granted, regardless of allowlist or declaration. Stored normalized
#: so membership tests are done against the same canonical form every other name uses.
DANGEROUS_TOOLS: Final[frozenset[str]] = frozenset(
    normalize_tool_name(t) for t in ("bash", "terminal", "read_file", "browser")
)

#: Reason strings recorded in ``EffectiveToolset.reasons`` for each dropped tool. Kept as
#: constants so tests can assert on them without pinning prose.
_REASON_NOT_ALLOWED: Final[str] = "not in fixed allowlist"
_REASON_DANGEROUS: Final[str] = "dangerous tool, always stripped"


class EffectiveToolset(BaseModel):
    """The result of narrowing a declared toolset against a fixed allowlist.

    ``tools`` is what the backend may actually use, in declaration order with duplicates
    intact. ``removed_tools`` accounts for every declared tool that did not make it, and
    ``reasons`` maps each removed tool to why. Between them, ``tools`` and ``removed_tools``
    account for every declared item (see the property test).
    """

    model_config = ConfigDict(frozen=True)

    tools: tuple[str, ...] = Field(default_factory=tuple)
    removed_tools: tuple[str, ...] = Field(default_factory=tuple)
    reasons: dict[str, str] = Field(default_factory=dict)


def effective_tools(
    fixed_allowlist: Sequence[str],
    declared: Sequence[str] | None,
) -> EffectiveToolset:
    """Narrow ``declared`` against ``fixed_allowlist``, enforcing invariant P3.

    :param fixed_allowlist: The host-supplied allowlist — the ONLY source of authority.
    :param declared: What the skill asks for. ``None`` means *the skill declared nothing*,
        which yields the full (dangerous-filtered) allowlist. An empty tuple ``()`` means
        *the skill declared an empty set*, which yields no tools. These are DIFFERENT and
        both are tested.
    :returns: An :class:`EffectiveToolset`. ``tools`` is a subset of
        ``set(fixed_allowlist) ∩ set(declared)`` (or of ``set(fixed_allowlist)`` when
        ``declared is None``), minus any dangerous tools, with order and multiplicity of the
        chosen sequence preserved.

    This function never raises. A denied tool is omitted from ``tools`` and recorded in
    ``removed_tools`` / ``reasons``.
    """
    # Normalize the allowlist ONCE, here, at the entry point. Membership is a set; the set
    # never leaks into the output ordering.
    allow_set: frozenset[str] = frozenset(
        normalize_tool_name(t) for t in fixed_allowlist if normalize_tool_name(t)
    )

    # `None` (declared nothing) is distinct from `()` (declared empty). When nothing was
    # declared, the skill inherits the whole allowlist, so we walk the allowlist as the
    # source sequence; otherwise we walk exactly what was declared.
    if declared is None:
        source: Sequence[str] = tuple(fixed_allowlist)
    else:
        source = declared

    kept: list[str] = []
    removed: list[str] = []
    reasons: dict[str, str] = {}

    for raw in source:
        name = normalize_tool_name(raw)
        if not name:
            # A name that normalizes to empty is not a tool at all — it cannot be granted
            # and there is nothing meaningful to record it under. Skip it.
            continue

        if name in DANGEROUS_TOOLS:
            # Stripped even if it is in the allowlist. Recorded, never silently dropped.
            removed.append(name)
            reasons[name] = _REASON_DANGEROUS
            continue

        if name not in allow_set:
            # Cannot widen authority: a declared tool absent from the fixed allowlist is
            # denied. This is the core of P3.
            removed.append(name)
            reasons[name] = _REASON_NOT_ALLOWED
            continue

        # Order and multiplicity preserved: no dedupe, no sort.
        kept.append(name)

    return EffectiveToolset(
        tools=tuple(kept),
        removed_tools=tuple(removed),
        reasons=reasons,
    )
