# 08 — Naming

## Decision (2026-08-29)

**Pikachu is the internal codename, effective now.** It replaces "Groot," which the team
rejected. Every doc in this directory uses it.

**The published package name is deliberately NOT decided.** It belongs to the marketing /
brand track, which is running separately and is currently on hold. Nothing in this
directory depends on it.

That split is the whole point:

| Thing | Cost to change | Decide when |
|---|---|---|
| Internal codename, directory name | one `git mv` | now — done |
| PyPI package name, GitHub org, public brand, domain | effectively permanent | before first publish |

So: build under `pikachu/`, settle the published name at release. No work is blocked.

---

## Identifier map — read this before renaming anything

The codename changed. **The code did not.** These are live and must keep their current
names until the extraction actually happens:

| Identifier | Where | Status |
|---|---|---|
| `GrootAgentBackend`, `HermesBackend`, `FakeBackend` | `api/app/groot/hermes_adapter.py` | live class names — do not rename |
| `groot_skills`, `groot_user_skills` | Postgres tables | live data — renaming needs a migration, no benefit |
| `groot_runs` | Postgres table, migration `9d23a1195799` | live data — same |
| `api/app/groot/**` | the running package | live import paths — 503 tests reference it |
| `POST /groot/chat` | live route | public-ish surface, clients depend on it |

The rename lands naturally when `pikachu/` becomes a real package: the new module is
written under the new name, and the old `app/groot` package is deleted at the end of the
migration in `02-architecture.md`. Renaming it *now*, in place, is churn on code we are
about to replace.

**Do not write a migration to rename a table because a doc says "Pikachu."**

---

## What we already know about published-name availability

Checked 2026-08-29, so this is settled research rather than an open question:

- `pikachu` is **taken on PyPI** ([pypi.org/project/pikachu](https://pypi.org/project/pikachu/)).
- The surrounding namespace is crowded: `pikachu-chem` (PIKAChU, a
  [published cheminformatics toolkit](https://jcheminf.biomedcentral.com/articles/10.1186/s13321-022-00616-5)),
  `pikapy`, `pytest-pikachu`, `pikkachu`, `PI-KA-CHU`, `Pikachu_Kit`, `Pokekachu`.

So `pip install pikachu` is not available to us regardless of anything else. The published
name will differ from the codename. That is normal and fine — plenty of projects ship under
a name different from their internal one.

## Trademark note

Pikachu and Pokémon are Nintendo / Creatures Inc. / GAME FREAK / The Pokémon Company marks,
and their enforcement record against third-party projects is aggressive — including a C&D
against an open source Pokémon MMO that forced the site down *and* surrender of the domain
([Engadget](https://www.engadget.com/2010-04-02-nintendo-shuts-down-fan-made-pokemon-mmo.html)).

An internal codename in a private repo is a materially different risk profile from a
published brand, which is why proceeding here is reasonable. The requirement is narrow and
firm: **the codename must not survive into the published package name, the public repo
name, or user-facing copy.** Get a trademark search from counsel before any public release.
Not legal advice.

## Constraints for the eventual published name

When marketing lands on something, check it against all of these before committing:

- [ ] Free on PyPI, npm, and as a GitHub org
- [ ] Domain available
- [ ] No adult or anatomical reading in English or major languages — this is the specific
      failure PicX is being rebranded to escape (`Pic` + `X`). It also rules out "mons"
      (*mons pubis*).
- [ ] Not phonetically confusable with an existing mark — likelihood of confusion is the
      legal test, so sounding like a mark is enough to infringe. Rules out "pokko".
- [ ] Searchable — more than two characters. Rules out "mu".
- [ ] Clean trademark search in the relevant classes
- [ ] Pronounceable, 2–3 syllables

Of the names floated so far, only **mito** clears the obvious hurdles, and it still needs a
real search.

## Themed naming: product surface yes, library API no

The Pokémon-power naming idea (`thunderbolt`, `voltage`, move-named commands) needs a split,
because one half is free and the other is the one thing you can never undo.

**Where theming is free — use it:**

| Surface | Why it's safe |
|---|---|
| UI copy, empty states, loading text | Not an interface anyone codes against |
| Docs voice, mascot, illustrations | Ours to own if the creature is ours |
| CLI *aliases* | Sugar over a real command; `--help` shows both |
| Internal release names | Never leaves the team |
| Log / telemetry event names | Internal, renameable, not a contract |

**Where it is not — the library's public API.** Three reasons, in order of severity:

1. **It is unrenameable.** A directory rename is one `git mv`. A published method name is written
   into every consumer's code. `08-naming.md` already established that the codename must not
   survive into the published package; **method names are the published package.**
2. **It breaks the simplicity constraint.** `09-design-constraints.md` C1 targets five concepts
   holdable after one README. `agent.run()` needs no explanation. `agent.thunderbolt()` requires
   the reader to learn a mapping from Pokémon moves to behaviour before they can write a line —
   pure added cognitive load, zero information.
3. **Trademark exposure moves from a codename to a product feature.** An internal codename in a
   private repo is defensible. A published API surface built from another company's move names is
   deliberate, documented, and easy to evidence.

**One specific name to drop regardless of Pokémon:** `Thunderbolt` is Intel's trademark for the
I/O standard. In a *software* context that is an active collision independent of Nintendo — a
developer reading `thunderbolt` in a Python library will think hardware. Worst of both worlds.

**One that is actually fine:** `voltage` is a generic English word with no Pokémon-exclusive
claim. If you want an energy metaphor in the product, that direction is usable — though as a
*concept name* (a credit/energy budget), not as a method name.

**Recommended split:**

```python
# library API — boring, descriptive, permanent
agent.run(...)
agent.skills.find(...)
agent.memory.recall(...)
```

```
# CLI — themed alias over the real command, both documented
pikachu zap "a watercolour fox"      →  alias of: pikachu run "..."
```

Verdict: theme the experience, keep the API boring. That is how Rust ships Ferris and still
calls it `cargo build`.

## Branding idea worth keeping regardless of the name

**Evolution** as the metaphor for the skill lifecycle. It is a common English word, staged
progression is not protectable, and it maps exactly onto the states already designed in
`03-skill-lifecycle.md`:

| Lifecycle state | Evolution framing |
|---|---|
| `draft` | just hatched — created from one successful turn |
| `candidate` | evolving — reused, earning trust |
| `active` | evolved — proven, in the retrieval set |
| `stale` → `archived` | dormant — recoverable, never deleted |

This gives the nostalgia and warmth the team wants, and it is ours to own.
