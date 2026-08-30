"""Lazy submodule loading — pay for a feature only when a turn actually touches it.

The measured problem (``docs/23-framework-comparison.md``): cold startup is ~236 ms on the
first turn and ~0 after, almost all of it ``import pydantic_ai`` at ~298 ms cold. In a
long-lived server that is paid once. In a **serverless / per-request** process it is paid on
every cold start. Separately, a user who only wants ``from pikachu import Skill`` should not
drag in a model framework to read a dataclass.

So the rule for the whole package (BUILD-PLAN-WAVE2.md): *import nothing at module scope that
a turn without that feature would not need.* This module supplies the mechanism that lets the
top-level ``pikachu`` namespace honour that rule without changing its public surface.

What this gives you
-------------------
`` install_lazy_submodules(module_globals, names) `` wires a **PEP 562** module-level
``__getattr__`` / ``__dir__`` onto a package so that ``pikachu.skills`` (etc.) imports the
real submodule on **first attribute access** and never at ``import pikachu`` time. The eager
symbols the package already exports — the 40 names in ``pikachu.__all__`` — keep resolving
directly and are untouched; only the *submodule* attributes become lazy.

Why a submodule ``__getattr__`` and not lazy top-level symbols
--------------------------------------------------------------
The 40 public symbols (``Agent``, ``Skill``, ``TrustTier``, …) all live in ``core.*``, which
is deliberately dependency-light (Pydantic + stdlib, no framework). Importing them eagerly is
already cheap — proven by ``scripts/startup_profile.py`` — so making *them* lazy would buy
nothing and would hurt ``from pikachu import Skill`` (a ``from`` import forces resolution of
every name anyway). The cost that matters is the **heavy submodules**: ``backends.pydantic_ai``
pulls the framework, ``skills`` pulls the scanner/embedder path, ``mcp`` pulls the protocol
SDK. Those are what must load on first use, and a submodule is exactly what
``module.__getattr__`` is designed to defer.

Tradeoffs, stated rather than hidden
------------------------------------
* **Static analysis / mypy.** A dynamic ``__getattr__`` returns ``Any``, which would erase
  types on ``pikachu.skills.load_skill``. The reserved ``__init__.py`` handoff (HANDOFF-K.md)
  therefore guards the *real* imports behind ``if TYPE_CHECKING:`` — mypy and IDEs see the
  genuine submodule types, the runtime sees only the lazy ``__getattr__``. ``mypy --strict``
  stays clean because the type checker never walks the runtime branch. The one honest gap:
  a tool that ignores ``TYPE_CHECKING`` (pure runtime introspection) sees ``Any`` for a
  submodule attribute. That is the price of not importing a model framework to read a type.
* **Autocomplete.** IDEs that honour ``TYPE_CHECKING`` (Pyright/Pylance, PyCharm) keep full
  completion on ``pikachu.skills.…``. The submodule names themselves are also advertised via
  ``__dir__`` so ``dir(pikachu)`` and REPL tab-completion list them.
* **Import errors surface late.** A broken submodule now raises on first *access*, not on
  ``import pikachu``. That is the intended behaviour — the whole point is not to pay for it —
  but it means a typo in ``skills/`` is not caught by ``import pikachu`` alone. The test suite
  (``tests/test_lazy_loading.py``) touches each lazy target to keep that honest.
* **A missing sibling lane is not an error here.** ``canvas`` / ``telemetry`` may not exist
  yet (other wave-2 lanes). Declaring them lazy is free — the ``ImportError`` only fires if
  someone actually accesses ``pikachu.canvas`` before that lane lands, with a clear message.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable

__all__ = [
    "LAZY_SUBMODULES",
    "install_lazy_submodules",
    "make_lazy_getattr",
]

# The submodules that must NOT be imported at ``import pikachu`` time. Each loads on first
# access via ``pikachu.<name>``. Order is irrelevant; membership is what matters.
#
# ``backends`` is listed (not ``backends.pydantic_ai``) because ``backends/__init__`` only
# pulls ``base`` + ``fake`` — both framework-free — so it is cheap; the framework lives in
# ``backends.pydantic_ai`` which ``backends/__init__`` does NOT import. Listing ``backends``
# here keeps even that cheap subpackage off the bare-import path, and ``pydantic_ai`` stays
# behind an explicit ``from pikachu.backends.pydantic_ai import PydanticAIBackend``.
LAZY_SUBMODULES: tuple[str, ...] = (
    "a2a",
    "authz",
    "backends",
    "billing",
    "canvas",
    "config",
    "curator",
    "discovery",
    "durability",
    "guard",
    "mcp",
    "memory",
    "plugins",
    "skills",
    "storage",
    "telemetry",
    "webmcp",
)
"""Deferred submodule/attribute names for the top-level ``pikachu`` package.

