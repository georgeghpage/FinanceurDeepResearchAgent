# Research experience and search recovery

The input is now one line by default and grows to a maximum of 160px. Enter submits,
Shift+Enter inserts a newline, and composition events are left to the input method.
Research settings, activity history, and developer diagnostics are collapsed by
default. The existing green theme, accessible focus styles, safe Markdown rendering,
source links, report downloads, chat history, cancellation, and reconnect controls
remain available. Status transitions respect reduced-motion preferences.

The existing fetch-based NDJSON protocol is preserved. `ResearchProgress` maps
actual stage starts and search outcomes into structured status messages, counts
unique retrieved source URLs, and ignores internal token events. Concurrent searches
stay in one stage; an individual retry does not override unrelated active work.
The frontend deduplicates event sequences, rejects events from another run, and
never routes reasoning, intermediate JSON, tool payloads, or findings to the answer.
Legacy saved chats get the same compact presentation from known stage events.

The answer stream adds `answer_start` and ordered `answer_delta` events containing
rendered report blocks, followed by the unchanged terminal `result` with the complete
report. Citation validation happens before blocks are sent. This preserves final
report quality but means prose begins streaming after the structured writer and
citation audit complete, rather than streaming unchecked model tokens. There are
no artificial typing delays. Original internal events remain in execution context,
saved history, and downloadable traces.

Search retries use one policy across the asynchronous pipeline, the sequential
compatibility workflow, and standalone Tavily calls. The default is four attempts,
1s initial backoff, multiplier 2, 20% jitter, and a 15s backoff cap. `Retry-After` is
honored beyond that cap when it fits the deadline. Concurrent requests share the
provider cooldown. Async delays and transport calls check cancellation; requests
and sessions have explicit deadlines and bounded concurrency. Search attempts
consume the existing request/cost budget, and `MAX_SEARCH_RETRIES` independently
bounds session retries. Configuration is documented in `.env.example` and README.

Network, timeout, rate-limit, temporary server/provider, and malformed-format
failures can retry. Authentication, validation, unsupported operations, identified
quota exhaustion, and account restrictions stop the request immediately. Tavily's
432/433 usage limits are distinct from temporary 429 rate limits, per the
[provider's error reference](https://help.tavily.com/articles/8645538886-understanding-http-errors).
Unrecognized provider errors are conservatively not retried.

Exhaustion produces a structured failure instead of a successful empty result.
Other queries continue, stored evidence is preserved, and failed query coverage is
recorded in final limitations. Empty evidence does not trigger empty extraction
calls. Metadata diagnostics retain run/invocation IDs, query hashes, attempts,
classification, latency, retry delay, and recovery/fallback outcomes without keys,
provider payloads, or reasoning.

## Key implementation files

| Area | Files |
| --- | --- |
| Interface | `src/deepsearchagent/web/index.html`, `app.js`, `style.css` |
| Status and answer stream | `src/deepsearchagent/progress.py`, `ui.py` |
| Shared retries and routing | `src/deepsearchagent/tools/retry.py`, `search.py`, `tavily.py` |
| Provider response classification | `src/deepsearchagent/provider_errors.py`, `diagnostics.py`, `tools/fetch.py` |
| Evidence continuity | `src/deepsearchagent/retrieval.py`, `orchestrator.py`, `sequential.py`, `researcher.py` |
| Configuration and contracts | `src/deepsearchagent/config.py`, `budget.py`, `models.py`, `.env.example` |
| Documentation | `README.md`, this file |

## Regression coverage

- Added `tests/test_research_experience.py`: retry classification, four-attempt
  recovery/exhaustion, capped backoff, session budgets, async cancellation, malformed
  formats, independent evidence preservation, status stability, and safe logs.
- Updated `tests/test_retry_policy.py`, `test_diagnostics.py`, `test_multi_agent.py`,
  and `test_ui.py` for the centralized retry and continuation behavior.
- Updated `tests/test_web.cjs`: hidden intermediate output, streamed report blocks,
  duplicate/stale events, stable statuses, keyboard behavior, replay, failure,
  cancellation, trace downloads, and retained diagnostic metadata.
- Expanded `tests/browser/workflow.spec.cjs` and the fixture server: actual HTTP
  retry recovery, persistent outages, partial coverage, compact/expanding input,
  responsive layout, keyboard controls, and WCAG AA checks.
- Updated and inspected 14 visual baselines; updated QA suite selection to include
  the new regression tests.

## Validation executed

| Command | Result |
| --- | --- |
| `PYTHONPATH=src .venv/bin/python scripts/run_tests.py --suite all --output reports/qa/ui-recovery` | 212 tests: 202 passed, 10 optional checks skipped, no failures/errors |
| `npm test` | 36 passed, one standalone real-HTTP test skipped; that test runs and passes through the Python suite |
| `PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearchagent-playwright npm run test:browser` | 21 Chromium tests passed, including axe WCAG AA checks |
| `PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearchagent-playwright QA_BROWSER_REPORT_DIR=reports/qa/ui-recovery-visual npm run test:visual:update` | 14 snapshots refreshed and inspected |
| `PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearchagent-playwright QA_BROWSER_REPORT_DIR=reports/qa/ui-recovery-visual npm run test:visual` | All 14 comparisons passed |
| `.venv/bin/python scripts/qa_checks.py` | Required fatal lint, formatting, and configuration/model type checks passed |
| `PYTHONPATH=src .venv/bin/mypy src/deepsearchagent/progress.py src/deepsearchagent/tools/retry.py src/deepsearchagent/tools/search.py --follow-imports=silent` | Additional type checks passed |
| `node --check src/deepsearchagent/web/app.js` | Passed |

A broader `ruff check src tests` also ran; it reports repository style-policy issues
outside the required fatal-lint gate. No unrelated lint cleanup was performed.

## Remaining limits

Live provider calls and PostgreSQL integration checks were not run; their optional
checks are skipped in this environment. Chromium was verified; Firefox/WebKit were
not run. The server keeps its existing single active UI research-session limit,
while searches inside a session still run concurrently. There is no new fallback
search provider: continuity uses configured tools, independent queries, and existing
evidence. Uncooperative synchronous adapters cannot be forcibly stopped, but their
late results cannot enter the cancelled/timed-out research state. The native Tavily
async transport and its retry waits are cancellable.
