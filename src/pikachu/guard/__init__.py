"""The permission engine — the product's USP and the most security-critical module.

Authority flows in exactly one direction: from the host-supplied fixed allowlist, narrowed
by what a skill declares. A skill can only ever *narrow* its authority; nothing it says can
widen it. Authority is never derived from the artifact requesting it — that was a real
escalation path in the parent system, where a skill's own frontmatter decided whether it
got a paid image tool.

Entry points:
  * :func:`effective_tools` — computes the narrowed toolset (invariant P3).
  * :func:`admit` — the single admission point every untrusted boundary routes through; it
    COMPOSES :func:`effective_tools` and merges the source's taint (S2's one path).
  * :func:`may_load` / :func:`resolve_trust` — the trust-tiered load gate.
"""

from __future__ import annotations

from pikachu.guard.allowlist import (
    DANGEROUS_TOOLS,
    EffectiveToolset,
    effective_tools,
)
from pikachu.guard.trust import may_load, resolve_trust
from pikachu.guard.untrusted import Admission, SourceKind, admit

__all__ = [
    "DANGEROUS_TOOLS",
    "Admission",
    "EffectiveToolset",
    "SourceKind",
    "admit",
    "effective_tools",
    "may_load",
    "resolve_trust",
]
