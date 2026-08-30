"""Tier-2 evaluation — F21, ``pydantic-evals`` wired in as a *trend*, never a gate.

★ THE ONE RULE (docs/12-evaluation.md, "two tiers, and only one of them gates")

    Badges are TIER 1: deterministic invariants, they FAIL THE BUILD (scripts/badges.py).
    Evals are TIER 2: scored signals, they are RECORDED AS A TREND and NEVER fail a build.

Conflating the two is the documented way eval suites become flaky and get disabled — "a
judge score that gates a merge will block a good change on a bad day." So everything in this
package is downstream of that rule:

* :mod:`pikachu.evals.runner` writes into the **Pokédex** surface (``scripts/report.py``,
  tier 2), never the badge case, and **exits 0 always** — a low score is information, not a
  failure.
* No module here registers a pytest marker. A badge marker is a tier-1 gate; an eval is not
  one, so it must never be able to sneak into the gate through a marker.

WHAT WE EVALUATE

Deterministic verifiers first (docs/12-evaluation.md — "hard invariants are true or false,
no score"): a skill body actually reaching the model, a denied tool being absent from what
the backend received, the guard narrowing an over-broad declaration, a tainted skill staying
unpromoted, a near-duplicate skill breaching the confusability threshold. Each of those has a
crisp pass/fail against a fake, so it is scored 1.0 or 0.0 with zero noise.

LLM-as-judge is used ONLY where nothing deterministic works, and every such case is flagged
``noisy=True`` in the code and rendered as a trend, never a verdict (docs/12-evaluation.md —
"never report a raw judge score as an accuracy figure"). With no judge wired in, those cases
SKIP rather than fail.

OPTIONAL DEPENDENCY

``pydantic-evals`` is the intended harness (``Case`` / ``Dataset`` / ``Evaluator``). It lives
in an **extra** (see ``HANDOFF-W.md``); this package must import and run its deterministic
case set with the library **absent** — absent means SKIP, not fail. :func:`pydantic_evals_available`
is the single seam that reports whether it is installed, imported lazily so importing this
package never pulls it in.
"""

from __future__ import annotations

from pikachu.evals.cases import (
    EvalCase,
    EvalKind,
    EvalScore,
    all_cases,
    deterministic_cases,
    judge_cases,
)
from pikachu.evals.runner import (
    EvalReport,
    pydantic_evals_available,
    render_report_json,
    run_and_report,
    run_cases,
)

__all__ = [
    "EvalCase",
    "EvalKind",
    "EvalReport",
    "EvalScore",
    "all_cases",
    "deterministic_cases",
    "judge_cases",
    "pydantic_evals_available",
    "render_report_json",
    "run_and_report",
    "run_cases",
]
