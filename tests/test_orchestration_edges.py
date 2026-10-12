"""Bounded stage deadlines, late results, refinement and cleanup outcomes."""

import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch

from deepsearchagent.cancellation import Cancellation
from deepsearchagent.config import Config
from deepsearchagent.llm import BudgetExceeded, ResponseError
from deepsearchagent.models import ReportDraft
from deepsearchagent.orchestrator import ResearchAgent, blocking_call
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge


class OrchestrationEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_discards_late_result_and_exception(self):
        for fails in (False, True):
            entered, release, exited = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )

            def provider():
                entered.set()
                release.wait(3)
                try:
                    if fails:
                        raise RuntimeError("CANARY-LATE")
                    return "too late"
                finally:
                    exited.set()

            try:
                with self.assertRaises(BudgetExceeded) as failure:
                    await blocking_call(provider, 0.01)
                self.assertEqual(failure.exception.category, "request_timeout")
                self.assertTrue(entered.is_set())
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(exited.wait, 1))

    async def test_cancel_during_writing_preserves_collected_evidence(self):
        cancellation, entered, release = (
            Cancellation(),
            threading.Event(),
            threading.Event(),
        )

        class BlockedWriter(AutoBackend):
            def structured(self, schema, *args, **kwargs):
                if schema is ReportDraft:
                    entered.set()
                    release.wait(3)
                return super().structured(schema, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            backend = BlockedWriter()
            task = asyncio.create_task(
                ResearchAgent(
                    backend,
                    Config(session_dir=root),
                    SearchRouter(FakeAsyncSearch(delay=0)),
                    cancellation=cancellation,
                    judge=FakeJudge(),
                ).arun("q")
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                cancellation.cancel()
                state, report = await asyncio.wait_for(task, 1)
                self.assertEqual(state.status, "CANCELLED")
                self.assertEqual(len(state.findings), 3)
                self.assertIn("42", report)
                self.assertFalse(
                    any(name == "ReportAudit" for name, _ in backend.limits)
                )
            finally:
                release.set()

    async def test_judge_initialization_failure_is_qualified_and_cleanup_is_safe(self):
        class OwnedJudge(FakeJudge):
            def close(self):
                raise RuntimeError("CANARY-CLOSE")

        with tempfile.TemporaryDirectory() as root:
            events = []
            with patch(
                "deepsearchagent.orchestrator.OpenAICompatibleJudge",
                side_effect=[ValueError("CANARY-INIT"), OwnedJudge()],
            ):
                agent = ResearchAgent(
                    AutoBackend(),
                    Config(
                        session_dir=root,
                        judge_provider="pai",
                        judge_fallback_provider="kimi",
                    ),
                    SearchRouter(FakeAsyncSearch(delay=0)),
                    trace=events.append,
                )
                state, report = await agent.arun("q")
            self.assertEqual(state.stop_reason, "coverage")
            self.assertIn("classifier_fallback", [e["kind"] for e in events])
            self.assertIn("cleanup_error", [e["kind"] for e in events])
            self.assertNotIn("CANARY", report)

    async def test_already_executed_followups_stop_without_new_searches(self):
        judge = FakeJudge(
            ("RESEARCH_MORE",), queries=["  OFFICIAL evidence fact 1 variant 1  "]
        )
        with tempfile.TemporaryDirectory() as root:
            state, report = await ResearchAgent(
                AutoBackend(),
                Config(session_dir=root),
                SearchRouter(FakeAsyncSearch(delay=0)),
                judge=judge,
            ).arun("q")
            self.assertEqual(state.stop_reason, "no_tasks")
            self.assertEqual(state.search_calls, 15)
            self.assertIn("No useful new targeted queries", report)

    async def test_judge_budget_exhaustion_still_reserves_best_effort_writer(self):
        with tempfile.TemporaryDirectory() as root:
            state, report = await ResearchAgent(
                AutoBackend(),
                Config(session_dir=root, max_calls=5),
                SearchRouter(FakeAsyncSearch(delay=0)),
                judge=FakeJudge(),
            ).arun("q")
            self.assertEqual(state.stop_reason, "budget")
            self.assertEqual(state.search_calls, 15)
            self.assertEqual(state.report_status, "best_effort_assembled")
            self.assertIn("42", report)

    async def test_empty_question_has_no_provider_or_storage_work(self):
        with tempfile.TemporaryDirectory() as root:
            backend = AutoBackend()
            agent = ResearchAgent(
                backend,
                Config(session_dir=root),
                SearchRouter(FakeAsyncSearch()),
                judge=FakeJudge(),
            )
            with self.assertRaises(ValueError):
                await agent.arun("  ")
            self.assertEqual(backend.calls, 0)

    async def test_primary_initialization_outage_without_fallback_discloses_missing_classification(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "deepsearchagent.orchestrator.OpenAICompatibleJudge",
                side_effect=ValueError("CANARY-INIT"),
            ),
        ):
            state, report = await ResearchAgent(
                AutoBackend(),
                Config(session_dir=root, judge_provider="pai"),
                SearchRouter(FakeAsyncSearch(delay=0)),
            ).arun("q")
        self.assertEqual(state.stop_reason, "no_tasks")
        self.assertIsNone(state.judge)
        self.assertIn("classification was unavailable", report)
        self.assertNotIn("CANARY", report)

    async def test_cancelled_classifier_releases_reservation_and_skips_writer(self):
        from deepsearchagent.cancellation import ResearchCancelled

        class CancelJudge(FakeJudge):
            def evaluate(self, *args):
                raise ResearchCancelled("Stop")

        with tempfile.TemporaryDirectory() as root:
            agent = ResearchAgent(
                AutoBackend(),
                Config(session_dir=root),
                SearchRouter(FakeAsyncSearch(delay=0)),
                judge=CancelJudge(),
            )
            state, report = await agent.arun("q")
            self.assertEqual(state.status, "CANCELLED")
            self.assertEqual(agent.budget.pending_tokens, 0)
            self.assertFalse(
                any(name == "ReportDraft" for name, _ in agent.backend.limits)
            )

    async def test_transports_receive_shared_cancellation_and_trace(self):
        class WiredSearch(FakeAsyncSearch):
            trace = None
            cancellation = None

        provider, signal, events = WiredSearch(delay=0), Cancellation(), []
        with tempfile.TemporaryDirectory() as root:
            agent = ResearchAgent(
                AutoBackend(),
                Config(session_dir=root),
                SearchRouter(provider),
                cancellation=signal,
                trace=events.append,
                judge=FakeJudge(),
            )
            self.assertIs(provider.cancellation, signal)
            self.assertEqual(provider.trace, events.append)
            state, _ = await agent.arun("q")
        self.assertEqual(state.stop_reason, "coverage")

    async def test_async_task_cancellation_persists_explicit_terminal_state(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockedSearch:
            active = 0

            async def asearch(self, query, *, timeout):
                self.active += 1
                entered.set()
                try:
                    await release.wait()
                finally:
                    self.active -= 1

        from pathlib import Path
        import json

        with tempfile.TemporaryDirectory() as root:
            search = BlockedSearch()
            agent = ResearchAgent(
                AutoBackend(),
                Config(session_dir=root),
                SearchRouter(search),
                judge=FakeJudge(),
            )
            task = asyncio.create_task(agent.arun("Facts"))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            state, _ = await asyncio.wait_for(task, 1)
            self.assertEqual(state.status, "CANCELLED")
            self.assertTrue(agent.cancellation.event.is_set())
            self.assertEqual(search.active, 0)
            saved = json.loads(
                (Path(state.facts_path).parent / "state.json").read_text()
            )
            self.assertEqual(saved["status"], "CANCELLED")
