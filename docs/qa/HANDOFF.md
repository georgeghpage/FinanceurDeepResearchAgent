# Latest continuation — 2026-10-13

The user's request to continue has been carried through the research/report upgrade. Use [IMPLEMENTATION_PROGRESS.md](../IMPLEMENTATION_PROGRESS.md) for the current changes and verification, and [WRITER_EVALUATION.md](../WRITER_EVALUATION.md) for backend-only model configuration and the evidence-frozen comparison tool.

Latest results: full backend **254 tests, 251 passed, three live skips, no failures/errors**; full three-engine browser matrix **84 passed** plus **six final autosave/conflict checks**; visual **16 cases / 17 PNGs passed** without baseline updates; scoped QA checks and wheel asset verification passed. No live providers were called. Orchestration branch coverage is **86.620%**, below the existing 95% release gate; production readiness and model superiority remain unestablished. The temporary debug server was stopped, existing user changes were preserved, and no commit/deployment was made.

The rest of this handoff is historical QA evidence from 2026-10-12. It does not describe the current editor or test inventory.

---

# Continuation update — 2026-10-12

The user explicitly asked to check this handoff and continue development. That request supersedes the historical stop below. The remainder of this file preserves the original unfinished snapshot; use [TESTING.md](TESTING.md), [MATRIX.md](MATRIX.md), [DEFECTS.md](DEFECTS.md) and [READINESS.md](READINESS.md) for the current implementation and results.

## Completed during continuation

- Fixed async search cancellation for adapters that do not observe the shared signal; added prompt cancellation and task-cleanup regressions.
- Corrected confirmed text contrast and invalid timeline ARIA labels, with axe checks in three engines.
- Repaired terminal-stream abort accounting, runtime live opt-in, browser fixture reset/cancellation waits and empty-quality fixture wording.
- Fixed scoped config typing and added focused classifier, orchestration deadline/cancellation/fallback/refinement and report-failure tests.
- Added CI workflows for fast checks, required real PostgreSQL, components/browser/accessibility/visual, bounded load/security scans, nightly soak and separate manual bounded live smoke.
- Added 14 visual cases/15 reviewed darwin baseline PNGs, deterministic dates/fonts and narrow duration masks. Comparisons passed after baseline generation.
- Measured 1/5/10/25/50 concurrent fake-agent workloads and a 500-session accelerated soak; added stage latency and resource measurements.
- Added Python advisory scanning (pip-audit dev dependency), npm scan artifacts, consolidated JSON/Markdown with artifact hashes, complete execution/matrix/defect/readiness docs and README commands.
- Ignored node_modules and QA caches. Preserved the already-untracked project; no commit/reset/clean or real credential inspection.

## Current verified results

- Full backend with required PostgreSQL: **202 total, 199 passed, zero failures/errors, three live skips**, 27.699s.
- Browser: **51 passed** across Chromium/Firefox/WebKit, zero retries; latest run 49.9s.
- Visual comparison: **14 cases passed** (15 PNGs), 11.8s, without update-snapshots.
- Evaluation: 14 deterministic gates (13 negatives, zero false eligible READY) and eight workflow fixtures passed. Semantic/human/live accuracy remains null.
- Line coverage: **92.496%** overall. Critical branch coverage: budget **100%**, judge **100%**, evidence_store **90.625%**, orchestrator **90.625%**. Orchestrator's 95% target remains **UNMET** and CI explicitly enforces it.
- Scoped typing, fatal lint and QA formatting passed. Formatting covers 21 QA Python files; typing covers config/models only.
- Scans: 90 Python dependencies audited, zero advisories; local editable application cannot be identified on PyPI. npm scan zero advisories.
- Load: all sessions passed; 500-session soak finished in 47.887s, p50 0.804s/p95 1.410s, fd 4→4/thread 1→1, zero temporary snapshots. Fake timings are not production SLOs.
- `scripts/qa_report.py --require-all` passes complete deterministic artifacts; release readiness remains false with the coverage gate and unverified production evidence clearly reported.

WebKit's native select emits a strict-CSP warning even in a script-free reproduction. The harness allows only the exact paired source-line diagnostic; production CSP is unchanged. Visual-only instrumentation bypasses CSP for screenshot styling. See DEFECTS/TESTING for details.

