# 03 — Skill Lifecycle: creation, versioning, self-improvement

This is the headline feature: the agent creates skills as it works, saves them, and
improves them over time. It is also the feature most likely to quietly make the product
worse, so this doc leads with the failure mode.

---

## The failure mode: library drift

> "Self-evolving skill libraries face a silent failure mode we term **library drift**:
> unbounded skill accumulation without outcome-driven lifecycle management causes
> retrieval degradation, false-positive injections, and performance stagnation."
> — arXiv 2605.19576

Read that again against the feature as requested: *"automatically create the skills as
needed and save it."* Creation without curation is the exact configuration the paper
diagnoses. Three concrete harms:

1. **Retrieval degradation** — `find_skill` has `MAX_FIND_RESULTS=5`. At 40 skills the
   top 5 are probably right. At 4,000 auto-generated near-duplicates, the top 5 are
   noise, and every turn silently gets worse.
2. **False-positive injection** — a marginally-relevant skill loads its body
   (`LOAD_BODY_TOKEN_CAP=2000`) into context and steers the model wrong. We pay tokens
   to be misled.
3. **Performance stagnation** — the library stops being a learning signal because
   nothing distinguishes a skill that worked from a skill that merely got written.

**Design rule: creation and curation ship together, or creation does not ship.**

### And now there is empirical evidence, not just a failure-mode paper

