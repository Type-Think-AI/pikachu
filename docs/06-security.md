# 06 — Security: running a stranger's skill

## Threat model

Pikachu loads `SKILL.md` documents from a **public catalog** written by people we do not
trust, mid-run, chosen by the model (`find_skill` / `load_skill`). The skill body enters
the system prompt. Therefore:

**A skill document is untrusted input that is placed in a privileged position.**

Assets at risk, in order:
1. The user's **credits** — tools that spend money.
2. The **host** — terminal/filesystem access.
3. Other users' **data** — tenant isolation.
4. The user's **intent** — a skill that quietly redirects the task.

## This threat model is evidenced, not hypothesised

Everything above was originally argued from first principles. It has since happened, at scale:

> "…the **ClawHavoc campaign in which nearly 1,200 malicious skills infiltrated a major agent
> marketplace, exfiltrating API keys, cryptocurrency wallets, and browser credentials at scale.**"
> — [SoK: Agentic Skills, arXiv 2602.20867](https://arxiv.org/abs/2602.20867) (cs.CR, Feb 2026)

Note what the payload was: **credential exfiltration** — asset class 1 and 3 above. Roughly 1,200
skills, on a real marketplace, published by people the users had no reason to distrust.

Two things follow. First, "we will review skills before publishing" is not conservatism, it is the
minimum viable posture, and the interim position in *The unsolved part* below is correct rather than
merely cautious. Second, the same paper names the countermeasure pattern — **trust-tiered
execution** — which is exactly what our allowlist tiers are. Use the industry term; it makes the
design legible to people who already know the literature.

The paper also confirms the direction of the curation gate: "curated skills can substantially improve
agent success rates while **self-generated skills may degrade them**." That is a second independent
source alongside SkillsBench (`03-skill-lifecycle.md`), which moves that finding to **strong**
evidence.

## Memory poisoning: a one-time injection becomes permanent

This is the threat that the self-improvement loop introduces, and it is not covered by the scanner.

> "**Memory evolution can convert one-time indirect injection into persistent compromise**, which
> suggests that defenses focused only on per-session prompt filtering are **not sufficient** for
> self-evolving agents."
> — [arXiv 2602.15654](https://arxiv.org/abs/2602.15654)

> "Untrusted content can be written into persistent agent state and **re-enter later sessions as an
> instruction**; the remaining systems question is how to preserve useful memory recall while
> preventing such state from justifying sensitive actions."
> — [Lineage-Guided Enforcement for LLM Agent Memory, arXiv 2605.14421](https://arxiv.org/abs/2605.14421)

**Our specific gap:** the scanner runs on **imported** skills. It does not run on **agent-generated**
ones. So the distil step in `13-self-improvement.md` is a laundering path — poison one turn, the
agent writes it into a `draft`, the curator promotes it on reuse, and the injection is now durable
*and trusted because we generated it*.

Three requirements, all new:

| # | Requirement |
|---|---|
| 1 | **Agent-generated skills go through the same scanner as imported ones.** Provenance `agent_created` confers no trust. |
| 2 | **Lineage/taint tracking.** A skill distilled from a turn that consumed untrusted tool output inherits that taint, and taint blocks promotion. |
| 3 | **Memory must never justify a sensitive action.** Authority comes from the allowlist only. No remembered content, retrieved style fact, or distilled skill can widen a tool grant — this is P3 restated across the memory boundary. |

Sequencing consequence: `guard/` (Phase 2) is a **hard prerequisite** for `curator/` (Phase 7), not
merely earlier in the list.

## The canvas is also an attack surface

Since the canvas is a shared artifact space that multiple agents write to
(`15-extensibility.md`), it inherits the blackboard threat model: **misalignment, malicious agents,
compromised communication, and data poisoning**
([arXiv 2510.14312](https://arxiv.org/html/2510.14312v1)).

Our canvas is **append-only** — artifacts are immutable, a revision is a new artifact with `parent`
set — which removes the overwrite vector that the classical mutable blackboard exposes. It does not
remove the *injection* vector: a poisoned artifact another agent reads is still poison. `guard/` must
therefore cover canvas reads, not only tool grants.


## Real escalation paths found in our own system

Both from the Lane I audit (43 property tests, committed `4d6b799f`). Both fixed. Both
worth remembering because they were subtle and neither was theoretical.

### 1. A skill could self-grant spending tools

`_resolve_store_skill` derived `produces_media` from the skill's **own**
`allowed_tools` frontmatter. Listing `generate_image` surfaced the paid PicX media
toolset. Bounded — `picx_tools.py` still charges and refunds — but the *author* controlled
whether their document got access to a paid capability.

**Lesson:** authority must never be derived from the artifact requesting it.

### 2. The terminal strip was door-dependent

`_sanitize_toolsets` normalised correctly, but the adapter's strip matched only the
literal string, so `" terminal "` and `"TERMINAL"` survived and died later at resolve.
Harmless until `load_skill` began feeding toolsets in directly — then it was a live hole.
Fixed by normalising (`strip().lower()`) in `_effective_enabled_toolsets`.

**Lesson:** the same value must be normalised identically at every entry point. A
guarantee that holds on one path and not another is not a guarantee.

## Invariants (enforced by property tests, not review)

| ID | Invariant |
|----|-----------|
| **P3** | Effective toolset = fixed allowlist **∩** declared. A skill can only ever *narrow* its authority, never widen it. |
| **P5** | Every paid operation flows through exactly one charging point, with refund on failure. |
| — | Imported foreign skills force `toolsets=[]`; a foreign document can never contribute a toolset. |
| — | `bash` / `terminal` / `read_file` / `browser` are stripped into `removed_tools` and recorded, not silently dropped. |
| — | Detected injection payloads are rejected with 422, not sanitised-and-accepted. |
| — | `find_skill` structurally cannot return private skills (`skill_catalog.search` hardcodes `_public_conditions()` with no viewer argument). |

That last one is the pattern to imitate: the safest access control is one that is
*structurally impossible* to get wrong, not one that depends on passing the right
argument.

## The unsolved part

**The scanner misses paraphrased prompt injection.** Pattern matching catches
"ignore previous instructions"; it does not catch a politely-worded paragraph that
redirects the agent's goal. Consequences:

- Auto-approve on a clean scan is **unsafe**. Public catalog entries require a human
  reviewer.
- `pending` / `approved` status is wired; the reviewer UI and written policy are not
  complete (`HANDOFF-J` has the remaining endpoints).
- Rate-limiting matters: a skill that can surface paid tools plus unlimited publishing is
  an abuse vector even with charge/refund intact.

Do not ship a fully open public catalog until the reviewer path is real. Curated or
invite-only publishing is the honest interim.

## Not yet threat-modelled

- **Skill scripts.** The agentskills.io spec allows executable scripts in a skill bundle.
  We currently strip them. If we ever execute them, that is a sandbox problem
  (container/WASM), not a scanner problem, and it deserves its own document.
- **Cross-tenant leakage through memory.** `04-memory.md` asserts one user's private
  workflow must never enter another's retrieval set; that needs a property test, not just
  a sentence. Sharper now that crews share long-term memory by design (`14-multi-agent.md`):
  the boundary is the production house, and "which house" must be structurally enforced the
  way `find_skill`'s privacy already is.
- **Detecting a poisoned lineage after the fact.** There is published work on this —
  forensic trajectory signatures find a behavioural invariant in poisoned agents
  ([arXiv 2606.30566](https://arxiv.org/html/2606.30566)) — but we have designed no detection,
  only prevention.

## Primary sources

| Source | What it gives us |
|---|---|
| [arXiv 2602.20867](https://arxiv.org/abs/2602.20867) | **ClawHavoc**, seven skill design patterns, trust-tiered execution. Read in full before finalising this doc. |
| [arXiv 2602.12430](https://arxiv.org/abs/2602.12430) | Skill abstraction layer survey, incl. securing loaded skills |
| [arXiv 2602.15654](https://arxiv.org/abs/2602.15654) | One-time injection → persistent compromise |
| [arXiv 2605.14421](https://arxiv.org/abs/2605.14421) | Lineage-guided memory enforcement — the named approach for requirement 2 |
| [arXiv 2604.11088](https://arxiv.org/abs/2604.11088) | Community-authored skills are a **reliability** risk, not only a security one |
| [arXiv 2510.14312](https://arxiv.org/html/2510.14312v1) | Blackboard attack vectors |
