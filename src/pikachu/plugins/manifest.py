"""Parse and validate a ``plugin.json`` against Agent Plugins 1.0.0.

The schema is small and CLOSED, so it is hand-validated here rather than through a
JSON-Schema library — that keeps the dependency list at one framework. Every field is
checked against the recorded, verified facts; a malformed manifest is a typed
:class:`~pikachu.core.errors.SkillParseError`, never a silent default.

Verified schema facts (agent-plugins.org, 1.0.0):

  * REQUIRED: ``$schema`` and ``name`` only.
  * ``$schema`` is a CONST — it must equal :data:`SCHEMA_CONST` exactly. Any other value,
    including a different version path, is invalid.
  * ``name`` matches :data:`NAME_PATTERN`. The negative lookahead forbidding ``--`` and
    ``..`` is a path-traversal and confusable-name defence; **our own skill names should
    adopt this same pattern.**
  * OPTIONAL: ``version``, ``description``, ``author`` (object), ``homepage``,
    ``repository``, ``license``, ``keywords`` (array), ``extensions`` (object).
  * ``additionalProperties`` is FALSE — the manifest is CLOSED. A top-level reverse-DNS
    vendor key makes a manifest INVALID; vendor extensions belong under ``extensions``.
  * ``skills/`` and ``mcp.json`` are directory/file CONVENTIONS, not manifest fields, and
    ``plugin.json`` itself cannot be relocated or inlined.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pikachu.core.errors import SkillParseError

__all__ = [
    "MANIFEST_FILENAME",
    "NAME_PATTERN",
    "SCHEMA_CONST",
    "PluginAuthor",
    "PluginManifest",
    "parse_manifest",
    "validate_plugin_name",
]

MANIFEST_FILENAME = "plugin.json"

#: The one legal value of ``$schema``. This is a CONST, asserted as a literal string.
SCHEMA_CONST = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

#: Legal plugin ``name`` — and the pattern our own skill names should adopt. The leading
#: negative lookahead forbids any occurrence of ``--`` or ``..``, blocking path traversal
#: and confusable names; the rest requires a lowercase-alnum start and end.
NAME_PATTERN = r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
_NAME_RE = re.compile(NAME_PATTERN)

# Fields the closed manifest permits at the top level. Anything else is rejected because
# additionalProperties is false.
_ALLOWED_KEYS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)


def validate_plugin_name(name: str) -> str:
    """Return ``name`` if it matches :data:`NAME_PATTERN`, else raise.

    The same pattern is recommended for our own skill names — its ``--``/``..`` lookahead is
    a deliberate path-traversal and confusable-name defence, not cosmetic.
    """
    if not isinstance(name, str):
        raise SkillParseError(
            f"manifest 'name' must be a string, got {type(name).__name__}"
        )
    if not _NAME_RE.match(name):
        raise SkillParseError(
            f"manifest 'name' {name!r} does not match the required pattern {NAME_PATTERN}"
        )
    return name


class PluginAuthor(BaseModel):
    """The optional ``author`` object. Frozen and closed to unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None
    email: str | None = None
    url: str | None = None


class PluginManifest(BaseModel):
    """A validated ``plugin.json``.

    ``$schema`` is stored under the alias ``schema_``; every optional field defaults to its
    empty/None form. The model is frozen and closed to unknown keys — but the closedness of
    the *raw* manifest is enforced in :func:`parse_manifest` so the rejection message names
    the offending key rather than surfacing a Pydantic error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="$schema")
    name: str
    version: str | None = None
    description: str | None = None
    author: PluginAuthor | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)


def _require_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise SkillParseError(f"manifest {key!r} must be a string, got {type(value).__name__}")
    return value


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    if key not in raw:
        return None
    return _require_str(raw[key], key)


def parse_manifest(raw_text: str, *, source: str | None = None) -> PluginManifest:
    """Parse ``plugin.json`` text and validate it against Agent Plugins 1.0.0.

    Raises :class:`SkillParseError` — never a silent default — on invalid JSON, a missing
    or wrong ``$schema``, a missing or malformed ``name``, an unknown top-level key
    (``additionalProperties: false``), or a wrongly-typed optional field.
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SkillParseError(f"{MANIFEST_FILENAME} is not valid JSON: {exc}", path=source) from exc

    if not isinstance(data, dict):
        raise SkillParseError(
            f"{MANIFEST_FILENAME} must be a JSON object, got {type(data).__name__}",
            path=source,
        )

    # additionalProperties: false — the manifest is CLOSED. Reject any unknown top-level
    # key by name (a reverse-DNS vendor key belongs under 'extensions', not at the root).
    unknown = sorted(k for k in data if k not in _ALLOWED_KEYS)
    if unknown:
        raise SkillParseError(
            f"{MANIFEST_FILENAME} has unknown top-level key(s) {unknown}; the manifest is "
            f"closed (additionalProperties: false) — vendor keys belong under 'extensions'",
            path=source,
        )

    # $schema — required CONST, asserted as a literal string.
    if "$schema" not in data:
        raise SkillParseError(f"{MANIFEST_FILENAME} is missing the required '$schema' key", path=source)
    schema_value = _require_str(data["$schema"], "$schema")
    if schema_value != SCHEMA_CONST:
        raise SkillParseError(
            f"{MANIFEST_FILENAME} '$schema' must equal {SCHEMA_CONST!r} exactly, "
            f"got {schema_value!r}",
            path=source,
        )

    # name — required, pattern-checked.
    if "name" not in data:
        raise SkillParseError(f"{MANIFEST_FILENAME} is missing the required 'name' key", path=source)
    name = validate_plugin_name(data["name"])

    # Optional scalars.
    version = _optional_str(data, "version")
    description = _optional_str(data, "description")
    homepage = _optional_str(data, "homepage")
    repository = _optional_str(data, "repository")
    license_ = _optional_str(data, "license")

    # author — optional object.
    author: PluginAuthor | None = None
    if "author" in data:
        author_raw = data["author"]
        if not isinstance(author_raw, dict):
            raise SkillParseError(
                f"manifest 'author' must be an object, got {type(author_raw).__name__}",
                path=source,
            )
        try:
            author = PluginAuthor.model_validate(author_raw)
        except ValueError as exc:
            raise SkillParseError(f"manifest 'author' is invalid: {exc}", path=source) from exc

    # keywords — optional array of strings.
    keywords: tuple[str, ...] = ()
    if "keywords" in data:
        kw_raw = data["keywords"]
        if not isinstance(kw_raw, list):
            raise SkillParseError(
                f"manifest 'keywords' must be an array, got {type(kw_raw).__name__}",
                path=source,
            )
        parsed_kw: list[str] = []
        for item in kw_raw:
            if not isinstance(item, str):
                raise SkillParseError(
                    f"manifest 'keywords' must be an array of strings; found "
                    f"{type(item).__name__}",
                    path=source,
                )
            parsed_kw.append(item)
        keywords = tuple(parsed_kw)

    # extensions — optional object. The one place vendor keys are allowed; not validated
    # further because its shape is vendor-defined.
    extensions: dict[str, Any] = {}
    if "extensions" in data:
        ext_raw = data["extensions"]
        if not isinstance(ext_raw, dict):
            raise SkillParseError(
                f"manifest 'extensions' must be an object, got {type(ext_raw).__name__}",
                path=source,
            )
        extensions = ext_raw

    return PluginManifest(
        schema_=schema_value,
        name=name,
        version=version,
        description=description,
        author=author,
        homepage=homepage,
        repository=repository,
        license=license_,
        keywords=keywords,
        extensions=extensions,
    )