Every name here is loaded lazily. ``core`` is deliberately absent: it holds the 40 public
types and is imported eagerly by ``__init__`` (cheap, and ``from pikachu import Skill`` needs
it resolved anyway).

**Kept alphabetical, and it MUST match the ``TYPE_CHECKING`` re-export block in
``pikachu/__init__.py``.** Those two lists are a guarantee split across two files, and the
audit (``docs/24-audit.md`` defect 1) caught them disagreeing: ``memory`` was re-exported for
type checkers while the runtime never wired it, so ``pikachu.memory`` type-checked and then
raised ``AttributeError``. A guarantee that holds in one place and not another is not a
guarantee — ``tests/test_regressions.py`` now asserts the two agree.
"""


def make_lazy_getattr(
    package: str,
    module_globals: dict[str, object],
    names: tuple[str, ...],
) -> Callable[[str], ModuleType]:
    """Build a PEP 562 ``__getattr__`` that imports ``package.<name>`` on first access.

    The returned function is what a package assigns to its module-level ``__getattr__``.
    On a hit it imports the submodule, caches it back into ``module_globals`` so subsequent
    accesses skip ``__getattr__`` entirely (Python only calls ``__getattr__`` on a miss), and
    returns it. On a miss it raises ``AttributeError`` with the standard message so
    ``hasattr`` and normal attribute semantics still behave.

    ``package`` is the importing package's ``__name__`` (e.g. ``"pikachu"``); ``names`` is the
    allow-list of lazily-importable child names.
    """
    lazy = frozenset(names)

    def __getattr__(name: str) -> ModuleType:
        if name in lazy:
            module = importlib.import_module(f"{package}.{name}")
            # Cache on the package so the next access is a plain global lookup, not a call.
            module_globals[name] = module
            return module
        raise AttributeError(f"module {package!r} has no attribute {name!r}")

    return __getattr__


def install_lazy_submodules(
    module_globals: dict[str, object],
    names: tuple[str, ...] = LAZY_SUBMODULES,
) -> None:
    """Wire lazy ``__getattr__`` and a completion-friendly ``__dir__`` onto a package.

    Call this once from a package's ``__init__`` **after** its eager symbols are defined::

        from pikachu import _lazy
        _lazy.install_lazy_submodules(globals())

    It sets ``module_globals['__getattr__']`` (PEP 562 submodule loader) and extends
    ``module_globals['__dir__']`` so ``dir(pikachu)`` and REPL tab-completion advertise the
    lazy submodule names alongside the eager ones — without importing them.

    Idempotent-friendly: names already present as real attributes (an eager import, or a
    submodule already accessed and cached) are simply shadowed by the normal attribute
    lookup, because Python only consults ``__getattr__`` when normal lookup misses.
    """
    package = module_globals.get("__name__")
    if not isinstance(package, str):  # pragma: no cover - a package always has __name__
        raise TypeError("install_lazy_submodules requires a real module globals() dict")

    module_globals["__getattr__"] = make_lazy_getattr(package, module_globals, names)

    # __dir__ so tooling/REPL can see the lazy names without triggering an import.
    existing_all = module_globals.get("__all__", ())
    static_names = tuple(existing_all) if isinstance(existing_all, (list, tuple)) else ()

    def __dir__() -> list[str]:
        return sorted(set(static_names) | set(names) | set(module_globals))

    module_globals["__dir__"] = __dir__
