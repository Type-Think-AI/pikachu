#!/usr/bin/env python
"""Prove what ``import pikachu`` actually costs — and what it does *not* pull.

The claim we are proving (BUILD-PLAN-WAVE2.md, docs/23-framework-comparison.md): importing the
library's **types** should be cheap, and must **not** drag in the model framework. A user who
writes ``from pikachu import Skill`` should not pay ~298 ms for ``import pydantic_ai``.

Every measurement here runs in a **fresh subprocess**. That is not a nicety — an in-process
re-import is a dict lookup in ``sys.modules`` and reports ~0, which would make any lazy-loading
claim look true whether or not it is. Cold is the only honest number for a startup cost.

    .venv/bin/python scripts/startup_profile.py                # full report
    .venv/bin/python scripts/startup_profile.py -n 7           # best-of-7 (more samples)
    .venv/bin/python scripts/startup_profile.py --attribute-only

What it reports
---------------
1. **Cold wall-clock**, best-of-N, for ``import pikachu`` against ``import pydantic_ai`` — the
   before/after headline. "After" is the framework staying absent; "before" is what a naive
   ``__init__`` that imported a backend eagerly would have cost.
2. **Absence assertions**: after ``import pikachu``, is ``pydantic_ai`` in ``sys.modules``?
   Which ``pikachu.*`` submodules loaded? This is the profile-time echo of the test invariant.
3. **Per-module attribution** via ``python -X importtime``: the self-time of the heaviest
   imports on the bare-import path, so a regression points at a module rather than a mystery.

Best-of-N, not mean: import cost is dominated by disk and scheduler noise on a loaded machine,
and the *floor* is the reproducible quantity. A slow sample tells you the box was busy, not
that the code got slower.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

# Run child interpreters with src/ on the path, so this works from a plain checkout without an
# editable install having run. Mirrors the sys.path insert scripts/bench.py uses.
_CHILD_ENV_PATH = str(_SRC)


def _run_child(code: str, *, importtime: bool = False) -> subprocess.CompletedProcess[str]:
    """Execute ``code`` in a FRESH interpreter. importtime routes -X importtime to stderr."""
    args = [sys.executable]
    if importtime:
        args += ["-X", "importtime"]
    args += ["-c", code]
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": _CHILD_ENV_PATH, "PATH": _child_path()},
        cwd=str(_REPO),
    )


def _child_path() -> str:
    import os

    return os.environ.get("PATH", "")


def cold_import_ms(module: str, samples: int) -> float:
    """Best-of-N cold ``import <module>`` wall-clock in milliseconds, fresh proc each sample."""
    code = (
        "import time,importlib;"
        "t=time.perf_counter();"
        f"importlib.import_module({module!r});"
        "print((time.perf_counter()-t)*1000)"
    )
    best = float("inf")
    for _ in range(samples):
        proc = _run_child(code)
        if proc.returncode != 0:
            raise RuntimeError(f"cold import of {module!r} failed:\n{proc.stderr}")
        best = min(best, float(proc.stdout.strip()))
    return best


def loaded_modules_after_pikachu() -> tuple[bool, list[str]]:
    """In a fresh proc: is pydantic_ai present after ``import pikachu``, and which pikachu.*?"""
    code = (
        "import sys;"
        "before=set(sys.modules);"
        "import pikachu;"
        "new=set(sys.modules)-before;"
        "pa=any(m=='pydantic_ai' or m.startswith('pydantic_ai.') for m in new);"
        "subs=sorted(m for m in new if m=='pikachu' or m.startswith('pikachu.'));"
        "print(pa);"
        "print('\\n'.join(subs))"
    )
    proc = _run_child(code)
    if proc.returncode != 0:
        raise RuntimeError(f"import pikachu failed:\n{proc.stderr}")
    lines = proc.stdout.splitlines()
    pa = lines[0].strip() == "True"
    subs = [ln for ln in lines[1:] if ln.strip()]
    return pa, subs


def attribute_importtime(module: str, top: int) -> list[tuple[float, str]]:
    """Parse ``-X importtime`` for a cold ``import <module>``; return (self_ms, name) desc.

    ``-X importtime`` prints ``import time: self | cumulative | name`` to STDERR, microseconds.
    We rank by **self** time — cumulative double-counts a parent's children, so self is the
    honest per-module attribution.
    """
    proc = _run_child(f"import {module}", importtime=True)
    if proc.returncode != 0:
        raise RuntimeError(f"importtime run for {module!r} failed:\n{proc.stderr}")
    rows: list[tuple[float, str]] = []
    for line in proc.stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        body = line[len("import time:") :]
        parts = body.split("|")
        if len(parts) != 3:
            continue
        self_us = parts[0].strip()
        name = parts[2].strip()
        if self_us == "self":  # header row
            continue
        try:
            rows.append((float(self_us) / 1000.0, name))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows[:top]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-n", "--samples", type=int, default=5, help="cold samples, best-of-N (default 5)"
    )
    parser.add_argument(
        "--top", type=int, default=12, help="rows in the per-module attribution (default 12)"
    )
    parser.add_argument(
        "--attribute-only", action="store_true", help="only print the -X importtime attribution"
    )
    args = parser.parse_args()

    if not args.attribute_only:
        print("\nSTARTUP COST — cold, fresh subprocess per sample, best-of-%d" % args.samples)
        print("  Importing the TYPES should be cheap and must not pull the model framework.\n")

        pikachu_ms = cold_import_ms("pikachu", args.samples)
        pydantic_ai_ms = cold_import_ms("pydantic_ai", args.samples)

        print(f"  {'import target':28s} {'cold (best-of-N)':>18s}")
        print(f"  {'-' * 28} {'-' * 18}")
        print(f"  {'import pikachu':28s} {pikachu_ms:>15.1f} ms")
        print(f"  {'import pydantic_ai':28s} {pydantic_ai_ms:>15.1f} ms")
        print(f"  {'-' * 28} {'-' * 18}")
        ratio = pydantic_ai_ms / pikachu_ms if pikachu_ms else float("inf")
        saved = pydantic_ai_ms - pikachu_ms
        print(
            f"\n  import pikachu is {ratio:.1f}x cheaper than import pydantic_ai "
            f"({saved:.1f} ms not paid)."
        )
        print("  BEFORE: an __init__ that eagerly imported a backend would pay the framework")
        print("          cost on every cold start — pikachu cost >= pydantic_ai cost.")
        print("  AFTER:  the framework stays off the bare-import path; you pay it only when a")
        print("          turn constructs a PydanticAIBackend.\n")

        pa_present, subs = loaded_modules_after_pikachu()
        verdict = "ABSENT ✓" if not pa_present else "PRESENT ✗ (regression!)"
        print(f"  pydantic_ai after `import pikachu`:  {verdict}")
        print("  pikachu.* modules loaded by the bare import:")
        for m in subs:
            print(f"    {m}")
        if pa_present:
            print("\n  ✗ pydantic_ai IS on the bare-import path — a submodule import leaked into")
            print("    __init__. Fix before shipping; the win is gone until you do.")

    print("\nPER-MODULE ATTRIBUTION — cold `import pikachu`, -X importtime self-time")
    print("  Ranked by self time (cumulative double-counts children). Top %d.\n" % args.top)
    rows = attribute_importtime("pikachu", args.top)
    print(f"  {'self':>10s}  module")
    print(f"  {'-' * 10}  {'-' * 40}")
    for self_ms, name in rows:
        flag = "  ← framework (should be ABSENT)" if name.split(".")[0] == "pydantic_ai" else ""
        print(f"  {self_ms:>7.2f} ms  {name}{flag}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
