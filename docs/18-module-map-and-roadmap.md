# 18 — Module Map and Roadmap

Modularisation principle: **one module per standard or per concern the standards leave open.** Not
arbitrary file-splitting — each module is independently useful and independently adoptable, mirroring
how the standards themselves layer (`17-standards-and-interop.md`).

---

## The module map

```
pikachu/
│
│  ── core: the turn, on Pydantic AI ────────────────────────────────
├── core/          Agent construction, turn loop, termination, events
├── backends/      PydanticAIBackend + FakeBackend  (C5: one framework)
│
│  ── standards: one module per spec ────────────────────────────────
├── skills/        Agent Skills (agentskills.io) — parse, progressive disclosure
├── plugins/       Agent Plugins 1.0.0 — plugin.json, skills/, mcp.json
├── mcp/           MCP 2026-07-28 — client first, then server
├── webmcp/        WebMCP browser bridge — expose + consume page tools
├── a2a/           A2A — signed Agent Cards, well-known URI, remote delegation
├── discovery/     ARD + AI Catalog — "what is available for this task?"
├── authz/         OAuth 2.1 resource server, RFC 9728, RFC 8707, CIMD
│
│  ── the layer the standards omit  ★ this is the USP ──────────────
├── guard/         Permission model, FIXED_ALLOWLIST ∩ declared (P3),
│                  injection scanning, provenance, removed_tools
│
│  ── product concerns ──────────────────────────────────────────────
├── canvas/        Artifact graph — internal coordination substrate
├── curator/       Self-improvement lifecycle, creation gate, promotion
├── memory/        short / mid / long tiers, budgeted retrieval
├── billing/       Metered tools (deprioritised, kept — Protocol only)
├── durability/    Run store, checkpoints keyed to paid effects
├── telemetry/     OTel GenAI spans, token ledger, cache hit ratio
└── tools/         Optional tool packs; picx_media as an extra
```

Two rules that keep this honest:

**1. `guard/` is not optional and everything routes through it.** Every path that introduces
capability — a skill, a plugin, an MCP server, an A2A peer, a user tool — passes the same
intersection. If a new module can grant authority without touching `guard/`, that is a bug.

**2. Standards modules depend on `core/`, never on each other.** `plugins/` may *contain* skills and
MCP entries but must not import `skills/` or `mcp/` internals — it resolves them through `core/`
registries. Otherwise "independently adoptable" is a fiction.

---

## Roadmap

Ordered by dependency and by how much each phase de-risks. Each has a **done-condition** that is a
demonstrable behaviour, not a code milestone.

### Phase 0 — Verification spike *(blocking, days)*

Nothing below is safe to build until these are read, not guessed:

- `agent-plugins.org/schemas/1.0.0/plugin.schema.json` — field-level conformance.
- **Does Pydantic AI's `MCPToolset` speak 2026-07-28?** Highest-priority unknown; gates `mcp/`.
- ARD wire format; AI Catalog entry format.
- Whether any A2A ↔ Pydantic AI integration exists or it is ours to write.

**Done when:** each answered with a citation, or marked UNVERIFIED with the consequence stated.

### Phase 1 — `core/` + `backends/` *(the foundation)*

`PydanticAIBackend` behind the existing one-method `GrootAgentBackend` seam, env-var selected,
A/B'd against the 503 tests + P3/P5 property tests.

**Done when:** a turn runs identically on both backends and `cache_hit_ratio` is measured on each.

### Phase 2 — `guard/` *(the USP, built early on purpose)*

Extract the confinement layer into its own module with the property tests as its public contract.
Built before the standards modules so every one of them plugs into an existing gate rather than
retrofitting.

**Done when:** a hostile skill, a hostile plugin and a hostile MCP server are all refused by the
same code path, proven by property test.

### Phase 3 — `skills/` + `plugins/`

Agent Skills conformance, then Agent Plugins loading — fixed locations, explicit transports,
**independent component failure** (a broken `mcp.json` entry must not take the skills down).

**Done when:** a third-party plugin directory loads, one deliberately broken MCP entry is skipped
and reported, and its skills still work.

### Phase 4 — `mcp/` client + `authz/`

MCP client speaking 2026-07-28: stateless, `server/discover`, `resultType`, MRTR. Auth as a client:
401 → protected resource metadata (RFC 9728) → PKCE + RFC 8707 resource indicator. Client ID
Metadata Documents, not DCR.

