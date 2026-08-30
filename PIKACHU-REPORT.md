# Pikachu — Reference Report

**Date:** 2026-08-30 · **Repo:** [Type-Think-AI/pikachu](https://github.com/Type-Think-AI/pikachu) (private) ·
**Status:** feature-complete, tested, GO for integration

A one-page reference to what Pikachu is, what it does, how it was tested, and — the part you asked
about — **how the agent actually responds when you run it.** Read top to bottom; every number here is
measured, not estimated.

---

## 1. What it is, in one line

An agent runtime that runs the open agent standards (Agent Skills, Agent Plugins, MCP, WebMCP, A2A)
and adds the **permission, confinement and provenance layer those standards leave out** — the thing
that matters when a stranger's skill can spend a real user's credits.

Built on **Pydantic AI**, default model **`google/gemini-3.7-flash`**.

## 2. Snapshot

| | |
|---|---|
| Features | **23 / 23** |
| Tests | **845 passing** (offline) + 5 live |
| Type check | `mypy --strict` clean, 60 files |
| Gym badges | **8 / 8 earned** |
| Modules | 19 · 129 Python files · 28,787 lines |
| Commits | 13, pushed to the private repo |

---

## 3. How the agent responds — the part to reference

This is what "does the agent work" actually looks like, verified against the real model.

### A skill responds *with* its tools

You author a `SKILL.md`, give it a tool, and run a turn. Live result:

```
Skill body:  "the palette is NOT in your head — call brand_palette and quote its hex."
Agent asked: "What is our signal colour? Give the hex."
→ the model CALLED brand_palette and answered "#FFB300"     (executed=True)
```

That is real tool **selection**, not just plumbing — the model chose to call the tool because the
skill body told it to, and used the tool's output in its answer.

### The guard narrows what the agent can touch

Same skill, but the tool removed from the agent's allowlist:

```
→ the model still emitted a call for brand_palette, but it NEVER RAN      (executed=False)
→ answer: "I cannot provide the amber hex — the brand_palette tool is unavailable."
```

No denied tool ever executes. The `executed` flag on every `tool_calls` record tells you which is
which — this was the one thing live testing caught that the offline fakes hid, and it is fixed.

### Declarative function tools

A plain Python function becomes a tool the model can call, with its **docstring as the description**
the model reads. Live, the model chose to call one unprompted when the task needed it.

### What a turn reports back

Every turn returns a `TurnResult` with:

- `text` — the answer
- `tool_calls` — each with `tool`, `args`, and `executed` (did it run, or was it only emitted)
- `timing` — split into **framework** (ours, ~0.3 ms) vs **model** (~2–3 s). So you can always tell
  whether *we* got slow or *the model* did.
- token counts, `cache_hit_ratio`, and which provider served it

---

## 4. The 23 features

| Group | Features |
|---|---|
| **Runtime** | turn runtime · guard (permissions) · event stream · cacheable prompt |
| **Standards** | Agent Skills · Agent Plugins · MCP client · MCP server · OAuth 2.1 · A2A · WebMCP |
| **Agents** | AgentSpec registry · conservative routing · canvas (artifact graph) |
| **Learning** | self-improvement loop · creation gate · memory tiers · durable runs |
| **Ops** | metered tools (charge/refund) · telemetry (OTel) · eval harness · plugin distribution |

The five nobody else has, and the reason Pikachu exists: **enforced tool authority · metered tools ·
no-double-charge on resume · append-only canvas · taint/lineage.**

---

## 5. How it was tested — three independent rounds

Three agents, three angles, then graded against each other:

| Round | What it did | Result |
|---|---|---|
| **1 · happy path** | a user authors skills, runs them with tools, streaming | pass |
| **2 · adversarial** | 98 attacks on skills, plugins, MCP servers | **every one held** |
| **3 · live + perf** | the real model actually calling tools; profiling | pass + 1 gap found & fixed |

**Verdict: GO for integration. No bugs, no blockers.** Full write-up in `docs/test-summary.md`.

The rounds found no code bug, but three real findings — the most important being the `executed`-flag
gap above, which passed offline and would have been wrong in production. That is exactly why a live
round existed.

---

## 6. Two things accepted, not broken

- **Prompt caching does not fire** on the default model (measured: 0 cache reads on a 1,964-token
  prefix). The model was chosen for **native video/audio/image input**, not caching — a deliberate
  trade, not a defect.
- **The injection scanner misses paraphrased attacks.** It catches literal override phrasing and
  credential exfiltration; a politely-worded redirect passes. So a human still reviews anything
  published — stated honestly, not hidden.

---

## 7. How to run it

```bash
cd pikachu
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python -m pytest tests/ -q          # 845 tests
.venv/bin/python scripts/badges.py            # the 8-badge gym case
.venv/bin/python examples/skill_with_tools.py # author a skill, give it a tool, run a turn
.venv/bin/python scripts/round3_live.py       # watch it call tools against the real model
```

## 8. Where to read more

- `PRD.md` — the 23 features and the 7 success criteria with verified status
- `docs/test-summary.md` — the go/no-go verdict
- `docs/23-framework-comparison.md` — why Pikachu vs Agno vs Pydantic AI
- `examples/` — runnable proof of skills, tools, and the canvas handoff
