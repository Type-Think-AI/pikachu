# 17 — Standards and Interop

Researched 2026-08-30. **This supersedes the USP framing in `16-sdk-plan.md`**, which argued for
canvas + curated learning + end-user agent creation. Those survive as features. The *position*
changes, because the standards landscape has a hole in exactly our shape.

---

## The find that reframes everything

**Agent Plugins 1.0.0** ([agent-plugins.org](https://agent-plugins.org/specification)) is an open,
vendor-neutral spec for packaging Agent Skills **and** MCP servers into one portable directory.
Its TSC of Core Maintainers is **Amazon, Cursor, Microsoft, OpenAI, Vercel — and Google joined as
a Core Maintainer on 2026-08-06**
([announcement](https://developers.googleblog.com/agent-plugins-package-your-skills-tools-and-more/)).

A plugin is a directory, and the restraint is the point:

```
reports-plugin/
├── plugin.json            # two lines of substance: $schema + name
├── skills/
│   └── summarize/
│       ├── SKILL.md
│       ├── scripts/
│       └── references/
├── mcp.json               # explicit type per entry — stdio | Streamable HTTP | legacy HTTP+SSE
└── com.example.client/    # reverse-domain namespace, owned by one client
```

Design properties worth copying wholesale:

- **Fixed locations.** `plugin.json` *cannot* relocate components or declare them inline. "There is
  no discovery path to configure and no precedence order to learn."
- **Independent failure.** "A `mcp.json` server that fails to start doesn't take the plugin's skills
  down with it — the client skips that entry, keeps loading, and reports the failure."
- **A legitimate escape hatch.** The reverse-domain dir is where non-portable client extensions go,
  so the portable core stays small.
- **Explicit transport type**, so a client never infers a transport from a config's shape.

### And here is the hole

Verbatim from the announcement:

> "Agent Plugins v1 is a package format and nothing more. It defines **no install mechanism, no
> distribution protocol, no permission model, no sandboxing requirements, no trust or provenance
> verification, and no user experience.** Those are named openly in the project's future
> considerations."

**That list is our existing work.** We already have, working and covered by 43 property tests:

| Agent Plugins omits | We have |
|---|---|
| permission model | P3 — effective toolset ⊆ fixed allowlist ∩ declared, enforced per step |
| sandboxing requirements | `bash`/`terminal`/`read_file`/`browser` stripped into a recorded `removed_tools` |
| trust / provenance verification | injection scanner, moderation queue, foreign skills forced to `toolsets=[]` |

A packaging standard backed by Amazon, Microsoft, OpenAI, Google, Cursor and Vercel deliberately
declines to define the permission layer. **We are a runtime that implements the standard and fills
that layer.** That is a far stronger position than "easier crews," and it is checkable by anyone who
reads the spec.

### The gap is not theoretical — it has an incident

*Added 2026-08-30.* The argument above is a reading of a spec. Here is what happens in the gap:

> "…the **ClawHavoc campaign in which nearly 1,200 malicious skills infiltrated a major agent
> marketplace, exfiltrating API keys, cryptocurrency wallets, and browser credentials at scale.**"
> — [SoK: Agentic Skills, arXiv 2602.20867](https://arxiv.org/abs/2602.20867) (cs.CR, Feb 2026)

A packaging standard with no trust or provenance verification, plus a marketplace, produced ~1,200
malicious skills and mass credential theft. That is the strongest available evidence for this
position, and it converts the pitch from a design opinion into a response to a documented failure.

The literature also names the countermeasure — **trust-tiered execution** — which is what our
allowlist tiers implement. Adopting the term makes the claim legible to people who know the field.

Worth being precise about the claim, though: we are not claiming the standards are careless. They
scope deliberately and say so openly. We are claiming the layer they leave out is the one where the
damage happens, and that a runtime is the correct place to put it.

### Restated USP

> **Pikachu runs the open agent standards — Agent Skills, Agent Plugins, MCP, WebMCP, A2A — and
> supplies the permission, confinement and provenance layer those standards deliberately leave to
> the runtime.**

---

## The four layers, and who owns what

Google's own framing of the ecosystem, which we should adopt rather than reinvent:

| Job | Standard | Our module |
|---|---|---|
| **Find it** | [Agentic Resource Discovery](https://agenticresourcediscovery.org/) — "what is available for this task?", treats a Plugin as a first-class resource type alongside agents, MCP servers, Skills; sits entirely before invocation | `discovery/` |
| **Describe it** | [AI Catalog](https://github.com/Agent-Card/ai-catalog) — the entry format ARD indexes; a [proposed change](https://github.com/Agent-Card/ai-catalog/pull/93) registers `application/agent-plugins+json` | `discovery/` |
| **Package it** | Agent Plugins 1.0.0 | `plugins/` |
| **Run it** | MCP + Agent Skills | `mcp/`, `skills/` |

"Each layer is independently useful and independently adoptable." Our modules must be too.

---

## Standard-by-standard conformance targets

### Agent Skills — [agentskills.io](https://agentskills.io/home)

Already the format our builtin skills use (`name`, `description`, `license`, `metadata`, plus a
markdown body; `scripts/`, `references/`, `assets/`; progressive disclosure). We are close to
conformant already.

**Target:** read and write spec-conformant skills, with progressive disclosure honoured (advertise
name → load instructions → read resources → run scripts). **Our addition:** the confinement layer,
because the spec says nothing about what a skill is allowed to reach.

### Agent Plugins 1.0.0

**Target:** load a plugin directory — `plugin.json`, `skills/*`, `mcp.json` — with fixed locations,
explicit transports, and independent component failure. Publish our own tool packs as plugins.

**Our addition:** a plugin from a stranger passes the same `FIXED_ALLOWLIST ∩ declared`
intersection as a skill. The spec has no opinion here; we must.

### MCP 2026-07-28 — [spec](https://modelcontextprotocol.io/specification/2026-07-28)

Verified facts that constrain implementation:

- Protocol is **stateless** — no `initialize` handshake; version + capabilities travel in `_meta`.
- **`server/discover` is required**, replacing capability probing.
- **MRTR** replaces server→client calls: return `resultType: "input_required"`.
- Every result carries `resultType: "complete"` or `"input_required"`.
- **Deprecated:** Roots, Sampling, Logging. Do not adopt in new code.
- Tasks moved to extension `io.modelcontextprotocol/tasks`.

Auth, which the user named explicitly:

- OAuth 2.1. The MCP server is a **resource server**, never an authenticator. It returns **401
  pointing at protected resource metadata (RFC 9728)**, publishes that metadata so clients find the
  authorization server, and **rejects any token not issued for it** (audience check).
- Authorization-code flow with **PKCE**, plus an **RFC 8707 `resource` parameter** on both the
  authorization and token requests.
- **2026-07-28 deprecated Dynamic Client Registration** in favour of **Client ID Metadata
  Documents**; DCR stays for back-compat for at least 12 months.

**Target:** be a conformant MCP **client** first (consume user-connected servers), then a server.
Client-side auth means implementing the 401 → metadata → PKCE + resource-indicator dance.

### WebMCP — [webmachinelearning/webmcp](https://github.com/webmachinelearning/webmcp)

Browser-side. Hard-won specifics already in our lessons: `document.modelContext`; tool `execute()`
**must** return the content envelope `{content:[{type:'text',text}]}` not a bare string;
`Origin-Agent-Cluster: ?1` is a **hard requirement** (`?0` disables WebMCP outright); never add
`tools=()` to Permissions-Policy; declarative form attributes are **bare** (`toolname`, not
`data-toolname`); Canary expects a **pre-stringified JSON string** for `executeTool` args despite
the IDL saying object.

**Target:** expose a Pikachu agent's tools to a page, and consume page-registered tools. This is the
one standard where we already have production experience and a conformance harness.

### A2A — agent-to-agent

- Google-originated, donated to the Linux Foundation 2025, now under the **Agentic AI Foundation**
  alongside MCP; **v1.0 in March 2026**; **150+ organisations**; production use at Microsoft, AWS,
  Salesforce, SAP, ServiceNow.
- v1.0 added **multi-protocol bindings, version negotiation, multi-tenancy, and cryptographically
  signed Agent Cards** — a signed card authenticates identity and metadata against a trusted key.
- Discovery is via an **Agent Card at a well-known URI**. Note the gap: A2A "does not define how
  agents on the same host or local network find each other in the first place" — there is an
  [IETF draft for DNS-SD](https://datatracker.ietf.org/doc/html/draft-zhao-a2a-dns-sd-00).

**Target:** publish a signed Agent Card per user-created agent, and consume remote A2A agents as
delegation targets.

**Important scoping note:** A2A is for **cross-boundary** agents — different vendors, different
orgs. It is *not* how our own in-process crew coordinates. Internal coordination stays the canvas
(`15-extensibility.md`); A2A is the door to the outside. Conflating them would reintroduce the
message-passing topology we refused.

---

## What this does to earlier docs

| Doc | Status |
|---|---|
| `16-sdk-plan.md` | **USP section superseded** by this doc. Its competitive audit of CrewAI/Agno and its phase ordering still stand. |
| `15-extensibility.md` | Still correct. "User tools connect as MCP" is now backed by a standard rather than a preference; the canvas stays internal-only. |
| `14-multi-agent.md` | Still correct, with A2A added as the external-boundary case. |
| `06-security.md` | **Promoted.** It was a defensive chapter; it is now the differentiator. |

---

## Unverified / open

- The **Agent Plugins schema itself** — I read the announcement, not
  `agent-plugins.org/schemas/1.0.0/plugin.schema.json`. Field-level conformance needs that file.
- **ARD's actual protocol** — I have its role in the ecosystem, not its wire format.
- **AI Catalog entry format** and whether the `application/agent-plugins+json` PR merged.
- **Whether Pydantic AI's `MCPToolset` speaks 2026-07-28** or an earlier revision. This gates the
  `mcp/` module and is the highest-priority verification.
- **A2A ↔ Pydantic AI**: no known first-party integration. Likely ours to write.
