# 11 — Project Structure

File-level structure. The directory-level rationale and phase ordering are in
`18-module-map-and-roadmap.md`; this is the concrete tree.

---

## Today — docs only, committed

```
picx-studio/
├── api/                    FastAPI service. The LIVE agent is here, at app/groot/
├── ui/                     Next.js
└── pikachu/                ← this project. 22 files, 3,544 lines, no code yet
    ├── PRD.md              features F1-F23, success criteria, open decisions
    ├── README.md           index
    ├── docs/               00-18, the reasoning
    └── research/
        └── prior-art.md
```

Committed on `dev` as `402be0b6` (21 docs) and `86e9dc83` (PRD).

## The live agent, unchanged

`api/app/groot/` — 8,180 lines, still on `hermes-agent 0.19.0`. Its identifiers
(`GrootAgentBackend`, `groot_skills`, `groot_runs`, `POST /groot/chat`) stay as they are until
extraction completes. See `08-naming.md` § Identifier map.

---

## Target — the package

One module per standard, plus `guard/` for the layer the standards omit.

```
pikachu/
├── PRD.md
├── README.md
├── pyproject.toml
├── docs/
├── research/
│
├── pikachu/                        ← the importable package
│   ├── __init__.py                 PUBLIC SURFACE: Agent, tool, AgentSpec, run
│   ├── py.typed
│   │
│   ├── core/                       the turn, on Pydantic AI
│   │   ├── agent.py                build an Agent from a spec
│   │   ├── turn.py                 loop phases, termination precedence
│   │   ├── context.py              prompt assembly — static prefix vs volatile suffix (P10)
│   │   ├── events.py               typed event stream (+ ArtifactProduced)
│   │   └── errors.py               exception taxonomy → billing actions
│   │
│   ├── backe             ★ THE USP — what the standards leave out
│   │   ├── allowlist.py            FIXED_ALLOWLIST, ∩ declared (P3)
│   │   ├── toolsets.py             .filtered() / PrepareTools gate construction
│   │   ├── scanner.py              injection scan (misses paraphrase — reviewer required)
│   │   ├── sanitize.py             strip bash/terminal/read_file/browser → removed_tools
│   │   └── provenannds/                   C5: one framework, plus a test double
│   │   ├── base.py                 AgentBackend protocol — THE SEAM
│   │   ├── pydantic_ai.py
│   │   └── fake.py
│   │
│   ├── guard/         ce.py           who authored, who produced, what was stripped
│   │
│   ├── skills/                     agentskills.io
│   │   ├── spec.py                 frontmatter model
│   │   ├── parser.py               SKILL.md parse
│   │   ├── disclosure.py           advertise → instructions → resources → scripts
│   │   ├── registry.py
│   │   └── store.py                SkillStore Protocol
│   │
│   ├── plugins/                    Agent Plugins 1.0.0
│   │   ├── manifest.py             plugin.json ($schema + name; cannot relocate/inline)
│   │   ├── loader.py               fixed locations, INDEPENDENT COMPONENT FAILURE
│   │   └── mcp_config.py           mcp.json, explicit transport type per entry
│   │
│   ├── mcp/                        MCP 2026-07-28
│   │   ├── client.py               stateless; version+capabilities in _meta
│   │   ├── discover.py             server/discover (required, replaces probing)
│   │   ├── transport.py            stdio | Streamable HTTP | legacy HTTP+SSE
│   │   └── mrtr.py                 resultType: complete | input_required
│   │
│   ├── authz/                      OAuth 2.1 as a client
│   │   ├── oauth.py                authorization-code + PKCE
│   │   ├── metadata.py             RFC 9728 protected resource metadata (401 → pointer)
│   │   ├── resource.py             RFC 8707 resource indicator
│   │   └── cimd.py                 Client ID Metadata Documents (DCR deprecated)
│   │
│   ├── agents/                     crews — user-created, declarative
│   │   ├── spec.py                 AgentSpec: 6 fields
│   │   ├── registry.py
│   │   ├── routing.py              trigger-match only, default-single
│   │   └── delegate.py             one direction, no topology
│   │
│   ├── canvas/                     internal coordination substrate
│   │   ├── artifact.py             id, kind, payload ref, provenance, parent
│   │   ├── graph.py                append-only; revision = new node
│   │   └── store.py                CanvasStore Protocol
│   │
│   ├── curator/                    self-improvement
│   │   ├── plan.py                 task decomposition (OTel `plan` span)
│   │   ├── gate.py                 4-check creation gate — F17, gates F16
│   │   ├── lifecycle.py            draft→candidate→active→stale→archived
│   │   └── review.py               idle-triggered, auxiliary model, archive-never-delete
│   │
│   ├── memory/
│   │   ├── tiers.py                short (working) / mid (episodic) / long (semantic+procedural)
│   │   ├── budget.py               per-tier caps, ~3,150 tok ceiling
│   │   ├── consolidate.py          mid → long promotion, confidence decay
│   │   └── store.py                MemoryStore Protocol (writes queued, never in request path)
│   │
│   ├── discovery/
│   │   ├── ard.py                  Agentic Resource Discovery
│   │   └── catalog.py              AI Catalog entries
│   │
│   ├── a2a/                        CROSS-BOUNDARY ONLY — never internal
│   │   ├── card.py                 signed Agent Card at well-known URI
│   │   └── peer.py                 remote delegation target
│   │
│   ├── webmcp/
│   │   └── bridge.py               document.modelContext; content envelope, not bare string
│   │
│   ├── billing/                    deprioritised; Protocol only
│   │   ├── protocol.py             MeteredTool: quote → reserve → capture → refund
│   │   └── noop.py                 default — tools are free
│   │
│   ├── durability/
│   │   ├── protocol.py             RunStore: start / checkpoint / resume / cancel
│   │   └── sqlite.py               reference impl, so tests need no Postgres
│   │
│   ├── telemetry/
│   │   ├── spans.py                OTel GenAI: invoke_agent, execute_tool, search_memory, plan
│   │   └── ledger.py               token ledger, cache_hit_ratio
│   │
│   └── tools/
│       ├── base.py
│       └── picx_media/             ← the ONLY place "picx" may appear
│
└── tests/
    ├── properties/                 the public contract
    │   ├── test_p3_toolset_confinement.py
    │   ├── test_p5_single_charging_point.py
    │   ├── test_p7_no_shared_agent.py
    │   ├── test_p9_no_recapture_on_resume.py
    │   └── test_p10_stable_prefix.py
    ├── conformance/
    │   ├── test_agent_skills.py
    │   ├── test_agent_plugins.py       incl. broken-mcp.json-is-skipped (S3)
    │   └── test_mcp_2026_07_28.py
    ├── unit/
    └── fixtures/
        ├── plugins/                    one valid, one with a deliberately broken mcp.json
        └── skills/                     incl. hostile ones for guard/ (S2)
```

