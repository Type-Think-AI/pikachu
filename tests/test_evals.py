"""Tests for the tier-2 eval layer (F21). Offline, FakeBackend only, no network.

The load-bearing rules being proven, straight from the lane spec and docs/12-evaluation.md:

  * Every deterministic case passes against the correct implementation AND fails against a
    deliberately broken stub — a case that cannot fail is not a test. Each broken-stub test
    monkeypatches the ONE surface the case checks so the case scores 0.0.
  * The runner exits 0 even when cases score badly. Tier-2 evals never gate.
  * The runner registers NO pytest badge marker — asserted by introspecting the marker table
    and the runner's own source, so an eval can never sneak into the tier-1 gate.
  * The optional ``pydantic-evals`` library absent -> judge cases skip cleanly and the module
    still imports.

These tests are intentionally NOT marked with any gym badge. Evals are tier 2; giving them a
badge marker would make them a gate, which is the exact conflation the layer exists to avoid.
"""

from __future__ import annotations

import pytest

from pikachu.evals import cases as cases_mod
from pikachu.evals import runner as runner_mod
from pikachu.evals.cases import (
    EvalKind,
    all_cases,
    deterministic_cases,
    filter_cases,
    judge_cases,
)
from pikachu.evals.runner import (
    pydantic_evals_available,
    render_report_human,
    render_report_json,
    run_and_report,
    run_cases,
)


# --------------------------------------------------------------------------------------
# Import safety — the module imports with or without the optional harness.
# --------------------------------------------------------------------------------------


def test_package_imports_without_pydantic_evals() -> None:
    # If this test module imported at all, pikachu.evals imported. Confirm the seam does not
    # require the harness and does not raise regardless of its presence.
    import pikachu.evals  # noqa: F401

    assert isinstance(pydantic_evals_available(), bool)


def test_import_pikachu_does_not_pull_pydantic_evals() -> None:
    # pydantic_evals_available must be a lazy check, not a module-scope import — importing the
    # package must not have imported the harness. If the harness is genuinely installed this
    # is vacuous, so it only asserts absence when absent.
    import sys

    if pydantic_evals_available():
        pytest.skip("pydantic-evals is installed; absence assertion is vacuous")
    assert "pydantic_evals" not in sys.modules


# --------------------------------------------------------------------------------------
# Each deterministic case: PASSES on the correct implementation.
# --------------------------------------------------------------------------------------


async def test_all_deterministic_cases_pass_on_correct_impl() -> None:
    report = await run_cases(deterministic_cases())
    assert report.deterministic_total == 5
    assert report.deterministic_held == 5, [
        (s.case_id, s.score, s.detail) for s in report.deterministic_scores if not s.held
    ]


@pytest.mark.parametrize("case", deterministic_cases(), ids=lambda c: c.case_id)
async def test_each_deterministic_case_scores_one_on_correct_impl(case: object) -> None:
    score = await case.run()  # type: ignore[attr-defined]
    assert score.score == 1.0, f"{score.case_id}: {score.detail}"
    assert score.held is True
    assert score.noisy is False


# --------------------------------------------------------------------------------------
# Each deterministic case: FAILS against a deliberately broken stub.
# A case that cannot fail is not a test — so we break the one surface it checks.
# --------------------------------------------------------------------------------------


async def _score_one(case_id: str) -> float:
    (case,) = (c for c in deterministic_cases() if c.case_id == case_id)
    return (await case.run()).score


async def test_skill_body_case_fails_when_body_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break the backend so it records a request whose skill is stripped of its body.
    from pikachu.backends import fake as fake_mod

    orig = fake_mod.FakeBackend.run_turn

    async def _drop_body(self: object, request: object):  # type: ignore[no-untyped-def]
        stripped = request.model_copy(update={"skill": None})  # type: ignore[attr-defined]
        return await orig(self, stripped)  # type: ignore[arg-type]

    monkeypatch.setattr(fake_mod.FakeBackend, "run_turn", _drop_body)
    assert await _score_one("skill-body-reaches-model") == 0.0


async def test_denied_tool_case_fails_when_backend_widens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break authority reading so the denied tool appears in the backend's authorized set.
    from pikachu.backends import base as base_mod

    def _widen(request: object) -> tuple[str, ...]:
        return (*request.effective_tools, "read_file")  # type: ignore[attr-defined]

    monkeypatch.setattr(base_mod.BaseBackend, "authorized_tools", staticmethod(_widen))
    assert await _score_one("denied-tool-absent") == 0.0


async def test_guard_case_fails_when_narrowing_is_stubbed_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break the guard so it keeps everything declared (an over-broad grant leaks through).
    from pikachu.guard import allowlist as allow_mod

    def _keep_all(fixed: object, declared: object):  # type: ignore[no-untyped-def]
        return allow_mod.EffectiveToolset(tools=tuple(declared or ()))

    monkeypatch.setattr(allow_mod, "effective_tools", _keep_all)
    assert await _score_one("guard-narrows-over-broad") == 0.0


async def test_tainted_case_fails_when_promotion_gate_is_stubbed_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break the curator so a tainted draft promotes to candidate without raising.
    from pikachu.curator import lifecycle as life_mod
    from pikachu.core.types import SkillStatus

    def _promote_anyway(skill: object):  # type: ignore[no-untyped-def]
        return skill.model_copy(update={"status": SkillStatus.CANDIDATE})  # type: ignore[attr-defined]

    monkeypatch.setattr(life_mod, "promote_on_reuse", _promote_anyway)
    assert await _score_one("tainted-stays-unpromoted") == 0.0


