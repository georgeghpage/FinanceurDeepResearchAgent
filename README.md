# Deep Research Agent

A bounded asynchronous research system using **DeepSeek V4.1 Flash through
Alibaba Cloud Model Studio** for planning and parallel extraction, **Qwen3.8 Flash**
for final writing, and **Qwen3.8 Max** for evidence and final-answer evaluation. Analysis defaults to `reasoning_effort="low"`;
query generation disables thinking on supported models (otherwise uses low effort).
Existing Tavily MCP transport, evidence store, cancellation, tracing, local UI,
and PostgreSQL history remain in use. Provider choice remains configurable.

```text
Question → isolated session/research_facts.md
         → Planner: concise objective, 2–5 steps, essential evidence requirements
         → Query generator: queries only, no thinking, up to 15 searches
         → bounded asynchronous Tavily retrieval
         → up to 3 parallel evidence extractors (16,384 output tokens each)
         → serialized evidence consolidation + atomic snapshots
         → Evidence Sufficiency Judge
             READY                 → Structural Agent
             RESEARCH_MORE         → ≤5 targeted searches → extractor → judge
             INSUFFICIENT_EVIDENCE → best-effort Structural Agent
         → citation/reference and semantic validation
         → Answer Quality Evaluator: original query + requirements + answer + evidence
             PASS                  → report
             REVISE                → targeted writer feedback
             MORE_RESEARCH         → missing requirements only → update answer
             INSUFFICIENT_EVIDENCE → accurately qualified answer
```

The orchestrator owns all transitions, shared resource reservations and termination.
No extraction, judge or writing model receives tools. `ResearchAgent.run()` is the
CLI/UI adapter; async integrations can `await ResearchAgent.arun()`.

Each extraction group starts as soon as its own searches complete. Searches share
duplicate requests and cache isolated results; actual request starts, including
retries, obey `SEARCH_REQUESTS_PER_SECOND`. Queued admission uses the run deadline,
while each transport attempt receives its own request timeout. Raw retrieved
documents remain in the evidence store; synthesis receives eligible findings and
bounded original passages.

Reports support direct rich-text editing, headings, emphasis, lists, links, tables,
undo/redo, safe formula/chart previews, Markdown export and Print / PDF. Completed
reports open ready to edit; Reading view preserves changes. Citations remain
keyboard accessible in Reading view; Ctrl/Command+Enter opens the citation at the
caret while editing. Citations show their source URL on hover. Autosave keeps a local
recovery copy and a separately revisioned PostgreSQL document. Conflicting tabs
retain their local edits and never overwrite a newer server revision. Streaming
previews stay read-only and clearly incomplete; replay preserves edited reports.
Edited documents do not modify research evidence or the original report events.

`RESEARCH_MODEL` and `FINAL_WRITER_MODEL` configure the two roles in the backend.
The writer defaults to `FINAL_WRITER_PROVIDER=aliyun`, `qwen3.8-flash`; research stays on `deepseek-v4.1-flash`. A different writer can use
`FINAL_WRITER_INPUT_PRICE_PER_MILLION` and `FINAL_WRITER_OUTPUT_PRICE_PER_MILLION`
for its own account-specific USD rates. See [writer evaluation](docs/WRITER_EVALUATION.md)
for verified candidate capabilities and an evidence-frozen comparison tool.

## Run

Requires Python 3.11+ and `uv`.

```sh
uv sync
export ALIYUN_API_KEY="your-model-studio-api-key"
export TAVILY_API_KEY="your-tavily-api-key"
uv run deepsearchagent "Compare Apple's FY2024 and FY2025 revenue drivers" \
  --output reports/apple.md --state reports/apple.json
```

`LLM_PROVIDER=aliyun` and `ALIYUN_MODEL=deepseek-v4.1-flash` are the defaults.
Set `ALIYUN_API_KEY` or `DASHSCOPE_API_KEY` and `TAVILY_API_KEY` in your ignored
`.env` or process environment. Exported variables take precedence. The UI displays
the server's configured model; provider/model selectors and automatic model
catalog discovery have been removed. CLI `--provider`, `--model`, and `--base-url`
remain available for integrations and configuration overrides.

`ALIYUN_BASE_URL` defaults to `https://maas.qianwenaiapi.com/compatible-mode/v1`.
For workspace endpoints, use your actual workspace ID and region, for example
`https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`.
Credentials must match the selected region and workspace. Moonshot remains
available with `LLM_PROVIDER=kimi` and separate Kimi credentials.

