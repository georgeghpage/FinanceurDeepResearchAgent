# Final-writer selection and evidence-frozen comparison

Reviewed 2026-10-13. Keep `deepseek-v4.1-flash` through Aliyun for both roles by default. The architecture and prompt changes preserve original evidence regardless of model choice. No live comparison establishes a better writer, lower latency, or lower cost for this application.

## Candidates investigated

The current [Aliyun model catalog](https://www.alibabacloud.com/help/en/model-studio/models) lists all three candidates below. Each can use the existing compatible backend; no additional provider dependency is needed. Availability on the configured workspace endpoint still requires account verification.

| Candidate | Purpose in the comparison | Documented limits | Evidence status |
| --- | --- | --- | --- |
| `deepseek-v4.1-flash` | Existing research/writing baseline with the improved prompt | Existing verified capability configuration | Offline pipeline verified; writing quality unmeasured |
| `qwen3.7-plus` | Candidate for evaluating writing quality and cost | 1,000,000 context; 131,072 maximum output | Provider docs and SDK serialization verified; live quality unmeasured |
| `qwen3.8-max` | Candidate for evaluating editorial synthesis | 1,000,000 context; 131,072 maximum output | Provider docs and SDK serialization verified; live quality unmeasured |

The capability ceilings come from the official [Qwen3.7 Plus](https://www.alibabacloud.com/help/en/model-studio/qwen3-7-plus) and [Qwen3.8 Max](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max) pages. Their actual request allocations remain bounded by workflow and shared budgets. JSON Object support is documented in [Aliyun structured output](https://www.alibabacloud.com/help/en/model-studio/qwen-structured-output). The adapter continues to validate local schemas and references, and omits unverified inference switches for these candidates.

The Singapore reference prices for input up to 256k are $0.40/$1.60 per million input/output tokens for Plus and $2/$6 for Max. For an illustrative 20k input / 8k output, that is $0.0208 versus $0.088. These are pricing arithmetic, not measured run costs. Other regions, input tiers, caching and account offers differ; use your account's USD rates. Prices do not establish writing quality.

## Configuration

`RESEARCH_MODEL` selects the research role; `FINAL_WRITER_MODEL` selects the backend-only writing role. Unset final writer shares the research backend. A different writer creates an independent backend with its own documented context/output ceilings and per-call pricing. Its synthesis and citation audit still consume the shared run budgets. The browser has no final-writer selector.

When the models differ, configure `FINAL_WRITER_INPUT_PRICE_PER_MILLION` and `FINAL_WRITER_OUTPUT_PRICE_PER_MILLION` for an enabled `MAX_COST_USD`. Research model rates are never silently copied to the writer. Research and search admission preserve the separate writer's monetary headroom. Unknown pricing remains unavailable, and an enabled cost ceiling rejects unpriced calls.

## Repeatable comparison

`scripts/evaluate_writers.py` freezes one saved state into the same question, requirements, eligible findings, original source passages, prompt and schema for every candidate. It selects a common bounded input/output ceiling and saves evidence/payload/prompt hashes. It never searches or changes the original evidence. Default execution prepares artifacts offline and does not read the workspace `.env` or credentials:

```sh
.venv/bin/python scripts/evaluate_writers.py \
  --state reports/sessions/SESSION/state.json \
  --models deepseek-v4.1-flash qwen3.7-plus qwen3.8-max
```

An explicitly enabled comparison makes one paid synthesis request per model, at most three total, with no retries, continuations, searches, or extra model audits. Each model has its own optional configured dollar ceiling. Rates can be supplied as JSON keyed by model, for example `{"qwen3.8-max":{"input":2,"output":6}}`:

```sh
# Paid opt-in; this command was not executed during implementation.
.venv/bin/python scripts/evaluate_writers.py \
  --state reports/sessions/SESSION/state.json \
  --models deepseek-v4.1-flash qwen3.8-max --rates writer-rates.json --live
```

Artifacts include `input.json`, one numbered draft and Markdown report per successful candidate, and `scorecard.json`. A new directory prevents overwriting previous comparisons. Structural checks report reference validity, finding-reference recall, actual latency, calls, tokens and known cost. Invalid drafts stay failed. Numbered review documents omit candidate model names; the scorecard maps them after review. Reference recall is not a semantic fidelity score.

For at least 20 real sessions spanning quantitative comparisons, technical analysis, multilingual evidence, contradictions, partial coverage and long documents, have reviewers blind-score each output from 1–5 for:

- Factual preservation: numbers, units, dates, qualifications, disagreements, and citation entailment. Unsupported material claims are a rejection regardless of style.
- Instruction following: requested scope, evidence boundaries and useful limitations.
- Document structure: headings, connected prose, appropriate comparisons, and readable formatting.
- Analytical quality: relationships, implications, tradeoffs and clearly qualified interpretation.

Review every critical requirement, record omissions, and compare paired latency/cost alongside quality. These human fields remain null until reviewed. Promote a separate writer only after repeated improvements on identical evidence, with no increase in unsupported claims and an acceptable cost/latency tradeoff. The current implementation establishes the comparison mechanism, not model superiority.
