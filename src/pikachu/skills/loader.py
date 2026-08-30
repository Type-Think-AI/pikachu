"""Load SKILL.md documents into typed objects.

Two entry points, one seam:

  * :func:`load_metadata` reads ONLY the frontmatter. It never touches the body. This is
    progressive disclosure and it is the point — a catalogue of 400 skills must be
    listable without paying to read 400 bodies into context.
  * :func:`load_skill` / :func:`load_bundle` do the full load, set lineage from the trust
    tier, and — for a bundle — strip and record any executable script without importing or
    running it.

Malformed frontmatter is a :class:`SkillParseError`, never a silent default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pikachu.core.errors import SkillParseError
from pikachu.core.types import Lineage, Skill, TrustTier
from pikachu.guard.untrusted import SourceKind, admit
from pikachu.skills.frontmatter import (
    FrontmatterValue,
    parse_frontmatter,
    split_frontmatter,
)

__all__ = [
    "SkillMeta",
    "load_bundle",
    "load_metadata",
    "load_skill",
]

# Recognised frontmatter keys per agentskills.io. Anything else is ignored on read but the
# hyphenated 'allowed-tools' maps onto Skill.declared_tools deliberately.
_KNOWN_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)

# Scripts inside a bundle that are stripped and recorded, never executed.
_SCRIPT_SUFFIXES = frozenset({".sh", ".py"})
_SCRIPT_DIR = "scripts"

_BUNDLE_FILENAME = "SKILL.md"


@dataclass(frozen=True)
class SkillMeta:
    """Everything a catalogue needs, parsed from the frontmatter ALONE.

    Note there is no ``body`` field. That is not an omission — it is the guarantee. If a
    body ever needed to be present here, progressive disclosure would be broken and every
    listing would pay to read every body.
    """

    name: str
    description: str = ""
    license: str | None = None
    declared_tools: tuple[str, ...] = ()
    compatibility: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


def _require_str(value: FrontmatterValue, key: str) -> str:
    if not isinstance(value, str):
        raise SkillParseError(f"key {key!r} must be a string, got {type(value).__name__}")
    return value


def _optional_str(value: FrontmatterValue, key: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, key)


def _as_str_list(value: FrontmatterValue, key: str) -> tuple[str, ...]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SkillParseError(
                    f"key {key!r} must be a list of strings; found {type(item).__name__}"
                )
            out.append(item)
        return tuple(out)
    if isinstance(value, str):
        # A single bare string is tolerated as a one-element list.
        return (value,)
    raise SkillParseError(f"key {key!r} must be a list of strings, got {type(value).__name__}")


def _as_str_mapping(value: FrontmatterValue, key: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SkillParseError(f"key {key!r} must be a mapping, got {type(value).__name__}")
    out: dict[str, str] = {}
    for k, v in value.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, (str, int, float, bool)):
            out[k] = str(v)
        else:  # pragma: no cover - the frontmatter parser cannot produce other types
            raise SkillParseError(f"mapping {key!r} value for {k!r} is not a scalar")
    return out


def _meta_from_frontmatter(fm: dict[str, FrontmatterValue]) -> SkillMeta:
    if "name" not in fm:
        raise SkillParseError("frontmatter is missing the required 'name' key")

    name = _require_str(fm["name"], "name").strip()
    if name == "":
        raise SkillParseError("frontmatter 'name' must not be empty")

    description = ""
    if "description" in fm:
        description = _require_str(fm["description"], "description")

    license_ = _optional_str(fm.get("license"), "license") if "license" in fm else None

    declared_tools: tuple[str, ...] = ()
    if "allowed-tools" in fm:
        declared_tools = _as_str_list(fm["allowed-tools"], "allowed-tools")

    compatibility: dict[str, str] = {}
    if "compatibility" in fm:
        compatibility = _as_str_mapping(fm["compatibility"], "compatibility")

    metadata: dict[str, str] = {}
    if "metadata" in fm:
        metadata = _as_str_mapping(fm["metadata"], "metadata")

    return SkillMeta(
        name=name,
        description=description,
        license=license_,
        declared_tools=declared_tools,
        compatibility=compatibility,
        metadata=metadata,
    )


def load_metadata(text: str) -> SkillMeta:
    """Parse the frontmatter of a SKILL.md document. The body is never read.

    Raises :class:`SkillParseError` on malformed or missing frontmatter.
    """
    fm_block, _body = split_frontmatter(text)
    fm = parse_frontmatter(fm_block)
    return _meta_from_frontmatter(fm)


def _lineage_for(trust: TrustTier, source: str, declared: tuple[str, ...]) -> Lineage:
    """A skill from an untrusted origin is tainted FOREIGN_SKILL; trusted stays clean.

    The untrusted taint is derived through the shared guard admission path
    (:func:`pikachu.guard.untrusted.admit`) rather than being built here by hand: routing
    every untrusted-input boundary through ``admit`` is exactly what success criterion S2
    requires — one path, not three mechanisms that each happen to work. ``admit`` composes P3
    (narrowing ``declared`` against the empty load-time allowlist — an untrusted skill declares
    nothing anyway, which the frozen model enforces) and merges the ``FOREIGN_SKILL`` taint for
    this source kind. A BUILTIN/VERIFIED skill may contribute tools, so it stays clean and is
    not run through ``admit`` — admission is for untrusted input.
    """
    if trust.may_contribute_tools:
        return Lineage.clean()
    # No fixed allowlist exists at skill-load time; the guard's structural rule (an untrusted
    # skill may declare no tools) is enforced by the Skill model. ``admit`` here supplies the
    # canonical FOREIGN_SKILL taint on the same code path MCP servers and plugins use.
    return admit(
        source,
        declared_tools=declared,
        fixed_allowlist=(),
        trust=trust,
        kind=SourceKind.FOREIGN_SKILL,
    ).lineage


def load_skill(text: str, *, trust: TrustTier, source: str) -> Skill:
    """Full load: frontmatter + body -> :class:`Skill`, with lineage set from ``trust``.

    An UNTRUSTED/COMMUNITY skill is tagged :class:`Taint.FOREIGN_SKILL` with ``source``
    recorded; BUILTIN/VERIFIED stay clean. If such a skill also declares tools, the frozen
    model validator rejects it — that rejection is correct and is re-raised here as a
    :class:`SkillParseError` naming the trust tier, so callers get one error type.
    """
    fm_block, body = split_frontmatter(text)
    fm = parse_frontmatter(fm_block)
    meta = _meta_from_frontmatter(fm)
    lineage = _lineage_for(trust, source, meta.declared_tools)

    try:
        return Skill(
            name=meta.name,
            description=meta.description,
            body=body,
            declared_tools=meta.declared_tools,
            trust=trust,
            lineage=lineage,
        )
    except ValueError as exc:
        # The model validator refuses tool declarations below the trusted tiers. Surface it
        # as our own typed error, naming the tier so the message is actionable.
        raise SkillParseError(
            f"skill {meta.name!r} at trust={trust.value} declares tools "
            f"{meta.declared_tools!r}, which that trust tier may not do",
            path=source,
        ) from exc


def _collect_scripts(directory: Path) -> tuple[str, ...]:
    """Relative paths of every executable script in a bundle. Recorded, never run.

    Anything under ``scripts/`` (any suffix) plus any ``*.sh`` / ``*.py`` anywhere in the
    tree counts. SKILL.md itself is never a script.
    """
    found: set[str] = set()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(directory)
        if rel.name == _BUNDLE_FILENAME:
            continue
        parts = rel.parts
        in_scripts_dir = len(parts) > 0 and parts[0] == _SCRIPT_DIR
        is_script_suffix = path.suffix in _SCRIPT_SUFFIXES
        if in_scripts_dir or is_script_suffix:
            found.add(rel.as_posix())
    return tuple(sorted(found))


def load_bundle(directory: Path, *, trust: TrustTier, source: str) -> Skill:
    """Load a skill from a directory bundle: read SKILL.md, strip and record scripts.

    Scripts are enumerated and recorded in ``Skill.stripped_scripts``; they are NEVER read
    for execution, imported, or run. This is the containment guarantee: a foreign bundle
    cannot execute code merely by being loaded.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise SkillParseError(f"bundle path is not a directory: {directory}", path=source)

    skill_md = directory / _BUNDLE_FILENAME
    if not skill_md.is_file():
        raise SkillParseError(f"bundle has no {_BUNDLE_FILENAME}: {directory}", path=source)

    text = skill_md.read_text(encoding="utf-8")
    base = load_skill(text, trust=trust, source=source)
    scripts = _collect_scripts(directory)

    # Skill is frozen; produce a new instance carrying the recorded scripts.
    return base.model_copy(update={"stripped_scripts": scripts})
