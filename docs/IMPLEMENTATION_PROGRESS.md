# Research/report upgrade — verified continuation

Updated 2026-10-13 (Asia/Shanghai), after the user explicitly requested continuation from the handoff and progress notes. The remaining deterministic implementation and verification work is complete. Changes remain in the working tree; no commit, reset or deployment was made. Existing user changes were preserved.

## Completed in this continuation

- Fixed citation semantics: native keyboard focus and Enter activation in Reading view; caret-based Ctrl/Command+Enter activation while editing. Citation URL previews are rendered by the editor schema, avoiding DOM mutations that can incorrectly mark a generated report as edited. Source clicks preserve the editor's citation selection.
- Added and verified formula/chart safety, Markdown restoration/export, read-only synthesis previews, cancellation/interruption recovery, stale-tab conflicts, server-save outages and local recovery in three browser engines.
- Fixed two autosave races: a monotonically increasing change counter preserves edits made during an in-flight save even with equal timestamps; further edits retain an existing conflict warning. Conflicting copies remain locally recoverable and never overwrite the newer server document.
- Moved synthesis preview publication onto the controller event loop. Closed or cancelled synthesis rejects delayed worker previews; a targeted regression verifies owner-thread publication and no terminal-state changes from late callbacks.
- Fixed search admission: actual transport starts are paced under the semaphore, including retries; queued requests retain their socket timeout, consume no unstarted search reservations, and stop before transport when the shared budget is exhausted. Successful independent results and shared-waiter cancellation remain covered.
- Added independent final-writer USD price overrides, writer monetary headroom during research/search admission, and verified Qwen3.7 Plus / Qwen3.8 Max JSON/context ceilings. Unknown writer prices never inherit research prices. Default model roles remain unchanged.
- Added `scripts/evaluate_writers.py` and [writer evaluation](WRITER_EVALUATION.md). Default execution prepares a frozen evidence/prompt/schema manifest offline, without reading credentials. Explicit `--live` enables at most one synthesis request per candidate, two or three candidates, with no searches/retries/extra audits. Scripted tests prove identical inputs and unchanged evidence; semantic/editorial quality remains unmeasured.
- Removed execution counters and internal stop codes from the report body; state, diagnostics and the UI retain them. Updated README, architecture, testing, matrix and handoff documentation.
- Reviewed completed, narrow, cancelled, edited and media layouts; refreshed only eight document-related existing baselines and added two editor/media baselines. Visual comparison now covers 16 cases / 17 PNGs. Functional tests keep production CSP enforced; screenshots run in the separately instrumented visual project. A script-free WebKit reproduction confirmed that Playwright screenshot styling itself triggers a CSP warning.
- Replaced a short-sleep extraction overlap assumption with a barrier. Instrumented fixture watchdogs allow filesystem/scheduler overhead while still bounding deadlocks and rejecting a five-second uncooperative transport.

## Final verified results

| Check | Result |
| --- | --- |
| Full backend, coverage and required real PostgreSQL | **254 total: 251 passed, zero failures/errors, three live skips**, 148.150s |
| Complete Chromium / Firefox / WebKit matrix | **84 passed**, zero failures/skips/retries, 6.5m |
| Final autosave/conflict checks across all three engines | **6 passed**; includes the new equal-timestamp in-flight save case and repeat conflict checks |
| Visual comparisons, without updating snapshots | **16 passed**, 17 PNGs, 59.0s |
| Node components | **31 passed, one standalone real-HTTP skip**; real HTTP reconnection is exercised by the full Python suite |
| Scoped QA checks | Fatal lint, formatting of 24 QA Python files and typing of config/models passed; `git diff --check` passed |
| Editor build and packaging | Build passed; wheel includes the exact current editor bundle, stylesheet and writer prompt |
| npm dependency advisory scan | Zero known advisories; installed dependency graph contains 58 packages |

Current backend coverage is **91.671% lines / 84.975% branches**. Critical branch coverage: budget **95.714%**, evidence store **91.176%**, judge **98.438%**, orchestrator **86.620%**. The existing **95% orchestrator release gate remains unmet**; no coverage target/exclusion was changed. Hosted CI, live compatibility, production latency and independently reviewed answer accuracy are not established.