**Done when:** Pikachu connects to a protected third-party MCP server end-to-end.

### Phase 5 — `canvas/`

Append-only artifact graph: id, kind, payload ref, provenance (prompt, model, cost, seed,
**producing agent**), `parent`.

Also in scope, and not optional: **coordination scaffolding.** A shared artifact space alone is not
sufficient — "adding additional collaborators can lower performance when coordination structure is
absent," and the remedy is shared group memory **plus** human-in-the-loop approval gates
([arXiv 2606.18413](https://arxiv.org/html/2606.18413)). Both primitives already exist unwired
(crew-shared long-term memory; `DeferredToolResults.approvals`), so this is wiring, not new
invention. And `guard/` must cover canvas **reads** — append-only stops overwrites, not injection.

**Done when:** a storyboard agent produces frames from a script artifact it was never passed as an
argument, and a third agent joining the board does not degrade the result.

### Phase 6 — `AgentSpec` + `discovery/`

User-created agents (`14-multi-agent.md`), conservative trigger-only routing, then ARD/AI Catalog so
a crew can answer "what is available for this task?"

**Done when:** an end user creates "Script Writer" in the UI and it discovers a plugin it was not
configured with.

### Phase 7 — `curator/` + `memory/`

The self-improvement loop with its creation gate, and short/mid/long tiers with budgeted retrieval.

**Done when:** our curated-auto arm beats SkillsBench's self-generated arm — or we learn it does not.

### Phase 8 — `a2a/` + `webmcp/`

External boundaries last, because they are the least load-bearing for the film use case. Signed
Agent Cards out; remote A2A peers in. WebMCP where we already have a conformance harness.

**Done when:** a remote A2A agent is delegated to, and a Pikachu agent's tools appear in a page.

### Phase 9 — `billing/` + `durability/` + `telemetry/`

Deprioritised per direction, kept as Protocols so nothing depends on a credit system. Telemetry
earlier if the eval work needs span-based evaluators sooner (`12-evaluation.md`).

---

## Sequencing rationale

**Why `guard/` at Phase 2 rather than last.** It is the USP and it is a cross-cutting invariant.
Retrofitting a permission model onto five standards modules that already grant capability is the
classic way this goes wrong — and Agent Plugins' own future-considerations list is evidence that
bolting it on later is hard enough that a six-vendor TSC deferred it.

**`guard/` (Phase 2) is a HARD prerequisite for `curator/` (Phase 7).** *Added 2026-08-30 — this
ordering was previously correct by luck rather than by stated reason.* The self-improvement loop
distils turns into skills, and "memory evolution can convert one-time indirect injection into
persistent compromise," with per-session filtering explicitly insufficient
([arXiv 2602.15654](https://arxiv.org/abs/2602.15654)). Without `guard/` scanning
**agent-generated** skills and tracking lineage, Phase 7 is a mechanism for making injections
permanent and stamping them with our own provenance. So this is not a preference about ordering:
**Phase 7 must not ship if Phase 2's scanner does not cover agent-authored artifacts.** See
`06-security.md` and `13-self-improvement.md`.

**Why `a2a/` late.** It is cross-boundary only. Internal coordination is the canvas. Building A2A
early invites using it internally, which reintroduces the message-passing topology we refused
(`14-multi-agent.md`).

**Why verification is Phase 0.** This session has already produced several corrections from
guessed API surfaces — `history_processors` vs `ProcessHistory`, `Usage` vs `RunUsage`,
`request_tokens_limit` vs `input_tokens_limit`. Four new specs is four new opportunities for the
same mistake at higher cost.

---

## What is still missing from this plan

Named openly rather than discovered later:

- **Distribution.** Agent Plugins defines no install mechanism or distribution protocol by design.
  If users install plugins, we own that — and it is a supply-chain surface, not a download.
- **Signing and provenance.** A2A has signed Agent Cards; Agent Plugins has no provenance
  verification. If we accept third-party plugins we need a trust decision we have not made.
- **Versioning across five specs.** Each moves independently. We need a compatibility matrix and a
  policy for what happens when one revs.
- **The public name.** Still unresolved (`08-naming.md`), and now more urgent: publishing conformance
  claims against public standards means being named in other people's compatibility tables.
