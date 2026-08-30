#!/usr/bin/env python
"""The badge case runner — the report the project owner actually reads.

Each gym badge is a pytest marker (defined once in ``pyproject.toml``). This runs pytest
per marker, collects pass/fail/skip/notimplemented, and prints a legible badge case in gym
order. It is the tier-1 gate: a badge with collected tests that fail exits non-zero.

Two states that mean completely different things are kept apart, because conflating them
makes the report useless while the project is half-built:

  * NOT YET BUILT — no tests are collected for that marker. The lane hasn't landed. This
    is expected during the build and does NOT gate.
  * FAILED — tests ran and at least one failed. This gates.

Usage:
    python scripts/badges.py            # human-readable badge case
    python scripts/badges.py --json     # machine-readable, one JSON object

No network. No colour codes unless stdout is a real TTY (guarded by isatty()).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# --------------------------------------------------------------------------------------
# The badge table. Order is gym order — badges are earned in sequence and gate shipping.
# Kept in sync with pyproject.toml [tool.pytest.ini_options] markers and BUILD-PLAN.md.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Badge:
    marker: str
    name: str
    gym: str  # "Trainer, Type"
    proves: str
    lane: str


BADGES: tuple[Badge, ...] = (
    Badge("boulder", "Boulder", "Brock, Rock", "types are solid — models validate, mypy strict", "C"),
    Badge("cascade", "Cascade", "Misty, Water", "contracts flow — every Protocol round-trips through its fake", "C"),
    Badge("thunder", "Thunder", "Lt. Surge, Electric", "the guard holds — effective ⊆ allowlist ∩ declared", "B"),
    Badge("rainbow", "Rainbow", "Erika, Grass", "skills load safely — SKILL.md parses, scanner rejects injection", "A/D"),
    Badge("soul", "Soul", "Koga, Poison", "taint propagates — lineage tracking, tainted never promotes", "G"),
    Badge("marsh", "Marsh", "Sabrina, Psychic", "memory cannot lie — memory never widens authority", "G"),
    Badge("volcano", "Volcano", "Blaine, Fire", "it actually runs — full turn end-to-end on FakeBackend", "F"),
    Badge("earth", "Earth", "Giovanni, Ground", "grounded in measurement — cache, token and latency budgets", "J"),
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


class Status(str, Enum):
    EARNED = "earned"
    FAILED = "failed"
    UNBUILT = "unbuilt"  # NOT YET BUILT — no tests collected for the marker


@dataclass
class BadgeResult:
    badge: Badge
    status: Status
    collected: int
    passed: int
    failed: int
    skipped: int

    def to_dict(self) -> dict[str, object]:
        return {
            "marker": self.badge.marker,
            "name": self.badge.name,
            "gym": self.badge.gym,
            "proves": self.badge.proves,
            "lane": self.badge.lane,
            "status": self.status.value,
            "collected": self.collected,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
        }


# --------------------------------------------------------------------------------------
# Running pytest per marker
# --------------------------------------------------------------------------------------


def _collected_count(marker: str) -> int:
    """How many tests carry this marker. Zero means the lane has not landed."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            marker,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # pytest --collect-only -q prints one line per collected test, then a summary line.
    # Exit 5 = "no tests collected"; treat as zero rather than an error.
    if proc.returncode == 5:
        return 0
    count = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "no tests", "warning")):
            count += 1
    return count


