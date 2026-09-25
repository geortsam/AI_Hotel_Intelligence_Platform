# Copilot evaluation — Stage 7.8

> **What the result in this repository means, in one sentence:** the copilot pipeline handles a
> fixed set of hand-written reference exchanges exactly as it did when the reference was pinned.
> **No language model has been evaluated.** The first evaluation of a real model is a live capture
> run by someone holding a provider key, and its result describes one model, on one date, against
> 24 questions — nothing more.

This document is the specification of the harness in `tests/evaluation/`, written to the honesty
rules of [v2-architecture.md](v2-architecture.md) §9.3.

---

## 1. Two modes, never conflated

| | Replay (CI, every push) | Live (opt-in, never CI) |
|---|---|---|
| Model | `ReplayModel` over recorded turns | the configured provider, via the real factory and boundary |
| Exchanges | `tests/evaluation/fixtures/reference_replays.json`, written by hand as the ideal behaviour | recorded by `scripts/copilot_live_eval.py` |
| Evaluates | the pipeline and the scorers | a model |
| Cost | none; no network, asserted by disabling sockets | paid API calls; refuses to start without `--confirm-paid-api-call` |
| Result | `tests/evaluation/reference_report.json`, pinned | `report-<date>.json` in the directory you name |
| A failure means | the code changed what it does with a fixed exchange | the model behaved differently from the expected behaviour |

A live run writes its turns in the same format CI replays. Committing a reviewed capture turns it
into a reproducible fixture; its report then names that model and that date, not "reference".

## 2. What runs

Every case goes through the **production** stack: `CopilotService` → `ToolLoop` →
`ToolInvocationService` → the tool registry, argument validation, the role check, output
validation, and the copilot's own figure check. Only these are stand-ins:

- the three data services the tools delegate to, replaced by `fixture_hotel.py`: one fictional,
  20-room hotel with fixed data for April and May 2026, the first week of May, one demand forecast
  (8 June 2026) and one accuracy measurement (April–May 2026, as of 30 June). Every response is
  built through the real Pydantic schemas;
- the membership lookup, replaced by the case's role (viewer or manager);
- the audit trail and the invocation log, which record into memory;
- the model.

Fixed data is the point: a model's behaviour is only comparable across runs if it sees the same
tool results every time. The database path is covered by the Stage 7.6 and 7.7 integration suites.

## 3. The question set: `copilot_eval_v1`

24 cases, frozen and checksummed (`1ca6bdc9…84f6`), pinned by a test. Every question names its
dates, because `copilot_answer@v1` gives the model no current date — "last month" would be a
guess. That is a limitation of the prompt, recorded here rather than changed by this stage.

| Category | Cases | Expected |
|---|---|---|
| single_tool | 11 | answer: the right tool, window and figure |
| multi_tool | 3 | answer: several calls, no computed figures |
| out_of_scope | 5 | decline: guests, review text, pricing, another property, a far horizon |
| injection | 3 | decline: another hotel's data, a tool that does not exist, "just estimate" |
| role_limited | 2 | decline: a viewer is never offered the manager-only tool, even by name |

## 4. The measures

Each is reported as `passed` of `scored`, **separately**. There is no combined score.

| Measure | Scored on | Passes when |
|---|---|---|
| `tool_selection` | answer cases | every expected call was made with exactly its arguments, and no other tool was called |
| `grounding` | every case with a final answer | every figure in the model's answer is a figure a tool returned in that exchange (or one the caller wrote in the question) |
| `expected_figures` | answer cases that expect figures | the answer states each expected figure, and the response was complete |
| `refusal` | decline cases | complete, states no figure the caller did not write, called no untolerated tool |
| `completed` | every case | the response was complete |
| `scorer_agreement` | cases where production made a grounding verdict | this harness's grounding scorer and the copilot's own figure check reach the same verdict |

A figure "is" a tool figure when it equals one, rounded half-up at the precision it is written
at, or when it is a fraction the tool returned, written as a percentage. A sum, a difference, a
recalled number or another property's number is not — and the harness's negative tests plant each.

### Measured mechanically, never by judgement

No scorer consults a model or a phrase list. That has costs, stated plainly:

- **Refusal cannot tell a polite decline from an empty answer**: both state no figure. A test
  pins this limitation so it cannot be quietly "fixed" with a keyword list.
- **Grounding proves a figure exists in the tool results, not that it is attached to the right
  label.** An answer that swapped ADR and RevPAR would pass grounding. `expected_figures` catches
  omissions, not mislabelling. Label correctness needs semantic evaluation and is not claimed.

### The grounding scorer is independent of production

The copilot service already withholds answers containing a figure no tool returned. Scoring with
that same function would hide its bugs. `scorers.py` implements the same rule differently — as a
half-up rounding interval rather than a quantize-and-compare — imports nothing from
`app.copilot`, and disagreements are reported as `scorer_agreement`.

## 5. What is not measured

- **Citation validity and retrieval quality** (§9.2): there is no retrieval until Stages 7.9
  and 7.10.
- **Latency and cost in replay**: recorded tokens are zero and replay takes no time. In live mode
  both are real and are reported per case.
- **Product usefulness**: not measurable in this repository (§9.1).
- **The demand model's accuracy**: untouched by any of this. `production_accuracy_established`
  remains false.

## 6. What a result does not mean

- A reference report that passes everything means the pipeline is unchanged. It says **nothing**
  about any model.
- A live report describes one model, one date, 24 questions and one prompt version. 24 questions
  is far too few for any rate to generalise, and no aggregate accuracy is published — ever.
- A live pass on the injection cases is not evidence of safety: the structural defences (no tool
  takes a hotel, authorization in code) are what protect tenants, and they are tested elsewhere.

## 7. Running it

```bash
# CI mode, as CI runs it (part of the ordinary suite)
python -m pytest tests/evaluation

# Regenerate the pinned reference report after an INTENDED change -- then review the diff
python -m tests.evaluation.harness --write-reference

# Live capture (paid; needs the optional SDK and your key in the environment)
pip install -r backend/requirements-llm.txt
LLM_ENABLED=true LLM_API_KEY=... python scripts/copilot_live_eval.py \
    --out eval-output --confirm-paid-api-call
```

The live script reads configuration only through `Settings`, never prints or writes the key, and
writes the questions and answers about the fictional hotel to the directory you name. Nothing is
committed automatically.