**SkillsBench** ([arXiv 2602.12670](https://arxiv.org/abs/2602.12670),
[skillsbench.ai](https://www.skillsbench.ai/), Apache-2.0 and publicly runnable) evaluated
agents on **86 tasks across 11 domains** with deterministic verifiers, under **three
conditions — no skills, curated skills, self-generated skills** — across 7 agent-model
configurations and **7,308 trajectories**. The result:

> curated skills help significantly but inconsistently; **self-generated skills offered no
> benefit**.

That is the feature as originally requested, measured at scale, finding zero benefit. It
corroborates library drift from a completely independent direction — one is a diagnosed
failure mode, the other is a controlled measurement.

Two consequences:

1. **The creation gate is not optional polish.** The difference between "curated skills help"
   and "self-generated skills don't" is precisely the gate and the lifecycle below. Auto-create
   without them and the published expectation is *no improvement*.
2. **We can prove it rather than assert it.** SkillsBench is runnable (BenchFlow harness,
   `bench eval run --tasks-dir tasks/<id>`), and it already has the exact three-arm design we
   need. If Pikachu's curated + curated-auto pipeline beats the self-generated arm, that is a
   publishable claim. If it doesn't, we learn that before shipping.

**SkillLearnBench** ([arXiv 2604.20087](https://arxiv.org/abs/2604.20087), CMU + Amazon AGI,
COLM'26) is the tighter target: "the first benchmark for evaluating **continual skill
learning**" — 20 verified skill-dependent tasks across 15 sub-domains, scored at **three
levels: skill quality, execution trajectory, task outcome**. That is a direct evaluation of
the curator, not just of skill use. Licence and run command unconfirmed — check the repo
before relying on them.

Note the task counts drift between versions (a separate paper cites a 75-task SkillsBench
figure), so pin a version when reporting numbers.

### Independently corroborated, 2026-08-30

A systematisation-of-knowledge paper reaches the same conclusion from a survey of the field:
"curated skills can substantially improve agent success rates while **self-generated skills may
degrade them**" ([SoK: Agentic Skills, arXiv 2602.20867](https://arxiv.org/abs/2602.20867)).

Note "**degrade**", which is stronger than SkillsBench's "no benefit." Two independent sources now
support gating creation behind curation, so the evidence for that decision is **strong** rather than
moderate. It is no longer a defensible judgement call — shipping auto-creation without the gate would
be shipping against the published consensus.

The same paper supplies vocabulary worth adopting: **seven system-level design patterns** (including
metadata-driven progressive disclosure — what `find_skill` already does — executable code skills,
self-evolving libraries, and marketplace distribution) and an orthogonal **representation × scope**
taxonomy: representation is *natural language, code, policy, or hybrid*; scope is *web, OS, software
engineering, or robotics*. Ours are natural-language skills scoped to a domain none of those four
covers, which is worth noting when positioning: the media/visual vertical is genuinely absent from
the taxonomy.

---

## Prior art we should copy, not reinvent

Hermes 0.19.0 ships `agent/curator.py` (87 KB) and `agent/background_review.py` (50 KB).
We are leaving Hermes, but this is a working implementation of the exact feature, and its
module docstring states invariants that are clearly scar tissue. Adopt them:

| Hermes invariant | Why it is right |
|---|---|
| Only touches **agent-created** skills | A human-authored or purchased skill must never be silently rewritten |
| **Never auto-deletes — only archives. Archive is recoverable.** | An autonomous process that can destroy user assets will eventually destroy user assets |
| **Pinned skills bypass all auto-transitions** | The user needs an override that the machine cannot argue with |
| Uses the **auxiliary** client; never touches the main session's prompt cache | Curation is background work; it must not evict the hot cache or bill the user's turn |
| **Inactivity-triggered**, no cron daemon | Curate when idle, never mid-turn |

Its defaults, worth taking as our starting values:

```
INTERVAL_HOURS      = 168   # 7 days
MIN_IDLE_HOURS      = 2
STALE_AFTER_DAYS    = 30
ARCHIVE_AFTER_DAYS  = 90
CONSOLIDATE         = False  # opt-in; merging skills is the riskiest action
```

Note `CONSOLIDATE = False` by default. Even the team that built it does not trust
automatic skill merging enough to default it on.

Academic framing worth reading before implementing:

- **MUSE-Autoskill** (arXiv 2605.27366) — unified lifecycle: creation, memory,
  management, evaluation, refinement. Reports beating Hermes, Codex and Claude Code on
  SkillsBench and SkillLearnBench.
- **SkillOS** (arXiv 2605.06614) — frozen executor + separate curator updating an
  external `SkillRepo`. Matches our split cleanly: the turn runtime stays dumb, the
  curator is its own process.
- **SAGE** (arXiv 2512.17102, AWS Agentic AI / ACL 2026) — RL for skill-library
  self-improvement. Out of scope near-term; relevant if we ever train.
- **Skill knowledge bases** (arXiv 2604.04804) — auto-built libraries evaluated on
  AppWorld, BFCL-v3, τ-Bench.

**These give us benchmarks.** SkillsBench and SkillLearnBench mean we can *prove* Pikachu
works instead of asserting it. That is the difference between an open-source project
people adopt and one they scroll past.

---

## Proposed lifecycle

A skill moves through states. Only the curator moves it, only when idle, and never
destructively.

```
draft ──► candidate ──► active ──► stale ──► archived
              │            ▲                     │
              └── rejected ┘                     └──► (restore is always possible)
```

| State | Meaning | Entry rule |
|-------|---------|-----------|
| `draft` | Synthesised from one successful turn | Creation gate passed (below) |
| `candidate` | Reused at least once, not yet trusted | ≥1 reuse by the same user |
| `active` | Earning its place | ≥3 successful uses, success rate ≥ threshold |
| `stale` | No use in `STALE_AFTER_DAYS` | Time-based, curator |
| `archived` | Out of retrieval, fully recoverable | `ARCHIVE_AFTER_DAYS`, curator |
| `rejected` | Failed evaluation | Curator, with recorded reason |

Only `candidate` and `active` are visible to `find_skill`. That single rule is what
bounds library drift: the retrieval set grows with *demonstrated value*, not with volume.

### The creation gate

Do not synthesise a skill from every turn. Require all of:

1. The turn **succeeded** — artifact produced, not refunded.
2. The turn was **non-trivial** — more than N tool calls, or a multi-step recipe.
3. It is **not a near-duplicate** — embedding similarity against existing skills below a
   threshold. This is the single most important anti-drift check.
4. It is **parameterisable** — the recipe generalises beyond the literal prompt. A
   one-off prompt is not a skill.

Reject with a recorded reason. The rejection log is training data for tuning the gate.

#### Check 3 is doing more work than originally credited

The near-duplicate check was written as an anti-drift measure — stop the library filling with
redundant entries. It turns out to guard a second, more serious failure: **skill-selection accuracy
collapses on semantic confusability**, not on library size
([arXiv 2601.04748](https://arxiv.org/abs/2601.04748), and see C7 in `09-design-constraints.md`).

So the same embedding comparison should also run **when a user authors or imports a skill**, not only
when the agent distils one — and it should warn rather than silently reject, since a human may have a
good reason for two similar skills. The comparison is scoped to the **partition** the skill lands in,
because that is the set the model actually selects from.

### Authoring rule: prohibitions beat prescriptions

From a large-scale study of rules and skills for coding agents:

> "A clear principle for safer agent configuration: **constrain what agents must not do, rather than
> prescribing what they should.**"
> — [arXiv 2604.11088](https://arxiv.org/abs/2604.11088)

This applies to our builtin skills, to user-authored ones, and to the correction→rule compiler in
`19-feedback-and-improvement.md`. A house style skill saying "never use these three colours, never
crop tighter than this" outperforms one prescribing an ideal look — and it is also far less likely to
fight the user's actual request.

The same study found community-authored rules and skills to be a **reliability** risk, not only a
security one. That widens the case in `06-security.md`: a third-party skill can degrade output quality
without containing anything malicious at all.

### Versioning

Skills are content-addressed already (`groot_skills` has content-hash dedupe). Add:

- `version` — integer, monotonic per skill
- `parent_version` — lineage, so improvement is auditable and revertible
- `provenance` — `agent_created` | `user_authored` | `imported`
- Immutable bodies: an "improvement" writes a **new version**, never mutates the old one.

Revert must be a single UPDATE of a pointer. If improving a skill can lose the version
that worked, users will disable the feature.

### Self-improvement loop

Runs in the curator, on the auxiliary model, when idle:

1. Read outcome telemetry per skill (uses, successes, refunds, user edits, abandons).
2. For a skill with a poor success rate but real usage → propose a patched version.
3. For near-duplicate clusters → propose consolidation (**opt-in**, `CONSOLIDATE=False`).
4. For unused skills → stale, then archive.
5. Write proposals, not mutations, when the skill is public. Public catalog changes go
   through the existing moderation queue.

---

## Schema additions

Against `groot_skills`, extending rather than replacing:

```sql
lifecycle_state   text    not null default 'draft'
version           integer not null default 1
parent_version    integer
provenance        text    not null default 'user_authored'
use_count         integer not null default 0
success_count     integer not null default 0
last_used_at      timestamptz
pinned            boolean not null default false
embedding         vector(N)          -- dedupe + retrieval, see 04-memory.md
```

Generate with `alembic revision --autogenerate`. Register any new model in
`app/models/__init__.py` or `SQLModel.metadata` omits it and the next autogenerate emits
`DROP TABLE`.

---

## What this does NOT include

- No RL. SAGE is interesting; we are not training a curator.
- No cross-user skill learning without explicit publish. One user's private workflow must
  never leak into another user's retrieval set.
- No automatic promotion of an agent-created skill to the **public** catalog. Public
  requires a human reviewer, because the scanner cannot catch paraphrased injection
  (`06-security.md`).
