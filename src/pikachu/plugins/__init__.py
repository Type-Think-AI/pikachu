"""Agent Plugins 1.0.0 — parse and load a plugin directory.

A plugin is a directory with a ``plugin.json`` manifest at its root, an optional
``skills/`` directory of bundled skills, and an optional ``mcp.json`` file. The manifest
schema is the Agent Plugins 1.0.0 standard (agent-plugins.org); this package validates the
small, CLOSED shape by hand rather than pulling in a JSON-Schema dependency — the framework
dependency list stays at one.

Two guarantees define this package:

  * **Everything from a plugin is UNTRUSTED.** Its skills load at
    :class:`~pikachu.core.types.TrustTier.UNTRUSTED`, tainted
    :class:`~pikachu.core.types.Taint.FOREIGN_SKILL` with the plugin source recorded, and
    contribute no toolsets. A foreign document cannot smuggle authority in by being
    packaged as a plugin.
  * **Components fail independently.** A broken ``mcp.json`` must not stop ``skills/`` from
    loading, and one bad skill among good ones must not sink the rest. :func:`load_plugin`
    loads each component separately, collects per-component errors, and returns a PARTIAL
    :class:`LoadedPlugin` with the failures attached rather than raising.
"""

from __future__ import annotations

from pikachu.plugins.loader import (
    ComponentError,
    LoadedPlugin,
    load_plugin,
)
from pikachu.plugins.manifest import (
    PluginAuthor,
    PluginManifest,
    parse_manifest,
    validate_plugin_name,
)

__all__ = [
    "ComponentError",
    "LoadedPlugin",
    "PluginAuthor",
    "PluginManifest",
    "load_plugin",
    "parse_manifest",
    "validate_plugin_name",
]
