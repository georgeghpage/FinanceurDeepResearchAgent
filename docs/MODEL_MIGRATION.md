# DeepSeek migration and reliability contracts

Research defaults to Alibaba Cloud Model Studio `deepseek-v4.1-flash`. Final writing
defaults to Aliyun `qwen3.8-flash`; evidence and final-answer evaluation default to
Aliyun `qwen3.8-max`. The server owns configuration;
the UI shows the configured model without provider/model selection or catalog
requests. Legacy CLI overrides and the separate PAI evaluator remain available.

## Verified provider compatibility

Verified on 2026-10-12 against Aliyun's
[DeepSeek API](https://www.alibabacloud.com/help/en/model-studio/deepseek-api) and
[V4.1 Flash model page](https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash):
thinking uses `extra_body={"enable_thinking": true}`, reasoning effort accepts
`low`, `high`, `max`, context is 1,000,000 tokens, and output is at most 393,216.
The adapter sends `max`, without a fixed thinking-token budget. JSON Object mode
is supported. Native prefix completion is unavailable; recovery uses ordinary
chat messages containing an explicitly untrusted checkpoint and requests its
missing JSON suffix. JSON mode is omitted for suffix requests. Final assembled
JSON must pass the original schema and all existing evidence/citation gates.

`capabilities.py` owns provider options and limits. Context/output settings may
lower documented ceilings. Unknown models receive conservative limits and do not
receive unverified thinking/reasoning options. Add verified capabilities there
when configuring another model. Endpoint availability still depends on region,
workspace and account permissions; offline tests cannot verify those.

`RESEARCH_MODEL` and backend-only `FINAL_WRITER_MODEL` can select the two roles
independently. The final writer defaults to Aliyun Qwen3.8 Flash; its provider is independently configurable. A separate writer
uses its own model limits and `FINAL_WRITER_INPUT_PRICE_PER_MILLION` /
`FINAL_WRITER_OUTPUT_PRICE_PER_MILLION` USD overrides; research rates are never
copied to a different writer. The shared budget preserves writer cost headroom
during research and search admission. Qwen3.7 Plus and Qwen3.8 Max JSON/context
ceilings are verified against provider documentation; their editorial quality
remains unmeasured. See [writer evaluation](WRITER_EVALUATION.md) for the frozen
input comparison tool and current evidence.

## Recovery and budgets

`StageBackend` owns bounded recovery and per-attempt reservations. SDK automatic
retries remain disabled. Network, 429, transient server failures, empty JSON
and schema failures can retry, using exponential backoff and jitter. Authentication,
quota, invalid parameters, refusals and evidence reference errors stop immediately.
Retry-After is honored through a shared cooldown, or recovery stops when the delay
cannot fit the deadline. Per-stage and overall deadlines include queueing and
recovery. One shared semaphore bounds all research model stages.

A `length` finish reason is always incomplete, even if its text parses as JSON.
Missing terminal stream metadata and network interruptions retain available JSON
content. Continuations default to zero. Planning, query generation, extraction and
writing salvage complete records and move forward rather than requesting more output.
Other callers can opt into bounded continuation with `MAX_CONTINUATIONS`.
Exact echoed content is deduplicated; malformed or contradictory rewrites fail
validation. Reasoning-only truncation never retries with a larger output allocation.
Request timeouts also move forward without replaying the stage. Provider usage is settled after every attempt;
missing usage conservatively retains the whole reservation.

Generation checkpoints are atomically saved as `generation-<worker>-<schema>.json`
in the existing session directory. They contain research content and should be
protected like `state.json` and the full trace. Metadata journals omit their
content. Complete extraction records are salvaged immediately after interruption,
then pass the existing source URL, requirement and verbatim-excerpt validation.
Incomplete evaluation never establishes READY. Exhausted writing recovery uses
stored, cited evidence and discloses the failure. Successful workers are merged
and persisted as they finish, before slower workers complete.

Tavily retains its existing bounded retry policy, total retry budget and cooldown.
After `SEARCH_CIRCUIT_THRESHOLD` exhausted transient logical requests, a shared
circuit pauses new operations for `SEARCH_CIRCUIT_SECONDS`. Individual transient
attempts that recover do not trip it. Successful requests reset failure counts.
Search failures remain distinct from successful empty results, and independent
queries and already collected evidence remain available.

Terminal research state is `COMPLETE`, `PARTIAL`, `FAILED`, or `CANCELLED`.
A successful evidence-backed generated report and sufficient coverage are needed
for COMPLETE; incomplete generation cannot silently become success. The result
stream carries the state, and UI/dashboard/history use it instead of inferring
success from coverage alone. Existing replay sequences, heartbeat, same-run
reconnection, cancellation and credential-safe diagnostics are preserved.

## Configuration

See `.env.example` for all defaults. Key additions are `THINKING_ENABLED`,
`REASONING_EFFORT`, `CONTEXT_TOKENS`, `MODEL_OUTPUT_TOKENS`, stage input/output
settings, `LLM_CONCURRENCY`, `LLM_RETRIES`, `MAX_CONTINUATIONS`, `LLM_BACKOFF`,
`LLM_MAX_DELAY`, `LLM_JITTER`, and search circuit settings. Total token/call/time
ceilings and writer reserves remain independently configurable. Monetary ceilings
require configured USD rates; no prices or API keys are inferred from another
provider. Existing `.env` credentials are untouched.

## Validation and operational limits

Existing unittest, mock HTTP/MCP, frontend stream and Playwright suites are reused.
Targeted tests cover real SDK request serialization, truncation, JSON continuation,
stream interruption, recovery exhaustion, Retry-After, request accounting,
context/cost guards, bounded concurrency, partial evidence salvage and circuits.
No paid live calls are made by these tests. A live region/account check remains
available through the existing opt-in integration script.

Continuation is best effort: providers may ignore a suffix instruction, in which
case the schema gate rejects it and the report remains qualified. Checkpoints do
not automatically restart a workflow after process failure; saved chats retain
interrupted status and evidence. Independently configured PAI/other evaluators
retain their existing bounded classifier policy rather than the default shared
research-adapter continuation policy. The compatibility sequential workflow
retains its original execution contract; the CLI and UI use the asynchronous
orchestrator by default. Uncooperative injected synchronous tools can finish in
daemon workers after timeout, but their late results never enter workflow state.

## Changed files and executed checks

Application changes: `config.py`, new `capabilities.py`, `llm.py`, `budget.py`,
`judge.py`, `orchestrator.py`, `researcher.py`, `provider_errors.py`,
`diagnostics.py`, `tools/retry.py`, `progress.py`, `history.py`, `sequential.py`,
`cli.py`, `ui.py`, and `web/{index.html,app.js,dashboard.js}`.
Configuration/docs: `.env.example`, `README.md`, this document, and the opt-in
probe in `scripts/check_integrations.py`.

Existing tests updated: `test_agent.py`, `test_config_prompt.py`,
`test_diagnostics.py`, `test_pricing.py`, `test_model_catalog.py`,
`test_multi_agent.py`, `test_provider_contracts.py`, `test_judge_boundaries.py`,
`test_orchestration_edges.py`, `test_reliability.py`, `test_retry_policy.py`,
`test_research_experience.py`, `test_ui.py`, `test_web.py`, `test_web.cjs`,
`test_history.py`, `test_smoke.py`, and `support_quality.py`.
Browser fixtures/expectations updated: `browser/{server.py,helpers.cjs,workflow.spec.cjs,visual.spec.cjs}`
and the existing 15 screenshot baselines.

Validation completed:

- `scripts/run_tests.py --suite all` with `TEST_DATABASE_URL` pointing to local
  PostgreSQL: **226 tests, no failures; three opt-in live smoke tests skipped**.
  The database suite uses disposable schemas and preserves application chats.
- `scripts/qa_checks.py`: fatal lint, existing formatting scope and configured
  type checks pass.
- `node --test tests/test_web.cjs`: **31 passed**, one real HTTP check skipped in
  standalone mode; that HTTP check passes through the Python suite.
- `npm run test:browser:matrix`: **63 passed** across Chromium, Firefox and WebKit.
- `npm run test:visual -- --update-snapshots=all`: **14 passed**, capturing the
  existing 15 snapshots. The default model display was visually inspected.
- Additional focused checks cover SDK recovery, streaming, configuration and
  persisted terminal states. No paid live provider calls were executed.

The standard QA artifacts are in `reports/qa/`; visual artifacts from this
migration are in `reports/qa/migration-visual/`.

## Planning and query separation

The planner now returns a concise objective, concrete steps and essential requirements.
A separate query-only stage disables thinking when the provider supports it and uses
low effort otherwise. IDs, groups and requirement mappings are built deterministically.
Both calls have 30-second and 4,096-token ceilings. The plan is persisted before query
generation, and incomplete output falls back to usable fields or the original question.
Analysis defaults to low reasoning effort; configured higher effort remains available
after retrieval. A worker timeout does not prevent classification of sibling results.

Final citation validation also disables thinking where supported, with a 20-second
and 4,096-token ceiling. If it cannot validate the draft, the run still returns the
stored source-backed findings rather than consuming the rest of the research budget.


## Qwen writer and answer evaluator update (2026-10-13)

[Qwen3.8 Flash](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash)
and [Qwen3.8 Max](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max)
support structured output, a 1,000,000-token context and 131,072-token maximum output.
Application limits remain lower and configurable. Unverified reasoning switches are
omitted. No additional summarization stage is introduced.

Final-answer evaluation is separate from evidence readiness: relevance and coverage
are primary and factual/citation support are mandatory gates. The strict schema, targeted
recovery loop and Error Center are described in README.md. `test_answer_quality.py`
covers representative false passes (topic drift, missing comparisons, unsupported numbers,
citation issues), useful revisions, targeted retrieval, judge outages and iteration limits.
These are scripted boundary regressions, not a measured Qwen-versus-PAI accuracy study.
No human-labeled final-answer dataset or paired live judge results were available; actual
false-pass/false-fail calibration still requires saved cases and human assessments.
