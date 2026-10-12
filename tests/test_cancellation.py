"""Cancellation interrupts blocked work and preserves already collected evidence."""

import asyncio
import threading
import unittest
from unittest.mock import Mock

from deepsearchagent.cancellation import Cancellation, ResearchCancelled
from deepsearchagent.config import Config
from deepsearchagent.llm import KimiBackend
from deepsearchagent.models import ResearchPlan
from deepsearchagent.orchestrator import SequentialResearchAgent as ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from deepsearchagent.tools.tavily import TavilyMCPSearch
from test_agent import FakeSearch, TASK, draft, extraction, verification
from test_streaming import chunk


class CancellationTests(unittest.TestCase):
    def test_interrupts_every_model_stage_without_waiting_for_provider(self):
        for blocked_stage in range(4):
            with self.subTest(stage=blocked_stage):
                cancellation = Cancellation()
                backend = KimiBackend(Config(), "offline", trace=lambda event: None)
                backend.cancellation = cancellation
                self.addCleanup(backend.client.close)
                entered = threading.Event()
                release = threading.Event()
                exited = threading.Event()
                results = [ResearchPlan(objective="Revenue", tasks=[TASK]), extraction(), verification(), draft()]
                index = 0

                def create(**kwargs):
                    nonlocal index
                    current, index = index, index + 1
                    def chunks():
                        yield chunk(reasoning="Working", tokens=7)
                        if current == blocked_stage:
                            entered.set()
                            try:
                                release.wait(5)
                                exited.set()
                                yield chunk(content=results[current].model_dump_json(), finish="stop", tokens=20)
                            finally:
                                exited.set()
                        else:
                            yield chunk(content=results[current].model_dump_json(), finish="stop", tokens=20)
                    stream = Mock()
                    stream.__iter__ = Mock(return_value=chunks())
                    stream.close.side_effect = release.set if current == blocked_stage else None
                    return stream

                backend.client.chat.completions.create = Mock(side_effect=create)
                output = []
                trace = []
                worker = threading.Thread(target=lambda: output.append(ResearchAgent(
                    backend, Config(), SearchRouter(FakeSearch()), trace=trace.append,
                    cancellation=cancellation).run("Revenue")))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    cancellation.cancel()
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), "Cancellation waited for the provider")
                    state, report = output[0]
                    self.assertEqual(state.stop_reason, "cancelled")
                    self.assertIsNone(state.failure)
                    self.assertEqual(state.api_calls, blocked_stage + 1)
                    self.assertEqual(state.tokens_used, blocked_stage * 20 + 7)
                    self.assertEqual(trace[-1]["kind"], "run_stopped")
                    if blocked_stage >= 2:
                        self.assertIn("Company filing", report)
                        self.assertEqual(len(state.findings), 1)
                    backend.close_stream()
                    self.assertTrue(exited.wait(1))
                    self.assertEqual(backend.client.chat.completions.create.call_count, blocked_stage + 1)
                finally:
                    release.set()
                    worker.join(2)

    def test_stopping_before_start_makes_no_model_calls(self):
        cancellation = Cancellation()
        cancellation.cancel()
        backend = KimiBackend(Config(), "offline")
        self.addCleanup(backend.client.close)
        state, _ = ResearchAgent(backend, Config(), SearchRouter(FakeSearch()), cancellation=cancellation).run("Revenue")
        self.assertEqual(state.stop_reason, "cancelled")
        self.assertEqual(state.api_calls, 0)

    def test_cancelled_search_cannot_start_extraction_or_append_late_sources(self):
        from test_agent import ScriptedBackend
        cancellation = Cancellation()
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        class BlockingSearch(FakeSearch):
            def search(self, *args, **kwargs):
                entered.set()
                release.wait(5)
                result = super().search(*args, **kwargs)
                exited.set()
                return result
        backend = ScriptedBackend(ResearchPlan(objective="Revenue", tasks=[TASK]))
        search = SearchRouter(BlockingSearch())
        output = []
        worker = threading.Thread(target=lambda: output.append(ResearchAgent(
            backend, Config(), search, cancellation=cancellation).run("Revenue")))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            cancellation.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            state, _ = output[0]
            self.assertEqual(state.stop_reason, "cancelled")
            self.assertEqual(state.api_calls, 1)
            self.assertEqual(state.search_calls, 1)
            release.set()
            self.assertTrue(exited.wait(1))
            self.assertEqual(state.sources, [])
            self.assertEqual(backend.calls, 1)
        finally:
            release.set()
            worker.join(2)

    def test_tavily_cancellation_cleans_up_the_async_request(self):
        cancellation = Cancellation()
        provider = TavilyMCPSearch("https://example.com/mcp", "offline")
        provider.cancellation = cancellation
        started = threading.Event()
        cleaned = threading.Event()
        errors = []
        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        provider._search = blocked
        def call():
            try:
                provider.search("revenue", timeout=60)
            except ResearchCancelled as exc:
                errors.append(exc)
        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(started.wait(2))
        cancellation.cancel()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(errors), 1)
