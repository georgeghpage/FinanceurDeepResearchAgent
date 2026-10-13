"""Compact progress, bounded tool recovery and complete offline research flows."""

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from deepsearchagent.budget import RunBudget
from deepsearchagent.cancellation import Cancellation, ResearchCancelled
from deepsearchagent.config import Config
from deepsearchagent.diagnostics import DiagnosticJournal, failure_from_exception
from deepsearchagent.models import PlannedQuery, SearchResult
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.progress import ResearchProgress
from deepsearchagent.retrieval import retrieve
from deepsearchagent.tools.fetch import parse_tavily_result
from deepsearchagent.tools.retry import RetryPolicy
from deepsearchagent.tools.search import SearchError, SearchRouter
from deepsearchagent.ui import ResearchRun
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_attempts_recover_then_exhaust_as_structured_failure(self):
        for recover in (True, False):
            calls, events = [], []

            class Provider:
                async def asearch(self, query, *, timeout):
                    calls.append(timeout)
                    if recover and len(calls) == 4:
                        return SearchResult(text="recovered", sources=[])
                    raise httpx.ConnectError("PRIVATE payload")

            budget = RunBudget(Config(search_backoff=1, search_jitter=0))
            with patch(
                "deepsearchagent.tools.retry.asyncio.sleep", AsyncMock()
            ) as sleeper:
                result = await retrieve(
                    [
                        PlannedQuery(
                            id="Q1", query="private query", requirement_ids=["R1"]
                        )
                    ],
                    SearchRouter(Provider()),
                    budget,
                    events.append,
                )
            self.assertEqual(len(calls), 4)
            self.assertEqual([c.args[0] for c in sleeper.call_args_list], [1, 2, 4])
            self.assertEqual(budget.search_retries, 3)
            self.assertEqual(bool(result[0].failure), not recover)
            self.assertEqual(result[0].text, "recovered" if recover else "")
            self.assertIn(
                "search_recovered" if recover else "search_degraded",
                [e["kind"] for e in events],
            )
            self.assertNotIn("PRIVATE", json.dumps(events))

    async def test_permanent_errors_and_session_retry_budget(self):
        for category in (
            "authentication",
            "quota_exhausted",
            "account_restricted",
            "invalid_request",
        ):
            calls = []

            class Provider:
                async def asearch(self, query, *, timeout):
                    calls.append(query)
                    raise SearchError("PRIVATE", category=category)

            router = SearchRouter(Provider())
            router.configure(Config(search_backoff=0))
            with self.assertRaises(SearchError) as raised:
                await router.asearch(
                    SimpleNamespace(source="web", query="q"), timeout=5
                )
            self.assertEqual(calls, ["q"])
            self.assertEqual(raised.exception.diagnostic.category, category)

        class Transient:
            async def asearch(self, query, *, timeout):
                raise TimeoutError("PRIVATE")

        budget = RunBudget(Config(max_search_retries=2, search_backoff=0))
        results = await retrieve(
            [
                PlannedQuery(id=f"Q{i}", query=f"q{i}", requirement_ids=["R1"])
                for i in range(3)
            ],
            SearchRouter(Transient()),
            budget,
            lambda e: None,
        )
        self.assertEqual(budget.searches, 5)
        self.assertEqual(budget.search_retries, 2)
        self.assertTrue(all(r.failure.category == "timeout" for r in results))

    def test_http_failures_distinguish_quota_rate_limits_and_unsupported_operations(
        self,
    ):
        for status, category in (
            (408, "timeout"),
            (429, "rate_limit"),
            (432, "quota_exhausted"),
            (433, "quota_exhausted"),
            (500, "provider_unavailable"),
            (502, "provider_unavailable"),
            (503, "provider_unavailable"),
            (504, "provider_unavailable"),
            (501, "invalid_request"),
        ):
            response = httpx.Response(
                status, request=httpx.Request("POST", "https://fixture.invalid")
            )
            error = httpx.HTTPStatusError(
                "PRIVATE", request=response.request, response=response
            )
            diagnostic = failure_from_exception(
                error, provider="tavily", stage="search", operation="search"
            )
            self.assertEqual(diagnostic.category, category)
            self.assertEqual(diagnostic.http_status, status)

    def test_malformed_entries_are_failures_and_empty_results_are_valid(self):
        with self.assertRaises(SearchError) as raised:
            parse_tavily_result(
                SimpleNamespace(
                    isError=False, structuredContent={"results": [None]}, content=[]
                )
            )
        self.assertEqual(raised.exception.category, "result_format")
        result = parse_tavily_result(
            SimpleNamespace(
                isError=False, structuredContent={"results": []}, content=[]
            )
        )
        self.assertIsNone(result.failure)
        self.assertEqual(result.sources, [])

    async def test_retry_sleep_is_cancellable_without_another_request(self):
        entered = asyncio.Event()
        cancellation = Cancellation()
        calls = []

        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()

        async def operation(timeout, meta):
            calls.append(timeout)
            raise httpx.ConnectError("PRIVATE")

        policy = RetryPolicy(Config(), sleep=sleep, uniform=lambda a, b: 0)
        task = asyncio.create_task(
            policy.run(
                operation,
                timeout=30,
                provider="tavily",
                trace=lambda e: None,
                cancellation=cancellation,
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        cancellation.cancel()
        with self.assertRaises(ResearchCancelled):
            await asyncio.wait_for(task, 1)
        self.assertEqual(len(calls), 1)

    async def test_backoff_cap_and_malformed_response_recovery(self):
        events = []
        provider = SimpleNamespace(
            asearch=AsyncMock(
                side_effect=[
                    SearchError("bad format", category="result_format"),
                    SearchResult(text="ok", sources=[]),
                ]
            )
        )
        router = SearchRouter(provider)
        router.configure(Config(search_backoff=100, search_max_delay=15))
        router.trace = events.append
        with patch.object(router.retry_policy, "sleep", AsyncMock()) as sleeper:
            result = await router.asearch(
                SimpleNamespace(source="web", query="q"), timeout=60
            )
        self.assertEqual(result.text, "ok")
        sleeper.assert_awaited_once_with(15)
        self.assertEqual(events[-1]["kind"], "search_recovered")

    async def test_full_research_recovers_or_preserves_independent_evidence(self):
        for fail_all, permanent in ((False, False), (False, True), (True, False)):

            class Provider(FakeAsyncSearch):
                def __init__(self):
                    super().__init__(delay=0)
                    self.attempts = {}

                async def asearch(self, query, *, timeout):
                    self.attempts[query] = self.attempts.get(query, 0) + 1
                    if fail_all or (permanent and query.endswith("variant 1")):
                        raise SearchError(
                            "PRIVATE",
                            category="quota_exhausted"
                            if permanent
                            else "provider_unavailable",
                        )
                    if self.attempts[query] == 1:
                        raise httpx.ConnectError("PRIVATE")
                    return await super().asearch(query, timeout=timeout)

            with tempfile.TemporaryDirectory() as root:
                run = ResearchRun("run-one", "Facts")
                provider = Provider()
                state, report = await ResearchAgent(
                    AutoBackend(),
                    Config(session_dir=root, search_backoff=0, max_searches=70),
                    SearchRouter(provider),
                    judge=FakeJudge(),
                    trace=lambda e: run.emit({"type": "trace", **e}),
                ).arun("Facts")
                self.assertIsNone(state.failure)
                self.assertEqual(
                    state.status,
                    "FAILED" if fail_all else "COMPLETE",
                )
                self.assertNotIn("PRIVATE", report)
                if permanent or fail_all:
                    self.assertTrue(
                        any("Source coverage incomplete" in w for w in state.warnings)
                    )
                self.assertNotIn("Source coverage incomplete", report)
                if fail_all:
                    self.assertEqual(state.sources, [])
                    self.assertIn("No source-backed evidence", report)
                else:
                    self.assertTrue(state.sources)
                    self.assertTrue(state.findings)
                statuses = [e for e in run.events if e["type"] == "status"]
                self.assertEqual(statuses[0]["code"], "planning")
                self.assertIn("writing", [e["code"] for e in statuses])
                self.assertTrue(all(e["run_id"] == "run-one" for e in statuses))

    def test_tavily_error_classification_never_retains_provider_payload(self):
        for payload, category in (
            ("Invalid API key PRIVATE", "authentication"),
            ("Usage limit exceeded PRIVATE", "quota_exhausted"),
            ("Rate limit reached PRIVATE", "rate_limit"),
            ("Temporary service unavailable PRIVATE", "provider_unavailable"),
            ("Invalid parameter PRIVATE", "invalid_request"),
        ):
            with self.assertRaises(SearchError) as raised:
                parse_tavily_result(
                    SimpleNamespace(
                        isError=True,
                        structuredContent=None,
                        content=[SimpleNamespace(type="text", text=payload)],
                    )
                )
            failure = failure_from_exception(
                raised.exception,
                provider="tavily",
                stage="search",
                operation="parse_result",
            )
            self.assertEqual(failure.category, category)
            self.assertNotIn("PRIVATE", failure.model_dump_json())


class ProgressTests(unittest.TestCase):
    def test_duplicates_tokens_concurrent_retries_and_late_events_are_stable(self):
        progress = ResearchProgress()
        self.assertEqual(
            progress.receive({"kind": "stage_start", "stage": "plan"})["code"],
            "planning",
        )
        self.assertIsNone(progress.receive({"kind": "stage_start", "stage": "plan"}))
        for kind in ("thinking_delta", "output_delta", "tavily_step", "findings"):
            self.assertIsNone(progress.receive({"kind": kind, "text": "PRIVATE"}))
        progress.receive({"kind": "stage_start", "stage": "search"})
        progress.receive({"kind": "search_start", "search_id": 1})
        progress.receive({"kind": "search_start", "search_id": 2})
        progress.receive({"kind": "search_error", "search_id": 1})
        self.assertIsNone(progress.receive({"kind": "search_retry", "search_id": 1}))
        update = progress.receive(
            {
                "kind": "search_complete",
                "search_id": 2,
                "sources": [{"url": "https://a"}, {"url": "https://a"}],
            }
        )
        self.assertEqual(update["source_count"], 1)
        self.assertIsNone(progress.receive({"kind": "search_retry"}))
        progress.receive({"kind": "stage_start", "stage": "write"})
        self.assertIsNone(progress.receive({"kind": "search_retry"}))
        self.assertEqual(
            progress.receive({"kind": "cancellation_requested"})["code"], "stopping"
        )
        self.assertIsNone(
            progress.receive({"kind": "stage_start", "stage": "validate"})
        )

    def test_journal_keeps_retry_metadata_but_no_queries_or_answer(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            journal = DiagnosticJournal(path, "run-one")
            journal.record(
                {
                    "kind": "search_retry",
                    "query": "PRIVATE",
                    "text": "PRIVATE",
                    "query_ref": "safe-ref",
                    "attempt": 2,
                    "retry_delay_seconds": 2,
                    "category": "timeout",
                    "recovered": False,
                }
            )
            payload = path.read_text()
            self.assertNotIn("PRIVATE", payload)
            self.assertEqual(json.loads(payload)["retry_delay_seconds"], 2)
