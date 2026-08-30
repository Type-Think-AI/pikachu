"""The eval runner — tier 2. Records a trend into the Pokédex surface, NEVER gates a build.

This is the deliberate counterpart to ``scripts/badges.py``. It runs the case set, collects
scores, and reports them as a **trend** — and it **exits 0 always**, because (docs/12-
evaluation.md) a trend that fails a build is a trend that gets disabled. A low score is
information, not a failure.

Three rules this module keeps, each traceable to the two-tier doc:

* **Results go to the Pokédex (tier 2), not the badge case.** The Pokédex
  (``scripts/report.py``) is the tier-2 surface whose defining property is that it never
  gates; the badge case is tier 1 and gates. This runner renders into a Pokédex-shaped
  report and carries the same "TIER 2 — TREND ONLY / never fails a build" banner, so a reader
  cannot mistake it for a gate.
* **Exit 0 always.** :func:`run_and_report` returns ``0`` unconditionally. There is no code
  path that exits non-zero on a low score — verified by a test.
* **No pytest marker.** Nothing here registers or references a badge marker. An eval is not a
  tier-1 invariant, so it must not be able to enter the gate through the marker table.

An empty filter is a first-class honest state: asking for a tag that matches no case prints an
"no cases matched" report rather than pretending everything passed.

``pydantic-evals`` is optional. It lives in an extra (``HANDOFF-W.md``); when it is absent the
deterministic case set still runs — absent means SKIP a labelled subset, not fail — and this
module still imports. :func:`pydantic_evals_available` is the single lazy seam that reports
whether it is installed; nothing at module scope imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pikachu.evals.cases import (
    EvalCase,
    EvalKind,
    EvalScore,
    Judge,
    all_cases,
)

__all__ = [
    "EvalReport",
    "pydantic_evals_available",
    "render_report_human",
    "render_report_json",
    "run_and_report",
    "run_cases",
]

# The Pokédex never gates. Stated as a constant so the human render, the JSON payload, and a
# reader grepping the source all find the guarantee in one place — matching scripts/report.py.
_NEVER_GATES = (
    "TIER 2 — TREND ONLY. Eval scores are recorded and tracked over time. They NEVER fail a "
    "build. The gate is the badge case (scripts/badges.py)."
)


def pydantic_evals_available() -> bool:
    """Whether the optional ``pydantic-evals`` harness is installed. Lazy, import-safe.

    Imported inside the function so that importing :mod:`pikachu.evals` never pulls the
    harness in — the same lazy-import discipline the ``mcp`` extra follows. Returns ``False``
    cleanly when the package is absent; the deterministic case set does not need it and runs
    either way.
    """
    import importlib.util

    return importlib.util.find_spec("pydantic_evals") is not None


@dataclass(frozen=True)
class EvalReport:
    """The rolled-up outcome of an eval run. A trend snapshot, never a gate result.

    Deterministic and judge scores are kept apart on purpose: ``deterministic_held`` /
    ``deterministic_total`` is a count of hard checks that held, while ``judge_scores`` is a
    list of noisy trend points that are averaged but never turned into a pass/fail. Skipped
    cases are their own count so "not measured" never reads as "scored zero".
    """

    scores: tuple[EvalScore, ...] = field(default_factory=tuple)
    pydantic_evals_present: bool = False

    @property
    def is_empty(self) -> bool:
        """True when no case matched the filter — an honest empty state, not a pass."""
        return not self.scores

    @property
    def deterministic_scores(self) -> tuple[EvalScore, ...]:
        return tuple(s for s in self.scores if s.kind is EvalKind.DETERMINISTIC and not s.skipped)

    @property
    def judge_scores(self) -> tuple[EvalScore, ...]:
        return tuple(s for s in self.scores if s.kind is EvalKind.JUDGE and not s.skipped)

    @property
    def skipped_scores(self) -> tuple[EvalScore, ...]:
        return tuple(s for s in self.scores if s.skipped)

    @property
    def deterministic_total(self) -> int:
        return len(self.deterministic_scores)

    @property
    def deterministic_held(self) -> int:
        return sum(1 for s in self.deterministic_scores if s.held)

    @property
    def judge_mean(self) -> float | None:
        """Mean of the noisy judge scores, or ``None`` when none ran. A trend, not a verdict."""
        js = self.judge_scores
        if not js:
            return None
        return sum(s.score for s in js) / len(js)


async def run_cases(
    cases: tuple[EvalCase, ...] | None = None,
    *,
    judge: Judge | None = None,
) -> EvalReport:
    """Run a case set offline and collect scores into an :class:`EvalReport`.

    ``cases`` defaults to the whole set (:func:`pikachu.evals.cases.all_cases`); pass a
    filtered tuple to run a subset. An empty tuple yields an empty report — a valid honest
    state, not an error. ``judge`` is threaded to judge cases; with it ``None`` those cases
    record a skipped score rather than failing.

    This never raises for a low score and never gates. It is pure measurement.
    """
    selected = all_cases() if cases is None else cases
    scores = tuple([await case.run(judge) for case in selected])
    return EvalReport(scores=scores, pydantic_evals_present=pydantic_evals_available())


# --------------------------------------------------------------------------------------
# Rendering — the Pokédex (tier-2) surface. Distinct banner so it is never a gate.
# --------------------------------------------------------------------------------------


def render_report_human(report: EvalReport) -> str:
    """A Pokédex-shaped, plain-text trend report. No colour, no gate language.

    Deliberately shaped and banner-matched to ``scripts/report.py`` so it reads as the same
    tier-2 surface: a "#NNN"-style dex of trend entries under a standing "never fails a build"
    banner. Deterministic held-counts and noisy judge means are shown separately, and the
    empty state is a first-class honest message rather than a wall of zeros.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("  ┌─ POKÉDEX · eval trend (F21) ─────────────────────────────┐")
    lines.append("  " + _NEVER_GATES)
    lines.append(
        "  pydantic-evals: "
        + ("installed" if report.pydantic_evals_present else "absent (deterministic set still runs; judge/harness cases skip)")
    )
    lines.append("")

    if report.is_empty:
        lines += [
            "  No cases matched.",
            "  A filter selected zero cases. This is an honest empty state — nothing was",
            "  measured, so nothing is reported. It is NOT a pass.",
            "",
            "  " + _NEVER_GATES,
            "",
        ]
        return "\n".join(lines)

    # #001 — deterministic hard checks (true/false, no noise)
    lines.append("  #001  DETERMINISTIC CHECKS (hard true/false — no noise)")
    lines.append(f"         {report.deterministic_held}/{report.deterministic_total} held")
    for s in report.deterministic_scores:
        mark = "held" if s.held else "BROKE"
        lines.append(f"           [{mark:>5}] {s.case_id:<28} {s.detail}")
    lines.append("")

    # #002 — noisy judge trend (never a verdict)
    lines.append("  #002  JUDGE SCORES (NOISY — trend only, never a verdict)")
    jm = report.judge_mean
    if jm is None:
        lines.append("         no judge scores in this run (no judge wired, or none selected)")
    else:
        lines.append(f"         mean {jm:.3f} over {len(report.judge_scores)} noisy case(s)")
        for s in report.judge_scores:
            lines.append(f"           [noisy] {s.case_id:<28} {s.score:.3f}  {s.detail}")
    lines.append("")

    # #003 — skipped (not measured — distinct from a zero score)
    skipped = report.skipped_scores
    if skipped:
        lines.append("  #003  SKIPPED (not measured — NOT a zero score)")
        for s in skipped:
            lines.append(f"           [ skip] {s.case_id:<28} {s.detail}")
        lines.append("")

    lines.append("  └──────────────────────────────────────────────────────────┘")
    lines.append("  " + _NEVER_GATES)
    lines.append("")
    return "\n".join(lines)


