"""Runtime configuration and defaults.

Kept deliberately tiny. Every value here is a decision someone made on purpose, with the
reasoning next to it, because a default nobody can explain becomes a default nobody dares
change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

__all__ = [
    "CACHE_FLOOR_UNVERIFIED",
    "DEFAULT_MODEL",
    "STABLE_PREFIX_TOKENS_MAX",
    "STABLE_PREFIX_TOKENS_MIN",
    "cache_is_expected_to_fire",
    "get_api_key",
    "parse_env_file",
]

DEFAULT_MODEL: Final = "google/gemini-3.8-flash"
"""The default model, chosen by the project owner 2026-08-30, upgraded to
``google/gemini-3.8-flash`` on 2026-09-03.

Verified present on OpenRouter on the upgrade day; the price/spec table below is
unchanged from 3.7-flash (same $0.75/$3.75 per MTok, same 1M context and native
video/audio/image/file input), so the cache economics that follow still hold:

===================  =========================================================
context length       1,048,576 tokens
input modalities     text, image, video, file, audio  ->  text out
prompt price         $0.75 / MTok
completion price     $3.75 / MTok
cache read           $0.075 / MTok  =  0.10x prompt
cache write          $0.0417 / MTok =  0.056x prompt  (billed as storage, NOT a premium)
supported params     tools, structured_outputs, reasoning, reasoning_effort, seed, ...
===================  =========================================================

**Why this model rather than the Sonnet-class alternative that the cache research
recommended:** this is a media and film product, and the input modality list is the deciding
factor. It accepts **video, audio, image and file** input natively, which a text-and-image
model cannot do at any price. Cost is explicitly deprioritised for this project -- capability
wins -- and a 1M context plus native tool calling covers the rest.

The cache-write economics are also better than the research assumed for the Anthropic path:
Google bills a cache write at roughly **0.056x** the input price rather than Anthropic's
1.25x premium, so a write costs less than simply sending the tokens. There is no break-even
to reach.

**The open risk, stated plainly:** see ``CACHE_FLOOR_UNVERIFIED``.
"""

CACHE_FLOOR_UNVERIFIED: Final = True
"""Whether the default model's minimum cacheable-prefix size is still unconfirmed.

OpenRouter publishes cache *pricing* for this model, so caching is supported. What is **not**
published anywhere we could find is the minimum prefix length required for a cache write to
happen at all. OpenRouter's guidance says only that *"Gemini models typically have a 4096
token minimum for cache write to occur"* -- a blanket statement, not a per-version fact, and
no per-model floor is exposed in the models API.

Our stable prefix measures ~1,500-2,400 tokens (see ``STABLE_PREFIX_TOKENS_*``). If the
4,096 figure holds for this model, **caching is enabled and does nothing**: no error, no
warning, ``cache_read_tokens`` simply stays 0 forever. That is the failure mode this flag
exists to keep visible.

Resolve it empirically, not by reading more docs: run one real multi-iteration turn and read
``RunUsage.cache_read_tokens``. Set this to ``False`` once measured, and record the number in
``docs/22-phase0-verification.md``.

Note also that Google's implicit caching reports 0 for ``cache_read_tokens`` through some
paths regardless of whether it fired (pydantic-ai issue #5205), so a 0 reading must be
confirmed against the OpenTelemetry span before concluding the floor is the cause.
"""

STABLE_PREFIX_TOKENS_MIN: Final = 1_500
STABLE_PREFIX_TOKENS_MAX: Final = 2_400
"""Measured size of the cacheable prefix: base instructions + skill body + tool schemas.

From a byte measurement of the parent system, where tool descriptions turned out to be the
larger half (3,232 B of tool schemas against a 2,734 B skill body). Byte-derived, since no
tokenizer was installed -- treat as an estimate with roughly a 4:1 bytes-per-token ratio.
"""


def cache_is_expected_to_fire(model: str, *, floor_tokens: int) -> bool:
    """Whether a cache write should occur for ``model`` given a published ``floor_tokens``.

    Compares against the **pessimistic** end of our measured prefix range, so this returns
    True only when caching fires even on our smallest prompt. A helper rather than a constant
    because the answer changes per model, and because a per-agent model override can silently
    move a run onto a model whose floor our prefix does not clear.

    ``model`` is accepted for call-site clarity and future per-model rules; it is not yet
    consulted.
    """
    del model  # reserved for a per-model floor table once floors are measured
    return floor_tokens <= STABLE_PREFIX_TOKENS_MIN


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------

# Where to look for a .env, nearest first. Relative to this file's package root so it works
# regardless of the caller's cwd.
_ENV_SEARCH_PATH: Final = (
    Path(__file__).resolve().parents[2] / ".env",  # pikachu/.env
    Path(__file__).resolve().parents[3] / "api" / ".env",  # the host app's existing .env
)


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` reader. No dependency, no shell evaluation.

    Deliberately does NOT support command substitution, variable interpolation or multi-line
    values: a config loader that can execute is a config loader that can be attacked. Quotes
    are stripped, ``export`` prefixes tolerated, blanks and ``#`` comments skipped.
    """
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            out[key] = value
    return out


def get_api_key(var: str = "OPENROUTER_API_KEY") -> str | None:
    """Resolve a credential: real environment first, then the .env search path.

    Returns ``None`` rather than raising so a caller can *skip* instead of failing — a live
    test suite with no key configured should report "skipped, no credential", not "broken".
    The value is never logged by this module.
    """
    from_env = os.environ.get(var)
    if from_env:
        return from_env
    for candidate in _ENV_SEARCH_PATH:
        value = parse_env_file(candidate).get(var)
        if value:
            return value
    return None