The disposable PostgreSQL cluster `/tmp/deepsearch-qa-pg/data` on port 55433 is **stopped**. No QA servers are intentionally left running. No paid/live provider tests ran; hosted CI, human labels, independent semantic answer evaluation, production SLOs and multi-hour soak remain unverified. Source files `state.json`/Markdown are audit snapshots, not durable research checkpoints. Remaining coverage/validation work is listed in READINESS.md; do not infer release readiness from these passing mocked suites.

---

# Historical stopped handoff (superseded snapshot)

# Handoff to the next model

## User instruction and authorization

The user requested implementation of the comprehensive QA system in the attached task file, then explicitly said: **“stop and leave instruction to next models.”** Work is stopped. Do not resume implementation until the user asks to continue. This handoff is intentionally an unfinished-work record, not a completion claim.

Original task attachment:
`/Users/george/.codex/attachments/08b095ed-b4d7-46be-96c6-813ecabe74f5/Pasted text.txt`

Workspace: `/Users/george/Desktop/WORKING/PROJECTS/deepsearchagent`.
Date: 2026-10-12, user timezone Asia/Shanghai.
No agents were spawned; current instructions prohibit delegation unless requested by user or applicable instructions. No goal was created.

## Repository state and guardrails

- The entire original project was already untracked in Git (`git status` showed all original files as `??`). Do not treat all untracked files as changes made by this task. Do not reset, clean, commit or overwrite the user's original work indiscriminately.
- Never print/read the real `.env` values. Offline QA patches `dotenv_values` and guards outbound socket connections/DNS. External requests are NOT evidence of mocked test success.
- This environment requires execution escalation for loopback servers, browsers, PostgreSQL and dependency downloads. Automatic review approved these actions during this turn. Use tools' normal escalation mechanism if needed; do not claim a denied sandbox run is an application failure.
- Use `UV_CACHE_DIR=/tmp/deepsearch-uv-cache` and npm cache `/tmp/deepsearch-npm-cache`; default UV cache is outside writable roots.
- Browser binaries installed successfully at `/tmp/deepsearch-playwright`; set `PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright` locally.
- No paid/live provider tests were run. Credentials may exist but are not authorization to run expensive work.
- A temporary PostgreSQL cluster was created at `/tmp/deepsearch-qa-pg/data`, loopback port 55433. It was **stopped** using `pg_ctl ... stop -m fast` after the user's stop request. No tests have run against it yet. The user's existing `.postgres` cluster was not changed.
- Browser run finished; Playwright's fixture web server exited. No QA test process was left intentionally running.

## Architecture audit completed

Read `docs/qa/ARCHITECTURE.md`, written before new tests.
Python 3.11, Pydantic, OpenAI SDK Kimi/AliYun/PAI, Tavily Streamable HTTP MCP, HTTPX, psycopg PostgreSQL, vanilla JS/CSS UI. CLI/default UI uses `orchestrator.ResearchAgent`; `sequential.py` is a compatibility LangGraph implementation. The local UI permits one active job; independent agent instances support concurrent sessions. Streaming is NDJSON, not SSE/WebSocket. No existing CI, queue or authentication system. Session artifacts are audit records, not resumable checkpoints.

Initial baseline command (needs PYTHONPATH before editable installation):
`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`
Result: **150 tests, OK, 7 PostgreSQL skips**, 19.349s. Log `/tmp/deepsearch-baseline-full.log`.
Initial sandbox-only attempt failed because loopback binds were prohibited. It was rerun with escalation and passed.

## Implemented changes

### Development dependencies

`pyproject.toml` and `uv.lock`: dev group with coverage, Hypothesis, Ruff, mypy, installed in `.venv` with UV.
`package.json` and `package-lock.json`: development-only Playwright and axe; Node built-in existing component tests retained. `npm install --ignore-scripts` reported **0 vulnerabilities for 6 npm packages**. This is NOT a Python vulnerability scan.

**Still needed:** add `node_modules/`, `.coverage*`, `.hypothesis/`, `.mypy_cache/`, `.ruff_cache/` to `.gitignore` as appropriate. `reports/` was already ignored. Avoid committing generated dependencies/artifacts accidentally.

### Shared fixtures

Extracted the existing `plan`, `AutoBackend`, `FakeAsyncSearch`, `FakeJudge` definitions from the beginning of `tests/test_multi_agent.py` into `tests/support_fixtures.py`. Existing test module now imports these names, retaining compatibility with existing imports. Tests use fake synthetic facts, not real semantic truth.

### Added test modules

