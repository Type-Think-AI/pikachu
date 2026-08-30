# 04 — Memory and Retrieval

## Decision: one Postgres, no separate vector database

PicX already runs Postgres (DigitalOcean) and Redis (Koyeb). Adding a dedicated vector
DB adds a service, a failure mode, a bill and a consistency problem. The 2026 consensus
is that one Postgres does this job:

> "A single PostgreSQL instance running pgvector (vector memory), pgmq (task queues), and
> pg_cron (scheduling) behind PgBouncer can serve as the entire state layer for a
> production AI agent, replacing a separate vector database, message broker, and job
> scheduler."

Relevant because we already have the pieces. `pgmq` in particular could absorb curator
scheduling without a new daemon.

**Constraint that shapes everything below:** local dev API runs against *remote* Postgres
and Redis, so every round trip is 250–600 ms. Memory reads must be bounded and few;
memory writes must never sit in the request path.

## Write path

> "an agent orchestration tier that writes through an async queue instead of the request
> path"

Embedding a turn costs an API call. Doing it inline adds latency to every turn for a
benefit realised on a *later* turn. So:

- **Reads** — synchronous, capped, in the turn.
- **Writes** — enqueued, processed by a worker. A dropped memory write degrades quality;
  it must never fail a paid generation.

Index with **HNSW**, and partition once volume warrants it.

## Memory tiers

Standard three-tier split (episodic / semantic / procedural), specialised for a
content/visual agent — which is where the domain differentiation actually lands.

| Tier | Holds | Lifetime | Retrieval |
|------|-------|----------|-----------|
| **Working** | Current turn's history | The run | In-process, already in `groot_runs` |
| **Episodic** | What happened: prompts, artifacts, models used, costs, accept/reject | Per user, decaying | pgvector similarity + recency |
| **Semantic** | Durable facts: brands, style preferences, palettes, aspect ratios, banned looks | Per user, curated | pgvector + structured filter |
| **Procedural** | How to do things — **this is the skill library** | Per user + public catalog | `find_skill`, see `03-skill-lifecycle.md` |

Procedural memory and the skill catalog are the **same thing**. Do not build two systems.
That mistake has already been made once in this codebase — three coexisting skill systems
(`app/skills`, `app/groot`, `app/agent_skills`) with `groot_skills` declared canonical.

## The domain-specific tier that nobody else has

For a design agent the highest-value memory is aesthetic:

```
style_memory
  user_id
  dimension        -- palette | aspect_ratio | subject | rendering_style | brand
  value            -- structured
  polarity         -- preferred | rejected
  confidence       -- decays without reinforcement
  evidence_count   -- how many artifacts support this
  last_seen_at
  embedding        vector(N)
```

Derived from **behaviour, not statements**: which generations the user kept, downloaded,
put on a board, or regenerated away from. A rejection is as informative as an acceptance
and cheaper to collect — the user regenerating is a labelled negative.

This is the moat in memory terms. General frameworks model "conversation history"; a
design tool needs "this user's taste."

## Consolidation

Raw episodic memory grows without bound and degrades retrieval — the same drift problem
as skills, so use the same discipline:

1. Cluster episodic rows by embedding similarity on a schedule.
2. Promote repeated patterns into semantic/style memory with `evidence_count`.
3. Decay confidence on anything unreinforced.
4. **Archive, never delete.** Same invariant as the skill curator.
5. Run in the curator on the auxiliary model, never in a user turn.

## Retrieval budget

Memory must not reintroduce the token waste `05-performance.md` is trying to remove.
Hard caps, enforced in code:

| Tier | Max items | Max tokens |
|------|-----------|------------|
| Semantic / style | 8 | 400 |
| Episodic | 3 | 600 |
| Procedural (skills) | 5 found / 1 loaded | 150 find + 2000 body |

Ceiling ~3,150 tokens per turn regardless of how much memory exists. Same philosophy as
the existing `FIND_TOKEN_CAP` / `LOAD_BODY_TOKEN_CAP` caps: bounded cost independent of
catalogue size.

## Prior art

- `Brotal-LLC/ilma` — framework-agnostic agent memory on Postgres + pgvector + MCP,
  explicitly lists Hermes Agent as a client. Evaluate before writing our own; possibly
  usable directly.
- Three-tier context store pattern (Postgres + pgvector + KV) — matches our
  Postgres + Redis shape.
- `sdimitrov/mcp-memory` — mem0-style long-term memory on Postgres + pgvector.

## Open

- Embedding model and dimension not chosen. Affects index size and cost; must be
  decided before the migration, since changing it later means re-embedding everything.
- Whether `pgmq` replaces Redis for curator scheduling or runs alongside it.
