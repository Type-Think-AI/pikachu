# 07 — Open Questions

Decisions that block design. Ordered by how much downstream work they gate.

---

### Q1 — Does the public name stay "Pikachu"? **(blocks first publish)**

Pikachu is a Marvel/Disney character. Fine internally; a trademark problem for a public PyPI
package and GitHub org. Renaming after release means a dead package name, broken imports
and lost stars.

Decide before the repo goes public, not after.

---

### Q2 — Do we validate the two positioning claims before publishing? **(blocks the pitch)**

The whole differentiation in `00-problem-statement.md` rests on two unverified claims:

1. No mainstream framework offers a metered-tool primitive.
2. Untrusted third-party skill execution is genuinely unsolved.

Both are inferred from adjacent evidence. If either is wrong, the positioning needs
rework. Cheap to check, expensive to get wrong publicly.

---

### Q3 — Reserve-and-refund, or charge-per-step? **(shapes the billing schema)**

Carried over unresolved from the skill upgrade plan. A long multi-tool run can either:

- **Reserve** an estimated budget up front, capture actuals, refund the remainder; or
- **Charge per step** as each tool completes.

Reserve gives the user a predictable ceiling and makes cancellation clean, but needs an
estimator and holds credits hostage. Per-step is simpler and matches
`picx_tools.py` today, but a cancelled run leaves partial spend and no ceiling.

This decides the `MeteredTool` protocol shape in `02-architecture.md`. Cannot defer.

---

### Q4 — Embedding model and dimension? **(blocks memory implementation)**

Affects index size, cost, and retrieval quality. Changing it later means re-embedding
every skill and memory row. Needs deciding before the first `vector(N)` column ships.

---

### Q5 — Does the curator get its own model tier?

Hermes runs curation on an **auxiliary** client specifically so it never touches the main
session's prompt cache. We should copy that, which means a second model configuration and
a second budget. Cheap model for curation, or the same model off-peak?

---

### Q6 — Skill scripts: execute or keep stripping?

The agentskills.io spec allows executable scripts in a bundle. We strip them today. Real
capability, real sandbox problem (`06-security.md`). Ship v1 without, or design the
sandbox now so the schema does not need reworking later?

---

### Q7 — What happens to the intern's `app/skills/**` during extraction?

`groot_skills` is canonical; the store contributes only its `adapter.py` and `scanner.py`
and is otherwise retired, with the `/skills` response shape preserved so the existing UI
keeps working. When `pikachu/` lifts out, those two modules must come along — they are the
only parts still load-bearing. Confirm nothing else in `app/skills/**` is still imported
before deleting.

---

### Q8 — Open-source timing?

Options: extract-then-open (clean, slower), open-from-day-one (forces protocol
discipline, exposes half-finished work), or extract-privately-and-open-at-v1.

`02-architecture.md` argues the protocol boundaries are what make it open-sourceable at
all, so this is really "when do we stop being able to take shortcuts."

---

## Closed decisions (recorded so they are not relitigated)

| Decision | Outcome | Where |
|---|---|---|
| Framework | Pydantic AI, via a new `GrootAgentBackend` subclass alongside Hermes | `02-architecture.md` |
| Rip out Hermes immediately? | No — run both, env-var selected, A/B on property tests | `02-architecture.md` |
| Install Hermes from GitHub instead of PyPI? | No — `setup.py` on `main` actively refuses wheel/sdist builds; pip is an unsupported distribution channel from 0.20 | — |
| Vertical or general? | Vertical: content + visual creation | `01-positioning.md` |
| Skills-first as the pitch? | No — table stakes since Microsoft/Google/Pydantic shipped it | `00-problem-statement.md` |
| Choose framework on instantiation benchmarks? | No — noise against 250–600 ms round trips | `05-performance.md` |
| Skill auto-creation without a curator? | No — library drift is a published failure mode | `03-skill-lifecycle.md` |
| Canonical skill table | `groot_skills` | `03-skill-lifecycle.md` |