Artifacts: `reports/qa/document-upgrade-final/`, `document-upgrade-browsers-final/`, `document-upgrade-autosave/`, `document-upgrade-visual-final/`, `document-upgrade-npm-audit.json`, `edited-report.png`, and `media-report.png`. Logs are `/tmp/deepresearch-continuation-{full,browser-final,autosave,visual-final}.log`. The wheel was verified at `/tmp/deepresearch-upgrade-dist/deepsearchagent-0.1.0-py3-none-any.whl`.

## Remaining validation, outside the offline implementation claim

1. Run an explicitly authorized evidence-frozen live writer comparison and independent human review. No paid provider calls or comparative superiority/latency/cost claims were made in this continuation.
2. Address the existing orchestration branch release gate with meaningful missing-path coverage; then run hosted CI. Other historical production-readiness requirements remain in [READINESS.md](qa/READINESS.md).
3. Validate real provider transport behavior and production performance. Fake-provider tests establish software contracts, not research accuracy or production SLOs.

The temporary citation-debug server on port 8878 was stopped. Browser fixture servers exit with their test commands. The user's existing PostgreSQL cluster was not stopped or reconfigured; integration tests used disposable schemas. No real `.env` values were inspected and no agents were delegated.

---

# Historical paused snapshot

Saved on 2026-10-13 (Asia/Shanghai). The user explicitly requested: “stop and save progress”. All implementation changes remain in the working tree; no commit or deployment was made.

## Original request

The full request is in `/Users/george/.codex/attachments/fe425a0b-86b4-4934-9f00-8c3ca23433e4/Pasted text.txt`.

Improve async research latency, preserve original evidence between agents, implement professional synthesis with independently configurable writer model, and provide an editable, autosaved rich-text report with safe streaming, citations and exports.

## Work saved

- SearchRouter now shares duplicate in-flight searches and caches successful results with deep copies. Cancellation uses waiter counts, plus explicit cleanup. Configurable rate pacing complements existing semaphore concurrency and shared budgets. Attempts and retries respect provider cooldowns. Initial admission queueing is separated from per-request timeouts; timeouts can receive up to three retries within the run deadline.
- Retrieval reports results immediately to the orchestrator. Each extraction group starts after its own prerequisite searches, without waiting for unrelated groups. Evidence mutation stays on the event-loop owner. Empty results skip extraction. Checkpoint filenames use UUIDs to prevent reuse of thread IDs overwriting prior checkpoints.
- Full original retrieved content is retained. Extraction selects contiguous, query-relevant passages. Judge snapshots no longer truncate individual claims/excerpts. Final synthesis receives eligible findings plus bounded original source passages and omission metadata.
- Added `prompts/writer.md`, based on the requested Senior Research Analyst role. Markdown emphasis, lists, tables and code are preserved while generated links/HTML remain restricted and citations come from stored evidence.
- Added `RESEARCH_MODEL`, `FINAL_WRITER_MODEL`, and `SEARCH_REQUESTS_PER_SECOND` configuration. Writer uses the existing provider abstraction, independent backend instance when needed, and model-specific context/output limits and pricing metadata. Default remains DeepSeek V4.1 Flash via Aliyun.
- Partial synthesis is rendered from complete, source-referenced paragraphs and exposed as explicitly incomplete previews, with `report.partial.md` checkpoints. Interrupted report status is distinguished from completed generation.
- Added bundled Tiptap editor, DOMPurify, Markdown parser, and esbuild (`frontend/report-editor.js`, compiled `src/deepsearchagent/web/editor.js`). Supports direct editing, headings, emphasis, lists, links, tables, undo/redo, Markdown copy/download, reading/editing modes and print/PDF.
- Separate PostgreSQL `report_documents` table and `/api/documents/{chat_id}` GET/POST routes. Revision compare-and-swap rejects stale writes. Local browser recovery copies protect edits during server outages. Research events/evidence are not edited. Replay cannot overwrite edited documents.
- Fixed an editor restoration race: `setEditable(..., false)` suppresses synthetic update events that previously marked the initial report dirty before server restoration.
- Added restrained document styles and safe deterministic formula/chart code-block previews (`frontend/report-media.js`). KaTeX renders MathML with trust disabled; chart JSON is validated and rendered using SVG DOM methods. Fenced source stays editable and exportable. These media features still need dedicated browser verification.
- Added targeted backend/browser/storage tests, updated the old truncation expectation and rate-sensitive timing fixtures.

