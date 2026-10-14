# Architecture and performance review — 2026-10-14

The default architecture is already asynchronous at the orchestration and search layers. Searches use bounded concurrency, duplicate-request sharing, caching, retry budgets and rate pacing. Each extraction group starts after its own search prerequisites, and up to three model workers overlap. The controller alone mutates evidence and owns transitions to judging, writing and answer evaluation. The synchronous model adapter uses deadline-aware daemon workers so an uncooperative provider cannot hold the event loop or executor shutdown open.

## Handoff audit

The latest entries in `HANDOFF_ERROR_CENTER_QUALITY.md`, `IMPLEMENTATION_PROGRESS.md`, and `qa/HANDOFF.md` supersede their historical stopped snapshots. The previous offline implementation work is recorded as complete. Outstanding handoff items remain: orchestration branch coverage below the existing 95% release gate, hosted CI execution, live provider/region compatibility, an explicitly enabled writer comparison, human answer evaluation and realistic production performance/soak measurements. This review does not establish those external results or trigger paid providers.

## Implemented changes

- **Evidence persistence no longer performs full snapshot serialization and fsync on the controller loop.** `EvidenceStore.apersist()` deep-copies state on the owner loop, then serializes and atomically writes that frozen copy using a worker thread. A per-session lock orders writes. Concurrent requests already covered by a newer snapshot share its durable result. Dependants await persistence before extraction starts.
- **Cancellation drains an active disk write before releasing its lock.** Cancelling an await does not cancel the underlying thread. Waiting for that thread prevents an older snapshot from overwriting a subsequently saved terminal state. Failures propagate and permit a later retry. Snapshot files retain individual atomic replace/fsync behavior; the pair is still not a transactional crash checkpoint.
- **Extraction merges write once.** The controller now merges without the synchronous implicit save, updates usage, and awaits one snapshot. Existing synchronous store callers retain the default save behavior.
- **Evaluation artifacts and the final report use awaited disk writes.** Retrieval accepts synchronous or awaitable result callbacks, so persisted result acceptance remains part of completion.
- **Excerpt checks normalize each source document once per merge.** Multiple findings no longer split/join the same full source content repeatedly. Eligibility and provenance rules remain unchanged.
- **Search pacing runs immediately before transport entry.** An extra scheduled task between admission and transport could bunch actual starts after a busy loop or slow trace callback. The attempt now awaits the async provider directly under a same-task timeout; it sets the next start interval after synchronous tracing and checks cancellation/budget again. The outer retry policy still owns cancellation, retries and admission deadlines.

Regressions cover frozen nested evidence, yielding during blocked disk I/O, coalescing ten pending saves into one subsequent snapshot, repeated cancellation ordering, disk-full retry and temporary-file cleanup, awaited result acceptance, slow-trace transport pacing, and cancellation immediately before transport entry.

## Next opportunities, in priority order

| Location | Observed inefficiency | Recommended follow-up |
| --- | --- | --- |
| `history.ChatStore.connect/save_event` and `ui.ResearchRun.emit` | Every trace/status event opens a PostgreSQL connection and commits independently. The database write runs inside the condition lock used by event streaming and cancellation. | Reuse bounded connections and introduce an ordered persistence queue with bounded batches. Define terminal flush, storage-failure reporting and restart/replay semantics before moving writes out of the lock. Measure actual PostgreSQL/stream/Stop latency. |
| `researcher.group_payload` | Whitespace normalization, pairwise `SequenceMatcher(..., autojunk=False)` on 3,000-character prefixes, passage scoring and repeated JSON budget sizing execute on the controller loop. | Profile realistic large documents; use safe similarity upper bounds/reused matchers and incremental exact UTF-8 budget accounting. If CPU work is material, offload an immutable payload input while keeping evidence mutation on its owner. |
| `orchestrator._write.publish_preview` | Partial report rendering and an atomic fsync still run on the owner loop, throttled to one preview per 0.5 seconds. | Use an ordered/coalescing preview writer and drain it when synthesis closes; retain the current late-preview rejection and incomplete-report semantics. |
| `orchestrator.blocking_call`, `llm`, `budget.StageBackend` | Model calls use synchronous SDK streams and one daemon thread per stage; cancellation is polled every 25 ms. | A native async backend could reduce thread/scheduling overhead, but must preserve model reservations, usage reconciliation, partial checkpoints, streaming callbacks, provider cooldowns and prompt interruption. Preserve the existing uncooperative-sync-adapter deadline regression. |
| `EvidenceStore.apersist` | A deep copy remains on the owner loop; raw source content is intentionally retained and appears in multiple parts of the state. | Measure copying/memory costs for large evidence collections. Consider immutable raw-document storage referenced by snapshots, with explicit export/migration behavior. |

