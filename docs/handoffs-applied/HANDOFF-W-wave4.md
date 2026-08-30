# HANDOFF-W — `pydantic-evals` dependency (F21)

`src/pikachu/evals/` wires in the declared eval library as a **tier-2 trend**, never a gate.
The runner and the deterministic case set run today with the library **absent** — absent
means SKIP, not fail. This handoff adds the library so the intended `pydantic-evals` harness
(`Case` / `Dataset` / `Evaluator` / `LLMJudge`, and the span-based evaluators
`docs/12-evaluation.md` calls the eval substrate) is available for a live trend run.

`pyproject.toml` is reserved. **Integrator: apply the exact change below.**

## Exact change

Add an **extra** to `[project.optional-dependencies]` in `pyproject.toml` (an extra, not a
core dependency — an agent that never runs evals must not install it, matching the wave-2
lazy-import rule and the `mcp` extra precedent in HANDOFF-I):

```toml
[project.optional-dependencies]
evals = [
    "pydantic-evals==1.20.4",
]
```

If an `evals` extra already exists, ensure it pins `pydantic-evals` exactly (no `^`/`~`/`>=`
range — the repo's stated policy is "pinned exactly, not ranged: a dependency changing its
distribution model with no warning is what motivated this project").

> The version above is a **placeholder to confirm before committing**. This lane could not
> reach the index (tests run with no network). Pick the exact version whose declared
> `requires-python` admits `>=3.13` and whose `pydantic` pin is compatible with the repo's
> `pydantic==2.13.5`, then pin that number. Confirm with:
>
> ```bash
> .venv/bin/python -m pip index versions pydantic-evals    # or check pypi.org/project/pydantic-evals
> .venv/bin/python -c "import pydantic_evals, pydantic; print(pydantic_evals.__version__, pydantic.__version__)"
> ```
>
> Distribution name is `pydantic-evals`; the import is `pydantic_evals` (underscore) — the
> code and the acceptance tests below key off the import name via `importlib.util.find_spec`.

## What works WITHOUT this dependency (already true today)

* `import pikachu.evals` — and `import pikachu` — do **not** pull `pydantic_evals`. The only
  reference is `pikachu.evals.runner.pydantic_evals_available()`, which calls
  `importlib.util.find_spec("pydantic_evals")` **inside the function** (lazy seam). Proven by
  `tests/test_evals.py::test_import_pikachu_does_not_pull_pydantic_evals`.
* The **five deterministic cases run and hold** with the library absent — they check live
  permission-layer surfaces (`guard.allowlist.effective_tools`, `FakeBackend`,
  `curator.lifecycle.promote_on_reuse`, `skills.confusability.check_new_skill`) against fakes,
  offline. Proven by `tests/test_evals.py::test_all_deterministic_cases_pass_on_correct_impl`.
* The **judge case skips cleanly** when no judge is wired (a skip, not a zero score). Proven by
  `tests/test_evals.py::test_judge_cases_skip_without_a_judge`.
* `scripts/badges.py` stays **8/8** and the eval layer registers **no** pytest badge marker —
  it is tier 2 and must never gate. Proven by
  `tests/test_evals.py::test_runner_source_references_no_badge_marker`.

## Acceptance tests — `xfail(strict=True)` that flip to required passes when applied

Add these to `tests/test_evals.py` (or a new `tests/test_evals_dep.py`). Each is
`xfail(strict=True)`: **before** the dependency is installed they xfail (skip-detected as
expected), and **after** it is installed they must PASS — a strict xfail that unexpectedly
passes is itself a failure, so this pattern catches "we said we added it but didn't". This is
the same pattern HANDOFF-I / HANDOFF-V use.

```python
import importlib.util
import pytest


def _pydantic_evals_installed() -> bool:
    return importlib.util.find_spec("pydantic_evals") is not None


@pytest.mark.xfail(
    not _pydantic_evals_installed(),
    reason="pydantic-evals extra not yet applied from HANDOFF-W",
    strict=True,
)
def test_pydantic_evals_extra_is_installed() -> None:
    # Flips from xfail to pass the moment the `evals` extra is installed.
    import pydantic_evals  # noqa: F401

    assert _pydantic_evals_installed() is True


@pytest.mark.xfail(
    not _pydantic_evals_installed(),
    reason="pydantic-evals extra not yet applied from HANDOFF-W",
    strict=True,
)
def test_runner_reports_harness_present_when_installed() -> None:
    # The runner's lazy seam must see the harness once it is installed.
    from pikachu.evals.runner import pydantic_evals_available

    assert pydantic_evals_available() is True


@pytest.mark.xfail(
    not _pydantic_evals_installed(),
    reason="pydantic-evals extra not yet applied from HANDOFF-W",
    strict=True,
)
def test_pydantic_evals_core_symbols_exist_when_installed() -> None:
    # The harness surface docs/12-evaluation.md names. If a future version renames these,
    # this strict-xfail fails loudly rather than the code silently drifting.
    from pydantic_evals import Case, Dataset  # noqa: F401
    from pydantic_evals.evaluators import LLMJudge  # noqa: F401
```

> Note: the strict-xfail marker is a *test-file* marker, not a gym badge marker — it does not
> put evals in the tier-1 gate. It only asserts the dependency wiring, and it lives in the
> test module, which carries no badge.

## Why this does not change the tier boundary

Installing the extra enables a **real judge** and the `pydantic-evals` harness for a live
trend run. It does **not** make evals gate: `pikachu.evals.runner.run_and_report` still
returns `0` unconditionally, still writes into the Pokédex (tier-2) surface, and still
registers no marker. The two-tier rule (`docs/12-evaluation.md`) holds with the dependency
present or absent — the dependency only decides whether the noisy judge cases produce a trend
point or skip.