def _run_marker(marker: str) -> tuple[int, int, int, int]:
    """Run the marker's suite. Returns (returncode, passed, failed, skipped)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            marker,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    passed, failed, skipped = _parse_summary(proc.stdout + proc.stderr)
    return proc.returncode, passed, failed, skipped


def _parse_summary(output: str) -> tuple[int, int, int]:
    """Extract passed/failed/skipped counts from pytest's terminal summary line.

    Parses the last line that looks like a summary (e.g. '102 passed in 0.43s' or
    '2 failed, 100 passed in 0.5s'). Robust to word order.
    """
    passed = failed = skipped = 0
    summary_line = ""
    for line in reversed(output.splitlines()):
        stripped = line.strip().strip("=").strip()
        if any(w in stripped for w in ("passed", "failed", "skipped", "error", "no tests")):
            summary_line = stripped
            break
    if not summary_line:
        return (0, 0, 0)
    tokens = summary_line.replace(",", " ").split()
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        prev = tokens[i - 1]
        if not prev.isdigit():
            continue
        n = int(prev)
        if tok.startswith("passed"):
            passed = n
        elif tok.startswith("failed"):
            failed = n
        elif tok.startswith("skipped"):
            skipped = n
        elif tok.startswith("error"):
            failed += n
    return (passed, failed, skipped)


def evaluate(badge: Badge) -> BadgeResult:
    collected = _collected_count(badge.marker)
    if collected == 0:
        return BadgeResult(badge, Status.UNBUILT, 0, 0, 0, 0)
    rc, passed, failed, skipped = _run_marker(badge.marker)
    status = Status.EARNED if (rc == 0 and failed == 0) else Status.FAILED
    return BadgeResult(badge, status, collected, passed, failed, skipped)


def evaluate_all() -> list[BadgeResult]:
    return [evaluate(b) for b in BADGES]


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

_TIER_REMINDER = (
    "Badges are TIER 1: they gate shipping. "
    "Pokédex trend stats (scripts/report.py) are TIER 2 and never gate."
)


class _Palette:
    """ANSI colours, disabled outside a real TTY so a pipe stays clean."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def bold(self, t: str) -> str:
        return self._wrap("1", t)


_STATUS_LABEL = {
    Status.EARNED: "EARNED",
    Status.FAILED: "FAILED",
    Status.UNBUILT: "NOT YET BUILT",
}


def _status_text(result: BadgeResult, pal: _Palette) -> str:
    if result.status is Status.EARNED:
        detail = f"{result.passed} passed"
        if result.skipped:
            detail += f", {result.skipped} skipped"
        return pal.green(f"✓ EARNED   ({detail})")
    if result.status is Status.FAILED:
        return pal.red(f"✗ FAILED   ({result.failed} failed, {result.passed} passed)")
    return pal.yellow("· NOT YET BUILT (no tests collected)")


def render_human(results: list[BadgeResult], pal: _Palette) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(pal.bold("  KANTO BADGE CASE"))
    lines.append(pal.dim("  " + "─" * 68))
    for i, r in enumerate(results, start=1):
        lines.append(
            f"  {i}. {pal.bold(r.badge.name.ljust(8))} {pal.dim(r.badge.gym.ljust(20))} "
            f"{_status_text(r, pal)}"
        )
        lines.append(pal.dim(f"       {r.badge.proves}  (lane {r.badge.lane})"))
    lines.append(pal.dim("  " + "─" * 68))

    earned = sum(1 for r in results if r.status is Status.EARNED)
    failed = sum(1 for r in results if r.status is Status.FAILED)
    unbuilt = sum(1 for r in results if r.status is Status.UNBUILT)
    summary = f"  {earned} earned · {failed} failed · {unbuilt} not yet built  (of {len(results)})"
    if failed:
        lines.append(pal.red(summary))
    else:
        lines.append(pal.green(summary))
    lines.append(pal.dim("  " + _TIER_REMINDER))
    lines.append("")
    return "\n".join(lines)


def render_json(results: list[BadgeResult]) -> str:
    earned = sum(1 for r in results if r.status is Status.EARNED)
    failed = sum(1 for r in results if r.status is Status.FAILED)
    unbuilt = sum(1 for r in results if r.status is Status.UNBUILT)
    payload = {
        "tier": 1,
        "gates_shipping": True,
        "reminder": _TIER_REMINDER,
        "summary": {
            "total": len(results),
            "earned": earned,
            "failed": failed,
            "unbuilt": unbuilt,
        },
        "badges": [r.to_dict() for r in results],
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the gym badge case.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    results = evaluate_all()

    if args.json:
        print(render_json(results))
    else:
        pal = _Palette(enabled=sys.stdout.isatty())
        print(render_human(results, pal))

    # Exit non-zero only if a badge WITH collected tests failed. Unbuilt badges never gate.
    any_failed = any(r.status is Status.FAILED for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