Judging, final writing and answer assessment have evidence dependencies. Making those stages concurrent would change the research/quality contract. The one-active-run UI policy also remains an admission decision, distinct from independent async agent sessions.

## Verification

Benchmarks use deterministic providers and local files; their timings are not production latency, model-quality or billing claims.

Command before and after: `.venv/bin/python scripts/qa_load.py --sessions 20 --concurrency 5 --output reports/qa/performance-{before,after}.json` (run each output separately). Both runs use the same default five search starts/second, fake search latency and tracing instrumentation. All 20 sessions passed in each run.

| Metric | Before | After |
| --- | ---: | ---: |
| Total 20-session runtime | 13.603 s | 13.307 s |
| Session p50 | 3.276 s | 3.308 s |
| Session p95 | 3.519 s | 3.328 s |
| Event-loop lag p95 | 18.111 ms | 7.772 ms |
| CPU time | 5.994 s | 6.188 s |
| Peak traced Python allocation | 4,591,813 bytes | 5,046,297 bytes |

The observed loop-lag reduction is approximately 57%; wall-time improvement is small because rate pacing dominates these short-source fixtures. This is one before/after sample, not a statistical production speedup claim. Freezing evidence adds copying/allocation costs. Both runs ended with four file descriptors, one thread and no temporary snapshot files; these checks do not establish long-term leak freedom.

- Full backend with real PostgreSQL and coverage: **275 total, 272 passed, zero failures/errors, three explicitly disabled live-provider skips**, 148.286 seconds. Command: `TEST_DATABASE_URL=postgresql://george@127.0.0.1:55433/postgres .venv/bin/python scripts/run_tests.py --suite all --coverage --require-database --output reports/qa/performance-review`. All nine PostgreSQL tests ran; artifacts include JSON/JUnit results and coverage.
- Chromium: **32 passed**, 2.3 minutes, including actual orchestration, early extraction, cancellation, reconnect/replay, editing, autosave and Error Center behavior. Artifacts: `reports/qa/performance-browser/`.
- Node components: **36 passed, one expected standalone-HTTP skip**, no failures. The full Python suite exercises real loopback HTTP.
- Focused transport/cancellation/retry checks after the final cancellation guard: **20 passed**.
- Scoped fatal lint, QA formatting, config/models typing and `git diff --check`: passed.

The first sandbox-only backend/browser attempts failed to bind local sockets. Verified reruns used approved loopback access; the backend runner continued to block external providers. PostgreSQL used the disposable QA cluster on port 55433 and random test schemas. The workspace database and real `.env` values were not accessed. No paid calls, commits or deployment occurred.


Current coverage is **91.657% lines / 85.273% branches**. Critical branch coverage is budget **94.286%** (95% target), evidence store **90.909%** (90% target), judge **94.444%** (95% target), and orchestrator **85.204%** (95% target). The release coverage gate remains **unmet**; the ordinary regression run passes without enabling that separate release gate. No target or exclusion was weakened. Historical handoffs contain older coverage snapshots; use this run for the current measured result.

The disposable PostgreSQL cluster was stopped after testing. No QA/browser servers were intentionally left running. Live/human/hosted-CI validation remains outstanding, as do the prioritized follow-up opportunities above.
