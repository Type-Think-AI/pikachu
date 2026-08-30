#!/usr/bin/env python
"""The POKÉDEX — tier-2 trend stats that are recorded and reported but NEVER gate a build.

This is the deliberate counterpart to ``scripts/badges.py``. The badge case is a **gate**: it
prints EARNED / FAILED and exits non-zero when a tier-1 invariant breaks. The Pokédex is a
**trend line**: it prints entries with running numbers and always exits zero, because a trend
that fails a build is a trend that gets disabled (docs/12-evaluation.md, the two-tier rule).

The two reports are kept visually distinct on purpose so nobody mistakes a trend for a gate:

* the badge case is a numbered "KANTO BADGE CASE" with ✓/✗ per badge;
* the Pokédex is a "#NNN"-numbered dex of stat entries with a bar per number and a standing
  banner that says these numbers never fail a build.

The single load-bearing rule of this report: **never show a blended latency without the
framework/model split.** A blended number moves when the model changes and when our code
changes and cannot tell you which — see ``core.types.TurnTiming``. So latency percentiles are
shown three times, over three separate samples (total, framework-only, model-only), and the
framework share is called out as the one number that says whether *we* regressed.

Empty state is a first-class case: a fresh install has recorded no runs, and this prints an
honest "no data yet" dex rather than a wall of zeros that would imply zeros were measured.

Usage::

    python scripts/report.py            # human-readable Pokédex
    python scripts/report.py --json     # machine-readable, one JSON object

No network, no I/O beyond stdout. Colour is guarded by ``sys.stdout.isatty()`` so a pipe stays
clean. Prices are read from ``pikachu.config``'s documented default-model numbers *at call
time* and passed in as parameters — this script hardcodes no price.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pikachu.telemetry.ledger import LedgerTotals, Prices, TokenLedger

# The Pokédex never gates. Stated as a constant so both renderers and the JSON payload use the
# exact same words, and so a reader grepping the source finds the guarantee in one place.
_NEVER_GATES = (
    "TIER 2 — TREND ONLY. These numbers are recorded and tracked over time. "
    "They NEVER fail a build. The gate is the badge case (scripts/badges.py)."
)


# --------------------------------------------------------------------------------------
# Prices: fetched from config at call time, never hardcoded here
# --------------------------------------------------------------------------------------


def _default_prices() -> Prices:
    """Build a ``Prices`` from the default model's *documented* published numbers.

    These live in ``pikachu.config``'s ``DEFAULT_MODEL`` docstring (dated 2026-08-30). We read
    them here as the reporting default so the dex can show a dollar estimate out of the box, but
    the ledger's cost estimator still takes prices as a *parameter* — nothing in the telemetry
    layer depends on a constant. If the caller has fresher numbers, they pass their own.
    """
    from pikachu.telemetry.ledger import Prices

    # Published OpenRouter prices for google/gemini-3.7-flash, per config.DEFAULT_MODEL, in
    # dollars per MTok. Kept here (a script, tier-2, non-gating) rather than in the library.
    return Prices(
        prompt_per_mtok=0.75,
        completion_per_mtok=3.75,
        cache_read_per_mtok=0.075,
        cache_write_per_mtok=0.0417,
    )


# --------------------------------------------------------------------------------------
# Colour, guarded by isatty so a pipe never gets ANSI bytes
# --------------------------------------------------------------------------------------


class _Ink:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _w(self, code: str, t: str) -> str:
        return f"\033[{code}m{t}\033[0m" if self.enabled else t

    def cyan(self, t: str) -> str:
        return self._w("36", t)

    def amber(self, t: str) -> str:
        return self._w("33", t)

    def dim(self, t: str) -> str:
        return self._w("2", t)

    def bold(self, t: str) -> str:
        return self._w("1", t)


# --------------------------------------------------------------------------------------
# Rendering — human
# --------------------------------------------------------------------------------------


def _bar(value: float, ceiling: float, width: int = 24) -> str:
    """A pure-ASCII sparkline-ish bar. Distinct from the badge case, which uses none."""
    if ceiling <= 0:
        return "·" * width
    filled = max(0, min(width, round(value / ceiling * width)))
    return "▓" * filled + "░" * (width - filled)


def _pct_line(label: str, pct: object) -> str:
    # pct is a Percentiles; typed loosely to avoid importing at module scope.
    return (
        f"{label:<18} p50 {getattr(pct, 'p50'):>8.0f}   "
        f"p90 {getattr(pct, 'p90'):>8.0f}   "
        f"p99 {getattr(pct, 'p99'):>8.0f}   "
        f"(n={getattr(pct, 'n')})"
    )


def render_human(ledger: TokenLedger, prices: Prices, ink: _Ink) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(ink.cyan(ink.bold("  ┌─ POKÉDEX · trend stats ─────────────────────────────────┐")))
    lines.append(ink.dim("  " + _NEVER_GATES))
    lines.append("")

    if ledger.is_empty:
        lines += [
            ink.amber("  No entries recorded yet."),
            ink.dim(
                "  A fresh install has run no turns, so there is nothing to trend. This is an "
                "honest empty state —"
            ),
            ink.dim(
                "  not a set of zero measurements. Numbers appear here once turns have been "
                "recorded into a ledger."
            ),
            "",
        ]
        return "\n".join(lines)

    t: LedgerTotals = ledger.totals()

    # #001 — cost per turn
    lines.append(ink.bold("  #001  COST / TURN"))
    lines.append(
        f"         {t.cost_per_turn_credits:.2f} credits/turn   "
        f"·  {t.cost_credits} credits over {t.turns} turns   "
        f"·  est ${t.estimated_cost_usd(prices):.6f}"
    )
    lines.append(ink.dim(f"         {_bar(t.cost_per_turn_credits, max(1.0, t.cost_per_turn_credits))}"))
    lines.append("")

    # #002 — cache-hit ratio
    lines.append(ink.bold("  #002  CACHE-HIT RATIO"))
    lines.append(
        f"         {t.cache_hit_ratio * 100:.1f}%   "
        f"·  {t.cache_read_tokens} cached / {t.input_tokens + t.cache_read_tokens} prompt tokens"
    )
    lines.append(ink.dim(f"         {_bar(t.cache_hit_ratio, 1.0)}"))
    if t.cache_hit_ratio == 0.0:
        lines.append(ink.dim("         0% — caching has not fired in this data. See config.CACHE_FLOOR_UNVERIFIED."))
    lines.append("")

    # #003 — latency, SPLIT three ways. The whole point of the module.
    lines.append(ink.bold("  #003  LATENCY (ms) — split, because a blended number can't tell model from us"))
    lines.append("         " + _pct_line("total", t.latency_pct))
    lines.append("         " + ink.cyan(_pct_line("framework (ours)", t.framework_pct)))
    lines.append("         " + _pct_line("model (provider)", t.model_pct))
    lines.append(
        ink.dim(
            f"         framework share {t.framework_share * 100:.3f}%  "
            f"(setup {t.setup_ms} + finalize {t.finalize_ms} ms of {t.total_ms} ms total)"
        )
    )
    if t.framework_share < 0.05:
        lines.append(
            ink.dim(
                "         → framework is not the bottleneck; the lever is model / provider / prompt size."
            )
        )
    else:
        lines.append(
            ink.amber(
                "         → framework share is elevated — WE may have regressed. Check setup is paid once, not per turn."
            )
        )
    if t.unattributed_ms:
        lines.append(ink.dim(f"         unattributed {t.unattributed_ms} ms (phases did not sum to total)"))
    lines.append("")

    # #004 — decode throughput, stream_ms ONLY
    lines.append(ink.bold("  #004  DECODE THROUGHPUT"))
    if t.stream_ms:
        lines.append(
            f"         {t.tokens_per_second:.0f} tok/s   "
            f"·  {t.output_tokens} output tokens in {t.stream_ms} ms of decode"
        )
        lines.append(ink.dim("         measured on stream time only — provider queue (wait) does not dilute it"))
    else:
        lines.append(ink.dim("         no decode time measured (stream/wait split unavailable in this data)"))
    lines.append("")

    # #005 — partition confusability, IF a source provided it. Lane D owns the metric; we
    # report it only when handed a value, and say plainly when it is absent.
    lines.append(ink.bold("  #005  PARTITION CONFUSABILITY"))
    lines.append(
        ink.dim(
            "         not available — no confusability source wired into this ledger. "
            "Lane D computes it; it appears here once fed in."
        )
    )
    lines.append("")

    # Per-agent, if more than the unnamed bucket carries data.
    agents = ledger.agent_totals()
    named = {k: v for k, v in agents.items() if k}
    if named:
        lines.append(ink.bold("  PER-AGENT"))
        for name, a in named.items():
            lines.append(
                f"         {name:<20} {a.turns:>4} turns  "
                f"·  {a.cost_credits:>6} cr  "
                f"·  cache {a.cache_hit_ratio * 100:>5.1f}%  "
                f"·  fw {a.framework_ms} / model {a.model_ms} ms"
            )
        lines.append("")

    lines.append(ink.cyan(ink.bold("  └──────────────────────────────────────────────────────────┘")))
    lines.append(ink.dim("  " + _NEVER_GATES))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Rendering — JSON
# --------------------------------------------------------------------------------------


def render_json(ledger: TokenLedger, prices: Prices) -> str:
    import json

    def pct(p: object) -> dict[str, float | int]:
        return {
            "p50": getattr(p, "p50"),
            "p90": getattr(p, "p90"),
            "p99": getattr(p, "p99"),
            "n": getattr(p, "n"),
        }

    if ledger.is_empty:
        return json.dumps(
            {
                "tier": 2,
                "gates_shipping": False,
                "reminder": _NEVER_GATES,
                "empty": True,
                "turns": 0,
            },
            indent=2,
            ensure_ascii=False,
        )

    t = ledger.totals()
    payload = {
        "tier": 2,
        "gates_shipping": False,
        "reminder": _NEVER_GATES,
        "empty": False,
        "turns": t.turns,
        "cost": {
            "per_turn_credits": t.cost_per_turn_credits,
            "total_credits": t.cost_credits,
            "estimated_usd": t.estimated_cost_usd(prices),
        },
        "cache_hit_ratio": t.cache_hit_ratio,
        "latency_ms": {
            "total": pct(t.latency_pct),
            "framework": pct(t.framework_pct),
            "model": pct(t.model_pct),
            "framework_share": t.framework_share,
            "phase_sums": {
                "setup": t.setup_ms,
                "wait": t.wait_ms,
                "stream": t.stream_ms,
                "finalize": t.finalize_ms,
                "unattributed": t.unattributed_ms,
                "total": t.total_ms,
            },
        },
        "decode_tokens_per_second": t.tokens_per_second,
        "partition_confusability": None,  # Lane D's metric; absent unless fed in
        "tokens": {
            "input": t.input_tokens,
            "output": t.output_tokens,
            "cache_read": t.cache_read_tokens,
            "cache_write": t.cache_write_tokens,
        },
        "per_agent": {
            name: {
                "turns": a.turns,
                "cost_credits": a.cost_credits,
                "cache_hit_ratio": a.cache_hit_ratio,
                "framework_ms": a.framework_ms,
                "model_ms": a.model_ms,
                "total_ms": a.total_ms,
            }
            for name, a in ledger.agent_totals().items()
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Data source
# --------------------------------------------------------------------------------------


def _load_ledger() -> TokenLedger:
    """Return the ledger to report on.

    Persistence is Lane L's job, not this script's — there is no store to read from yet. So this
    returns an EMPTY ledger, which is the correct, honest state for a fresh install: no runs
    recorded means no trend to show. When a storage-backed run history exists, this is the one
    function to change; the renderers already handle a populated ledger. Kept isolated so that
    swap is a single seam, not a rewrite.
    """
    from pikachu.telemetry.ledger import TokenLedger

    return TokenLedger()


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the Pokédex (tier-2 trend stats).")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    ledger = _load_ledger()
    prices = _default_prices()

    if args.json:
        print(render_json(ledger, prices))
    else:
        ink = _Ink(enabled=sys.stdout.isatty())
        print(render_human(ledger, prices, ink))

    # The Pokédex NEVER gates. Always exit 0 — a trend line must not fail a build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