---

## Four structural rules

**1. Everything that grants capability routes through `guard/`.** Skills, plugins, MCP servers, A2A
peers, user tools. If a module can widen authority without touching `guard/`, that is a bug, not a
shortcut.

**2. Standards modules depend on `core/`, never on each other.** `plugins/` *contains* skills and
MCP entries but must not import `skills/` or `mcp/` internals — it resolves them through `core/`
registries. Otherwise "independently adoptable" is fiction.

**3. Protocols, not implementations, in `billing/`, `durability/`, `memory/`, and the `*/store.py`
files.** PicX supplies Postgres + credits; an adopter supplies SQLite + a no-op biller. Each
omission degrades to something simpler rather than failing.

**4. `grep -r "picx" pikachu/ --include=*.py` returns hits only under `tools/picx_media/`.** That is
success criterion S7 and the honest test of whether extraction actually happened.

---

## Migration mapping

| Today in `api/app/groot/` | Lines | Becomes |
|---|---|---|
| `hermes_adapter.py` | 379 | `backends/base.py` + `backends/pydantic_ai.py` + `backends/fake.py`; `HermesBackend` deleted |
| `run_service.py` | 1,445 | `core/turn.py` — the largest single piece of work |
| `skill_*.py` (6 files) | 3,233 | `skills/` + `curator/` — the bulk of the value |
| `picx_tools.py` | 497 | `billing/protocol.py` + `tools/picx_media/` — charging splits from media calls |
| `run_store.py` | 488 | `durability/` + a PicX impl |
| `token_ledger.py` | 310 | `telemetry/ledger.py` — mostly as-is |
| `sandbox_*.py` | 386 | `guard/` or dropped — see `07-open-questions.md` Q6 |
| `agent_tools.py` | 829 | split across `tools/`, `skills/`, `guard/` |
| `router.py`, `schemas.py` | 229 | **stay in PicX** — the HTTP surface is not the library's business |
| `app/skills/adapter.py`, `scanner.py` | — | `skills/` + `guard/scanner.py` — the only load-bearing parts of the intern's store |

## Which phase creates which directory

Directories are created by the phase that puts real code in them — empty scaffolding nothing
imports is dead weight.

| Phase | Creates |
|---|---|
| 0 verification | nothing |
| 1 foundation | `core/`, `backends/`, `tests/properties/` |
| 2 guard ★ | `guard/` |
| 3 skills + plugins | `skills/`, `plugins/`, `tests/conformance/`, `tests/fixtures/` |
| 4 MCP + authz | `mcp/`, `authz/` |
| 5 canvas | `canvas/` |
| 6 agents + discovery | `agents/`, `discovery/` |
| 7 curator + memory | `curator/`, `memory/` |
| 8 external | `a2a/`, `webmcp/` |
| 9 ops | `billing/`, `durability/`, `telemetry/` |
