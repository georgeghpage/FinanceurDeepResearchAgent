# Evidence judge benchmark

`review_queue.jsonl` contains 225 **synthetic, unlabeled** research snapshots in nine categories. Example-domain URLs and statements are fixture data, not retrieved research. None of these records has been labeled by a human; this queue does not constitute the requested human-reviewed evaluation dataset.

Have reviewers independently inspect snapshots and enter `human_decision` (`READY`, `RESEARCH_MORE`, `INSUFFICIENT_EVIDENCE`), `human_gap_requirement_ids`, `reviewer`, and `review_notes`. Resolve disagreements before benchmarking. Replace or supplement fixtures with anonymized real `evaluation-*.json` snapshots from sessions. Record a mixture of complete, incomplete, contradictory, unsupported, irrelevant, outdated, unavailable and diminishing-return evidence; split calibration and held-out evaluation sets. Keep model scores out of reviewer annotations.

Run each judge against the same snapshots:

```sh
PYTHONPATH=src .venv/bin/python -m deepsearchagent.benchmark predict benchmarks/review_queue.jsonl reports/pai.jsonl --provider pai --model pai-judge
PYTHONPATH=src .venv/bin/python -m deepsearchagent.benchmark predict benchmarks/review_queue.jsonl reports/pai-plus.jsonl --provider pai --model pai-judge-plus
PYTHONPATH=src .venv/bin/python -m deepsearchagent.benchmark predict benchmarks/review_queue.jsonl reports/qwen-flash.jsonl --provider aliyun --model qwen-flash --base-url YOUR_VERIFIED_ENDPOINT
PYTHONPATH=src .venv/bin/python -m deepsearchagent.benchmark predict benchmarks/review_queue.jsonl reports/qwen-plus.jsonl --provider aliyun --model qwen-plus --base-url YOUR_VERIFIED_ENDPOINT
PYTHONPATH=src .venv/bin/python -m deepsearchagent.benchmark score benchmarks/review_queue.jsonl reports/pai.jsonl
```

Prediction uses live API calls and each snapshot's configurable token, time, request and monetary limits. No benchmark API calls have been run as part of implementation. Use `--json-mode json_schema` only after verifying support for the exact Qwen model, endpoint and region. PAI uses JSON Object mode by default, with application validation.

False READY rate is false approvals divided by human-labeled non-READY cases; incorrect approval rate is false approvals divided by all approvals. False RESEARCH_MORE rate is unnecessary continuation divided by human-labeled READY cases. Metrics also include valid-decision agreement, failure rate, gap precision/recall/F1, mean/p95 latency, tokens and configured monetary cost. Null cost means the deployment lacks configured pricing. Predictions retain advisory and deterministic-gated decisions.

`follow_up_query_usefulness` is annotated on each model prediction with human booleans for its actual follow-up retrievals (in suggested-query order). Dataset-level utility placeholders are not scored across different models. Until those retrievals are reviewed, utility is null. Do not interpret completeness scores as calibrated probabilities. Choose thresholds and models using low false-READY rate on held-out human labels, then query utility, latency and account-specific operating cost.
