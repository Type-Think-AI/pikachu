"""Lane A tests — skills loader. Earns Rainbow (jointly with Lane D's scanner).

House rules: no network (enforced by conftest autouse), property tests over examples where
the shape is invariant. Everything here is marked ``rainbow``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pikachu.core.errors import SkillParseError
from pikachu.core.types import Skill, Taint, TrustTier
from pikachu.skills.loader import (
    SkillMeta,
    load_bundle,
    load_metadata,
    load_skill,
)

pytestmark = pytest.mark.rainbow


WELL_FORMED = """\
---
name: brand-palette
description: "Apply the house colour palette to a generated image."
license: MIT
allowed-tools: [generate_image, read_canvas]
compatibility:
  runtime: pikachu>=0.0.1
metadata:
  author: house
  category: colour
---
# Brand palette

Never use pure black. Never crop tighter than 16:9.
"""


# --------------------------------------------------------------------------------------
# round trip
# --------------------------------------------------------------------------------------


def test_round_trip_well_formed() -> None:
    skill = load_skill(WELL_FORMED, trust=TrustTier.BUILTIN, source="repo:builtin")
    assert isinstance(skill, Skill)
    assert skill.name == "brand-palette"
    assert skill.description.startswith("Apply the house colour palette")
    assert skill.declared_tools == ("generate_image", "read_canvas")
    assert skill.trust is TrustTier.BUILTIN
    assert skill.lineage.is_clean
    # Body preserved verbatim (leading/trailing handled by the split).
    assert "Never use pure black." in skill.body
    assert skill.body.lstrip().startswith("# Brand palette")


def test_metadata_matches_full_load() -> None:
    meta = load_metadata(WELL_FORMED)
    assert isinstance(meta, SkillMeta)
    assert meta.name == "brand-palette"
    assert meta.license == "MIT"
    assert meta.declared_tools == ("generate_image", "read_canvas")
    assert meta.compatibility == {"runtime": "pikachu>=0.0.1"}
    assert meta.metadata == {"author": "house", "category": "colour"}


# --------------------------------------------------------------------------------------
# progressive disclosure: metadata never reads the body
# --------------------------------------------------------------------------------------


def test_load_metadata_has_no_body_field() -> None:
    """SkillMeta structurally cannot carry a body — that is the guarantee, not luck."""
    meta = load_metadata(WELL_FORMED)
    assert not hasattr(meta, "body")


def test_load_metadata_ignores_a_body_that_would_break_a_strict_parse() -> None:
    """A body full of '---' delimiters and colon-laden lines that would confuse a naive
    parser must not affect metadata loading, because the body is never parsed."""
    doc = (
        "---\n"
        "name: only-frontmatter-read\n"
        "description: metadata still loads\n"
        "---\n"
        "--- this is not frontmatter ---\n"
        "key: value: value: not: yaml\n"
        "- a dangling list item\n"
        "\ttabbed: line\n"
        "--- another fake delimiter ---\n"
    )
    meta = load_metadata(doc)
    assert meta.name == "only-frontmatter-read"
    assert meta.description == "metadata still loads"


# --------------------------------------------------------------------------------------
# malformed frontmatter -> SkillParseError (several shapes)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param("no frontmatter at all\njust a body\n", id="no-delimiter"),
        pytest.param("---\nname: unclosed\nno closing delimiter\n", id="unclosed"),
        pytest.param("---\ndescription: missing name\n---\nbody\n", id="missing-name"),
        pytest.param("---\nname:\n---\nbody\n", id="empty-name"),
        pytest.param('---\nname: "unterminated\n---\nbody\n', id="unterminated-quote"),
        pytest.param("---\nname: a\nname: b\n---\nbody\n", id="duplicate-key"),
        pytest.param("---\nname: a\n  stray: indent\n---\nbody\n", id="bad-indent"),
        pytest.param("---\nname: a\nno colon here\n---\nbody\n", id="not-a-mapping"),
        pytest.param("---\nname: a\nallowed-tools: [a, b\n---\nbody\n", id="broken-list"),
    ],
)
def test_malformed_frontmatter_raises(doc: str) -> None:
    with pytest.raises(SkillParseError):
        load_metadata(doc)


# --------------------------------------------------------------------------------------
# allowed-tools (hyphen) -> declared_tools
# --------------------------------------------------------------------------------------


def test_allowed_tools_hyphen_maps_to_declared_tools() -> None:
    doc = (
        "---\n"
        "name: mapper\n"
        "allowed-tools: [generate_image, Read_Canvas]\n"
        "---\n"
        "# body\n"
    )
    meta = load_metadata(doc)
    # The hyphenated file key populates declared_tools.
    assert meta.declared_tools == ("generate_image", "Read_Canvas")

    skill = load_skill(doc, trust=TrustTier.VERIFIED, source="repo:verified")
    # The Skill validator normalizes tool names (lower-cased), order preserved.
    assert skill.declared_tools == ("generate_image", "read_canvas")


# --------------------------------------------------------------------------------------
# an UNTRUSTED skill declaring tools is rejected with a clear error
# --------------------------------------------------------------------------------------


def test_untrusted_declaring_tools_rejected_clearly() -> None:
    doc = (
        "---\n"
        "name: sneaky\n"
        "allowed-tools: [bash]\n"
        "---\n"
        "# body\n"
    )
    with pytest.raises(SkillParseError) as exc:
        load_skill(doc, trust=TrustTier.UNTRUSTED, source="catalog:community")
    msg = str(exc.value)
    assert "untrusted" in msg
    assert "sneaky" in msg


def test_community_declaring_tools_also_rejected() -> None:
    doc = "---\nname: c\nallowed-tools: [web]\n---\n# body\n"
    with pytest.raises(SkillParseError) as exc:
        load_skill(doc, trust=TrustTier.COMMUNITY, source="catalog:community")
    assert "community" in str(exc.value)


# --------------------------------------------------------------------------------------
# foreign load records taint + source
# --------------------------------------------------------------------------------------


def test_foreign_load_records_taint_and_source() -> None:
    doc = "---\nname: foreign\ndescription: a community skill\n---\n# body\n"
    skill = load_skill(doc, trust=TrustTier.UNTRUSTED, source="catalog:acme")
    assert Taint.FOREIGN_SKILL in skill.lineage.taints
    assert "catalog:acme" in skill.lineage.sources
    assert not skill.lineage.is_clean


def test_verified_load_stays_clean() -> None:
    doc = "---\nname: trusted\ndescription: reviewed\n---\n# body\n"
    skill = load_skill(doc, trust=TrustTier.VERIFIED, source="repo:verified")
    assert skill.lineage.is_clean


# --------------------------------------------------------------------------------------
# a bundle with scripts/run.sh records it and does NOT execute it
# --------------------------------------------------------------------------------------


def test_bundle_strips_and_records_scripts_without_executing(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: bundled\ndescription: has scripts\n---\n# body\n",
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # A script that, if ever executed, would leave a witness file.
    witness = tmp_path / "WITNESS"
    (scripts / "run.sh").write_text(
        f"#!/bin/sh\ntouch {witness}\n", encoding="utf-8"
    )
    (scripts / "helper.py").write_text(
        f"open({str(witness)!r}, 'w').close()\n", encoding="utf-8"
    )
    # A loose .py at the root also counts as an executable script.
    (tmp_path / "tool.py").write_text("raise SystemExit('should never run')\n", encoding="utf-8")

    skill = load_bundle(tmp_path, trust=TrustTier.VERIFIED, source="bundle:test")

    assert skill.name == "bundled"
    assert set(skill.stripped_scripts) == {"scripts/run.sh", "scripts/helper.py", "tool.py"}
    # Proof of non-execution: no witness file was created by loading the bundle.
    assert not witness.exists()


def test_bundle_missing_skill_md_raises(tmp_path: Path) -> None:
    with pytest.raises(SkillParseError):
        load_bundle(tmp_path, trust=TrustTier.BUILTIN, source="bundle:empty")


def test_bundle_path_not_a_directory_raises(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(SkillParseError):
        load_bundle(f, trust=TrustTier.BUILTIN, source="bundle:file")


# --------------------------------------------------------------------------------------
# hypothesis: load_metadata never raises anything but SkillParseError on arbitrary text
# --------------------------------------------------------------------------------------


@given(st.text())
def test_load_metadata_only_ever_raises_skill_parse_error(text: str) -> None:
    try:
        result = load_metadata(text)
    except SkillParseError:
        return
    # If it did not raise, it must have produced a valid SkillMeta with a non-empty name.
    assert isinstance(result, SkillMeta)
    assert result.name != ""


def _is_bare_string_name(s: str) -> bool:
    """A name that survives the flat parser AS a string (not int/float/bool/null)."""
    t = s.strip()
    if t == "" or "\n" in s or ":" in s or "#" in s:
        return False
    if t.startswith(("-", "[", "]", "'", '"')):
        return False
    # Would the scalar parser coerce it to a non-string? Reject those: they are a genuine
    # ambiguity in this YAML subset, not a name.
    if t.lower() in ("null", "~", "true", "false"):
        return False
    body = t[1:] if t[:1] in ("+", "-") else t
    if body.isdigit():
        return False
    if body.count(".") == 1:
        left, _, right = body.partition(".")
        if (left.isdigit() or right.isdigit()) and (left + right).isdigit():
            return False
    return True


@given(name=st.text(min_size=1, max_size=50).filter(_is_bare_string_name))
def test_minimal_well_formed_always_loads(name: str) -> None:
    doc = f"---\nname: {name}\n---\nbody\n"
    meta = load_metadata(doc)
    assert meta.name == name.strip()
