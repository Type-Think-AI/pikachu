"""Load a plugin DIRECTORY into a partial, failure-tolerant result.

The single most important behaviour here is **independent component failure**: a plugin has
three loosely-coupled parts (the manifest, the ``skills/`` directory, and an optional
``mcp.json``), and a fault in one must not sink the others. A malformed ``mcp.json`` still
lets ``skills/`` load; one bad skill among good ones still yields the good ones. Each
component is loaded separately, its errors collected as :class:`ComponentError` values, and
a PARTIAL :class:`LoadedPlugin` is returned with those failures attached — rather than
raising and losing everything that did load.

Everything a plugin provides is UNTRUSTED. Skills load at
:class:`~pikachu.core.types.TrustTier.UNTRUSTED`, tainted
:class:`~pikachu.core.types.Taint.FOREIGN_SKILL` with the plugin source recorded, so they
contribute no toolsets. The frozen :class:`~pikachu.core.types.Skill` model already refuses
an untrusted skill that declares tools; that rejection is caught and reported as a plugin
error rather than crashing the load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pikachu.core.errors import PikachuError, SkillParseError
from pikachu.core.types import Skill, TrustTier
from pikachu.plugins.manifest import MANIFEST_FILENAME, PluginManifest, parse_manifest
from pikachu.skills.loader import load_bundle

__all__ = [
    "ComponentError",
    "LoadedPlugin",
    "load_plugin",
]

_SKILLS_DIRNAME = "skills"
_MCP_FILENAME = "mcp.json"
_BUNDLE_FILENAME = "SKILL.md"


@dataclass(frozen=True)
class ComponentError:
    """One component's failure, recorded so a partial load stays diagnosable.

    ``component`` names the part that failed (``"manifest"``, ``"skills"``, ``"mcp"``, or a
    specific skill path). ``detail`` is the human-readable reason.
    """

    component: str
    detail: str
    path: str | None = None


@dataclass(frozen=True)
class LoadedPlugin:
    """The result of loading a plugin directory — possibly PARTIAL.

    A plugin with a broken ``mcp.json`` but a good ``skills/`` yields a ``LoadedPlugin``
    with populated ``skills`` and one ``errors`` entry. Callers inspect :attr:`ok` /
    :attr:`errors` to decide what to do; the loader never throws away what did load.
    """

    root: Path
    source: str
    manifest: PluginManifest | None = None
    skills: tuple[Skill, ...] = ()
    mcp: dict[str, object] | None = None
    errors: tuple[ComponentError, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True when nothing failed. A partial load has ``ok is False`` and a usable body."""
        return not self.errors


def _load_manifest(
    root: Path, source: str, errors: list[ComponentError]
) -> PluginManifest | None:
    """Load and validate ``plugin.json``. plugin.json cannot be relocated or inlined."""
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        errors.append(
            ComponentError(
                component="manifest",
                detail=f"{MANIFEST_FILENAME} not found at plugin root",
                path=str(manifest_path),
            )
        )
        return None
    try:
        text = manifest_path.read_text(encoding="utf-8")
        return parse_manifest(text, source=source)
    except SkillParseError as exc:
        errors.append(
            ComponentError(component="manifest", detail=str(exc), path=str(manifest_path))
        )
        return None
    except OSError as exc:
        errors.append(
            ComponentError(
                component="manifest",
                detail=f"could not read {MANIFEST_FILENAME}: {exc}",
                path=str(manifest_path),
            )
        )
        return None


def _load_skills(
    root: Path, source: str, errors: list[ComponentError]
) -> tuple[Skill, ...]:
    """Load every bundle under ``skills/``, collecting per-skill errors.

    Each skill directory (one holding a ``SKILL.md``) is loaded independently: one bad
    skill records a :class:`ComponentError` and does not abort the others. Skills load
    UNTRUSTED and tainted with the plugin source; a skill that declares tools trips the
    model validator, which :func:`~pikachu.skills.loader.load_bundle` re-raises as a
    :class:`SkillParseError` — caught here and reported as a plugin error, never a crash.
    """
    skills_dir = root / _SKILLS_DIRNAME
    if not skills_dir.is_dir():
        # No skills/ is not an error — it is simply a plugin without bundled skills.
        return ()

    loaded: list[Skill] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (child / _BUNDLE_FILENAME).is_file():
            # Not a bundle directory; skip silently rather than inventing an error.
            continue
        skill_source = f"{source}:{_SKILLS_DIRNAME}/{child.name}"
        try:
            loaded.append(
                load_bundle(child, trust=TrustTier.UNTRUSTED, source=skill_source)
            )
        except SkillParseError as exc:
            errors.append(
                ComponentError(
                    component=f"{_SKILLS_DIRNAME}/{child.name}",
                    detail=str(exc),
                    path=str(child),
                )
            )
        except PikachuError as exc:  # any other typed plugin fault stays a per-skill error
            errors.append(
                ComponentError(
                    component=f"{_SKILLS_DIRNAME}/{child.name}",
                    detail=str(exc),
                    path=str(child),
                )
            )
    return tuple(loaded)


def _load_mcp(
    root: Path, source: str, errors: list[ComponentError]
) -> dict[str, object] | None:
    """Load ``mcp.json`` if present. A malformed one is an isolated component error.

    Returns the parsed object, or ``None`` when the file is absent OR broken — in the
    broken case a :class:`ComponentError` is appended so the caller knows the difference
    via :attr:`LoadedPlugin.errors`.
    """
    mcp_path = root / _MCP_FILENAME
    if not mcp_path.is_file():
        return None
    try:
        text = mcp_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(
            ComponentError(
                component="mcp", detail=f"could not read {_MCP_FILENAME}: {exc}", path=str(mcp_path)
            )
        )
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(
            ComponentError(
                component="mcp",
                detail=f"{_MCP_FILENAME} is not valid JSON: {exc}",
                path=str(mcp_path),
            )
        )
        return None
    if not isinstance(data, dict):
        errors.append(
            ComponentError(
                component="mcp",
                detail=f"{_MCP_FILENAME} must be a JSON object, got {type(data).__name__}",
                path=str(mcp_path),
            )
        )
        return None
    return data


def load_plugin(directory: Path | str, *, source: str | None = None) -> LoadedPlugin:
    """Load a plugin directory into a partial, failure-tolerant :class:`LoadedPlugin`.

    The manifest, ``skills/``, and ``mcp.json`` are loaded as INDEPENDENT components — a
    fault in one is recorded in :attr:`LoadedPlugin.errors` and does not prevent the others
    from loading. The function returns a result rather than raising; inspect :attr:`ok`.

    ``source`` labels the plugin's origin for taint recording; it defaults to the directory
    path.
    """
    root = Path(directory)
    src = source if source is not None else str(root)
    errors: list[ComponentError] = []

    if not root.is_dir():
        errors.append(
            ComponentError(
                component="plugin", detail=f"plugin path is not a directory: {root}", path=str(root)
            )
        )
        return LoadedPlugin(root=root, source=src, errors=tuple(errors))

    manifest = _load_manifest(root, src, errors)
    skills = _load_skills(root, src, errors)
    mcp = _load_mcp(root, src, errors)

    return LoadedPlugin(
        root=root,
        source=src,
        manifest=manifest,
        skills=skills,
        mcp=mcp,
        errors=tuple(errors),
    )