Aliyun documents V4.1 Flash thinking mode, `reasoning_effort="max"`, a 1,000,000-token
context and a 393,216-token output ceiling. No fixed `thinking_budget` is sent.
See the [official DeepSeek API reference](https://www.alibabacloud.com/help/en/model-studio/deepseek-api)
and [model limits](https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash).
The capability registry in `capabilities.py` controls inference parameters and
ceilings. Unknown models receive conservative limits and local JSON validation.
Provider-specific options stay outside orchestration.

Set `ALIYUN_INPUT_PRICE_PER_MILLION`, `ALIYUN_OUTPUT_PRICE_PER_MILLION`, and optional
cache rates using your account's **USD** prices. Unknown prices remain explicitly
unpriced; an enabled `MAX_COST_USD` ceiling refuses unpriced model calls.

See [migration and recovery details](docs/MODEL_MIGRATION.md) and `.env.example`
for configuration, checkpoints, recovery contracts and remaining limitations.

Click **Stop research** while a run is thinking, searching, or writing to cancel
it on the server. Cancellation stops new calls, interrupts the local wait, closes
model transport resources, and cancels active Tavily tasks. The run ends with
`stop_reason: cancelled`, and the existing evidence, partial report, and trace
remain available to save and reopen. A disconnected run can also be stopped;
the UI reconnects to retrieve its final result. Closing the browser alone still
leaves research running. An already-submitted provider request may still be billed,
and its final token usage may be unavailable after cancellation.

## Simple web UI

The UI stores chats in PostgreSQL. With PostgreSQL installed and its tools on
`PATH` (on macOS, `brew install postgresql@14`), start the workspace database and UI:

```sh
python3 scripts/start_postgres.py
uv run --no-editable deepsearchagent-ui
```

Open <http://127.0.0.1:8000>, enter a question, and click **Start research**.
The main page shows a compact research status derived from actual stage and tool
boundaries: planning, searching, reading sources, checking findings, writing, and
validation. The query input starts at one line, grows to 160px, submits with Enter,
and accepts new lines with Shift+Enter (IME composition never submits). Research
activity history and developer diagnostics are collapsed by default.
Source counts reflect unique retrieved URLs; there are no estimated percentages.

Internal reasoning, intermediate JSON, tool payloads, and extracted findings remain
available to orchestration and saved traces, but are not rendered in the research
conversation. **Save trace** downloads the original events for development.
The existing NDJSON stream adds typed `status`, `answer_start`, and `answer_delta`
events with the same run ID and replay sequence. The answer channel contains only
the rendered report after citation validation; it streams in blocks without fake
typing delays. The terminal `result` still carries the full report for older
clients. Because the writer returns structured JSON and runs a citation audit,
prose streaming starts after that audit, rather than exposing an unchecked draft.
Old saved chats use a compact mapping of known stage events.

The answer appears with clickable source citations; **Save report**
downloads the Markdown. Budget-limited or failed runs retain their partial answer
and limitations. Missing configuration is shown on the page.

Expand **Developer diagnostics** for the tracing dashboard. It sits beside the research chat on wide desktops and below it on smaller screens. It shows observed tokens, estimated USD cost, wall-clock run
time, and model/tool call counts and errors. Its waterfall includes every model
request and search call, with per-call duration, tokens, and cost. Select a call
for input/output, cache reads/writes, reasoning usage, first streamed text timing,
search transport steps, or failure details. Filters show model calls, tools, or
errors. The dashboard restores from saved events and does not double-count replay
or reconnection. **Save trace** includes the pricing snapshot and usage metadata.

Prices are estimates using published USD reference rates verified October 12,
2026: [Kimi pricing](https://platform.kimi.ai/docs/pricing/chat) and
[Tavily pricing](https://docs.tavily.com/documentation/api-credits). Kimi K3 uses
$3 input, $15 output, $0.30 cached input, and $3 cache writes (default 5-minute TTL)
per million tokens. Tavily advanced searches use the pay-as-you-go reference of
$0.016 per successful call. Account plans, free credits, local endpoint rates,
discounts and taxes can change actual charges. Unknown models, unavailable usage,
and failed search billing remain unpriced; totals label the known subtotal.
When cache usage is missing, input uses the uncached reference rate. Reasoning
tokens are a subset of output and are never billed a second time.

Set account-specific **USD** rates in `.env` if needed (zero is supported):
`KIMI_INPUT_PRICE_PER_MILLION`, `KIMI_OUTPUT_PRICE_PER_MILLION`,
`KIMI_CACHED_PRICE_PER_MILLION`, `KIMI_CACHE_WRITE_PRICE_PER_MILLION`, and
`TAVILY_PRICE_PER_SEARCH`. Restart the UI to apply them. Each new run saves its
rates, so later changes do not alter old estimates. Older chats without a pricing
snapshot retain timing and usage but show unknown cost.

If the connection drops, the page retries up to three times and resumes from the
last received event. Research continues in the background, and repeated requests
use the same run ID so reconnection does not start another model run. If automatic
retries fail, **Reconnect research** resumes that run while preserving its trace.
The server sends a heartbeat every ten seconds during quiet stages and keeps the
five most recent runs in memory. PostgreSQL retains all accepted questions,
assistant reports, and research events across restarts. **Your chats** lists saved
chats, with **Load older chats** for additional pages; selecting a chat restores
its question, report, trace and downloads. **New chat** clears the view. Sent
messages animate into a chat bubble, with typing dots while research runs.
Animations respect reduced-motion preferences. A chat interrupted by a server
restart retains its partial trace and is marked interrupted; it never silently
restarts paid model calls. A running chat can be reopened and reconnected.

The setup script creates an isolated PostgreSQL cluster in ignored `.postgres/`,
bound to `127.0.0.1:55432`, with a `deepsearchagent` database and role. Rerunning
the script preserves existing data. Stop it with `pg_ctl -D .postgres/data stop`.
For an existing PostgreSQL server, set `DATABASE_URL` in `.env` or your environment;
the UI creates its tables automatically, so the database role needs table creation
permission. The UI requires the database at startup and refuses to start a new
run when its chat cannot be saved. If a later write fails, the live answer is
retained and the UI asks you to download it. Chat storage includes full research
content; diagnostic JSONL logs continue to contain metadata only.

To recover older saved `.state.json`/`.md` reports, run:

```sh
PYTHONPATH=src uv run python scripts/import_chat_history.py
# Before restarting a pre-database UI, also recover its still-available traces:
PYTHONPATH=src uv run python scripts/import_chat_history.py --legacy-port 8000
```

Imports are idempotent. Older in-memory traces did not record the exact user
prompt; recovered chats display their research objective and a recovery notice.
Already-evicted traces without a saved report cannot be reconstructed from the
metadata-only diagnostic logs.

Restart the UI after updating installed application code. To force installation
of the latest local changes, use `uv run --no-editable --reinstall-package deepsearchagent deepsearchagent-ui`.
You can check whether
the running server sees both keys at <http://127.0.0.1:8000/api/status>.
This reports configuration presence only, without exposing keys or calling providers.

Failures now identify the provider (`kimi`, `aliyun`, `tavily`, or the agent), stage,
operation, and category in saved traces and partial reports. The normal UI shows a short explanation; developer diagnostics retain metadata. The result and
saved state include a structured `failure` object. Model diagnostics include
the finish reason, configured timeout/output limit, elapsed time, output and
thinking character counts, observed usage, and HTTP status when available.
Unreported token usage is marked unavailable. Tavily diagnostics identify
connection, MCP initialization, tool execution, parsing, and session cleanup.

The visible **Research outcome & issues** summary explains why a run stopped,
which stage failed, its error category and cause, and any search recovery or
planning fallback. The final web result also returns `stop_explanation`,
`issues`, `warnings`, and `unresolved`, including failures from runs that
continued with partial evidence. Call labels describe the work: **Define research
plan**, **Generate search queries**, **Search Q13: <query>**, **Read sources and
extract evidence**, **Evaluate evidence coverage**, **Write cited report**, and
**Validate report citations**. Provider names and call numbers remain in the
call details.

The UI and CLI write compact diagnostic JSONL files to `reports/logs/<run-id>.jsonl`.
These logs survive disconnections and server restarts. They contain timestamps,
provider/stage metadata, endpoint and proxy hosts, limits, usage and safe failure
details. They omit API keys, exception bodies, prompts, queries, retrieved text,
reasoning and report content. **Save trace** still downloads the full browser
trace, including research content. Use `deepsearchagent-ui --log-dir PATH` or
`deepsearchagent --log PATH` to choose the diagnostic destination. A failed log
write is reported and does not stop research. Restart the UI to enable changes.

For an opt-in live connectivity check (uses the configured paid APIs):

```sh
PYTHONPATH=src uv run python scripts/check_integrations.py
# Reproduce a small complete research run and save its state/report as well:
PYTHONPATH=src uv run python scripts/check_integrations.py --research
# Compare Tavily without the system proxy, for this test only:
PYTHONPATH=src uv run python scripts/check_integrations.py --direct-tavily
```

The live checks save logs in `reports/diagnostics/`. The offline suite never runs
these probes. Fault-injection tests cover Tavily HTTP authentication/rate/server
errors, nested MCP exceptions, TLS/proxy/timeout failures, and a successful search
followed by a failed Kimi verification call. They also cover model refusal,
reasoning-only output truncation, missing finish reasons, invalid JSON, individual
request and overall run time limits, budgets, and credential-safe log persistence.

Use `uv run --no-editable deepsearchagent-ui --port 8001` to choose another port. The server runs
only on your computer, accepts one research run at a time, and uses the existing
agent's default limits. Keep the page open while research runs. No Node.js,
frontend build is required to use the UI: the editor bundle is checked in. When
changing `frontend/`, run `npm ci --ignore-scripts` and `npm run build:web`.
The PostgreSQL driver is installed by `uv sync`.

`TAVILY_MCP_URL` defaults to `https://mcp.tavily.com/mcp/`. The client connects
directly over Streamable HTTP, authenticates with a bearer header, and calls
`tavily_search` with advanced search, five results, and raw page content enabled.
The agent uses only this search tool. It does not require a local Node.js server
or OpenAI's hosted web search.

## Structure

```text
src/deepsearchagent/
├── cli.py             # CLI, report and JSON output
├── ui.py              # Local web server and live progress stream
├── history.py         # PostgreSQL chats, messages and replayable events
├── web/               # Minimal browser UI (HTML, CSS, JavaScript)
├── config.py          # Environment settings and run limits
├── orchestrator.py    # Deterministic asynchronous controller
├── retrieval.py       # Bounded Tavily fan-out and transient retry
├── evidence_store.py  # Serialized merge, source/excerpt checks, atomic artifacts
├── budget.py          # Shared atomic token/time/request/cost reservations
├── judge.py           # Provider-agnostic evidence/answer judges and deterministic gates
├── benchmark.py       # Human-review queue, prediction and metrics
├── planner.py         # Bounded plan with source routing
├── researcher.py      # Evidence extraction and source validation
├── sequential.py      # Explicit compatibility workflow for existing integrations
├── verifier.py        # Compatibility workflow verification
├── writer.py          # Structured report and deterministic citations
├── models.py          # Tasks, sources, findings, report, run state
├── llm.py             # Compatible provider adapter, streaming and JSON checkpoints
├── capabilities.py    # Verified model limits and inference parameters
├── pricing.py         # Per-run snapshot of estimated USD model/tool rates
├── system_prompt.py   # Shared prompt plus stage instructions and JSON Schema
├── prompts/system.md  # Compatibility workflow system prompt
├── prompts/judge.md   # Relevance-focused final-answer rubric
└── tools/
    ├── search.py        # SearchProvider contract and routing
    ├── tavily.py        # Tavily MCP transport and tool call
    ├── fetch.py         # Normalize retrieved text and source metadata
    ├── documentation.py # Documentation search plug
    └── financial.py     # Financial source and numerical context guidance
```

## Compatibility documentation search

The default multi-agent pipeline retrieves all initial and targeted queries through Tavily.
The explicitly exported `SequentialResearchAgent` retains the former bounded LangGraph
workflow for existing integrations and transport regression checks. It is not the CLI/UI default.

Implement `SearchProvider.search(query, *, timeout) -> SearchResult`, or wrap a
callback with `DocumentationSearch`. It can call your documentation MCP server,
local index, or API. Return actual retrieved content and matching HTTP(S) source
URLs. Documentation search is disabled until a provider is injected; there is no
default vendor or fabricated implementation.

```python
from deepsearchagent.config import Config, require_api_key, require_tavily_key
from deepsearchagent.llm import KimiBackend
from deepsearchagent.models import SearchResult, Source
from deepsearchagent.orchestrator import SequentialResearchAgent
from deepsearchagent.tools.documentation import DocumentationSearch
from deepsearchagent.tools.search import SearchRouter
from deepsearchagent.tools.tavily import TavilyMCPSearch


def search_docs(query: str, timeout: float) -> SearchResult:
    # Replace this body with your documentation provider's search call.
    # Each result's text must be associated with its returned source URL.
    raise NotImplementedError("Connect your documentation search provider here")


config = Config.from_env()
router = SearchRouter(
    web=TavilyMCPSearch(config.tavily_mcp_url, require_tavily_key()),
    documentation=DocumentationSearch(search_docs),
)
agent = SequentialResearchAgent(KimiBackend(config, require_api_key()), config, router)
state, markdown = agent.run("Your research question")
```

The planner and verifier choose `source="web"` or `source="documentation"` only
from the injected providers. Both pass through the same evidence and citation
pipeline. `agent.graph` exposes the compiled LangGraph for inspection.

## Quality evaluator configuration

The default online judge is `JUDGE_PROVIDER=aliyun`, `JUDGE_MODEL=qwen3.8-max`.
It uses the same Aliyun endpoint and credentials as research, unless `JUDGE_BASE_URL`
is explicitly set. The provider-agnostic adapter also supports Kimi, opt-in PAI
(`JUDGE_MODEL_TOKEN` and an explicit PAI model), or `JUDGE_PROVIDER=research`.
SDK retries are disabled; the controller allows at most `JUDGE_RETRIES=1` recovery
per evaluation. A failed evaluation remains unverified and never becomes a quality pass.

Evidence readiness uses [evidence-judge.md](src/deepsearchagent/prompts/evidence-judge.md)
and the existing deterministic requirement/excerpt gates. Final-answer evaluation uses
[judge.md](src/deepsearchagent/prompts/judge.md) and a strict `QualityEvaluation` JSON contract.
The final judge sees the original question (including explicit constraints), extracted
requirements, actual rendered report, source excerpts and citation metadata, completion
status, and configurable thresholds. It assesses relevance, requirement coverage,
factual grounding, citation quality, analysis and uncertainty. It cannot verify remote
URL availability from metadata alone and must distinguish unverified from false claims.

Primary and grounding scores must be at least 4/5; the weighted overall score must be
at least 4/5. These defaults are configurable through `QUALITY_MIN_PRIMARY_SCORE`,
`QUALITY_MIN_GROUNDING_SCORE`, and `QUALITY_MIN_OVERALL_SCORE`. Scores do not establish
calibrated model accuracy. Unknown IDs, omitted requirement assessments and invalid
schemas are hard failures. A proposed PASS cannot bypass code-level evidence gates.

`QUALITY_MAX_ITERATIONS=2` bounds additional corrections. REVISE sends the previous
answer and specific feedback to the writer. MORE_RESEARCH retrieves only queries attached
to missing requirement IDs and preserves the evidence store and initial plan. All work
shares the existing time, request, token and optional cost limits. Evaluation snapshots,
advisory decisions, gated decisions and scores are retained under the session directory.
`JUDGE_PROMPT_PATH` can override the rubric; server-side gates remain mandatory.

The **Error Center** is accessible from the status icon. It retains grouped retry history,
timestamps, severity, recovery and request/correlation identifiers. Toast dismissal
preserves that history. Recovered issues stop contributing to the unresolved count.
Only a terminal failure without a usable answer opens a blocking dialog. Waterfall
rows preserve overlapping calls and parent tasks; detailed causes are labeled confirmed,
suspected or unknown. Reports contain research limitations, never operational warnings.

## Limits and output

Defaults: one or two queries per requirement, up to 15 initial queries in groups
of at most five (fewer for simple questions or small search budgets), three concurrent LLM
requests, maximum two additional rounds and five targeted queries per round,
25 search attempts, 20 model attempts, 240,000 reserved/observed tokens and 600 seconds.
Transient retries consume the same request, token, time and cost limits.
Timeouts and output limits move forward with available results instead of restarting
the stage. `MAX_CONTINUATIONS=0` is the default; planning, query generation, extraction
and writing always salvage partial output without requesting continuations.

Planning and query generation each have a 4,096-token output ceiling and a
30-second deadline (lower configured limits still apply). Output allocations are
16,384 for extraction and evaluation,
32,768 for writing, and at most 4,096 for semantic validation. The citation check
disables thinking where supported and has a 20-second deadline. Input caps are 24,000 for
planning/extraction, 32,000 for evaluation and 64,000 for writing. All are
configurable. Capability ceilings and available shared budgets can lower an
allocation. Conservative UTF-8 byte bounds
include prompt/schema overhead. Requirement-balanced evidence compaction preserves
IDs, source URLs and essential excerpts, and records omitted findings.
Complete streamed findings survive a stage deadline and pass the same source checks
as completed output. Complete report paragraphs still require semantic validation;
if writing or validation cannot finish, stored findings form a cited best-effort report.
Planning failures can fall back to the original question; query failures use the
completed plan requirements. The compact plan is saved in `research_facts.md`.
Observed provider usage replaces reservations; unavailable usage retains the full
reservation and marks `token_usage_complete=false`. Reasoning tokens are a subset
of billed output and are never counted twice.

Research reserves 48,000 tokens, one model request and 60 seconds for final writing
(reserves scale down for small budgets). Monetary limits
are optional via `MAX_COST_USD` or `--max-cost-usd`. Set USD input/output rates for
all selected models; unpriced models cannot spend against an enabled dollar ceiling.
Tavily cost uses the configured per-search rate. Cached usage is conservatively
charged at the full input rate. Unknown model prices produce incomplete cost
estimates, clearly marked with `cost_complete=false`; they are never assumed free.

On READY, insufficiency, budget exhaustion, repeated queries or negligible marginal
information gain, research transitions to writing. Each stage has bounded recovery. Every generated factual
paragraph must cite eligible finding IDs. A bounded semantic paragraph audit checks
for unsupported additions. Exhausted writing or audit recovery produces an extractive report
assembled from stored claims and validated source references, with no generation loop.
Partial and missing requirements, contradictions and classifier outages are disclosed.
Explicit cancellation prevents further writing calls.

Each request creates `reports/sessions/<unique-session-id>/` with:

- `research_facts.md`: canonical human-readable question, requirement IDs, status,
  findings, source metadata, actual excerpts, contradictions, gaps, round history and
  latest classifier decision.
- `state.json`: structured evidence and execution metadata.
- `evaluation-N.json`: classifier decision, provider/model, exact bounded input,
  evidence version and SHA-256 snapshot hash.
- `report.md`: final cited report.

`SESSION_DIR` / `--session-dir` changes the parent directory. Writes use temporary
files and atomic replacement; only the orchestrator consolidates evidence. URLs and
content are deduplicated, stable source/claim IDs remain traceable, and unknown dates
stay unknown. Source excerpts are checked against the particular cited document;
unmatched excerpts become UNVERIFIED. Citation links always come from stored metadata.
A semantic audit is a model evaluation, not independent factual verification.

`--output` and `--state` still save copies to chosen paths. CLI and UI preserve existing
trace/history behavior and also expose session artifacts, classifier outcomes, report
validation status, token usage and configured API costs. Create a new agent/backend
for each run; evidence artifacts and generation checkpoints preserve recoverable work within a live run.
Process restart recovery remains read-only; it never silently replays paid calls.

## Classifier evaluation

[Benchmark instructions](benchmarks/README.md) and `python -m deepsearchagent.benchmark`
provide model predictions and human-label metrics: false READY (primary), incorrect
approvals, unnecessary RESEARCH_MORE, gap precision/recall/F1, follow-up query utility,
latency, tokens, cost, failure rate and decision agreement.

The initial dataset contains 225 synthetic **unlabeled** review snapshots across nine
requested categories. Human reviewers must annotate these and add real session
snapshots before comparisons or threshold calibration are valid. No human labels,
model comparisons or live API results are claimed by this implementation.

## Local checks

The offline tests run the default asynchronous orchestrator and the compatibility
graph with scripted model/search providers. Local HTTP/MCP servers and optional
PostgreSQL exercise transport and persistence without paid provider calls.

```sh
uv run python -m unittest discover -s tests -v
# Include real PostgreSQL persistence checks (uses isolated disposable schemas):
TEST_DATABASE_URL=postgresql://deepsearchagent@127.0.0.1:55432/deepsearchagent \
  uv run python -m unittest discover -s tests -v
```

When Node.js is available, the suite also executes the actual frontend script
against an offline HTTP server, interrupts its fetch stream, and verifies that it
reconnects to the same run and delivers a cited report. Frontend checks cover live
thinking and JSON output, split UTF-8 packets, source links, downloads, partial
reports, duplicate events, retry limits, and errors before and after the result.
Run the component checks separately with `node --test tests/test_web.cjs` (the real
HTTP case is exercised by the Python suite). These component checks require no npm
dependencies; real browser/accessibility/visual tests require the QA dev packages.

For the full QA system, suite commands, browser installation, load/evaluation,
security scans and CI, see [QA testing](docs/qa/TESTING.md), the
[32-category matrix](docs/qa/MATRIX.md), [defects](docs/qa/DEFECTS.md) and
[release-readiness assessment](docs/qa/READINESS.md).

```sh
uv sync --frozen --group dev
npm ci --ignore-scripts
uv run --frozen python scripts/qa_checks.py
uv run --frozen python scripts/run_tests.py --suite all --coverage
uv run --frozen python scripts/qa_evaluate.py
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser:matrix
QA_BROWSER_REPORT_DIR=reports/qa/visual PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:visual
uv run --frozen python scripts/qa_report.py
```

The report records missing artifacts and unmet release gates. Default checks are
offline; live smoke requires a separate explicit opt-in. Passing scripted tests
does not establish live compatibility or semantic answer accuracy.

Implementation references: [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api),
[Tavily MCP](https://docs.tavily.com/documentation/mcp), and
[Kimi API quickstart](https://platform.kimi.ai/docs/overview) and
[Kimi JSON mode](https://platform.kimi.ai/docs/guide/use-json-mode-feature-of-kimi-api).

## Search recovery and configuration

All Tavily entry points share `tools/retry.py`; the search router owns retries for
research runs, while standalone Tavily calls use the same policy. There is no
nested retry loop. Requests default to four total attempts with 1s exponential
backoff, a multiplier of 2, up to 20% jitter, and a 15s backoff cap. `Retry-After`
can exceed the cap: it is honored if it fits the remaining deadline, otherwise
that request stops. A shared cooldown prevents concurrent requests from ignoring
the provider's wait instruction. Delays and transports are asynchronous and
cooperatively cancelled; concurrency defaults to five.

Configure `SEARCH_RETRIES`, `SEARCH_BACKOFF`, `SEARCH_BACKOFF_MULTIPLIER`,
`SEARCH_MAX_DELAY`, `SEARCH_JITTER`, `MAX_SEARCH_RETRIES`, `SEARCH_CONCURRENCY`,
`MAX_SEARCHES`, and `REQUEST_TIMEOUT` in `.env`. `MAX_SEARCH_RETRIES` bounds retries
across the session (default 25); every attempt also consumes the existing search
and cost budget. The tighter limit wins. A logical search, including queueing and
backoff, cannot outlive its request timeout or the research deadline.

Connection failures, resets, DNS errors, timeouts, HTTP 429, temporary 500/502/503/504
errors, explicit temporary tool errors, and unsupported response formats can retry.
Credentials, invalid parameters, unsupported operations, account restrictions,
and identified credit/plan exhaustion stop that request immediately. Tavily's
432/433 usage limits are treated as exhausted quota, separately from temporary
429 rate limits ([provider error reference](https://help.tavily.com/articles/8645538886-understanding-http-errors)).
Unrecognized tool errors are conservatively not retried.

Exhausted searches return a structured failure, never a successful empty result.
Independent queries continue. Previously collected sources and findings stay in
the evidence store, and failed query coverage is recorded in the report's
limitations. The existing targeted-query scheduler avoids repeating executed
queries. If no evidence is available, a controlled report explains the missing
coverage; it does not invent a fallback provider. Metadata logs retain run and
invocation IDs, a query hash, attempts, latency, failure category, retry delay, and
recovery/fallback outcomes without provider payloads or credentials.

Offline regression coverage lives in `tests/test_research_experience.py`,
`tests/test_retry_policy.py`, `tests/test_web.cjs`, and the browser workflow tests.
Run `.venv/bin/python scripts/run_tests.py --suite all`, `npm test`,
`npm run test:browser`, and `.venv/bin/python scripts/qa_checks.py`.
