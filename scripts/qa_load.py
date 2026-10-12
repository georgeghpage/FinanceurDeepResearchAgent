#!/usr/bin/env python3
"""Bounded deterministic load/soak for independent agents; never paid providers."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from deepsearchagent.config import Config
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * q + 0.5)))]


def descriptor_count():
    for path in ("/proc/self/fd", "/dev/fd"):
        if Path(path).is_dir():
            return len(list(Path(path).iterdir()))
    return None


async def workload(args, root):
    semaphore = asyncio.Semaphore(args.concurrency)
    results, lag = [], []
    started = time.monotonic()
    done = asyncio.Event()

    async def observe_loop():
        while not done.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.01)
            lag.append(max(0, time.monotonic() - before - 0.01))

    monitor = asyncio.create_task(observe_loop())

    async def one(index):
        queued = time.monotonic()
        async with semaphore:
            if args.seconds and time.monotonic() - started >= args.seconds:
                return
            begin = time.monotonic()
            events = []

            def record(event):
                events.append({**event, "observed_seconds": time.monotonic() - begin})

            backend, search = AutoBackend(), FakeAsyncSearch(delay=args.search_delay)
            try:
                state, _ = await ResearchAgent(
                    backend,
                    Config(session_dir=root),
                    SearchRouter(search),
                    trace=record,
                    judge=FakeJudge(),
                ).arun(f"Load fixture {index}")
                valid = (
                    state.stop_reason == "coverage"
                    and len(state.findings) == 3
                    and state.search_calls == 15
                )
                stages = [e for e in events if e.get("kind") == "stage_start"]
                stage_times = {}
                for stage_index, event in enumerate(stages):
                    end = (
                        stages[stage_index + 1]["observed_seconds"]
                        if stage_index + 1 < len(stages)
                        else time.monotonic() - begin
                    )
                    stage_times[event["stage"]] = (
                        stage_times.get(event["stage"], 0)
                        + end
                        - event["observed_seconds"]
                    )
                results.append(
                    {
                        "id": index,
                        "passed": valid,
                        "seconds": time.monotonic() - begin,
                        "queue_seconds": begin - queued,
                        "tokens": state.tokens_used,
                        "calls": state.api_calls,
                        "searches": state.search_calls,
                        "peak_searches": search.peak,
                        "peak_researchers": backend.peak,
                        "stop_reason": state.stop_reason,
                        "stage_seconds": stage_times,
                        "trace": [
                            {"stage": e["stage"], "kind": e["kind"]}
                            for e in events
                            if e.get("kind") == "stage_start"
                        ],
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "id": index,
                        "passed": False,
                        "seconds": time.monotonic() - begin,
                        "error_type": type(exc).__name__,
                    }
                )

    try:
        # Limit queued tasks as well as running work; no unbounded task backlog.
        for offset in range(0, args.sessions, args.concurrency):
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            await asyncio.gather(
                *(
                    one(i)
                    for i in range(
                        offset, min(offset + args.concurrency, args.sessions)
                    )
                )
            )
    finally:
        done.set()
        await monitor
    return results, lag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="Optional scheduling window (also bounded by session count)",
    )
    parser.add_argument("--search-delay", type=float, default=0.005)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/qa/performance.json"
    )
    args = parser.parse_args()
    if (
        not 1 <= args.sessions <= 10000
        or not 1 <= args.concurrency <= 50
        or not 0 <= args.seconds <= 3600
        or not 0 <= args.search_delay <= 1
    ):
        parser.error(
            "Use 1–10000 sessions, 1–50 concurrency, 0–3600 seconds and 0–1s fake search delay"
        )
    before_fds, before_threads = descriptor_count(), threading.active_count()
    tracemalloc.start()
    cpu, started = time.process_time(), time.monotonic()
    with tempfile.TemporaryDirectory() as root:
        results, lag = asyncio.run(workload(args, root))
        leftovers = len(list(Path(root).rglob(".snapshot-*")))
    elapsed, cpu_elapsed = time.monotonic() - started, time.process_time() - cpu
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    times = [r["seconds"] for r in results]
    stage_names = sorted({name for r in results for name in r.get("stage_seconds", {})})
    result = {
        "workload": "scripted_providers_no_live_billing",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sessions": len(results),
        "concurrency": args.concurrency,
        "duration_seconds": elapsed,
        "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results),
        "p50_seconds": percentile(times, 0.5),
        "p95_seconds": percentile(times, 0.95),
        "p99_seconds": percentile(times, 0.99) if len(times) >= 100 else None,
        "stage_latency_seconds": {
            name: {
                "p50": percentile(
                    [
                        r["stage_seconds"][name]
                        for r in results
                        if name in r.get("stage_seconds", {})
                    ],
                    0.5,
                ),
                "p95": percentile(
                    [
                        r["stage_seconds"][name]
                        for r in results
                        if name in r.get("stage_seconds", {})
                    ],
                    0.95,
                ),
            }
            for name in stage_names
        },
        "stage_note": "Search stage includes result persistence; extract stage includes serialized consolidation. Stage wall time may include contention from other sessions.",
        "sessions_per_second": len(results) / elapsed,
        "cpu_seconds": cpu_elapsed,
        "python_current_bytes": current,
        "python_peak_bytes": peak,
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        "event_loop_lag_p95_seconds": percentile(lag, 0.95),
        "fds_before": before_fds,
        "fds_after": descriptor_count(),
        "threads_before": before_threads,
        "threads_after": threading.active_count(),
        "temporary_snapshots_left": leftovers,
        "real_provider_cost_usd": None,
        "synthetic_tokens_mean": statistics.mean([r.get("tokens", 0) for r in results])
        if results
        else None,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(
        "# Deterministic performance baseline\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in result.items() if k != "results")
        + "\n\nFake-provider timings are not production SLOs. A short run cannot establish absence of leaks.\n"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, indent=2))
    return 0 if result["sessions"] and not result["failed"] and not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
