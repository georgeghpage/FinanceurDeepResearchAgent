# QA coverage matrix

Status describes implemented scope and local verification updated on 2026-10-13. “Partial” identifies requirements that are modeled or not independently verified; it does not convert simulated behavior to live evidence. The upgrade `all` run includes 254 Python tests (251 passed, three live skips). Browser checks are separate: 84 full-matrix checks plus six final autosave/conflict checks passed across Chromium, Firefox and WebKit; 16 visual cases compare 17 PNGs. Historical load/semantic/advisory artifacts retain their original scope/date. See [TESTING.md](TESTING.md), [upgrade progress](../IMPLEMENTATION_PROGRESS.md) and [READINESS.md](READINESS.md).

| # | Category | Implemented tests / evidence | Scope and outstanding work |
| --- | --- | --- | --- |
| 1 | Environment/configuration | `test_config_prompt`, `test_multi_agent.ContractTests`, `test_smoke_control` | Passed offline; actual deployment env and secrets not inspected. |
| 2 | Network connectivity | `test_mcp`, `test_diagnostics`, `test_provider_contracts` | Partial: loopback real HTTP/MCP and modeled provider failure; external route unverified. |
| 3 | DNS/TLS/proxy/transport | `test_diagnostics`, `test_provider_contracts`, `test_retry_policy` | Partial: error classification, safe metadata, retry bounds and numeric/date Retry-After; real certificate chains, proxy servers, DNS rotation, pooling and redirects not exhaustively exercised. |
| 4 | External API integration | `test_provider_contracts`, `test_mcp`, `test_smoke` | Real SDK/HTTPX contracts passed; all three live smoke cases skipped. |
| 5 | Authentication/credentials | `test_config_prompt`, `test_diagnostics`, `test_multi_agent.JudgeTransportTests`, `test_ui` | Passed deterministic isolation/redaction/header capture. Actual PAI SDK auth acceptance unverified. |
| 6 | Schemas/data validation | `test_multi_agent.ContractTests`, `test_properties`, `test_judge_boundaries`, `test_references` | Passed strict schemas, IDs, counts, references, UTF-8 and selected property invariants; not exhaustive malformed input combinations. |
| 7 | Unit correctness | Runner `unit`; `test_pricing`, `test_model_catalog`, `test_reporting`, `test_consolidated_report` | Passed; typing scoped to config/models and formatting scoped to QA files. |
| 8 | Query planner | `test_multi_agent`, `test_agent`, `test_properties` | Passed 15 queries/three groups/mappings/unique IDs and bounded parsing. Intent/semantic diversity requires real-model evaluation. |
| 9 | Tavily retrieval | `test_mcp`, `test_provider_contracts`, `test_retry_policy`, `test_multi_agent.AdditionalFailureTests` | Passed real loopback MCP, SDK payloads, malformed and multilingual results, retries/concurrency/provenance. Search relevance/ranking and live quota unverified. |
| 10 | Research workers | `test_multi_agent`, `test_reliability`, `support_fixtures` | Passed tool-free schemas, configured 16,384 output cap, early group-specific extraction, original passages, concurrent barriers and partial failure. Multilingual factual extraction quality unverified. |
| 11 | Markdown evidence storage | `test_storage_reliability`, `test_security`, `test_multi_agent` | Passed fsync/replace/partial-write faults, paths, symlinks, UTF-8, provenance and merge. Multiple snapshot files are not one transaction. |
| 12 | Concurrency/races | `test_reliability`, `test_properties`, `test_multi_agent` | Passed synchronized workers, reservations, independent sessions and cancellation. Broad distributed/load races remain outside this single-process system. |
| 13 | PAI classifier | `test_judge_boundaries`, `test_quality`, `test_provider_contracts`, `test_multi_agent` | Passed schema/reference gates, recovery, fallback, exact bounded snapshot and adversarial READY checks; judge module 98.438% branches. Human-labeled/live accuracy unverified. |
| 14 | Orchestration/state machine | `test_orchestration_edges`, `test_multi_agent`, `test_reliability` | Passed actual default workflow, budgets, deadlines, late results and cleanup. 86.620% branch coverage remains below 95% target; no durable execution engine. |
| 15 | Refinement | `test_multi_agent.WorkflowTests`, `test_quality`, `test_orchestration_edges` | Passed two-round hard maximum, five followups, duplicate suppression, low gain and qualified finalization. |
| 16 | Structural writer | `test_references`, `test_multi_agent`, `test_orchestration_edges` | Passed supported references, audit rejection, input/output failure fallback and cancellation. Bounded long browser fixture verified; arbitrary-length section splitting absent. |
| 17 | Citations/grounding | `test_references`, `test_security`, `test_quality`, browser `workflow.spec.cjs` | Passed IDs/URLs, escaped content, preserved citations and deterministic audit rejection. Semantic entailment is not established by mocked audits. |
| 18 | LLM evaluation | `qa_evaluate.py`, `test_benchmark`, `test_quality`, `benchmarks/` | Partial: 14 contract cases/eight workflow regressions passed; 225 snapshots await human labels. Independent final-answer judge and public benchmark adapters not implemented/validated. |
| 19 | Backend integration | Runner `integration`/`all`; PostgreSQL `test_history` cases | Passed real local HTTP/MCP, actual components and PostgreSQL persistence/replay. Providers are scripted. |
| 20 | Research E2E | `test_multi_agent.EntrypointTests`, `test_quality`, browser `workflow.spec.cjs` | Passed CLI and actual HTTP default orchestrator through report; success/refine/empty/outage/worker failure/cancel/isolation. No paid full research run. |
| 21 | Security | `test_security`, `test_storage_reliability`, `test_diagnostics`, `test_ui`, browser unsafe-content case | Passed deterministic tool/reference/path/XSS/origin/redaction boundaries; no claim of real LLM prompt-injection resistance. Advisory scans found zero known vulnerabilities. Arbitrary URL fetch/SSRF endpoint and user accounts are absent. |
| 22 | Performance/load | `qa_load.py`, `reports/qa/load/c*.json`, browser latency attachment | Measured five concurrency levels, stage times, CPU/memory/loop lag/throughput; scripted timing and synthetic tokens only. Production latency/cost SLOs unverified. |
| 23 | Stress/soak | `qa_load.py`, `test_reliability`, `test_storage_reliability` | Partial: 50 concurrent fake agents and 500-session accelerated soak passed; bounded nightly workload configured. Hours-long, real-provider and resource-growth time series remain unverified. |
| 24 | Fault/recovery | `test_storage_reliability`, `test_orchestration_edges`, `test_multi_agent`, browser reconnect | Passed local write faults, timeouts, malformed provider data, late results, cancellation/replay. Files are audit snapshots; restart/resume of research from checkpoints is unsupported. |
| 25 | Observability/logging | `test_diagnostics`, `test_streaming`, `test_pricing`, `test_web.cjs`, `qa_load.py` | Passed safe JSONL, stages, IDs, usage and dashboard replay. Full distributed OpenTelemetry tracing and privacy policy enforcement are absent. |
| 26 | Frontend components | `test_web.cjs` via npm/`test_web.py` | Passed input, NDJSON, render/download, trace/dashboard, failure, reconnect and usage components. JavaScript with a bundled Tiptap/ProseMirror editor; no React framework. |
| 27 | Frontend integration | `test_ui`, `test_history`, `test_web.cjs`, Playwright workflow | Passed actual HTTP payloads, stream ordering/replay, history, busy/stop/error handling. |
| 28 | Browser E2E | `workflow.spec.cjs`, `document.spec.cjs`, `native-select.spec.cjs` | Full matrix: 84 passed plus six final autosave/conflict checks across three engines; console/request monitoring and failure artifacts, no retries. Native WebKit select CSP diagnostic narrowly documented/allowed after independent reproduction. |
| 29 | Accessibility | Axe in three engines; keyboard/focus/reduced-motion/viewport test | Passed automated WCAG tags and targeted keyboard checks; screen-reader usability and full manual WCAG 2.2 AA audit remain unverified. |
| 30 | Visual regression | `visual.spec.cjs`; 17 reviewed darwin PNGs | 16 comparison cases passed locally, including editing and media. MacOS baselines only; hosted comparison not executed. Rich text, tables, code, formulas and charts are functionally covered; dark mode is unsupported. |
| 31 | Compatibility/responsive | Five widths plus narrow viewport; Chromium/Firefox/WebKit | Passed 360/430/768/1280/1920 CSS widths and long report links/scroll/focus. Real mobile hardware, touch/AT and older browsers unverified. |
| 32 | Production smoke | `test_smoke`, manual `live-smoke.yml` | Implemented runtime explicit opt-in, ≤three paid requests and credential skips; not run live. No full real-provider E2E/cost calibration. |

## Component branch coverage

| Component | Line % | Branch % | Target | Status |
| --- | --- | --- | --- | --- |
| Overall backend | 91.671 | 84.975 | line ≥85 | Pass |
| Budget | 98.565 | 95.714 | branch ≥95 | Pass |
| Evidence store | 100 | 91.176 | branch ≥90 | Pass |
| Judge | 99.310 | 98.438 | branch ≥95 | Pass |
| Orchestrator | 97.704 | 86.620 | branch ≥95 | Unmet |

The orchestrator's remaining paths include late-future races, defensive empty extraction groups, reservation/deadline boundaries and loop exits. Some are difficult to reach through validated plans; no exclusion pragmas or target relaxation were added. Inspect `reports/qa/document-upgrade-final/coverage-html` and `coverage.json` rather than assuming every uncovered edge is a reachable defect.
