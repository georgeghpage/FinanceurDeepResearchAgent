# Release-readiness evidence — 2026-10-12

The implemented QA infrastructure can reproduce deterministic software regressions across the default research pipeline, provider boundaries, PostgreSQL and browser UI. **Production readiness is not established.** Orchestration coverage remains below the requested gate; live providers and human-reviewed research accuracy were not validated.

This continuation resumed on the user's explicit instruction after the historical stop. No paid APIs were called, no agents were delegated, no project reset/commit/publish was performed and no real `.env` values were inspected. The repository was already entirely untracked. The disposable QA database on port 55433 was stopped after verification; the user's workspace database was not changed.

## Actual local execution

| Check | Result |
| --- | --- |
| Full backend + coverage + required real PostgreSQL | 202 tests: 199 passed, zero failures/errors, three live-smoke skips; 27.699s unittest duration. All seven database tests passed. |
| Chromium / Firefox / WebKit | 51 passed, zero failures/skips/retries. Includes axe, cancellation, bounded busy handling, replay/history, long report, keyboard and responsive checks. |
| Visual regression | 14 cases comparing 15 darwin PNGs passed without baseline updates, 11.8s. |
| Deterministic classifier gates | 14 cases, including 13 adversarial negatives; zero false eligible READY decisions. These are contract expectations, not human accuracy labels. |
| Scripted workflow evaluation | Eight of eight workflow fixtures passed all checks; termination 100%, check pass 100%. |
| Fatal lint / QA formatting / scoped type checks | Passed. Mypy scope: config/models; formatting scope: 21 QA Python files. |
| Dependency security | 90 Python dependencies scanned, zero known vulnerabilities; the local editable application has no PyPI advisory identity. npm scan: zero advisories. No automatic dependency upgrades/suppressions. |
| CI configuration | Five-stage deterministic workflow plus separate manually enabled live smoke implemented; YAML parsed locally. Hosted CI not executed. |
| Consolidation | Complete deterministic artifacts pass `qa_report.py --require-all`; release coverage remains separately unmet. Machine report includes input hashes. |

The only accepted browser console exception is the installed WebKit's independently reproduced native-select CSP diagnostic, paired with exact source-line violation evidence. Production CSP stays enforced in functional projects. Visual-only browser instrumentation bypasses CSP for screenshot styling. See [TESTING.md](TESTING.md) and [DEFECTS.md](DEFECTS.md).

## Code coverage

| Scope | Lines | Branches | Requested target | Gate |
| --- | --- | --- | --- | --- |
| Backend overall | 92.496% | 87.136% | ≥85% lines | Pass |
| Budget | 100% | 100% | ≥95% branches | Pass |
| Evidence store | 100% | 90.625% | ≥90% branches | Pass |
| Judge | 100% | 100% | ≥95% branches | Pass |
| Orchestrator | 99.392% | 90.625% | ≥95% branches | **Unmet** |

The release coverage CLI and CI gate intentionally fail. No exclusion pragma or coverage-target reduction was used. Uncovered orchestration paths include defensive empty groups, late future races, loop exits and a reservation/deadline boundary; inspect actual branch arcs before adding tests. Coverage is separate from semantic answer quality.

## Measured fake-provider baseline

Python 3.11.16, macOS 15.7.3 arm64; independent agent instances, local temporary filesystem, fake search delay 5ms and scripted model latency. Results are local baselines with instrumentation overhead, not production promises. Sample counts vary; p99 is only emitted with ≥100 sessions.

| Concurrency | Sessions | Failed | p50 seconds | p95 seconds |
| --- | --- | --- | --- | --- |
| 1 | 10 | 0 | 0.126 | 0.190 |
| 5 | 20 | 0 | 0.410 | 0.473 |
| 10 | 20 | 0 | 1.220 | 1.242 |
| 25 | 50 | 0 | 2.229 | 2.304 |
| 50 | 100 | 0 | 3.791 | 3.901 |
| Soak at 10 | 500 | 0 | 0.804 | 1.410 |

The load tool measures plan/search/extract/verify/write/validate stage wall time, throughput, loop lag, CPU, tracemalloc, process RSS and before/after resource counts. Search includes persistence; extract includes consolidation. All workloads left zero temporary snapshots. The 500-session accelerated soak returned fd/thread counts to their starting values. These snapshots do not establish absence of leaks over hours or under real sockets. The measured loop lag increases under local file-I/O contention; determine production budgets before setting an SLO. Synthetic usage averages 700 tokens per successful session; real provider cost is null. UI request timing is attached to browser long-report cases, rather than treated as a calibrated production latency metric.

## Release criteria still outstanding

1. Reach the 95% orchestration branch gate with meaningful assertions or formally review the remaining defensive paths without hiding them.
2. Run explicitly authorized, bounded provider validation for configured account/model/region, including PAI authentication and JSON behavior. A mocked SDK header is not live evidence.
3. Human-review the 225 snapshot queue, add real research cases and measure answer correctness, semantic citation support, requirement completeness, false READY precision/recall and judge disagreement. Independent final-answer evaluation/public benchmark adapters remain incomplete.
4. Measure realistic provider latency, usage/cost, rate limits and admission behavior. The UI has one active run, and the agent API's local concurrency benchmark is a different workload.
5. Execute hosted CI and review macOS runner baseline differences. Supply independently reviewed Linux/Windows visual baselines before claiming those platforms.
6. Perform manual screen-reader/keyboard/zoom/touch review and longer soak/transport testing on deployment infrastructure. The automated axe result is not a full accessibility certification.
7. Treat snapshots as audit files. Multi-file crash consistency and durable research checkpoint resume are unsupported; saved UI traces/history replay safely without automatically restarting paid research.

No known critical dependency vulnerability was reported by the executed scans. Deterministic prompt/data/reference/path boundaries passed; resistance of actual LLMs to adversarial source instructions is unverified. Authentication/accounts, arbitrary URL fetching, distributed tracing, dark mode and report tables/code blocks are not implemented features and were not fabricated for test coverage.

Canonical generated evidence: `reports/qa/results.json`, `coverage.json`, `coverage-html/`, `browser/results.json`, `visual/results.json`, `evaluation.json`, `load/*.json`, `python-audit.json`, `npm-audit.json`, and `consolidated/QUALITY_REPORT.md` / `results.json`. Generated reports are ignored in Git; this documentation preserves the measured snapshot. See the [32-category matrix](MATRIX.md) and [execution guide](TESTING.md).
