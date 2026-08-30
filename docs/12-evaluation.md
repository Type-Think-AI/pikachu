# 12 — Evaluation

How we know Pikachu is good, rather than asserting it. This is the layer that turns
`09-design-constraints.md`'s review tests into something a CI job can run.

---

## The load-bearing decision: two tiers, and only one of them gates

The single most useful pattern from the field
([Arize, *Evals in CI*](https://arize.com/blog/evals-in-ci-how-to-write-llm-evals-as-tests/)):
split every check into

| Tier | What it is | Effect on CI |
|---|---|---|
| **Hard invariants** | Deterministic assertions. True or false, no score. | **Fail the build.** |
| **Quality signals** | Scored, often model-judged. Noisy. | **Tracked as a trend. Never fails the build.** |

Conflating them is how eval suites become flaky and get disabled. A judge score that gates a
merge will block a good change on a bad day.

**We already have tier 1.** The 43 safety property tests are exactly hard invariants — P3
(toolset ⊆ allowlist ∩ declared), P5 (one charging point, refund on failure), P7 (no shared
agent), P9 (resume never re-captures), P10 (identical static prefix). None of them are scores.
They assert, they're deterministic, and they belong in the gate on day one.

Everything about *skill quality* is tier 2. It is a trend line, not a gate.

### One tier-2 metric earns special mention: partition confusability

**Max pairwise cosine similarity between skill descriptions within one agent's partition.**

It belongs here rather than in tier 1 because it is a *leading indicator*, not a correctness
assertion — but it is the only metric we have that predicts a failure before the failure is visible.
Skill-selection accuracy "remains stable up to a critical library size, then drops sharply," driven
by semantic confusability rather than count ([arXiv 2601.04748](https://arxiv.org/abs/2601.04748)).
Once selection starts failing, nothing errors — the agent just picks the wrong skill.

So: track it per partition over time, alert on the trend, and warn the author at skill-creation time
(C7 in `09-design-constraints.md`). Cheap to compute — the embeddings already exist for `find_skill`.

## Offline: golden datasets and held-out splits

Offline eval = curated datasets before deploy; online = scoring live traffic. Standard split
across LangSmith, Arize and Braintrust.

Worth copying: **OpenAI Evals bakes the split into the eval identifier** —
`<eval_name>.<split>.<version>`, where split is `dev` / `val` / `test`
([build-eval docs](https://github.com/openai/evals/blob/main/docs/build-eval.md)). Data is
JSONL, one object per line, with `input` and `ideal` keys. If our eval IDs carry the split, it
is structurally hard to accidentally tune on the test set.

Datasets must be **versioned** — Braintrust frames an experiment as "an immutable snapshot of
an evaluation run," which is what makes regression comparison meaningful over months.

## CI shape

From [Braintrust, *run in CI*](https://www.braintrust.dev/docs/evaluate/run-in-ci):

- **Smoke on PR** — a truncated run (`--first 20`), non-final, fast feedback.
- **Full on merge** — the whole suite.
- The runner's **non-zero exit code is the gate**. Nothing more exotic is required.

And their honest caveat, worth respecting before we set any threshold: you need *"a few weeks
of evaluation data before regression gates become reliable."* So ship tier 1 gating
immediately; let tier 2 accumulate a baseline before it gates anything, if ever.

## LLM-as-judge: usable, but know the numbers

`pydantic-evals` ships `LLMJudge` and we will use it. The primary research says use it with
eyes open — these are measured, not folklore
([Zheng et al., MT-Bench, arXiv 2306.05685](https://arxiv.org/abs/2306.05685); [Ye et al., *Justice or Prejudice?*, arXiv 2410.02736](https://arxiv.org/abs/2410.02736)):

| Bias | Measured effect |
|---|---|
| **Position** | Consistency under answer swap: Claude-v1 **23.8%**, GPT-3.5 **46.2%**, GPT-4 **65%**. Most favour the *first* position. |
| **Verbosity** | "Repetitive list" attack (doubled length, no new information) failure rate: Claude-v1 **91.3%**, GPT-3.5 **91.3%**, GPT-4 **8.7%**. |
| **Self-enhancement** | Confirmed by Ye et al.: models rate their own outputs more favourably *even when anonymised*. Their recommendation: **"avoid using the same model to generate and judge answers."** |
| **Prompt sensitivity** | GPT-4 consistency **65% → 51.2%** merely by switching to a "score" prompt. Few-shot raises it to 77.5% but costs 4× and "high consistency may not imply high accuracy." |
| **Authority / bandwagon / distraction** | Fake citations, "*n*% of people believe X is better", and an irrelevant sentence all measurably shift verdicts. |
| **Calibration** | "Predicted confidence significantly overstates actual correctness" ([arXiv 2508.06225](https://arxiv.org/abs/2508.06225)); reporting raw judge scores is "statistically problematic" ([arXiv 2511.21140](https://arxiv.org/abs/2511.21140)). |

Note the honesty in the source: Zheng explicitly writes *"our study cannot determine whether
the models exhibit a self-enhancement bias"* — the confirmation came later, from Ye et al. Do
not over-cite the original.

**Mitigations we adopt, all from the same sources:**

1. **Swap-and-average** every pairwise judgement. Position bias is the largest single effect
   and swapping is the cheapest fix.
2. **Never judge with the generating model.** Different judge model, always.
3. **Reference-guided grading** where a reference exists — it cut math grading failures from
   **70% to 15%** in the MT-Bench experiments.
4. **Pin the judge prompt and judge model version.** A prompt tweak alone moved consistency 14
   points; an unpinned judge makes trend lines meaningless.
5. **Never report a raw judge score as an accuracy figure.** Report it as a trend with the
   judge identified.

Position bias is worst exactly where our product lives: humanities **36%** consistent, writing
**42%**, versus math **86%**. Judging creative/visual output is the hard case, not the easy one.

## Benchmarks

| Benchmark | What it gives us |
|---|---|
| **SkillsBench** ([arXiv 2602.12670](https://arxiv.org/abs/2602.12670)) | The three-arm design — no skills / curated / self-generated — with deterministic verifiers. Apache-2.0, runnable via BenchFlow. Their result (self-generated = no benefit) is the bar we must beat. |
| **SkillLearnBench** ([arXiv 2604.20087](https://arxiv.org/abs/2604.20087)) | Direct evaluation of *continual skill learning* at three levels: skill quality, trajectory, outcome. This is the curator's benchmark. Licence and run command unconfirmed. |
| **BFCL v4** | Tool-calling correctness, AST + executable. `pip install bfcl-eval`. |
| **τ³-bench** | Multi-turn agent/user simulation, `pass^k` reliability metric. Note grading changed at v1.0.1 — results are not comparable across that boundary. |
| **AppWorld** | State-verified interactive coding, TGC/SGC metrics. |

`pydantic-evals` is the harness: `Case`, `Dataset`, `Evaluator.evaluate(ctx) -> float`, built-in
`IsInstance` and `LLMJudge`, and **span-based evaluators that inspect tool calls and execution
flow via OpenTelemetry traces**. That last one is why the telemetry work in
`02-architecture.md` is a prerequisite, not a nice-to-have — conventional spans are the eval
substrate.

## Ordering

1. Tier 1 in CI now — the property tests already exist and already pass.
2. Golden dataset with `dev`/`val`/`test` splits, versioned, ~30 cases to start.
3. Smoke-on-PR / full-on-merge wiring, exit code as gate.
4. Tier 2 judge scores as trends only. No threshold until a baseline exists.
5. SkillsBench three-arm run once the curator is real — that is the claim we want to make.

## Not covered here

Online evaluation (scoring live production traffic) and outcome tracking. Three research
results on those topics are unread on disk at
`/Users/yash/.kiro/crew/subagents/394cf0ce/result.txt`,
`/Users/yash/.kiro/crew/subagents/0a8272c9/result.txt` and
`/Users/yash/.kiro/crew/subagents/43714c0d/result.txt`. Offline eval and CI gating come first
regardless — there is no production traffic to score until the backend ships.
