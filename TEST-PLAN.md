# TEST PLAN — three independent rounds of 360° testing

Three lanes, **deliberately different angles**, run in parallel. The point is not to run more tests
of the same kind — it is that a lane looking for the wrong thing misses what a lane looking for the
right thing finds. Each round hunts a different failure class, and each writes findings the others
can check.

## The baseline no round may regress

```
offline tests   726 passed
badges          8 / 8 earned
tree            clean
```

If any round leaves the offline suite below 726 or a badge unearned, that is a finding to report,
not a thing to hide.

## The rule that makes this worth doing

> **Every finding is a runnable artifact, not a sentence.** A bug is a failing (or `xfail(strict)`)
> test. A gap is a test that cannot be written because the API does not exist. A passing
> demonstration is a script that runs. Prose alone is not a finding — the next person cannot re-check
> prose.

Each lane writes its findings to `docs/test-round-N.md` **and** its artifacts to `tests/round_N/` (or
`scripts/`), so a claim and its evidence live together.

## What the user specifically asked to see exercised

- **Create some skills** and see how a skill responds *with tools* — the skill body plus its declared
  tools, narrowed by the guard, reaching a real turn.
- **Declarative function tools** — how a plain Python function becomes a tool the agent can call, and
  how the WebMCP declarative-form path works.

Every round touches these from its own angle rather than one round owning them.

---

## Round 1 — the happy path, end to end (Teo)
**Owns:** `tests/round_1/`, `docs/test-round-1.md`, `examples/skill_with_tools.py`

Prove the things a *user* does actually work, on `FakeBackend` (deterministic, offline) with a
`--live` flag for a real run.

1. **Author three skills from scratch** as real `SKILL.md` documents — a colourist palette skill (a
   tool-using skill), a script-writer skill (text only), a sticker skill (declares a tool it is not
   granted, to show the guard narrow it). Load each through `skills.loader`, and show metadata-only
   load never reads the body.
2. **Skill + tools through a turn:** a skill whose body says "use `brand_palette` for colour" and
   whose declared tools include it — run a turn, assert the tool was called and the answer used its
   output. Then run the *same* skill with the tool removed from the agent's allowlist, and assert the
   guard omitted it and the turn still completed (degraded, not crashed).
3. **Declarative function tools:** register three plain Python functions as tools, confirm their
   schemas are generated once (the toolset cache), and that a function's docstring becomes the tool
   description the model sees.
4. **Streaming:** run one turn through `stream_turn` and assert the event order, and that
   `TurnFinished` carries the same result as the non-streaming call including timing phases.

Deliverable: `examples/skill_with_tools.py` that a human can read to learn the API, plus tests
asserting each claim. Report what worked and, honestly, anything that was awkward to do — API
friction is a finding even when nothing is broken.

## Round 2 — adversarial and edge cases (Nema)
**Owns:** `tests/round_2/`, `docs/test-round-2.md`

Try to break it. Assume every input is hostile or malformed.

1. **The S2 claim, re-attacked independently:** feed a hostile tool-grab through a skill, a plugin
   AND an MCP server, and confirm all three are narrowed by the guard. Do NOT reuse Lane T's test —
   write your own, from the attacker's side, and try to find an input that slips through. If you
   cannot break it, that is a strong result; if you can, that is the most important finding in the
   whole exercise.
2. **Injection the scanner should and should not catch:** confirm literal override phrasing and
   env-var-style credential exfiltration are caught; confirm — and state plainly — that a
   paraphrased injection passes clean, because the docstrings claim exactly that and an overclaim
   would be a defect.
3. **Malformed everything:** a `SKILL.md` with broken frontmatter, a `plugin.json` with a top-level
   unknown key, a broken `mcp.json` (skills must still load), a markdown memory file with a value
   that is not JSON. Each must raise a typed `PikachuError`, never a raw library exception or a silent
   default.
4. **The money path:** try to double-capture a reservation; try to capture after release; drive an
   `INTERRUPTED` outcome and confirm it is not silently released. Try to get `total charged > total
   reserved` by any interleaving.
5. **Taint laundering:** try to promote a tainted skill by any route — reuse count, archive-then-
   restore, distillation from a poisoned turn. All must be refused.

Deliverable: a failing/`xfail(strict)` test for anything that slips, and an explicit "attempted and
could not break" list for anything that held. The second list is as valuable as the first.

## Round 3 — live behaviour + performance regression (Kai)
**Owns:** `tests/round_3/`, `docs/test-round-3.md`, `scripts/round3_live.py`
**Uses the real model** (`google/gemini-3.7-flash`), so keep it to a handful of turns and print the
cost.

1. **Skill-with-tools against the real model:** the colourist skill from round 1, live. Does the
   model actually call `brand_palette` and quote its output? Report the timing split (framework vs
   model) and the served provider.
2. **Declarative tool, live:** a function tool the model chooses to call unprompted when the task
   needs it — proving tool *selection* works against a real model, not just tool *plumbing*.
3. **Performance regression:** re-run `scripts/profile_all.py` and compare against the recorded
   baselines (framework total, cached toolset lookup 0.24 µs, SQLite search 7.5 µs, agent
   construction 24.5 µs). Flag any real regression; explain any that is host memory pressure rather
   than code (the audit already found the SQLite search rows sit ~3.5× under load — do not re-report
   that as new).
4. **Cache, one more honest look:** confirm the S1 negative reproduces, and note the caveat.

Deliverable: `scripts/round3_live.py` that anyone can re-run, the numbers, and a clear verdict on
whether live tool-calling behaviour matches the offline fakes.

---

## Cross-check — the lanes grade each other

After the three rounds land, each finding is checked against the others:

- Round 2's adversarial S2 result is checked against Round 1's happy-path S2 — do they agree the
  guard narrows correctly?
- Round 3's live tool-calling is checked against Round 1's `FakeBackend` behaviour — does the fake
  faithfully model the real thing, or did the fake hide something?
- Any regression Round 3 finds is checked against Round 2's edge cases — is it a real bug or an
  artefact of a hostile input?

The integrator (me) does this cross-check and writes the verdict in `docs/test-summary.md`: which
findings are real, which lane found what, and whether Pikachu is ready to integrate into picx-studio
or has blockers that must be fixed first.

## Definition of done

- Three round docs + a summary, each finding backed by a runnable artifact
- The 726/8-badge baseline still holds (or a regression is reported with a failing test)
- A clear go / no-go verdict for picx-studio integration, with any blocker named
- Nothing committed by the lanes; the integrator commits after the cross-check
