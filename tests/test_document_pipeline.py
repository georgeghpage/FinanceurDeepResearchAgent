"""Targeted contracts for lossless evidence, fan-out and independent writing."""

import asyncio
import json
from pathlib import Path
import runpy
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from deepsearchagent.config import Config
from deepsearchagent.budget import RunBudget, StageBackend
from deepsearchagent.llm import BudgetExceeded, KimiBackend
from deepsearchagent.models import (
    EvidenceExtraction,
    ReportDraft,
    ResearchTask,
    SearchResult,
)
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.fetch import parse_tavily_result
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge, plan


class SearchContracts(unittest.IsolatedAsyncioTestCase):
    async def test_queued_searches_cannot_start_after_shared_budget_exhaustion(self):
        provider = FakeAsyncSearch(delay=0.01)
        config = Config(
            max_searches=1, search_concurrency=1, search_requests_per_second=10000
        )
        budget = RunBudget(config)
        router = SearchRouter(provider)
        router.configure(config, budget)
        results = await asyncio.gather(
            *(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=1)
                for i in range(4)
            ),
            return_exceptions=True,
        )
        self.assertEqual(len(provider.queries), 1)
        self.assertEqual(budget.searches, 1)
        self.assertEqual(sum(isinstance(result, SearchResult) for result in results), 1)
        failures = [result for result in results if isinstance(result, BudgetExceeded)]
        self.assertEqual(
            [result.category for result in failures], ["search_budget"] * 3
        )

    async def test_queueing_does_not_consume_socket_time_or_extra_attempts(self):
        provider = FakeAsyncSearch(delay=0.05)
        config = Config(search_concurrency=1, search_requests_per_second=10000)
        budget = RunBudget(config)
        router = SearchRouter(provider)
        router.configure(config, budget)
        results = await asyncio.gather(
            *(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=0.07)
                for i in range(3)
            )
        )
        self.assertTrue(all(result.sources for result in results))
        self.assertEqual(budget.searches, 3)
        self.assertEqual(len(provider.queries), 3)

    async def test_congested_queue_paces_actual_transport_starts(self):
        starts = []

        class Provider:
            async def asearch(self, query, *, timeout):
                starts.append(asyncio.get_running_loop().time())
                if query == "0":
                    await asyncio.sleep(0.1)
                return SearchResult(text="", sources=[])

        router = SearchRouter(Provider())
        router.configure(Config(search_concurrency=1, search_requests_per_second=50))
        await asyncio.gather(
            *(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=1)
                for i in range(4)
            )
        )
        self.assertTrue(all(b - a >= 0.015 for a, b in zip(starts, starts[1:])), starts)

    async def test_cancelled_queue_does_not_reserve_unstarted_requests(self):
        started = asyncio.Event()

        class Provider:
            async def asearch(self, query, *, timeout):
                started.set()
                await asyncio.sleep(10)

        config = Config(search_concurrency=1, search_requests_per_second=10000)
        budget = RunBudget(config)
        router = SearchRouter(Provider())
        router.configure(config, budget)
        tasks = [
            asyncio.create_task(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=1)
            )
            for i in range(4)
        ]
        await started.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(budget.searches, 1)
        self.assertEqual(router._inflight, {})

    async def test_duplicate_inflight_and_cached_searches_are_isolated(self):
        provider = FakeAsyncSearch(delay=0.02)
        router = SearchRouter(provider)
        router.configure(Config(search_requests_per_second=10000))
        task = ResearchTask(question="q", query="identical query")
        a, b = await asyncio.gather(
            router.asearch(task, timeout=1), router.asearch(task, timeout=1)
        )
        a.sources[0].id = "mutated"
        c = await router.asearch(task, timeout=1)
        self.assertEqual(len(provider.queries), 1)
        self.assertNotEqual(b.sources[0].id, "mutated")
        self.assertNotEqual(c.sources[0].id, "mutated")
        await router.aclose()

    async def test_timeouts_receive_at_most_three_retries(self):
        class Slow:
            calls = 0

            async def asearch(self, query, *, timeout):
                self.calls += 1
                await asyncio.sleep(1)
                return SearchResult(text="", sources=[])

        provider = Slow()
        router = SearchRouter(provider)
        router.configure(
            Config(search_backoff=0, search_requests_per_second=10000, max_seconds=3)
        )
        with self.assertRaises(Exception):
            await router.asearch(ResearchTask(question="q", query="slow"), timeout=0.01)
        self.assertEqual(provider.calls, 4)
        await router.aclose()

    async def test_cancelled_waiter_does_not_cancel_other_waiter(self):
        provider = FakeAsyncSearch(delay=0.04)
        router = SearchRouter(provider)
        router.configure(Config(search_requests_per_second=10000))
        query = ResearchTask(question="q", query="shared")
        a = asyncio.create_task(router.asearch(query, timeout=1))
        b = asyncio.create_task(router.asearch(query, timeout=1))
        await asyncio.sleep(0.01)
        a.cancel()
        await asyncio.gather(a, return_exceptions=True)
        self.assertTrue((await b).sources)
        self.assertEqual(len(provider.queries), 1)
        self.assertEqual(router._inflight, {})

    async def test_rate_gate_bounds_request_starts(self):
        starts = []

        class Provider:
            async def asearch(self, query, *, timeout):
                starts.append(asyncio.get_running_loop().time())
                return SearchResult(text="", sources=[])

        router = SearchRouter(Provider())
        router.configure(Config(search_requests_per_second=20))
        await asyncio.gather(
            *(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=1)
                for i in range(3)
            )
        )
        self.assertGreaterEqual(starts[2] - starts[0], 0.09)

    async def test_slow_trace_cannot_compress_transport_start_intervals(self):
        import time

        starts = []

        class Provider:
            async def asearch(self, query, *, timeout):
                starts.append(time.monotonic())
                return SearchResult(text="", sources=[])

        router = SearchRouter(Provider())
        router.configure(Config(search_requests_per_second=20))
        stalled = False

        def trace(event):
            nonlocal stalled
            if event["kind"] == "search_start" and not stalled:
                stalled = True
                time.sleep(0.07)

        router.trace = trace
        await asyncio.gather(
            *(
                router.asearch(ResearchTask(question="q", query=str(i)), timeout=1)
                for i in range(3)
            )
        )
        self.assertEqual(len(starts), 3)
        for previous, current in zip(starts, starts[1:]):
            self.assertGreaterEqual(current - previous, 0.045)

    async def test_cancellation_in_start_trace_prevents_transport_entry(self):
        from deepsearchagent.cancellation import Cancellation, ResearchCancelled

        provider = FakeAsyncSearch(delay=0)
        router = SearchRouter(provider)
        router.configure(Config(search_requests_per_second=10000))
        router.cancellation = Cancellation()

        def trace(event):
            if event["kind"] == "search_start":
                router.cancellation.cancel()

        router.trace = trace
        with self.assertRaises(ResearchCancelled):
            await router.asearch(ResearchTask(question="q", query="cancel"), timeout=1)
        self.assertEqual(provider.queries, [])
        self.assertEqual(router._inflight, {})

    async def test_retrieval_waits_for_async_result_acceptance(self):
        from deepsearchagent.retrieval import retrieve

        config = Config(search_requests_per_second=10000)
        budget = RunBudget(config)
        router = SearchRouter(FakeAsyncSearch(delay=0))
        entered, release = asyncio.Event(), asyncio.Event()
        accepted = []

        async def accept(query, result):
            entered.set()
            await release.wait()
            accepted.append(query.id)

        task = asyncio.create_task(
            retrieve(
                plan().search_queries[:1], router, budget, lambda event: None, accept
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            self.assertEqual(accepted, [])
        finally:
            release.set()
        results = await task
        self.assertEqual(accepted, ["Q1"])
        self.assertTrue(results[0].sources)


class PipelineContracts(unittest.TestCase):
    def test_writer_comparison_freezes_identical_evidence_and_keeps_quality_unmeasured(
        self,
    ):
        compare = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/evaluate_writers.py")
        )["compare"]
        with tempfile.TemporaryDirectory() as root:
            config = Config(session_dir=root, search_requests_per_second=10000)
            state, _ = ResearchAgent(
                AutoBackend(),
                config,
                SearchRouter(FakeAsyncSearch()),
                judge=FakeJudge(),
            ).run("Facts")
            original = state.model_dump_json()
            inputs = []

            def factory(candidate):
                backend = AutoBackend()
                backend.config = candidate
                structured = backend.structured

                def capture(schema, instructions, payload, **options):
                    inputs.append((instructions, payload, options["output_tokens"]))
                    return structured(schema, instructions, payload, **options)

                backend.structured = capture
                return backend

            models = [config.model, "qwen3.8-max"]
            rates = {model: {"input": 0, "output": 0} for model in models}
            scored = compare(
                state,
                config,
                models,
                Path(root) / "comparison",
                factory=factory,
                rates=rates,
            )
            self.assertEqual(len(inputs), 2)
            self.assertEqual(inputs[0], inputs[1])
            self.assertTrue(
                all(result["passed_references"] for result in scored["results"])
            )
            self.assertEqual([r["calls"] for r in scored["results"]], [1, 1])
            self.assertIsNone(scored["factual_preservation"])
            self.assertIsNone(scored["analytical_quality"])
            prepared = compare(state, config, models, Path(root) / "offline")
            self.assertFalse(prepared["executed"])
            self.assertEqual(prepared["results"], [])
            self.assertEqual(state.model_dump_json(), original)

    def test_writer_clone_uses_independent_prices_and_context_limits(self):
        config = Config(
            input_price_per_million=99,
            output_price_per_million=99,
            final_writer_input_price_per_million=2,
            final_writer_output_price_per_million=6,
        )
        original = KimiBackend(config, "fixture-key")
        self.addCleanup(original.client.close)
        writing = original.for_model("unknown-writer")
        self.addCleanup(writing.client.close)
        self.assertEqual(writing.config.input_price_per_million, 2)
        self.assertEqual(writing.config.output_price_per_million, 6)
        fake = AutoBackend()
        fake.config = writing.config
        budget = RunBudget(Config(max_cost_usd=1, writer_reserve_tokens=0))
        from deepsearchagent.models import PlanningBrief

        StageBackend(fake, budget, 40000, 64000, writing=True).structured(
            PlanningBrief, "Plan", "{}"
        )
        self.assertEqual(fake.limits[0][1]["output_tokens"], 8192)
        self.assertAlmostEqual(budget.cost, (80 * 2 + 20 * 6) / 1_000_000)
        with self.assertRaises(BudgetExceeded):
            StageBackend(fake, budget, 40000, 64000, writing=True).structured(
                PlanningBrief, "Plan", "x" * 33000
            )
        self.assertEqual(fake.calls, 1)

    def test_research_cost_reserve_uses_final_writer_rates(self):
        budget = RunBudget(
            Config(
                final_writer_model="writer",
                final_writer_input_price_per_million=20,
                final_writer_output_price_per_million=20,
                max_cost_usd=0.5,
            )
        )
        with self.assertRaises(BudgetExceeded) as caught:
            budget.reserve_model(100, 100, (0, 0))
        self.assertEqual(caught.exception.category, "cost_budget")
        self.assertEqual(budget.calls, 0)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_search()
        self.assertEqual(budget.searches, 0)
        budget = RunBudget(Config(final_writer_model="writer", max_cost_usd=1))
        with self.assertRaises(BudgetExceeded) as caught:
            budget.reserve_model(100, 100, (0, 0))
        self.assertEqual(caught.exception.category, "unpriced_model")

    def test_original_content_is_not_truncated(self):
        original = "navigation " * 1000 + "Material finding: 42 units."
        result = parse_tavily_result(
            SimpleNamespace(
                structuredContent={
                    "results": [
                        {
                            "url": "https://example.com/a",
                            "title": "Primary",
                            "raw_content": original,
                        }
                    ]
                },
                isError=False,
            )
        )
        self.assertEqual(result.documents[0]["content"], original)
        self.assertFalse(result.documents[0]["truncated"])

    def test_fast_group_extracts_before_unrelated_slow_search(self):
        extracted = threading.Event()
        slow_queries = {q.query for q in plan().search_queries[5:]}

        class Search(FakeAsyncSearch):
            async def asearch(self, query, *, timeout):
                if query in slow_queries:
                    for _ in range(200):
                        if extracted.is_set():
                            break
                        await asyncio.sleep(0.005)
                    if not extracted.is_set():
                        raise AssertionError("Extraction waited for unrelated searches")
                return await super().asearch(query, timeout=timeout)

        class Backend(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                if schema is EvidenceExtraction:
                    extracted.set()
                return super().structured(schema, instructions, payload, **options)

        with tempfile.TemporaryDirectory() as root:
            state, _ = ResearchAgent(
                Backend(),
                Config(session_dir=root, search_requests_per_second=10000),
                SearchRouter(Search()),
                judge=FakeJudge(),
            ).run("Facts")
        self.assertTrue(extracted.is_set())
        self.assertEqual(state.status, "COMPLETE", state.warnings)
        self.assertEqual(state.search_calls, 15)

    def test_writer_gets_raw_passages_and_uses_separate_backend(self):
        research, writing = AutoBackend(), AutoBackend()
        writing.close_stream = lambda: None
        captured = []
        original = writing.structured

        def structured(schema, instructions, payload, **options):
            if schema is ReportDraft:
                captured.append(json.loads(payload))
                self.assertIn("Senior Research Analyst", instructions)
            return original(schema, instructions, payload, **options)

        writing.structured = structured
        research.for_model = lambda name, provider=None: writing
        with tempfile.TemporaryDirectory() as root:
            state, _ = ResearchAgent(
                research,
                Config(
                    session_dir=root,
                    final_writer_model="writer-model",
                    search_requests_per_second=10000,
                ),
                SearchRouter(FakeAsyncSearch()),
                judge=FakeJudge(),
            ).run("Facts")
        self.assertEqual(state.status, "COMPLETE", state.warnings)
        self.assertNotIn("ReportDraft", [name for name, _ in research.limits])
        self.assertTrue(captured[0]["evidence"]["source_passages"])
        self.assertIn("42", captured[0]["evidence"]["source_passages"][0]["content"])

    def test_backend_model_configuration(self):
        with patch(
            "deepsearchagent.config.environment_values",
            return_value={
                "RESEARCH_MODEL": "deepseek-v4.1-flash",
                "FINAL_WRITER_MODEL": "qwen3.8-max",
            },
        ):
            config = Config.from_env()
        self.assertEqual(config.model, "deepseek-v4.1-flash")
        self.assertEqual(config.final_writer_model, "qwen3.8-max")


class PreviewContracts(unittest.IsolatedAsyncioTestCase):
    async def test_late_writer_callbacks_cannot_change_terminal_preview(self):
        release, finished = threading.Event(), threading.Event()

        class LateWriter(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                result = super().structured(schema, instructions, payload, **options)
                if schema is ReportDraft:
                    content = result.model_dump_json()
                    options["on_partial"](content)
                    release.wait(3)
                    options["on_partial"](content)
                    finished.set()
                return result

        events, owners = [], []

        def trace(event):
            events.append(event)
            if event["kind"] == "report_preview":
                owners.append(threading.get_ident())

        with tempfile.TemporaryDirectory() as root:
            agent = ResearchAgent(
                LateWriter(),
                Config(
                    session_dir=root,
                    request_timeout=0.15,
                    quality_max_iterations=0,
                    search_requests_per_second=10000,
                ),
                SearchRouter(FakeAsyncSearch()),
                judge=FakeJudge(),
                trace=trace,
            )
            try:
                state, report = await agent.arun("Facts")
                self.assertEqual(state.report_status, "incomplete")
                self.assertIn("42", report)
                count = len(events)
                self.assertEqual(owners, [threading.get_ident()])
                await asyncio.sleep(0.5)
                release.set()
                for _ in range(100):
                    if finished.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(finished.is_set())
                await asyncio.sleep(0.03)
                self.assertEqual(len(events), count)
            finally:
                release.set()
