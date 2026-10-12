# Running QA

The default suites use scripted providers, a real OpenAI SDK with HTTPX mock transports, real local HTTP/MCP servers, temporary session directories and isolated PostgreSQL schemas. They do not establish live compatibility or real answer accuracy. Read [ARCHITECTURE.md](ARCHITECTURE.md), [MATRIX.md](MATRIX.md), [DEFECTS.md](DEFECTS.md) and [READINESS.md](READINESS.md) for scope and evidence.

## Install

```sh
UV_CACHE_DIR=/tmp/deepsearch-uv-cache uv sync --frozen --group dev
npm ci --ignore-scripts --cache /tmp/deepsearch-npm-cache
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npx playwright install chromium firefox webkit
```

Python 3.11+ and Node are required. The npm dependencies are development-only. These paths accommodate this workspace's restricted caches; ordinary environments may omit the cache variables. The environment sandbox may require execution escalation for loopback sockets, browsers, database processes and dependency downloads. A blocked sandbox bind is an infrastructure failure.

## Python suites

```sh
.venv/bin/python scripts/qa_checks.py
.venv/bin/python scripts/run_tests.py --suite all --coverage
.venv/bin/python scripts/run_tests.py --suite unit --output reports/qa/unit
.venv/bin/python scripts/run_tests.py --suite contract --output reports/qa/contract
.venv/bin/python scripts/run_tests.py --suite integration --output reports/qa/integration
.venv/bin/python scripts/run_tests.py --suite network --output reports/qa/network
.venv/bin/python scripts/run_tests.py --suite agent --output reports/qa/agent
.venv/bin/python scripts/run_tests.py --suite evaluation --output reports/qa/evaluation-tests
.venv/bin/python scripts/run_tests.py --suite security --output reports/qa/security
.venv/bin/python scripts/run_tests.py --suite performance --output reports/qa/performance-tests
.venv/bin/python scripts/run_tests.py --suite frontend --output reports/qa/frontend
.venv/bin/python scripts/run_tests.py --suite e2e --output reports/qa/e2e
.venv/bin/python scripts/run_tests.py --suite smoke --output reports/qa/smoke
```

Suites intentionally overlap; never add their counts to the `all` count. The `performance` unittest suite verifies invariants; measured workloads use `qa_load.py`. Python `frontend`/`e2e` include Node components and actual local HTTP, while the browser matrix is a separate command. The offline runner disables dotenv credential loading and blocks external DNS/socket connects. Live opt-in is checked at runtime and is forced off by the offline runner. Smoke is visibly skipped by default. No test retries are used, and flakiness remains unmeasured.

Every run produces results JSON, JUnit XML and Markdown. Use separate `--output` directories for partial suites so they do not overwrite the canonical `all` artifact. Coverage includes branch data, XML, JSON and HTML. Subtest failures and setup errors fail the suite and are reflected in the reports.

Formatting checks cover the explicit QA-file list in `scripts/qa_checks.py`; fatal lint checks cover all `src`, `tests` and `scripts`. Type checking currently covers `config.py` and `models.py`, with imported modules followed silently. This is a scoped gate, not a claim of repository-wide typing or formatting compliance.

## Required PostgreSQL integration

Use a disposable database where the role can create/drop schemas. Seven tests create random schemas and remove them after execution. Never use a production database.

```sh
pg_ctl -D /tmp/deepsearch-qa-pg/data -l /tmp/deepsearch-qa-pg/server.log \
  -o '-h 127.0.0.1 -p 55433 -k /tmp/deepsearch-qa-pg' start
TEST_DATABASE_URL=postgresql://george@127.0.0.1:55433/postgres \
  .venv/bin/python scripts/run_tests.py --suite all --coverage --require-database
pg_ctl -D /tmp/deepsearch-qa-pg/data stop -m fast
```

The cluster above is the disposable cluster from this workspace's QA session. Other machines must initialize their own cluster or use the CI PostgreSQL service. `--require-database` fails when the URL is missing; failed connections fail tests. Without it, the seven optional database tests explicitly skip.

To enforce requested deterministic release coverage:

```sh
TEST_DATABASE_URL=postgresql://george@127.0.0.1:55433/postgres \
  .venv/bin/python scripts/run_tests.py --suite all --coverage \
  --require-database --require-release-coverage
```

The command intentionally fails while any requested coverage target is unmet. Do not remove branches or lower targets to conceal failures.

## Evaluation and measured workloads

```sh
.venv/bin/python scripts/qa_evaluate.py
.venv/bin/python scripts/qa_load.py --sessions 100 --concurrency 1 --output reports/qa/load/c1.json
.venv/bin/python scripts/qa_load.py --sessions 100 --concurrency 5 --output reports/qa/load/c5.json
.venv/bin/python scripts/qa_load.py --sessions 100 --concurrency 10 --output reports/qa/load/c10.json
.venv/bin/python scripts/qa_load.py --sessions 100 --concurrency 25 --output reports/qa/load/c25.json
.venv/bin/python scripts/qa_load.py --sessions 100 --concurrency 50 --output reports/qa/load/c50.json
.venv/bin/python scripts/qa_load.py --sessions 500 --concurrency 10 --seconds 60 --output reports/qa/load/soak.json
```

The versioned fixtures contain 14 deterministic READY contract cases and eight scripted workflows. Their expectations are not human labels. Broader semantic accuracy, human false READY rates, independent answer-judge agreement and live predictions remain null. The benchmark review queue has 225 unlabeled snapshots; the benchmark tooling requires named human reviewers before treating labels as reference truth.

