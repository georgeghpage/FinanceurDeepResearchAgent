# Error Center and answer-quality handoff

Resumed and completed the remaining offline implementation and verification on 2026-10-13 (Asia/Shanghai), at the user's request to continue using this handoff. Changes are uncommitted. The repository already contained extensive uncommitted changes when this task began; preserve them and do not reset the working tree.

## Requested work

The full request is in `/Users/george/.codex/attachments/c0ec9432-5556-4c85-b29e-0b5ef823c3ad/Pasted text.txt`: dedicated Error Center, correlated waterfall failure diagnostics, Aliyun `qwen3.8-flash` final writer, Aliyun `qwen3.8-max` relevance-focused evaluator, strict quality schema, and bounded targeted recovery. Research remains on `deepseek-v4.1-flash`.

## Implemented

- Added a native-dialog Error Center, nonblocking toast, unresolved count, retained/dismissible history, recovered status, keyboard access, and terminal-failure dialog. Historical replay suppresses popups.
- Added session `ErrorHistory` and separate diagnostic events. Retry attempts share a logical identifier. Operational warnings no longer enter report limitations. Storage failures emit nonblocking diagnostics.
- Extended waterfall metadata with invocation IDs, distinct backend span namespaces, parent tasks, timestamps, attempts, safe provider/request codes, recovery, outcome, and confidence-labeled root causes. Evaluator scores and rejection reasons appear in the call inspector.
- Changed writer/judge defaults and added independent writer-provider selection. Qwen Flash/Max limits were checked against Alibaba's model documentation. Existing streaming, citations, editing, budgets, and provider adapters remain in use.
- Retained evidence-readiness evaluation in `prompts/evidence-judge.md`; replaced `prompts/judge.md` with the relevance-focused final-answer rubric. Added strict `QualityEvaluation`, requirements assessments, six scores, and deterministic PASS gates.
- Added final-answer assessment and bounded REVISE/MORE_RESEARCH/INSUFFICIENT_EVIDENCE handling. Revision payloads include the previous answer and specific feedback. Follow-up searches target only missing requirement IDs. Judge failures remain unverified. Failed revisions retain the prior draft.
- Updated documentation, environment example, offline fixtures, legacy assertions that expected diagnostics inside reports, and targeted backend/browser tests.

Primary implementation files: `config.py`, `models.py`, `judge.py`, `orchestrator.py`, `writer.py`, `diagnostics.py`, `budget.py`, `llm.py`, `cancellation.py`, `ui.py`, `tools/retry.py`, `tools/search.py`, and `web/{app.js,dashboard.js,index.html,style.css}` under `src/deepsearchagent`.

## Resumed correction

Fixed `failure_from_exception()` so propagated diagnostics retain the original provider, stage, transport operation, and correlation metadata. Downstream consequences are stored separately in a deduplicated `downstream_effects` list. Error history retains these effects even when the first observed event is a downstream consequence; the Error Center and call inspector both display them.

The previous failure in `test_multi_agent.NativeTavilyConcurrencyTests.test_concurrent_transport_errors_keep_independent_phases` now passes with the actual `initialize` and `call_tool` phases intact. Expanded existing regressions cover repeated propagation, preserved upstream identifiers, first-observed downstream events, and display of the original phase alongside its consequences.

## Final resumed validation

- Targeted concurrency/diagnostic/answer-quality tests: **29 passed**.
- Full offline backend run: **268 total, 256 passed, zero failures/errors, 12 skipped** (140.4s). Artifacts: `/tmp/deepsearch-qa-resumed`; log: `/tmp/deepsearch-resumed-tests-loopback.log`.
- Node components: **36 passed, 1 skipped**, no failures. Log: `/tmp/deepsearch-resumed-web-tests.log`. The standalone HTTP skip is covered by the full Python suite's real local HTTP reconnection test.
- Full Chromium workflow/editor/Error Center suite: **32 passed**, no failures/skips (2.8m). Artifacts: `/tmp/deepsearch-browser-resumed`; log: `/tmp/deepsearch-resumed-browser.log`.
- Final Error Center rerun after display changes: **3 passed**, including recovery, dismissal, keyboard focus, accessibility, terminal failure, and silent replay. Artifacts: `/tmp/deepsearch-error-center-resumed`; log: `/tmp/deepsearch-resumed-error-center.log`.
- Scoped fatal lint, QA formatting, config/models typing, answer-quality test formatting, JavaScript syntax checks, and `git diff --check`: passed.
- Visually inspected the regenerated `reports/qa/error-center-preview.png`: readable dialog with recovered status and retained history.

The initial sandboxed backend/browser attempts could not bind local sockets. Successful validation above used approved loopback access; the backend runner still blocked outbound network calls. The unsuccessful initial backend log is `/tmp/deepsearch-resumed-tests.log` and must not be mistaken for the final result.

## Remaining external validation

- Nine PostgreSQL tests were skipped because `TEST_DATABASE_URL` was not configured; three paid live-provider smoke tests were explicitly disabled. Database/provider availability was not established by this run.
- No live Qwen/PAI comparison or human-label calibration was performed. Scripted false-pass/targeted-recovery tests establish software contracts, not measured model accuracy.
- For future backend runs use `.venv/bin/python scripts/run_tests.py`; plain unittest imports an outdated installed package unless `PYTHONPATH=src:tests` is set. Browser tests use the existing offline fixture server.

No commits, deployment, publication, or live paid model calls were made.