- `tests/test_storage_reliability.py`: atomic fsync/replace/partial-write faults, safe session paths, existing/symlink session protection, multilingual large snapshots, provenance and source group routing.
- `tests/test_properties.py`: deterministic Hypothesis reservation/writer reserve, UTF-8 bound, unsafe protocols, duplicate plan query IDs.
- `tests/test_reliability.py`: barriers for three simultaneous extractors; zero/one/two/all worker failure cases; five concurrent independent sessions; cancellation cleanup; refinement hard cap.
- `tests/test_provider_contracts.py`: real OpenAI SDK HTTPX MockTransport request capture for Kimi/AliYun/PAI, status/error matrix, malformed/incomplete outputs, multilingual Tavily parsing.
- `tests/test_retry_policy.py`: numeric `Retry-After`, delay beyond remaining time, malformed header redaction.
- `tests/test_security.py`: forged source/requirement references, escaped model links/HTML, symlink replacement. Tests verify deterministic boundaries, NOT actual LLM resistance to injection.
- `tests/test_reporting.py`: failure/subtest/skip XML/JSON integrity, actual suite inventory, missing critical coverage gate rejection.
- `tests/test_smoke.py`: explicit opt-in live smoke, at most one request each for research model, Tavily, PAI (3 total), no full research stress. Skips clearly without enabled flag/credentials. Not executed live.

The following were added **after the last successful backend execution and remain unexecuted**:

- `tests/support_quality.py`, `tests/test_quality.py`
- `tests/fixtures/classifier_gates.v1.json`: 14 deterministic contract cases (1 positive, 13 adversarial), explicitly NOT human labels.
- `tests/fixtures/workflows.v1.json`: 8 scripted workflow regression scenarios; counts/terms/limits/reference checks, explicitly NOT real-model accuracy.
- `scripts/qa_evaluate.py`: runs these and saves deterministic scorecard JSON, semantic/live accuracy null.
- `scripts/qa_load.py`: bounded fake-provider load/soak, 1–50 concurrency, capped session count/time, p50/p95 (p99 only ≥100 samples), CPU, Python memory, peak RSS, fd/thread counts, loop lag, temp snapshots, traces. No measured baseline yet. Does not invoke real APIs.

### Test runner/reporting

`scripts/run_tests.py` uses unittest + coverage, no pytest conversion. Supports `all`, `unit`, `contract`, `integration`, `network`, `agent`, `evaluation`, `security`, `performance`, `frontend`, `e2e`, `smoke`.
- `--coverage`: branch data, XML/JSON/HTML.
- JSON results, JUnit XML, Markdown QUALITY_REPORT.
- Failure in subtests is not reported as a pass.
- No auto retries; flaky rate marked unmeasured.
- Offline external DNS/socket guard, loopback allowed.
- `--require-database` prevents silently skipping required DB configuration.
- `--require-release-coverage` requires all+coverage and fails on unmet requested targets.
- `--live` only allowed with suite smoke, paid request budget ≤3.
Potential issue to review: setup-level skip decorators are evaluated during discovery, before the runner enters the QA_LIVE_ENABLED patch. Consequently `--live` may remain skipped unless `QA_LIVE_ENABLED=1` is set before module discovery. Fix the runner to enable the opt-in flag before discovery (or runtime skips), while keeping default CI strictly offline. This was noticed at handoff and has NOT been fixed/tested.

### Real browser infrastructure

`playwright.config.cjs`: Chromium/Firefox/WebKit projects, one worker, no retries, HTML/JSON/JUnit/trace/screenshot artifacts; separate visual project planned.
`tests/browser/server.py`: actual production HTTP handler and default orchestrator, fake provider boundaries, gated workflow stages, memory history. Pure loopback fixture. `/__qa__/release` exists ONLY in fixture handler, not production code.
`tests/browser/workflow.spec.cjs`: 14 scenarios including normal report/download, refinement, failure, cancellation, two-tab busy/session isolation, reconnection, keyboard/input, axe WCAG AA, 5 responsive widths, unsafe content.
Browser binaries installed successfully.
**Visual project currently has no tests or baselines**. No long-report visual fixture yet. A screenshot test suite was planned but not written. Dark mode/tables/code blocks are not existing generated report features; document absent features instead of pretending to test them.

## Confirmed application defects and fixes

