"""The model choice is enforced, not remembered.

Standing instruction from the project owner: Pikachu uses ``google/gemini-3.7-flash``.
Gemini 2.x must not appear as a model anywhere — not as a default, not as a fallback, not as a
"runner-up" recommendation somebody could act on.

A rule that lives only in a doc or a conversation drifts. This test makes it a build failure,
which is the only kind of rule that holds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pikachu.config import DEFAULT_MODEL

REPO_ROOT = Path(__file__).resolve().parents[1]

# Any gemini 1.x or 2.x model id. Deliberately narrow: it must not match 3.x, and it must not
# match the words "gemini" or "2.5" on their own, which appear in legitimate prose.
FORBIDDEN = re.compile(r"gemini[-_/.]?(?:1|2)\.\d", re.IGNORECASE)

SEARCH_DIRS = ("src", "tests", "scripts")
SKIP_PARTS = {".venv", "__pycache__", ".git", ".mypy_cache", ".pytest_cache", "reports"}


def _files_to_check() -> list[Path]:
    out: list[Path] = []
    for directory in SEARCH_DIRS:
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            out.append(path)
    return out


@pytest.mark.boulder
def test_default_model_is_gemini_3_7_flash() -> None:
    assert DEFAULT_MODEL == "google/gemini-3.7-flash", (
        f"the default model must be google/gemini-3.7-flash, found {DEFAULT_MODEL!r}"
    )


@pytest.mark.boulder
def test_no_forbidden_model_reference_in_code() -> None:
    """No gemini 1.x/2.x model id anywhere in src/, tests/ or scripts/.

    If this fails, someone reintroduced an older Gemini as a default, fallback or example.
    Replace it with ``pikachu.config.DEFAULT_MODEL`` rather than another literal.
    """
    offenders: list[str] = []
    for path in _files_to_check():
        if path.name == Path(__file__).name:
            continue  # this file names the forbidden pattern on purpose
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:100]}")

    assert not offenders, "forbidden model reference(s) found:\n  " + "\n  ".join(offenders)


@pytest.mark.boulder
def test_no_stale_model_literal_anywhere() -> None:
    """No ``google/gemini-*`` literal other than the CURRENT default, anywhere.

    Two separate rules, and the second exists because the first was not enough:

    * ``src/`` may only name a model inside ``config.py`` — everything else imports
      ``DEFAULT_MODEL``.
    * ``tests/`` and ``scripts/`` may name the *current* model (a benchmark or a measurement
      script reasonably does), but never a **different** one.

    A stale literal is the real defect class here. Wave 4 shipped
    ``google/gemini-3.5-flash`` in an OTel docstring and in five test assertions — neither the
    forbidden-model check (which bans gemini 1.x/2.x) nor the src-only literal check caught
    them, because 3.5 is neither forbidden nor in ``src/``. The result was a test asserting a
    span name for a model the project does not use.
    """
    literal = re.compile(r"[\"']google/gemini-[\w.\-]+[\"']")
    offenders: list[str] = []
    for directory in SEARCH_DIRS:
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts) or path.name == Path(__file__).name:
                continue
            if path.name == "config.py":
                continue  # the one place a literal belongs
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for match in literal.findall(line):
                    named = match.strip("\"'")
                    in_src = path.parts[len(REPO_ROOT.parts)] == "src"
                    if in_src or named != DEFAULT_MODEL:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{lineno}: {named}"
                        )

    assert not offenders, (
        "stale or misplaced model literal(s) — import DEFAULT_MODEL instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.boulder
def test_model_literals_are_not_hardcoded_outside_config() -> None:
    """Only ``config.py`` may contain a raw ``google/gemini-...`` string in ``src/``.

    Everything else must import ``DEFAULT_MODEL``, so changing the model is a one-line edit
    rather than a search-and-replace that misses a file.
    """
    literal = re.compile(r"[\"']google/gemini-[\w.\-]+[\"']")
    offenders: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        if SKIP_PARTS & set(path.parts) or path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if literal.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:100]}")

    assert not offenders, (
        "hardcoded model string outside config.py - import DEFAULT_MODEL instead:\n  "
        + "\n  ".join(offenders)
    )
