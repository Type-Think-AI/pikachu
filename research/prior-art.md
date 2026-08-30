# Prior Art

Everything cited elsewhere in these docs, with what it actually gives us. Collected
2026-08-29.

---

## Self-evolving skill libraries

| Source | What it gives us |
|--------|------------------|
| [arXiv 2605.19576 — Diagnosing and Fixing a Silent Failure Mode in Self-Evolving LLM Skill Libraries](https://arxiv.org/html/2605.19576v2) | **The most important citation here.** Names *library drift*: unbounded skill accumulation without outcome-driven lifecycle management causes retrieval degradation, false-positive injections and performance stagnation. This is why creation without curation is not shippable. |
| [arXiv 2605.27366 — MUSE-Autoskill: Self-Evolving Agents via Skill Creation, Memory, Management, and Evaluation](https://arxiv.org/html/2605.27366v2) | Unified five-stage lifecycle (creation, memory, management, evaluation, refinement). Reports beating Hermes, Codex and Claude Code on **SkillsBench** and **SkillLearnBench** — i.e. benchmarks exist, so we can prove rather than assert. |
| [arXiv 2605.06614 — SkillOS: Learning Skill Curation for Self-Evolving Agents](https://arxiv.org/abs/2605.06614) | Frozen executor + trainable curator over an external `SkillRepo`. Validates our split: dumb turn runtime, separate curator process. |
| [arXiv 2512.17102 — SAGE: RL for Self-Improving Agent with Skill Library](https://arxiv.org/abs/2512.17102v1) (AWS Agentic AI, ACL 2026) | RL-based skill-library evolution. Out of near-term scope; relevant only if we ever train a curator. |
| [arXiv 2604.04804 — Automatically Constructing Skill Knowledge Bases for Agents](https://arxiv.org/html/2604.04804v1) | Auto-built reusable skill library, transferability measured on AppWorld, BFCL-v3, τ-Bench. More candidate benchmarks. |
| [arXiv 2602.12430 — Agent Skills for LLMs: Architecture, Acquisition, Security, and the Path Forward](https://arxiv.org/abs/2602.12430) | Systematic treatment including security. Read before finalising `06-security.md`. |

## Hermes 0.19.0 curator — primary source

Read directly from the installed package at
`api/.venv/lib/python3.13/site-packages/agent/`:

- `curator.py` — 86,924 bytes
- `background_review.py` — 50,183 bytes

Stated responsibilities: auto-transition lifecycle states from derived activity
timestamps; spawn a background review agent that can pin / archive / consolidate / patch
agent-created skills via `skill_manage`; persist state in `.curator_state`.

Stated invariants — adopt all five:

1. Only touches agent-created skills.
2. **Never auto-deletes — only archives. Archive is recoverable.**
3. Pinned skills bypass all auto-transitions.
4. Uses the auxiliary client; never touches the main session's prompt cache.
5. Inactivity-triggered, no cron daemon.

Defaults: `INTERVAL_HOURS=168`, `MIN_IDLE_HOURS=2`, `STALE_AFTER_DAYS=30`,
`ARCHIVE_AFTER_DAYS=90`, `CONSOLIDATE=False`.

Notable structure: `CURATOR_REVIEW_PROMPT`, `_classify_removed_skills`,
`_reconcile_classification`, `_build_rename_summary`, `_render_report_markdown` — a large
fraction of the 87 KB is *reconciliation and reporting*, not generation. That ratio is the
real lesson: the hard part of self-improvement is auditing what the machine decided, not
getting it to decide.

`background_review.py` carries `_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT` and
`_COMBINED_REVIEW_PROMPT` — memory review and skill review are the same background pass.
Worth mirroring.

## Agent memory on Postgres

| Source | What it gives us |
|--------|------------------|
| [PostgreSQL Agent Architecture: pgvector, pgmq, pg_cron in One DB](https://markaicode.com/architecture/postgres-agent-architecture/) | One Postgres behind PgBouncer replaces vector DB + broker + scheduler. Directly applicable — we already run Postgres. |
| [pgvector Agent Architecture: Production System Design](https://markaicode.com/architecture/pgvector-agent-architecture/) | Stateless embedding tier; partitioned HNSW-indexed store; **write through an async queue, not the request path.** |
| [Building AI Agents with Persistent Memory](https://www.tigerdata.com/learn/building-ai-agents-with-persistent-memory-a-unified-database-approach) | Episodic / semantic / procedural in one Postgres. |
| [Postgres, pgvector and KV as a Three-Tier Context Store](https://render.com/articles/give-your-agent-a-memory-postgres-pgvector-and-key-value-as-a-three-tier-context) | Matches our Postgres + Redis shape. |
| [Brotal-LLC/ilma](https://github.com/Brotal-LLC/ilma) | Framework-agnostic agent memory: Postgres + pgvector + MCP, explicitly supports Hermes Agent. **Evaluate before building our own.** |
| [sdimitrov/mcp-memory](https://github.com/sdimitrov/mcp-memory) | mem0-style long-term memory on Postgres + pgvector. |

## Skills support already shipping elsewhere

Evidence that skills-first is table stakes, not a differentiator:

- [Agent Skills for Python Is Now Released](https://devblogs.microsoft.com/agent-framework/agent-skills-for-python-is-now-released/) — Microsoft, four-stage progressive disclosure.
- [Microsoft Learn — Agent Skills](https://learn.microsoft.com/en-us/agent-framework/agents/skills)
- [Developer's Guide to Building ADK Agents with Skills](https://developers.googleblog.com/developers-guide-to-building-adk-agents-with-skills/) — Google `SkillToolset`.
- [Pydantic AI — Agent Skills](https://pydantic.dev/docs/ai/harness/skills/)
- [DougTrajano/pydantic-ai-skills](https://github.com/DougTrajano/pydantic-ai-skills) — full agentskills.io spec: remote registries, script execution, runtime reload.
- [openskills-sdk](https://pypi.org/project/openskills-sdk/) — standalone progressive-disclosure skills layer.

## Framework evidence

| Source | What it gives us |
|--------|------------------|
| [Hermes Agent vs CrewAI vs AutoGPT (Aug 2026)](https://markaicode.com/best/best-hermes-agent-production-practices/) | Independent verdict: Hermes is "an agent you run, not a library you import into a custom multi-step pipeline" — exactly PicX's shape. |
| [hermes-agent issue #20957](https://github.com/NousResearch/hermes-agent/issues/20957) | Prompt caching never transmits on OpenRouter + Claude: `cache_control` is ignored on the OpenAI-compat path. Breaks the Phase-4 cost plan. |
| [Pydantic AI OpenRouter provider](https://pydantic.dev/docs/ai/models/openrouter/) | First-party provider with app attribution. |
| [Pydantic AI changelog](https://pydantic.dev/docs/ai/project/changelog/) | V1 since Sept 2025 with a no-breaking-changes-until-V2 commitment — the stability that Hermes just failed to provide. |
| [Writing durable workflows — LlamaIndex](https://developers.llamaindex.ai/python/llamaagents/workflows/durable_workflows/) | "Resume is at-least-once, and step side effects need to be safe to repeat." The sentence that proves generic durability cannot handle paid tools. |
| [conductor-agent-sdk / Agentspan](https://pypi.org/project/conductor-agent-sdk/) | Durable runtime wrapping an existing agent. Competes with our `groot_runs`; check before extending ours. |
| [Best AI Agent Frameworks 2026](https://www.agentmail.to/blog/best-ai-agent-frameworks-2026) | AutoGen in maintenance mode since late 2025. |
| [Agno benchmarks](https://www.agno.com/benchmarks) | 3.2 µs / 5.2 KiB per agent — **instantiation only.** Self-published. Noise for our workload. |

## Not yet checked

- Whether hermes-agent 0.19.0 specifically still carries the #20957 caching bug.
- Whether any framework has a metered-tool primitive (claim in `00-problem-statement.md`).
- Whether `ilma` is good enough to adopt rather than reimplement `memory/`.
- SkillsBench / SkillLearnBench: are they runnable by us, and on what harness?
