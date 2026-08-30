"""Tests for the badge runner itself.

The load-bearing behaviour is that NOT YET BUILT and FAILED are reported differently and
gate differently: an unbuilt badge is expected mid-project and must not fail the build,
while a failed badge must. If the runner conflates them the report is worthless, so these
tests pin the distinction from three angles: status classification, exit code, and
rendered text.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# scripts/ is not an importable package; load the module by path. It must be registered
# in sys.modules BEFORE exec_module, or dataclasses cannot resolve the module's __dict__
# for its string annotations (from __future__ import annotations) during class creation.
_BADGES_PATH = Path(__file__).resolve().parent.parent / "scripts" / "badges.py"
_spec = importlib.util.spec_from_file_location("badges_runner", _BADGES_PATH)
assert _spec is not None and _spec.loader is not None
badges = importlib.util.module_from_spec(_spec)
sys.modules["badges_runner"] = badges
_spec.loader.exec_module(badges)


# --------------------------------------------------------------------------------------
# Summary parsing — the fragile bit, so it gets the most cases.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("102 passed in 0.43s", (102, 0, 0)),
        ("2 failed, 100 passed in 0.50s", (100, 2, 0)),
        ("1 passed, 3 skipped in 0.10s", (1, 0, 3)),
        ("5 failed, 2 passed, 1 skipped in 1.2s", (2, 5, 1)),
        ("1 error in 0.01s", (0, 1, 0)),
        ("no tests ran in 0.01s", (0, 0, 0)),
    ],
)
def test_parse_summary(output: str, expected: tuple[int, int, int]) -> None:
    assert badges._parse_summary(output) == expected


def test_parse_summary_picks_last_summary_line() -> None:
    """A verbose log with a decoy line earlier still parses the real summary at the end."""
    output = "collected 3 items\ntest_a PASSED\ntest_b FAILED\n=== 1 failed, 2 passed in 0.2s ==="
    assert badges._parse_summary(output) == (2, 1, 0)


# --------------------------------------------------------------------------------------
# Status classification — unbuilt vs failed vs earned.
# --------------------------------------------------------------------------------------


def _badge() -> "badges.Badge":
    return badges.Badge("demo", "Demo", "Trainer, Type", "proves a thing", "Z")


def test_evaluate_reports_unbuilt_when_no_tests_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(badges, "_collected_count", lambda marker: 0)

    def _should_not_run(marker: str) -> tuple[int, int, int, int]:
        raise AssertionError("must not run pytest for an unbuilt marker")

    monkeypatch.setattr(badges, "_run_marker", _should_not_run)

    result = badges.evaluate(_badge())
    assert result.status is badges.Status.UNBUILT
    assert result.collected == 0
    assert result.failed == 0


def test_evaluate_reports_failed_when_tests_run_and_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(badges, "_collected_count", lambda marker: 5)
    monkeypatch.setattr(badges, "_run_marker", lambda marker: (1, 3, 2, 0))

    result = badges.evaluate(_badge())
    assert result.status is badges.Status.FAILED
    assert result.collected == 5
    assert result.failed == 2
    assert result.passed == 3


def test_evaluate_reports_earned_when_tests_run_and_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(badges, "_collected_count", lambda marker: 5)
    monkeypatch.setattr(badges, "_run_marker", lambda marker: (0, 5, 0, 0))

    result = badges.evaluate(_badge())
    assert result.status is badges.Status.EARNED
    assert result.passed == 5
    assert result.failed == 0


def test_unbuilt_and_failed_are_distinct_states() -> None:
    """The whole point: these are not the same status."""
    assert badges.Status.UNBUILT is not badges.Status.FAILED
    assert badges.Status.UNBUILT.value != badges.Status.FAILED.value


# --------------------------------------------------------------------------------------
# Exit code gating — unbuilt does NOT gate, failed DOES.
# --------------------------------------------------------------------------------------


def _stub_results(monkeypatch: pytest.MonkeyPatch, results: list["badges.BadgeResult"]) -> None:
    monkeypatch.setattr(badges, "evaluate_all", lambda: results)


def test_main_exits_zero_when_only_unbuilt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-built project with unbuilt badges is not a build failure."""
    results = [
        badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0),
        badges.BadgeResult(_badge(), badges.Status.UNBUILT, 0, 0, 0, 0),
    ]
    _stub_results(monkeypatch, results)
    assert badges.main([]) == 0


def test_main_exits_nonzero_when_any_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = [
        badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0),
        badges.BadgeResult(_badge(), badges.Status.FAILED, 3, 1, 2, 0),
        badges.BadgeResult(_badge(), badges.Status.UNBUILT, 0, 0, 0, 0),
    ]
    _stub_results(monkeypatch, results)
    assert badges.main([]) == 1


def test_main_exits_zero_when_all_earned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = [badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0)]
    _stub_results(monkeypatch, results)
    assert badges.main([]) == 0


# --------------------------------------------------------------------------------------
# Rendering — the report must SHOW the distinction, not just encode it in an exit code.
# --------------------------------------------------------------------------------------


