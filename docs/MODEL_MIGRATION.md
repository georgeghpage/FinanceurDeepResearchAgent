# DeepSeek migration and reliability contracts

The default is Alibaba Cloud Model Studio `deepseek-v4.1-flash`, including the
evidence evaluator (`JUDGE_PROVIDER=research`). The server owns configuration;
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

## Recovery and budgets

`StageBackend` owns bounded recovery and per-attempt reservations. SDK automatic
retries remain disabled. Network, 408, 429, transient server failures, empty JSON
and schema failures can retry, using exponential backoff and jitter. Authentication,
quota, invalid parameters, refusals and evidence reference errors stop immediately.
Retry-After is honored through a shared cooldown, or recovery stops when the delay
cannot fit the deadline. Per-stage and overall deadlines include queueing and
recovery. One shared semaphore bounds all research model stages.

A `length` finish reason is always incomplete, even if its text parses as JSON.
Missing terminal stream metadata and network interruptions retain available JSON
content. Continuation preserves that prefix, can grow output allocation, and is
bounded by `MAX_CONTINUATIONS`, model limits and the remaining shared budget.
Exact echoed content is deduplicated; malformed or contradictory rewrites fail
validation. Reasoning-only truncation retries the same stage with more output
space, without replaying retrieval. Provider usage is settled after every attempt;
missing usage conservatively retains the whole reservation.

Generation checkpoints are atomically saved as `generation-<worker>-<schema>.json`
in the existing session directory. They contain research content and should be
protected like `state.json` and the full trace. Metadata journals omit their
content. Complete extraction records can be salvaged after failed continuation,
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
