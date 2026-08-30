#!/usr/bin/env python
"""Verify the Pydantic AI API surface this project's design docs assume.

Offline: introspection only, no model calls, no network. Run this after every
``pydantic-ai`` upgrade — the docs argued stability on "V1, no breaking changes until V2",
and V2 has now shipped, so the assumed surface needs re-checking rather than recalling.

    .venv/bin/python scripts/verify_pydantic_ai.py

Exit code is 1 if anything the docs depend on is ABSENT, so this can gate a dependency bump.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from typing import Any, Final

# (module, symbol, why the project cares)
SYMBOLS: Final[tuple[tuple[str, str, str], ...]] = (
    ("pydantic_ai", "Agent", "the core object"),
    ("pydantic_ai", "RunUsage", "token accounting; docs corrected Usage -> RunUsage"),
    ("pydantic_ai", "ModelRetry", "consumes the retry budget"),
    ("pydantic_ai", "UsageLimits", "budget ceilings"),
    ("pydantic_ai", "CachePoint", "explicit cache breakpoint"),
    ("pydantic_ai.usage", "RunUsage", "canonical import path"),
    ("pydantic_ai.usage", "UsageLimits", "canonical import path"),
    ("pydantic_ai.messages", "ToolReturnPart", "carries .outcome"),
    ("pydantic_ai.messages", "ModelRequest", "history shape"),
    ("pydantic_ai.exceptions", "UsageLimitExceeded", "budget breach"),
    ("pydantic_ai.exceptions", "UnexpectedModelBehavior", "wraps content-filter cases"),
    ("pydantic_ai.exceptions", "ModelHTTPError", "carries retry_after"),
    ("pydantic_ai.exceptions", "ToolRetryError", "tool-level retry"),
    ("pydantic_ai.tools", "ToolDefinition", "what .filtered() predicates receive"),
    ("pydantic_ai.toolsets", "FunctionToolset", "tool grouping"),
    ("pydantic_ai.models.openai", "OpenAIChatModel", "OpenRouter goes through OpenAI-compat"),
    ("pydantic_ai.providers.openrouter", "OpenRouterProvider", "the provider we use"),
)

# Fields the design docs assume exist on a dataclass/model.
FIELDS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    (
        "pydantic_ai.usage",
        "UsageLimits",
        (
            "cost_limit",
            "request_limit",
            "tool_calls_limit",
            "input_tokens_limit",
            "output_tokens_limit",
            "count_tokens_before_request",
            # Legacy V1 name. Presence here would mean the docs' correction was wrong.
            "request_tokens_limit",
        ),
    ),
    (
        "pydantic_ai.usage",
        "RunUsage",
        ("cache_read_tokens", "cache_write_tokens", "cost", "cache_hit_ratio"),
    ),
)


def _members(obj: Any) -> set[str]:
    """Field names plus attributes, so a property is found as well as a dataclass field."""
    names: set[str] = set(dir(obj))
    if dataclasses.is_dataclass(obj):
        names |= {f.name for f in dataclasses.fields(obj)}
    model_fields = getattr(obj, "model_fields", None)
    if isinstance(model_fields, dict):
        names |= set(model_fields)
    return names


def main() -> int:
    try:
        import pydantic_ai
    except ImportError:
        print("pydantic_ai is not installed in this interpreter")
        return 1

    print(f"pydantic_ai {pydantic_ai.__version__}   python {sys.version.split()[0]}\n")

    missing: list[str] = []

    print("SYMBOLS")
    for module, symbol, why in SYMBOLS:
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - report, never crash the check
            print(f"  ERR      {module}.{symbol}  ({type(exc).__name__}) - {why}")
            missing.append(f"{module}.{symbol}")
            continue
        if hasattr(mod, symbol):
            print(f"  PRESENT  {module}.{symbol}")
        else:
            print(f"  ABSENT   {module}.{symbol}  - {why}")
            missing.append(f"{module}.{symbol}")

    print("\nFIELDS")
    for module, symbol, fields in FIELDS:
        try:
            obj = getattr(importlib.import_module(module), symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR      {module}.{symbol} ({type(exc).__name__})")
            missing.append(f"{module}.{symbol}")
            continue
        have = _members(obj)
        for field in fields:
            legacy = field == "request_tokens_limit"
            if field in have:
                # The legacy name being present is a finding, not a pass.
                print(f"  {'LEGACY!  ' if legacy else 'PRESENT  '}{symbol}.{field}")
            elif legacy:
                print(f"  gone     {symbol}.{field}  (expected - V1 name)")
            else:
                print(f"  ABSENT   {symbol}.{field}")
                missing.append(f"{symbol}.{field}")

    print("\nDEFAULTS")
    try:
        from pydantic_ai.usage import UsageLimits

        limits = UsageLimits()
        for name in ("request_limit", "tool_calls_limit", "cost_limit"):
            print(f"  UsageLimits().{name} = {getattr(limits, name, '<absent>')!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not instantiate UsageLimits: {type(exc).__name__}: {exc}")

    print("\nMCP REVISION")
    # pydantic-ai wraps fastmcp/mcp rather than implementing the protocol, so the revision
    # spoken is whichever the installed SDK speaks - check both layers.
    for pkg in ("mcp", "fastmcp", "fastmcp_tasks"):
        try:
            m = importlib.import_module(pkg)
            print(f"  {pkg}: installed {getattr(m, '__version__', '(no __version__)')}")
        except ImportError:
            print(f"  {pkg}: NOT INSTALLED")

    print("\nTOOL FILTERING")
    try:
        from pydantic_ai.toolsets import FunctionToolset

        ts = FunctionToolset()
        for attr in ("filtered", "prepared", "renamed", "filter_tools"):
            print(f"  FunctionToolset.{attr}: {'yes' if hasattr(ts, attr) else 'no'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not inspect toolsets: {type(exc).__name__}: {exc}")

    print()
    if missing:
        print(f"MISSING ({len(missing)}): {', '.join(missing)}")
        return 1
    print("All assumed symbols and fields are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