1. **EvidenceStore unsafe session path (high)**
   - Repro: construct ResearchState(session_id='../escape' or absolute path), then EvidenceStore(root).
   - Before: directory/files created outside root.
   - Fix in `src/deepsearchagent/evidence_store.py`: reject session IDs unless safe single-directory identifier (`[A-Za-z0-9][A-Za-z0-9_-]{0,79}`), before any write.
   - Regression: `test_session_id_cannot_escape_root`.
   - First repro created specific test files outside temp root; these were checked for synthetic question `q` and removed. Regression inputs now use unique temp-derived paths.

2. **Refinement hard maximum bypass (medium)**
   - Repro: Config(max_rounds=3) accepted.
   - Fix `config.py`: only 0–2 additional rounds accepted.
   - Regression: `test_refinement_hard_limit_cannot_be_relaxed_by_configuration`.

3. **Transport failure classification gaps (medium)**
   - Repro: HTTPX ReadError and socket.gaierror classified unexpected_error.
   - Fix `diagnostics.py`: HTTPX NetworkError normalized connection_error; DNS classified dns_error with safe message. Add dns_error to transient retry set.
   - Regression: `test_transport_error_classes_are_preserved_without_secrets`.

4. **Ignored Retry-After (medium)**
   - Repro: 429 Retry-After:2 produced sleep 0.1; huge Retry-After retried immediately despite deadline.
   - Fix `models.py`: safe nullable nonnegative finite retry_after_seconds in FailureDiagnostic.
   - Fix `diagnostics.py`: parse numeric/date Retry-After only for 429/503; omit malformed/nonfinite/negative headers; never store raw header.
   - Fix `retrieval.py`: max(server delay, exponential backoff + bounded jitter), enforce remaining-time before sleeping.
   - Regression: test_retry_policy.py.

No other production changes made. Create a formal `docs/qa/DEFECTS.md` with severity/reproduction/expected/actual/root cause/fix/test refs when continuing.

## Actual most recent backend result

Command (executed with escalation):
`.venv/bin/python scripts/run_tests.py --suite all --coverage`

**181 total, 171 passed, 0 failed, 0 errors, 10 skipped**, 22.723s.
Skips: 7 optional PostgreSQL tests + 3 live smoke tests.
Log: `/tmp/deepsearch-qa-second.log`.
Artifacts: `reports/qa/results.json`, `junit.xml`, `QUALITY_REPORT.md`, `coverage.json`, `coverage.xml`, `coverage-html/`.
This result predates `test_quality.py` and the two new QA scripts; do not report their execution as passing.

Measured coverage (statement and branch percentages separated):

| Component | Line % | Branch % | Requested branch gate |
| --- | --- | --- | --- |
| Overall backend | 88.905 | 81.327 | overall line ≥85 |
| budget | 98.876 | 95.455 | ≥95 |
| evidence_store | 100 | 90.625 | ≥90 |
| judge | 89.063 | 74.194 | ≥95 UNMET |
| orchestrator | 93.617 | 83.333 | ≥95 UNMET |

Release coverage gates currently **UNMET**. Do not suppress branches just to hit targets. Add meaningful missing-path tests or explicitly leave unmet.

Ruff command `.venv/bin/ruff check src tests scripts --select E9,F63,F7,F82` passed before the latest quality/load files. No formatter configuration or formatting gate completed.
Mypy command `.venv/bin/mypy src/deepsearchagent/config.py src/deepsearchagent/models.py --follow-imports=silent` FAILED with 5 arg-type errors at `config.py:176` due to `Config(**values)` dict[str, object]. This was existing typing debt exposed by the tool, not fixed. Do not claim type checking passed.

## Actual browser result and next investigation

Command:
`PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser`

**14 tests: 1 passed, 13 failed**, duration 35.339s, no retries. Chromium only. Firefox and WebKit not executed.
Log: `/tmp/deepsearch-browser-first.log`.
Artifacts: `reports/qa/browser.json`, browser-junit.xml, browser-html, browser-artifacts (failure PNG, error-context.md, trace.zip).

Many failures show the common afterEach assertion:
`requestfailed: net::ERR_ABORTED, http://127.0.0.1:8765/api/research`.
Likely harness false positive: production `readResearchStream` cancels the reader once a terminal result is consumed; Chromium can mark this deliberate stream close as an aborted network request. Inspect actual app code and each test's original failure before modifying assertions. Whitelist only verified intentional aborts after a received terminal result, while retaining actual JS errors/unexpected failures. Do NOT blanket-ignore failed research requests.
The reconnection test (which expects an interrupted request) was the sole pass. The console/failed-request fixtures currently record and attach requests and JS errors separately. `__qaExpectedDisconnect` is used for the intentional network-interruption case. Inspect cancellation expected title/status selectors too.

