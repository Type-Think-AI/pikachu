"""Lazy-loading invariants — the deliverable of Lane K.

A lazy-loading *claim* is worthless; a lazy-loading *test* is the thing that stops the claim
rotting. The moment someone adds ``from pikachu.backends.pydantic_ai import PydanticAIBackend``
to ``__init__.py`` for convenience, the framework lands back on the bare-import path and every
serverless cold start pays ~200 ms again — silently, because nothing errors. These tests fail
that commit instead.

Why subprocesses
----------------
The central assertion — "``pydantic_ai`` is not in ``sys.modules`` after ``import pikachu``" —
is only meaningful in a **fresh interpreter**. Inside the pytest process, ``pydantic_ai`` may
already be imported by another test, and an in-process re-``import pikachu`` is a ``sys.modules``
dict hit that measures ~0 and imports nothing new. So the cold assertions shell out to a clean
``python -c`` with ``src/`` on the path, exactly as a serverless cold start would run.

The ``conftest`` autouse ``_no_network`` fixture guards the *pytest* process; the child
interpreters do not inherit it, which is fine — nothing here touches the network, and asserting
that is part of the point (importing types must not open a socket either).

xfail until the integrator applies HANDOFF-K.md
-----------------------------------------------
Making ``pikachu.skills`` resolve *lazily as an attribute* requires a PEP 562 ``__getattr__`` in
``src/pikachu/__init__.py`` — a RESERVED file this lane must not write. The mechanism lives in
``pikachu._lazy`` and is proven here directly; the tests that need it wired into ``__init__``
are marked ``xfail(strict=True)`` and name HANDOFF-K.md. ``strict`` means they flip to a hard
failure the instant the handoff is applied, so this file becomes the acceptance test for that
integration rather than a permanently-yellow suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pikachu

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

# Every heavy submodule that must stay off the bare-import path, mirroring
# pikachu._lazy.LAZY_SUBMODULES (minus the always-cheap ones). pydantic_ai is the headline.
_MUST_STAY_ABSENT = ("pydantic_ai", "griffe")

# A generous ceiling for cold `import pikachu`. The floor measures ~55 ms here; the framework
# alone is ~217 ms. 150 ms sits comfortably above our floor and comfortably below "the
# framework leaked in", so it catches a real regression (a submodule import creeping into
# __init__) without flaking when the machine is loaded. It is a REGRESSION gate, not a
# micro-benchmark — deliberately not tight.
_IMPORT_BUDGET_MS = 150.0


def _run_child(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with src/ on the path. No inherited imports."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(_SRC), "PATH": os.environ.get("PATH", "")},
        cwd=str(_REPO),
    )


def _modules_after_import_pikachu() -> set[str]:
    """The set of module names present after a cold ``import pikachu``, from a fresh proc."""
    proc = _run_child(
        "import sys; import pikachu; "
        "print('\\n'.join(sorted(m for m in sys.modules)))"
    )
    assert proc.returncode == 0, f"cold import pikachu failed:\n{proc.stderr}"
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


# ======================================================================================
# THE headline invariant — passes now, must never regress
# ======================================================================================


def test_import_pikachu_does_not_pull_pydantic_ai() -> None:
    """After a cold ``import pikachu``, the model framework is NOT loaded.

    This is the whole deliverable. If it fails, a submodule import leaked into __init__ and
    every cold start pays the framework cost again.
    """
    loaded = _modules_after_import_pikachu()
    leaked = sorted(
        m
        for m in loaded
        for heavy in _MUST_STAY_ABSENT
        if m == heavy or m.startswith(f"{heavy}.")
    )
    assert not leaked, (
        f"import pikachu pulled framework modules that must load lazily: {leaked}. "
        "A `from pikachu.backends.pydantic_ai import ...` (or similar) crept into "
        "src/pikachu/__init__.py — move it behind lazy access or TYPE_CHECKING."
    )


def test_import_pikachu_loads_only_core_submodules() -> None:
    """The bare import pulls ``core.*`` and nothing heavier — skills/mcp/backends stay out."""
    loaded = _modules_after_import_pikachu()
    pikachu_subs = {m for m in loaded if m == "pikachu" or m.startswith("pikachu.")}
    heavy_subs = {
        m
        for m in pikachu_subs
        for feat in ("pikachu.skills", "pikachu.mcp", "pikachu.backends", "pikachu.storage")
        if m == feat or m.startswith(f"{feat}.")
    }
    assert not heavy_subs, (
        f"import pikachu eagerly loaded feature submodules that should be lazy: "
        f"{sorted(heavy_subs)}"
    )


def test_import_pikachu_opens_no_socket() -> None:
    """Importing the types must not touch the network. Proven in a fresh proc.

    A framework import can transitively open a connection (telemetry, config fetch). If the
    bare import stays framework-free, it also stays socket-free — assert it directly.
    """
    proc = _run_child(
        "import socket\n"
        "_orig = socket.socket.connect\n"
        "def _boom(self, *a, **k):\n"
        "    raise AssertionError('import pikachu opened a socket')\n"
        "socket.socket.connect = _boom\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('import pikachu opened a socket'))\n"
        "import pikachu\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, f"import pikachu touched the network:\n{proc.stderr}"
    assert proc.stdout.strip() == "ok"


# ======================================================================================
# Public API is unchanged — all 40 exported symbols still reachable
# ======================================================================================


def test_all_public_symbols_reachable() -> None:
    """Every name in ``pikachu.__all__`` resolves after import. The API must not change."""
    missing = [name for name in pikachu.__all__ if not hasattr(pikachu, name)]
    assert not missing, f"public symbols vanished from pikachu.__all__: {missing}"


def test_public_symbol_count_is_stable() -> None:
    """Guard the exported-symbol count so a lazy refactor cannot silently drop one

    (or quietly widen the surface). ``__all__`` holds 39 named symbols + ``__version__`` = 40.
    """
    assert len(pikachu.__all__) == len(set(pikachu.__all__)), "duplicate names in __all__"
    assert "__version__" in pikachu.__all__
    assert len(pikachu.__all__) == 40, (
        f"expected 39 symbols + __version__ = 40 in __all__, got {len(pikachu.__all__)}. "
        "If the surface changed on purpose, update this count deliberately."
    )


def test_from_import_still_works_in_fresh_proc() -> None:
    """``from pikachu import Agent, Skill, TrustTier`` works cold and still pulls no framework.

    Uses AgentSpec/Skill/TrustTier — the names BUILD-PLAN calls out as the canonical import —
    and re-checks framework absence, because a ``from`` import forces name resolution and is
    the most likely place an eager backend import would bite.
    """
    proc = _run_child(
        "import sys\n"
        "from pikachu import AgentSpec, Skill, TrustTier\n"
        "assert AgentSpec.__name__ == 'AgentSpec'\n"
        "assert Skill.__name__ == 'Skill'\n"
        "assert TrustTier.BUILTIN.value == 'builtin'\n"
        "assert 'pydantic_ai' not in sys.modules, 'from-import pulled pydantic_ai'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ======================================================================================
# Touching a feature pulls exactly what it needs, and nothing more
# ======================================================================================


def test_using_skills_does_not_pull_mcp_or_framework() -> None:
    """Importing the skills feature loads skills/ but not mcp/ and not the framework.

    ``skills`` is a real feature that does not depend on either the MCP client or the model
    framework, so exercising it must not drag them in. This is the "exactly what it needs"
    half of the deliverable.
    """
    proc = _run_child(
        "import sys\n"
        "from pikachu.skills import scan, load_skill\n"
        "scan('# hello\\n\\njust a heading, no injection')\n"
        "assert 'pikachu.skills' in sys.modules\n"
        "assert 'pydantic_ai' not in sys.modules, 'skills pulled the framework'\n"
        "assert 'pikachu.mcp' not in sys.modules, 'skills pulled the mcp client'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_using_guard_does_not_pull_framework() -> None:
    """The permission engine is framework-free — using it must not import pydantic_ai."""
    proc = _run_child(
        "import sys\n"
        "from pikachu.guard import effective_tools\n"
        "result = effective_tools(('web_search',), ('web_search', 'bash'))\n"
        "assert result.tools == ('web_search',), result\n"
        "assert 'pydantic_ai' not in sys.modules, 'guard pulled the framework'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_pydantic_ai_backend_is_where_the_framework_lives() -> None:
    """Constructing the real backend DOES pull the framework — that is the one place it should.

    The negative tests above only mean something if the framework is genuinely reachable when
    a turn asks for it. This is the positive control: import the backend, confirm the framework
    arrives. (No backend is instantiated and no model is called — import alone.)
    """
    proc = _run_child(
        "import sys\n"
        "from pikachu.backends.pydantic_ai import PydanticAIBackend\n"
        "assert 'pydantic_ai' in sys.modules, 'the backend did not pull its framework'\n"
        "assert PydanticAIBackend.__name__ == 'PydanticAIBackend'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ======================================================================================
# Cold import stays under budget
# ======================================================================================


def test_cold_import_pikachu_under_budget() -> None:
    """Cold ``import pikachu`` stays under the regression budget, measured best-of-N cold.

    Best-of-N because the floor is the reproducible quantity; a single slow sample means the
    box was busy, not that the code got slower. The budget is loose on purpose (see
    ``_IMPORT_BUDGET_MS``) — it fires when a submodule import leaks into __init__, not when the
    scheduler hiccups.
    """
    code = (
        "import time, importlib\n"
        "t = time.perf_counter()\n"
        "importlib.import_module('pikachu')\n"
        "print((time.perf_counter() - t) * 1000)\n"
    )
    best = float("inf")
    for _ in range(5):
        proc = _run_child(code)
        assert proc.returncode == 0, proc.stderr
        best = min(best, float(proc.stdout.strip()))
    assert best < _IMPORT_BUDGET_MS, (
        f"cold import pikachu took {best:.1f} ms (best of 5), over the {_IMPORT_BUDGET_MS} ms "
        "regression budget. Something heavy landed on the bare-import path — run "
        "`scripts/startup_profile.py` to see which module."
    )
    # Guard against a wall-clock timer that reports zero (a broken measurement passing vacuously).
    assert best > 0.0


# ======================================================================================
# The _lazy mechanism itself — unit-tested without depending on __init__ wiring
# ======================================================================================


def test_lazy_getattr_defers_and_caches() -> None:
    """``make_lazy_getattr`` imports on first access, caches back, and misses raise cleanly."""
    from pikachu import _lazy

    g: dict[str, object] = {"__name__": "pikachu"}
    getter = _lazy.make_lazy_getattr("pikachu", g, ("guard",))

    assert "guard" not in g
    module = getter("guard")
    assert module.__name__ == "pikachu.guard"
    assert g["guard"] is module, "resolved submodule must be cached back into globals"

    with pytest.raises(AttributeError, match="has no attribute 'not_a_submodule'"):
        getter("not_a_submodule")


def test_install_lazy_submodules_wires_getattr_and_dir() -> None:
    """``install_lazy_submodules`` sets __getattr__ and advertises names via __dir__."""
    from pikachu import _lazy

    g: dict[str, object] = {"__name__": "pikachu", "__all__": ["Skill"]}
    _lazy.install_lazy_submodules(g, names=("skills", "guard"))

    assert callable(g["__getattr__"])
    assert callable(g["__dir__"])
    dir_fn = g["__dir__"]
    getattr_fn = g["__getattr__"]
    assert callable(dir_fn) and callable(getattr_fn)
    listed = dir_fn()
    assert "skills" in listed and "guard" in listed and "Skill" in listed

    got = getattr_fn("guard")
    assert got.__name__ == "pikachu.guard"


def test_lazy_submodules_list_matches_real_package() -> None:
    """Every name in LAZY_SUBMODULES is an importable child of pikachu (or a not-yet lane).

    canvas/telemetry may not exist yet — declaring them lazy is free and only errors on access.
    Names that DO exist must import cleanly, so a typo in the list is caught.
    """
    import importlib

    from pikachu import _lazy

    pending: list[str] = []
    for name in _lazy.LAZY_SUBMODULES:
        try:
            mod = importlib.import_module(f"pikachu.{name}")
            assert mod.__name__ == f"pikachu.{name}"
        except ModuleNotFoundError:
            pending.append(name)
    # Only the known not-yet-built lanes are allowed to be missing.
    assert set(pending) <= {"canvas", "telemetry"}, (
        f"LAZY_SUBMODULES names a submodule that does not exist and is not a known pending "
        f"lane: {pending}"
    )


# ======================================================================================
# Lazy ATTRIBUTE access on the top-level package — needs the reserved __init__.py change.
# xfail(strict) until HANDOFF-K.md is applied; flips to a hard pass-requirement then.
# ======================================================================================


# HANDOFF-K.md has been APPLIED (PEP 562 loader installed in __init__.py), so this is a
# required pass rather than an expected failure.
def test_lazy_attribute_access_resolves_submodule() -> None:
    """``pikachu.skills`` resolves as an attribute without a prior explicit import.

    Fails today because __init__ has no __getattr__. Passes once the integrator applies the
    HANDOFF-K.md __init__.py, at which point strict xfail turns this into a required pass.
    """
    proc = _run_child(
        "import pikachu\n"
        "loader = pikachu.skills.load_skill\n"
        "assert callable(loader)\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# HANDOFF-K.md has been APPLIED (PEP 562 loader installed in __init__.py), so this is a
# required pass rather than an expected failure.
def test_lazy_attribute_access_still_defers_framework() -> None:
    """Accessing ``pikachu.skills`` lazily still does not pull the framework.

    Guards against the wiring being done in a way that eagerly imports every lazy target.
    """
    proc = _run_child(
        "import sys, pikachu\n"
        "_ = pikachu.skills\n"
        "assert 'pikachu.skills' in sys.modules\n"
        "assert 'pydantic_ai' not in sys.modules, 'lazy attr access pulled the framework'\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# NOTE on dir(pikachu): the ``__dir__`` that HANDOFF-K.md installs is unit-tested directly in
# ``test_install_lazy_submodules_wires_getattr_and_dir``. It is deliberately NOT asserted on the
# live ``pikachu`` package here, because Python's default ``dir()`` already surfaces any
# ``pikachu.<sub>`` that another test has imported into ``sys.modules`` — so the outcome would
# depend on test ordering rather than on the wiring, making it flaky either way.