## Validation so far

- `npm run build:web`: successful; generated bundle is saved.
- `npm test`: 31 passed, 1 skipped.
- Targeted pipeline/orchestration tests: 37 passed (`/tmp/deepresearch-targeted.log`).
- Targeted retry/reference/security tests: 23 passed (`/tmp/deepresearch-regressions.log`).
- New pipeline contracts: 8 passed, including dedup, independent cancellation, rate pacing, four-attempt timeout limit, early extraction, raw evidence and independent writer backend.
- Latest targeted provider/reliability/pipeline tests: 34 passed (`/tmp/deepresearch-final-targets.log`).
- Two dedicated editor browser tests passed: editing, undo/redo, original-result replay protection, local/server restoration, citation preservation, Markdown export and table editing.
- Full Chromium suite finished with **22 passed, 1 failed**. Remaining failure: `tests/browser/workflow.spec.cjs:143`, long report keyboard accessibility; programmatically focusing the last citation link inside the editable ProseMirror document does not focus the link. Investigate focus behavior/tabindex and view-mode semantics; do not just remove the check.
- Full Python suite with local PostgreSQL: last run reported 242 tests, 11 errors, 3 skipped. Errors were caused by an indentation error introduced while updating `test_provider_contracts.py`, cascading into importing tests. That file was fixed and its affected targeted suites subsequently passed. The full suite has NOT been rerun after that fix and the last media/context changes.
- Full-suite artifacts: `reports/qa/document-upgrade-final/`; browser artifacts: `reports/qa/browser/`; editor screenshot: `reports/qa/edited-report.png`.
- Broad Ruff checking encounters pre-existing style violations. Run the repository's required scoped/fatal checks, not an indiscriminate formatter over the pre-existing edits.

## Remaining work

1. Fix the citation-link keyboard-focus browser regression; verify behavior in reading and editing modes.
2. Add/execute dedicated formula/chart, streaming-preview/interruption, and stale-document-conflict browser tests; visually inspect the saved screenshot and final document layout.
3. Review final changes for cancellation and late-worker preview races, fair rate-limit/budget admission, partial status accuracy, and server/local autosave failure behavior. Test model pricing/context accounting when the writer differs from research.
4. Complete the requested alternative-writer investigation/evaluation and documentation. No live comparative model benchmark has been run, and no superiority/latency/cost claims are established. Official docs were consulted for Tiptap and Aliyun; keep both model roles the same by default pending controlled evidence-preservation/writing comparisons. Consider an evidence-frozen A/B evaluation using the existing provider abstraction. Independent writer price override configuration may be needed for unknown models under a monetary cap.
5. Update README/architecture/config documentation and finish full regression + required scoped QA checks. Do not claim the full suite passes yet.
6. No live end-to-end paid provider research was run. Browser tests use the actual orchestration/HTTP path with scripted provider boundaries and PostgreSQL integration tests use disposable schemas.

## Environment and cautions

- Workspace: `/Users/george/Desktop/WORKING/PROJECTS/deepsearchagent`.
- Use `.venv/bin/python` with `PYTHONPATH=src:tests`, or `scripts/run_tests.py` (it sets source paths). Direct imports without PYTHONPATH can load the older installed package from site-packages.
- Dependency installation and loopback/browser testing needed sandbox escalation. Chromium was installed successfully. No approval rejection occurred; initial sandbox attempts failed DNS/local socket restrictions.
- Example full test command used: `TEST_DATABASE_URL=postgresql://deepsearchagent@127.0.0.1:55432/deepsearchagent .venv/bin/python scripts/run_tests.py --suite all --output reports/qa/document-upgrade-final`.
- Browser commands: `npm run build:web`, `npm run test:browser`. Build after changing frontend source; Python server serves the checked-in bundle.
- The working tree already had extensive user changes before this task (config, orchestration, prompts, tests, browser snapshots and other files). Preserve them. Existing snapshot diffs are not newly approved visual baselines from this task. No subagents were used.
- No tests or installs are known to remain running after the stop request: the last browser run and initial install session both returned completed results.