async def test_near_duplicate_case_fails_when_check_never_breaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break confusability so it never reports a breach even on an identical description.
    from pikachu.skills import confusability as conf_mod

    async def _never_breach(new_description: str, existing: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return conf_mod.ConfusabilityReport(
            new_description=new_description,
            partition=kwargs.get("partition"),  # type: ignore[arg-type]
            threshold=float(kwargs.get("threshold", 0.85)),  # type: ignore[arg-type]
            nearest_description=None,
            nearest_score=0.0,
            breaches_threshold=False,
        )

    monkeypatch.setattr(conf_mod, "check_new_skill", _never_breach)
    assert await _score_one("near-duplicate-refused") == 0.0


# --------------------------------------------------------------------------------------
# The runner NEVER gates: exit 0 even when cases score badly.
# --------------------------------------------------------------------------------------


async def test_runner_exits_zero_on_clean_run() -> None:
    assert await run_and_report([]) == 0


async def test_runner_exits_zero_even_when_every_case_scores_badly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Force every deterministic verifier to score 0.0 by breaking the guard surface several
    # cases depend on, then confirm the exit code is STILL 0. A low score is information.
    from pikachu.guard import allowlist as allow_mod

    def _keep_all(fixed: object, declared: object):  # type: ignore[no-untyped-def]
        return allow_mod.EffectiveToolset(tools=tuple(declared or ()))

    monkeypatch.setattr(allow_mod, "effective_tools", _keep_all)

    code = await run_and_report([])
    assert code == 0
    out = capsys.readouterr().out
    assert "never fail a build" in out.lower() or "trend only" in out.lower()


async def test_runner_json_reports_gates_shipping_false() -> None:
    import json

    report = await run_cases()
    payload = json.loads(render_report_json(report))
    assert payload["tier"] == 2
    assert payload["gates_shipping"] is False


async def test_runner_empty_filter_is_honest_empty_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await run_and_report(["--tag", "no-such-tag"])
    assert code == 0
    out = capsys.readouterr().out
    assert "no cases matched" in out.lower()
    # An empty match must NOT read as a pass.
    assert "5/5 held" not in out


async def test_empty_filter_report_is_empty_not_passing() -> None:
    report = await run_cases(filter_cases(all_cases(), tag="no-such-tag"))
    assert report.is_empty is True
    assert report.deterministic_total == 0


# --------------------------------------------------------------------------------------
# The runner registers NO pytest badge marker.
# --------------------------------------------------------------------------------------

_BADGE_MARKERS = frozenset(
    {"boulder", "cascade", "thunder", "rainbow", "soul", "marsh", "volcano", "earth"}
)


def test_runner_source_references_no_badge_marker() -> None:
    import inspect

    src = inspect.getsource(runner_mod) + inspect.getsource(cases_mod)
    # Neither the marker names nor a pytest.mark reference may appear in the eval layer.
    for marker in _BADGE_MARKERS:
        assert f"pytest.mark.{marker}" not in src, f"eval layer references badge marker {marker!r}"
    assert "pytest.mark" not in src, "eval layer must register no pytest marker at all"


def test_eval_test_module_carries_no_badge_marker() -> None:
    # This very module must not be collected under any badge. If it were, an eval failure
    # would gate. Assert none of our own test functions carry a badge mark.
    import inspect

    src = inspect.getsource(__import__(__name__, fromlist=["_"]))
    for marker in _BADGE_MARKERS:
        assert f"pytest.mark.{marker}" not in src


# --------------------------------------------------------------------------------------
# Judge cases: absent judge -> skip cleanly (not a zero score).
# --------------------------------------------------------------------------------------


async def test_judge_cases_skip_without_a_judge() -> None:
    report = await run_cases(judge_cases(), judge=None)
    assert len(report.skipped_scores) == len(judge_cases())
    assert report.judge_mean is None  # nothing measured
    for s in report.skipped_scores:
        assert s.skipped is True
        assert s.kind is EvalKind.JUDGE


async def test_judge_case_runs_with_a_scripted_judge() -> None:
    class _ScriptedJudge:
        async def score(self, *, answer: str, rubric: str) -> float:
            return 0.73  # deterministic — a scripted stub, never a real model

    report = await run_cases(judge_cases(), judge=_ScriptedJudge())
    assert report.judge_mean == pytest.approx(0.73)
    assert not report.skipped_scores
    # Still noisy, still a trend — never promoted to a hard held/failed.
    for s in report.judge_scores:
        assert s.noisy is True
        assert s.held is False  # a judge score is never a hard "held"


async def test_skipped_is_distinct_from_a_zero_score() -> None:
    # A skipped judge case must not be counted among judge scores (which would drag a mean
    # toward zero and misread "not measured" as "scored badly").
    report = await run_cases(judge_cases(), judge=None)
    assert report.judge_scores == ()
    assert report.skipped_scores != ()


# --------------------------------------------------------------------------------------
# Absent-library behaviour, simulated by forcing the seam to report absence.
# --------------------------------------------------------------------------------------


async def test_absent_library_still_runs_deterministic_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate pydantic-evals absent: the deterministic set must still run and hold, and the
    # report must record the harness as not present.
    monkeypatch.setattr(runner_mod, "pydantic_evals_available", lambda: False)
    report = await run_cases(deterministic_cases())
    assert report.pydantic_evals_present is False
    assert report.deterministic_held == report.deterministic_total == 5


def test_human_render_names_the_harness_state() -> None:
    from pikachu.evals.runner import EvalReport

    absent = render_report_human(EvalReport(scores=(), pydantic_evals_present=False))
    assert "absent" in absent.lower()