Axe may have additional real violations independent of the common teardown failure; inspect browser JSON/HTML. Do not assume every browser failure is only harness noise. No accessibility pass claimed.

## Recommended continuation order

1. Read this file and the complete original attachment; preserve existing user files.
2. Fix browser test-harness terminal-stream abort handling with narrow evidenced assertions. Inspect/fix confirmed accessibility or UI defects with regressions. Re-run Chromium, then Firefox and WebKit.
3. Run newly added deterministic quality tests and scripts. Fix legitimate fixture mismatches (e.g. exact refined finding count) without weakening safety assertions.
4. Start disposable DB, run full suite with TEST_DATABASE_URL=postgresql://george@127.0.0.1:55433/postgres and `--require-database`. Existing DB tests create/drop isolated random schemas. Stop cluster after use. Startup example below.
5. Add focused missing judge/orchestrator branches, date Retry-After and malformed SDK response cases, recovery/storage caveat tests as warranted.
6. Run deterministic load baselines (1,5,10,25,50 concurrency), extended soak bounded to safe local workload; report measured results, do not invent production SLOs.
7. Implement screenshot visual tests with controlled timestamps/fonts/masked real duration fields, review PNGs, store platform-specific baselines. Linux CI baseline handling must be explicit; no silent missing-baseline pass.
8. Implement CI (none written yet): PR fast validation; database/integration; Node/components/browser; nightly extended load/browser matrix; manually enabled bounded live job. Always upload failure artifacts, never silently skip mandatory DB/browser checks. Fix typing issue or clearly document scope; avoid introducing irrelevant formatting rewrites.
9. Python dependency vulnerability scan has NOT been done. Add/run appropriate scanner with machine-readable output. Do not automatically break dependency versions to silence findings.
10. Complete `docs/qa/TESTING.md`, `MATRIX.md` mapping all 32 categories with implemented/unverified/unsupported status, `DEFECTS.md`, final evidence-based release-readiness report and README commands. Human labels and live provider compatibility remain unverified; existing benchmark review queue contains 225 unlabeled snapshots.
11. Build centralized consolidated report from backend, browser, evaluation, performance and dependency scan artifacts. Current runner's Markdown points to separate scorecards but does not yet aggregate them.
12. Rerun affected suites and final all+coverage; record precise skipped, failed, coverage and remaining gates. Do not present mocked citations as semantic validation or synthetic labels as human accuracy.

## Commands for continuation

```sh
UV_CACHE_DIR=/tmp/deepsearch-uv-cache uv sync --group dev
npm ci --ignore-scripts --cache /tmp/deepsearch-npm-cache
.venv/bin/python scripts/run_tests.py --suite all --coverage
.venv/bin/python scripts/run_tests.py --suite security --output reports/qa/security
.venv/bin/python scripts/qa_evaluate.py
.venv/bin/python scripts/qa_load.py --sessions 25 --concurrency 5
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser:matrix
pg_ctl -D /tmp/deepsearch-qa-pg/data -l /tmp/deepsearch-qa-pg/server.log -o '-h 127.0.0.1 -p 55433 -k /tmp/deepsearch-qa-pg' start
TEST_DATABASE_URL=postgresql://george@127.0.0.1:55433/postgres .venv/bin/python scripts/run_tests.py --suite all --coverage --require-database
pg_ctl -D /tmp/deepsearch-qa-pg/data stop -m fast
```

Run live tests only if explicitly authorized/enabled, with the noted runner discovery issue fixed. Existing `scripts/check_integrations.py` can be expensive with --research; do not use for default CI.

## Provider documentation inspected

Official PAI docs were browsed:
https://www.alibabacloud.com/help/en/pai/judge-model-api-reference
They document the unusual SDK api_key string `Authorization: Bearer {token}`, custom templates and endpoint/model IDs. Existing constructor test captures that value. Actual HTTP header acceptance and JSON mode for the configured region are **not live verified**; avoid assuming all compatible providers are identical. Do not change production PAI authentication based solely on a mocked SDK capture.

## Completion status

Incomplete by explicit user stop. No CI, full matrix docs, visual regression tests, real DB results, live results, human evaluation results, performance baseline or final readiness assessment yet. Backend tests are passing at the documented snapshot, but browser checks and release coverage gates are failing/unmet. Preserve these facts in subsequent reporting.