def test_human_render_labels_unbuilt_and_failed_differently() -> None:
    pal = badges._Palette(enabled=False)
    unbuilt = [badges.BadgeResult(_badge(), badges.Status.UNBUILT, 0, 0, 0, 0)]
    failed = [badges.BadgeResult(_badge(), badges.Status.FAILED, 3, 1, 2, 0)]

    unbuilt_text = badges.render_human(unbuilt, pal)
    failed_text = badges.render_human(failed, pal)

    assert "NOT YET BUILT" in unbuilt_text
    assert "FAILED" not in unbuilt_text
    assert "FAILED" in failed_text
    assert "NOT YET BUILT" not in failed_text


def test_human_render_has_no_ansi_codes_when_disabled() -> None:
    """isatty() guard: piped output must be free of escape codes."""
    pal = badges._Palette(enabled=False)
    text = badges.render_human(
        [badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0)], pal
    )
    assert "\033[" not in text


def test_human_render_has_ansi_codes_when_enabled() -> None:
    pal = badges._Palette(enabled=True)
    text = badges.render_human(
        [badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0)], pal
    )
    assert "\033[" in text


def test_human_render_includes_tier_reminder() -> None:
    pal = badges._Palette(enabled=False)
    text = badges.render_human(
        [badges.BadgeResult(_badge(), badges.Status.EARNED, 5, 5, 0, 0)], pal
    )
    assert "TIER 1" in text
    assert "TIER 2" in text


def test_badges_are_rendered_in_gym_order() -> None:
    pal = badges._Palette(enabled=False)
    # Render the canonical table directly, not a live run.
    canonical = [
        badges.BadgeResult(b, badges.Status.UNBUILT, 0, 0, 0, 0) for b in badges.BADGES
    ]
    text = badges.render_human(canonical, pal)
    order = ["Boulder", "Cascade", "Thunder", "Rainbow", "Soul", "Marsh", "Volcano", "Earth"]
    positions = [text.index(name) for name in order]
    assert positions == sorted(positions), "badges must appear in gym order"


# --------------------------------------------------------------------------------------
# JSON output — machine readable, and encodes the same distinction.
# --------------------------------------------------------------------------------------


def test_json_render_is_valid_and_carries_states() -> None:
    results = [
        badges.BadgeResult(badges.BADGES[0], badges.Status.EARNED, 5, 5, 0, 0),
        badges.BadgeResult(badges.BADGES[2], badges.Status.FAILED, 3, 1, 2, 0),
        badges.BadgeResult(badges.BADGES[3], badges.Status.UNBUILT, 0, 0, 0, 0),
    ]
    payload = json.loads(badges.render_json(results))
    assert payload["tier"] == 1
    assert payload["gates_shipping"] is True
    assert payload["summary"] == {"total": 3, "earned": 1, "failed": 1, "unbuilt": 1}
    statuses = {b["marker"]: b["status"] for b in payload["badges"]}
    assert statuses["boulder"] == "earned"
    assert statuses["thunder"] == "failed"
    assert statuses["rainbow"] == "unbuilt"


def test_main_json_output_is_parseable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_results(
        monkeypatch,
        [badges.BadgeResult(badges.BADGES[0], badges.Status.EARNED, 5, 5, 0, 0)],
    )
    rc = badges.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["badges"][0]["marker"] == "boulder"
    assert rc == 0


# --------------------------------------------------------------------------------------
# The badge table matches the pytest markers defined in pyproject.toml.
# --------------------------------------------------------------------------------------


def test_badge_table_matches_pyproject_markers() -> None:
    """A badge with no matching marker (or a marker with no badge) would silently
    never run — pin the two lists together."""
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    marker_lines = data["tool"]["pytest"]["ini_options"]["markers"]
    marker_names = {line.split(":", 1)[0].strip() for line in marker_lines}
    badge_markers = {b.marker for b in badges.BADGES}
    assert badge_markers == marker_names


def test_badge_table_is_in_documented_gym_order() -> None:
    order = ["boulder", "cascade", "thunder", "rainbow", "soul", "marsh", "volcano", "earth"]
    assert [b.marker for b in badges.BADGES] == order


# --------------------------------------------------------------------------------------
# End-to-end against the REAL suite: boulder is built and passing right now; a marker no
# lane owns is genuinely unbuilt. This exercises the actual pytest subprocess path.
# --------------------------------------------------------------------------------------


def test_evaluate_real_boulder_is_earned() -> None:
    result = badges.evaluate(badges.BADGES[0])  # boulder
    assert result.badge.marker == "boulder"
    assert result.status is badges.Status.EARNED
    assert result.collected > 0
    assert result.failed == 0


def test_evaluate_real_unowned_marker_is_unbuilt() -> None:
    """A marker with no tests yet (e.g. earth, owned by an unlanded lane) is UNBUILT,
    not FAILED — this is the correct state for a half-built project."""
    earth = next(b for b in badges.BADGES if b.marker == "earth")
    result = badges.evaluate(earth)
    # If lane J has not landed, earth has no collected tests.
    if result.collected == 0:
        assert result.status is badges.Status.UNBUILT
    else:  # pragma: no cover - only once lane J lands
        assert result.status in (badges.Status.EARNED, badges.Status.FAILED)