def render_report_json(report: EvalReport) -> str:
    """Machine-readable trend snapshot. ``gates_shipping`` is always ``False``."""
    import json

    payload = {
        "tier": 2,
        "gates_shipping": False,
        "reminder": _NEVER_GATES,
        "pydantic_evals_present": report.pydantic_evals_present,
        "empty": report.is_empty,
        "deterministic": {
            "held": report.deterministic_held,
            "total": report.deterministic_total,
            "cases": [
                {"case_id": s.case_id, "score": s.score, "held": s.held, "detail": s.detail}
                for s in report.deterministic_scores
            ],
        },
        "judge": {
            "noisy": True,
            "mean": report.judge_mean,
            "cases": [
                {"case_id": s.case_id, "score": s.score, "detail": s.detail}
                for s in report.judge_scores
            ],
        },
        "skipped": [
            {"case_id": s.case_id, "kind": s.kind.value, "detail": s.detail}
            for s in report.skipped_scores
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Entry point — always exits 0.
# --------------------------------------------------------------------------------------


async def run_and_report(
    argv: list[str] | None = None,
    *,
    judge: Judge | None = None,
) -> int:
    """Run the eval case set, print the Pokédex trend report, and RETURN 0 — always.

    A low score, a broken deterministic check, an empty filter, a missing harness — none of
    them change the exit code. Tier-2 evals never gate a build (docs/12-evaluation.md). The
    only thing this function's return value ever is, is ``0``.

    ``--json`` selects the machine-readable render; ``--tag`` / ``--kind`` filter the case set
    (an empty match prints an honest empty report). Filtering and rendering are the whole job;
    there is deliberately no ``--strict`` or threshold flag, because a threshold that fails a
    build is the exact tier-1/tier-2 conflation this module exists to avoid.
    """
    import argparse

    from pikachu.evals.cases import EvalKind as _EvalKind
    from pikachu.evals.cases import all_cases as _all_cases
    from pikachu.evals.cases import filter_cases as _filter_cases

    parser = argparse.ArgumentParser(description="Run tier-2 evals and print the Pokédex trend.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--tag", default=None, help="only cases carrying this tag")
    parser.add_argument(
        "--kind",
        default=None,
        choices=[k.value for k in _EvalKind],
        help="only cases of this kind",
    )
    args = parser.parse_args(argv)

    kind = _EvalKind(args.kind) if args.kind is not None else None
    selected = _filter_cases(_all_cases(), tag=args.tag, kind=kind)
    report = await run_cases(selected, judge=judge)

    if args.json:
        print(render_report_json(report))
    else:
        print(render_report_human(report))

    # Tier-2. NEVER gates. Always 0.
    return 0