Load uses independent agent instances with fake providers, not concurrent UI users: the actual UI admits one active run and returns busy to overlaps. Sessions and queued tasks are bounded; `--seconds` caps scheduling, with already-started bounded jobs allowed to finish. The workload records p50/p95, p99 only with at least 100 samples, throughput, stage durations, CPU, Python memory, peak RSS, fd/thread counts, event-loop lag and temporary snapshots. Search stage includes persistence; extraction includes consolidation. Session durations include local I/O contention. Queue measurements do not model a production admission queue. Cost is null and token counts are synthetic. A short soak cannot prove absence of leaks or determine production SLOs.

## Browser and visual verification

```sh
npm test
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser
PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:browser:matrix
QA_BROWSER_REPORT_DIR=reports/qa/visual PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:visual
```

The fixture runs the production HTTP handler and default orchestrator with paid-provider boundaries replaced. Fixture-only reset/release endpoints isolate tests and control stages; they are absent from production. All pages in a test context are monitored for page errors, console errors and failed requests. Only request-specific aborts after a terminal NDJSON record was actually read are accepted. The reconnection case deliberately resets one request; it still verifies exactly two POSTs and one final report. Expected 409 errors are restricted to observed busy responses.

WebKit in the installed browser build emits a CSP inline-style warning when parsing native selects even on a script-free page. `native-select.spec.cjs` reproduces it. The main harness accepts only the exact message paired with a violation at the actual source line of a native select, with empty source and style directive; other console errors remain failures. Production CSP is unchanged. WebKit keyboard navigation uses Option+Tab, which includes buttons under its default platform preference.

Axe checks WCAG A/AA tags for empty and completed views in all three engines. Keyboard/focus, reduced motion, a narrower CSS viewport equivalent to 200% zoom, five responsive widths, report download, reconnection, cancellation, history and bounded long-report rendering are covered. Real assistive-technology use and browser zoom controls remain manual checks. Reports do not support Markdown tables/code blocks or dark mode, so these are marked unsupported rather than simulated as features.

Visual baselines live in `tests/browser/snapshots/darwin/visual/visual.spec.cjs`. Fifteen PNGs cover 14 cases: empty/entered, five gated stages, completed/refined/failure/cancelled/long report and mobile/tablet/desktop. Fixture dates are fixed; duration fields and waterfall geometry are masked; Arial and reduced motion stabilize layout. Visual-only context bypasses CSP to permit Playwright screenshot style instrumentation; functional/security browser projects enforce production CSP. Missing baselines fail. CI compares on macOS 15; Linux/Windows need their own reviewed baselines and never silently pass absent screenshots.

Intentional UI changes can regenerate baselines:

```sh
QA_BROWSER_REPORT_DIR=reports/qa/visual PLAYWRIGHT_BROWSERS_PATH=/tmp/deepsearch-playwright npm run test:visual:update
```

Inspect PNGs/diffs, then rerun `test:visual` without updates. Baseline creation alone is not a comparison pass. Review the JSON/JUnit, HTML, failure PNGs and traces under `reports/qa/browser` or `reports/qa/visual`.

## Dependency security and consolidation

```sh
.venv/bin/pip-audit --skip-editable --format json --output reports/qa/python-audit.json --progress-spinner off
npm audit --json --cache /tmp/deepsearch-npm-cache > reports/qa/npm-audit.json
.venv/bin/python scripts/qa_report.py --require-all
```

Dependency scans query public advisory services and require network access; the application and live providers are not exercised. No automatic upgrade or vulnerability suppression is applied. The local editable application has no PyPI advisory identity and is covered by source/security checks. Other unaudited dependencies fail the consolidated gate. JSON includes advisory IDs, available fixes and skipped dependencies; severity is only reported if supplied by the scanner.

The consolidator requires complete backend, database, three-browser, visual, evaluation, five load levels, soak and security artifacts. It records input SHA-256 hashes, component results, coverage and separate semantic/live/performance unknowns. `--require-all` enforces deterministic execution, reporting coverage separately. `--require-release` cannot pass while production evidence remains unverified. Do not mix artifacts from different source versions; rerun affected suites after changes.

## CI and optional live smoke

`.github/workflows/qa.yml` defines PR/push validation, required PostgreSQL integration, component/browser/accessibility/visual checks, bounded load and advisory scans, plus nightly extended soak (02:00 Asia/Shanghai). Failure artifacts always upload; the report job aggregates them. Requested coverage is an explicit failing gate while unmet. Workflow YAML and local commands were checked; hosted CI has not been executed from this workspace.

Actions usage follows the upstream [checkout](https://github.com/actions/checkout), [setup-node](https://github.com/actions/setup-node), [artifact](https://github.com/actions/upload-artifact) and [setup-uv](https://github.com/astral-sh/setup-uv) documentation.

`.github/workflows/live-smoke.yml` requires manual `enable_live=true` and a `live-qa` environment. Configure credentials as environment secrets and environment approvals as appropriate. It is separate from PR/nightly jobs. Local explicit opt-in:

```sh
.venv/bin/python scripts/run_tests.py --suite smoke --live --output reports/qa/live
```

At most one small completion, one Tavily search and one PAI classification are attempted; missing credentials explicitly skip. No live smoke was run during this continuation. SDK authentication/JSON capture is a contract test, not evidence of live PAI endpoint acceptance. Full paid research and live load are outside this authorization.
