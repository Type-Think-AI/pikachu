"""Tests for the plugins package: manifest validation and failure-tolerant loading.

All fixtures are built under ``tmp_path``; no network, no fixed paths. The two load-bearing
behaviours under test are (1) the CLOSED Agent Plugins 1.0.0 manifest schema and (2)
independent component failure — a broken ``mcp.json`` or one bad skill never sinks the rest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pikachu.core.errors import SkillParseError
from pikachu.core.types import Taint, TrustTier
from pikachu.plugins import (
    LoadedPlugin,
    PluginManifest,
    load_plugin,
    parse_manifest,
    validate_plugin_name,
)
from pikachu.plugins.manifest import SCHEMA_CONST

# --------------------------------------------------------------------------------------
# Manifest helpers
# --------------------------------------------------------------------------------------


def _manifest_text(**overrides: object) -> str:
    base: dict[str, object] = {"$schema": SCHEMA_CONST, "name": "my.plugin-1"}
    base.update(overrides)
    return json.dumps(base)


def _write_plugin(
    root: Path,
    *,
    manifest: str | None = None,
    skills: dict[str, str] | None = None,
    mcp: str | None = None,
) -> Path:
    """Materialise a plugin directory. ``skills`` maps a skill dir name to its SKILL.md text."""
    root.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (root / "plugin.json").write_text(manifest, encoding="utf-8")
    if skills:
        skills_dir = root / "skills"
        skills_dir.mkdir(exist_ok=True)
        for name, text in skills.items():
            d = skills_dir / name
            d.mkdir(exist_ok=True)
            (d / "SKILL.md").write_text(text, encoding="utf-8")
    if mcp is not None:
        (root / "mcp.json").write_text(mcp, encoding="utf-8")
    return root


def _skill_md(name: str, *, extra_frontmatter: str = "") -> str:
    fm = f"name: {name}\ndescription: A bundled skill.\n{extra_frontmatter}"
    return f"---\n{fm}---\n\n# {name}\n\nBody text.\n"


# --------------------------------------------------------------------------------------
# Manifest schema — required fields, const $schema
# --------------------------------------------------------------------------------------


def test_minimal_valid_manifest_parses() -> None:
    m = parse_manifest(_manifest_text())
    assert isinstance(m, PluginManifest)
    assert m.schema_ == SCHEMA_CONST
    assert m.name == "my.plugin-1"
    assert m.version is None
    assert m.keywords == ()
    assert m.extensions == {}


def test_wrong_schema_value_rejected() -> None:
    text = json.dumps(
        {
            "$schema": "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",
            "name": "ok",
        }
    )
    with pytest.raises(SkillParseError, match="\\$schema"):
        parse_manifest(text)


def test_missing_schema_rejected() -> None:
    with pytest.raises(SkillParseError, match="missing the required '\\$schema'"):
        parse_manifest(json.dumps({"name": "ok"}))


def test_missing_name_rejected() -> None:
    with pytest.raises(SkillParseError, match="missing the required 'name'"):
        parse_manifest(json.dumps({"$schema": SCHEMA_CONST}))


def test_invalid_json_rejected() -> None:
    with pytest.raises(SkillParseError, match="not valid JSON"):
        parse_manifest("{ not json ")


def test_non_object_manifest_rejected() -> None:
    with pytest.raises(SkillParseError, match="must be a JSON object"):
        parse_manifest(json.dumps(["a", "list"]))


# --------------------------------------------------------------------------------------
# name pattern
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["my.plugin-1", "a", "a1", "plugin.name-2.thing"])
def test_name_pattern_accepts(name: str) -> None:
    assert validate_plugin_name(name) == name
    assert parse_manifest(_manifest_text(name=name)).name == name


@pytest.mark.parametrize(
    "name",
    ["My-Plugin", "a--b", "a..b", "-lead", "trail-", "UPPER", "has space", "", ".lead"],
)
def test_name_pattern_rejects(name: str) -> None:
    with pytest.raises(SkillParseError, match="does not match the required pattern"):
        validate_plugin_name(name)


def test_double_hyphen_and_double_dot_are_traversal_defence() -> None:
    # The negative lookahead is specifically the '--' / '..' guard.
    with pytest.raises(SkillParseError):
        parse_manifest(_manifest_text(name="a--b"))
    with pytest.raises(SkillParseError):
        parse_manifest(_manifest_text(name="a..b"))


# --------------------------------------------------------------------------------------
# additionalProperties: false — closed manifest
# --------------------------------------------------------------------------------------


def test_top_level_unknown_key_rejected() -> None:
    text = json.dumps({"$schema": SCHEMA_CONST, "name": "ok", "com.acme.vendor": {"x": 1}})
    with pytest.raises(SkillParseError, match="unknown top-level key"):
        parse_manifest(text)


def test_vendor_key_belongs_under_extensions() -> None:
    text = _manifest_text(extensions={"com.acme.vendor": {"x": 1}})
    m = parse_manifest(text)
    assert m.extensions == {"com.acme.vendor": {"x": 1}}


def test_optional_fields_parse() -> None:
    text = _manifest_text(
        version="1.2.3",
        description="A test plugin.",
        author={"name": "Zara", "email": "z@example.com"},
        homepage="https://example.com",
        repository="https://github.com/x/y",
        license="MIT",
        keywords=["image", "brand"],
    )
    m = parse_manifest(text)
    assert m.version == "1.2.3"
    assert m.description == "A test plugin."
    assert m.author is not None and m.author.name == "Zara"
    assert m.keywords == ("image", "brand")


def test_wrongly_typed_optional_rejected() -> None:
    with pytest.raises(SkillParseError, match="'keywords' must be an array"):
        parse_manifest(_manifest_text(keywords="not-a-list"))
    with pytest.raises(SkillParseError, match="'author' must be an object"):
        parse_manifest(_manifest_text(author="not-an-object"))


# --------------------------------------------------------------------------------------
# Directory loading
# --------------------------------------------------------------------------------------


def test_minimal_plugin_directory_loads(tmp_path: Path) -> None:
    root = _write_plugin(tmp_path / "p", manifest=_manifest_text())
    result = load_plugin(root)
    assert isinstance(result, LoadedPlugin)
    assert result.ok
    assert result.manifest is not None
    assert result.manifest.name == "my.plugin-1"
    assert result.skills == ()
    assert result.mcp is None


def test_missing_manifest_is_a_component_error(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    result = load_plugin(root)
    assert not result.ok
    assert any(e.component == "manifest" for e in result.errors)


def test_non_directory_path_returns_error_not_raise(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = load_plugin(missing)
    assert not result.ok
    assert any(e.component == "plugin" for e in result.errors)


def test_plugin_with_good_skills_loads(tmp_path: Path) -> None:
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        skills={"one": _skill_md("one"), "two": _skill_md("two")},
    )
    result = load_plugin(root)
    assert result.ok
    assert {s.name for s in result.skills} == {"one", "two"}


def test_mcp_json_loaded_when_present(tmp_path: Path) -> None:
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        mcp=json.dumps({"mcpServers": {"x": {"command": "run"}}}),
    )
    result = load_plugin(root)
    assert result.ok
    assert result.mcp == {"mcpServers": {"x": {"command": "run"}}}


# --------------------------------------------------------------------------------------
# ★ Independent component failure
# --------------------------------------------------------------------------------------


def test_malformed_mcp_json_still_loads_skills(tmp_path: Path) -> None:
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        skills={"one": _skill_md("one")},
        mcp="{ this is not json ",
    )
    result = load_plugin(root)
    # skills survived
    assert {s.name for s in result.skills} == {"one"}
    # mcp failed, in isolation
    assert result.mcp is None
    assert any(e.component == "mcp" for e in result.errors)
    # and nothing else failed
    assert [e.component for e in result.errors] == ["mcp"]


def test_one_bad_skill_among_three_good_never_total_failure(tmp_path: Path) -> None:
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        skills={"g1": _skill_md("g1"), "g2": _skill_md("g2"), "g3": _skill_md("g3")},
    )
    # Corrupt one skill's SKILL.md so its frontmatter cannot parse.
    (root / "skills" / "g2" / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    result = load_plugin(root)
    good = {s.name for s in result.skills}
    assert good == {"g1", "g3"}
    assert len(result.skills) >= 2
    assert any(e.component == "skills/g2" for e in result.errors)
    # The manifest and (absent) mcp are unaffected.
    assert result.manifest is not None


def test_untrusted_skill_declaring_tools_is_reported_not_crash(tmp_path: Path) -> None:
    # An untrusted skill that declares tools trips the Skill model validator; load_bundle
    # re-raises it as SkillParseError, which the loader records as a plugin error.
    bad = _skill_md("greedy", extra_frontmatter="allowed-tools: [generate_image]\n")
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        skills={"greedy": bad, "fine": _skill_md("fine")},
    )
    result = load_plugin(root)
    assert {s.name for s in result.skills} == {"fine"}
    assert any(e.component == "skills/greedy" for e in result.errors)


# --------------------------------------------------------------------------------------
# Trust and taint
# --------------------------------------------------------------------------------------


def test_loaded_skills_are_untrusted_and_tainted(tmp_path: Path) -> None:
    root = _write_plugin(
        tmp_path / "p",
        manifest=_manifest_text(),
        skills={"one": _skill_md("one")},
    )
    result = load_plugin(root)
    (skill,) = result.skills
    assert skill.trust is TrustTier.UNTRUSTED
    assert not skill.trust.may_contribute_tools
    assert Taint.FOREIGN_SKILL in skill.lineage.taints
    assert not skill.lineage.is_clean
    assert skill.declared_tools == ()
    # The source string is recorded in the taint lineage for attribution.
    assert any(str(root) in s for s in skill.lineage.sources)
