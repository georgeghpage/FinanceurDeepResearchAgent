"""Barrier-driven worker failure, cancellation and multi-session integration."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from deepsearchagent.cancellation import Cancellation
from deepsearchagent.config import Config
from deepsearchagent.llm import ResponseError
from deepsearchagent.models import EvidenceExtraction
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge


class BarrierBackend(AutoBackend):
    def __init__(self, failed=()):
        super().__init__()
        self.barrier = threading.Barrier(3, timeout=3)
        self.failed = set(failed)
        self.arrivals = []

    def structured(self, schema, instructions, payload, **options):
        if schema is EvidenceExtraction:
            rid = json.loads(payload)["requirements"][0]["id"]
            with self.lock:
                self.arrivals.append(rid)
            self.barrier.wait()
            if rid in self.failed:
                raise ResponseError("Secret-truncated-output", category="output_limit")
        return super().structured(schema, instructions, payload, **options)


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_and_total_worker_failures_preserve_successes(self):
        for failed in ((), ("R1",), ("R1", "R2"), ("R1", "R2", "R3")):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as root:
                backend = BarrierBackend(failed)
                state, report = await ResearchAgent(
                    backend,
                    Config(session_dir=root, search_requests_per_second=10000),
                    SearchRouter(FakeAsyncSearch(delay=0)),
                    judge=FakeJudge(),
                ).arun("facts")
                self.assertCountEqual(backend.arrivals, ["R1", "R2", "R3"])
                self.assertEqual(
                    {f.requirement_id for f in state.findings},
                    {"R1", "R2", "R3"} - set(failed),
                )
                self.assertEqual(
                    state.status,
                    "COMPLETE"
                    if not failed
                    else "PARTIAL"
                    if state.findings
                    else "FAILED",
                )
                self.assertEqual(state.search_calls, 15)
                self.assertNotIn("Secret-truncated-output", report)
                self.assertEqual(
                    len({f.id for f in state.findings}), len(state.findings)
                )
                if failed:
                    self.assertNotEqual(state.stop_reason, "coverage")
                    self.assertIn("Unresolved requirement", report)
                saved = json.loads(
                    (Path(state.facts_path).parent / "state.json").read_text()
                )
                self.assertEqual(len(saved["findings"]), len(state.findings))
                self.assertEqual(list(Path(root).rglob(".snapshot-*")), [])

    async def test_independent_sessions_execute_concurrently(self):
        with tempfile.TemporaryDirectory() as root:

            async def run(index):
                question = f"Isolated question {index}"
                return await ResearchAgent(
                    BarrierBackend(),
                    Config(session_dir=root, search_requests_per_second=10000),
                    SearchRouter(FakeAsyncSearch(delay=0)),
                    judge=FakeJudge(),
                ).arun(question)

            results = await asyncio.wait_for(
                # This is a deadlock guard, not a production latency target;
                # coverage plus five concurrent fsync-heavy stores adds overhead.
                asyncio.gather(*(run(i) for i in range(5))),
                30,
            )
            self.assertEqual(len({s.session_id for s, _ in results}), 5)
            for index, (state, report) in enumerate(results):
                text = Path(state.facts_path).read_text()
                self.assertIn(f"Isolated question {index}", text)
                for other in range(5):
                    if other != index:
                        self.assertNotIn(f"Isolated question {other}", text)
                self.assertEqual(state.stop_reason, "coverage", report)
                self.assertEqual((len(state.findings), state.search_calls), (3, 15))

    async def test_cancellation_releases_all_active_async_searches(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cancellation = Cancellation()

        class BlockingSearch:
            active = 0

            async def asearch(self, query, *, timeout):
                self.active += 1
                entered.set()
                try:
                    await release.wait()
                finally:
                    self.active -= 1

        with tempfile.TemporaryDirectory() as root:
            search = BlockingSearch()
            agent = ResearchAgent(
                AutoBackend(),
                Config(session_dir=root, request_timeout=60),
                SearchRouter(search),
                judge=FakeJudge(),
                cancellation=cancellation,
            )
            task = asyncio.create_task(agent.arun("Cancel during search"))
            await asyncio.wait_for(entered.wait(), 3)
            cancellation.cancel()
            state, _ = await asyncio.wait_for(task, 1)
            self.assertEqual(state.status, "CANCELLED")
            self.assertEqual(search.active, 0)
            self.assertEqual(agent.judge.calls, 0)
            self.assertFalse(
                any(name == "ReportDraft" for name, _ in agent.backend.backend.limits)
            )

    def test_refinement_hard_limit_cannot_be_relaxed_by_configuration(self):
        with self.assertRaises(ValueError):
            Config(max_rounds=3)


class ModelRecoveryTests(unittest.TestCase):
    """Exercise recovery through the real compatible SDK and shared reservations."""

    def setup_transport(self, responses, **overrides):
        import httpx
        from openai import OpenAI
        from deepsearchagent.budget import RunBudget, StageBackend
        from deepsearchagent.llm import AliyunBackend

        config = Config(llm_backoff=0, llm_jitter=0, **overrides)
        events, requests = [], []
        replies = iter(responses)

        def handle(request):
            requests.append(json.loads(request.content))
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            if requests[-1].get("stream") and response.status_code == 200:
                data = response.json()
                data["object"] = "chat.completion.chunk"
                for choice in data["choices"]:
                    choice["delta"] = choice.pop("message")
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n",
                )
            return response

        backend = AliyunBackend(
            config, "OFFLINE-CANARY", trace=None, diagnostic_trace=events.append
        )
        backend.client.close()
        backend.client = OpenAI(
            api_key="OFFLINE-CANARY",
            base_url="https://offline.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        self.addCleanup(backend.client.close)
        budget = RunBudget(config)
        stage = StageBackend(backend, budget, 1000, 12000)
        return stage, backend, budget, events, requests

    @staticmethod
    def response(content, finish="stop", status=200, headers=None):
        import httpx
        from test_provider_contracts import completion

        return httpx.Response(
            status,
            json=completion(content, finish=finish)
            if status == 200
            else {"error": {"message": "CANARY"}},
            headers=headers,
        )

    def test_opt_in_bounded_json_continuation_preserves_content(self):
        from deepsearchagent.models import ResearchPlan

        stage, backend, budget, events, requests = self.setup_transport(
            [
                self.response('{"objective":"Checkpoint with citation F1",', "length"),
                self.response('"tasks":[]}'),
            ],
            max_continuations=2,
        )
        checkpoints = []
        budget.checkpoint = lambda *args: checkpoints.append(args)
        result = stage.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(result.objective, "Checkpoint with citation F1")
        self.assertEqual(requests[0]["model"], "deepseek-v4.1-flash")
        self.assertTrue(requests[0]["enable_thinking"])
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertNotIn("thinking_budget", requests[0])
        self.assertEqual(requests[1]["max_tokens"], 2000)
        self.assertNotIn("response_format", requests[1])
        self.assertIn(
            "Checkpoint with citation F1", requests[1]["messages"][1]["content"]
        )
        self.assertEqual(
            (backend.calls, budget.calls, budget.pending_tokens), (2, 2, 0)
        )
        self.assertEqual(budget.tokens, backend.tokens_used)
        self.assertEqual([c[2] for c in checkpoints], [False, True])
        self.assertIn("llm_recovered", [e["kind"] for e in events])
        self.assertNotIn("Checkpoint with citation F1", str(events))

    def test_reasoning_only_truncation_does_not_restart_or_grow_allocation(
        self,
    ):
        from deepsearchagent.models import ResearchPlan
        from deepsearchagent.llm import IncompleteResponse

        stage, _, budget, _, requests = self.setup_transport(
            [
                self.response("", "length"),
                self.response("", "length"),
                self.response("", "length"),
            ]
        )
        with self.assertRaises(IncompleteResponse):
            stage.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual([r["max_tokens"] for r in requests], [1000])
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.pending_tokens, 0)

    def test_valid_json_with_length_is_never_silently_accepted(self):
        from deepsearchagent.models import ResearchPlan
        from deepsearchagent.llm import IncompleteResponse

        stage, _, budget, _, _ = self.setup_transport(
            [self.response('{"objective":"looks valid","tasks":[]}', "length")],
            max_continuations=0,
        )
        with self.assertRaises(IncompleteResponse):
            stage.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(budget.calls, 1)

    def test_query_thinking_override_is_serialized_and_does_not_leak(self):
        from deepsearchagent.budget import StageBackend
        from deepsearchagent.models import ResearchPlan

        _, backend, budget, _, requests = self.setup_transport(
            [
                self.response('{"objective":"Queries","tasks":[]}'),
                self.response('{"objective":"Analysis","tasks":[]}'),
            ],
            reasoning_effort="max",
        )
        queries = StageBackend(backend, budget, 1000, 12000, thinking_enabled=False)
        analysis = StageBackend(backend, budget, 1000, 12000)
        queries.structured(ResearchPlan, "Queries only", "Question")
        analysis.structured(ResearchPlan, "Analyze", "Evidence")
        self.assertFalse(requests[0]["enable_thinking"])
        self.assertNotIn("reasoning_effort", requests[0])
        self.assertTrue(requests[1]["enable_thinking"])
        self.assertEqual(requests[1]["reasoning_effort"], "max")

    def test_transient_retries_count_all_attempts_terminal_failures_stop(self):
        from openai import APIStatusError
        from deepsearchagent.models import ResearchPlan

        for status, attempts in ((429, 2), (408, 1), (503, 2), (401, 1), (400, 1)):
            with self.subTest(status=status):
                stage, _, budget, events, requests = self.setup_transport(
                    [
                        self.response("", status=status, headers={"Retry-After": "0"}),
                        self.response('{"objective":"Recovered","tasks":[]}'),
                    ]
                )
                if attempts == 2:
                    self.assertEqual(
                        stage.structured(ResearchPlan, "Plan", "Question").objective,
                        "Recovered",
                    )
                else:
                    with self.assertRaises(APIStatusError):
                        stage.structured(ResearchPlan, "Plan", "Question")
                self.assertEqual(len(requests), attempts)
                self.assertEqual(budget.calls, attempts)
                self.assertEqual(budget.pending_tokens, 0)
                self.assertNotIn("CANARY", str(events))

    def test_retry_after_cannot_escape_deadline_or_start_another_call(self):
        from deepsearchagent.models import ResearchPlan
        from deepsearchagent.llm import BudgetExceeded

        stage, _, budget, _, requests = self.setup_transport(
            [self.response("", status=429, headers={"Retry-After": "1000"})],
            request_timeout=0.1,
        )
        with self.assertRaises(BudgetExceeded):
            stage.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(len(requests), 1)
        self.assertEqual(budget.pending_tokens, 0)

    def test_context_and_cost_caps_prevent_provider_calls(self):
        from deepsearchagent.models import ResearchPlan
        from deepsearchagent.llm import BudgetExceeded

        for options in ({"context_tokens": 100}, {"max_cost_usd": 0.01}):
            stage, _, budget, _, requests = self.setup_transport([], **options)
            with self.assertRaises(BudgetExceeded):
                stage.structured(ResearchPlan, "Plan", "Question")
            self.assertEqual(requests, [])
            self.assertEqual(budget.pending_tokens, 0)

    def test_optional_research_classifier_uses_the_same_model_and_recovery_budget(self):
        from deepsearchagent.models import JudgeDecision

        class DefaultJudgeBackend(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                if schema is JudgeDecision:
                    data = json.loads(payload)
                    return FakeJudge().evaluate(
                        data["question"],
                        plan().research_requirements,
                        data["evidence"],
                        data["research_state"],
                    )
                return super().structured(schema, instructions, payload, **options)

        from support_fixtures import plan

        with tempfile.TemporaryDirectory() as root:
            agent = ResearchAgent(
                DefaultJudgeBackend(),
                Config(session_dir=root, judge_provider="research"),
                SearchRouter(FakeAsyncSearch(delay=0)),
            )
            state, _ = agent.run("Facts")
            self.assertEqual(state.status, "COMPLETE", state.warnings)
            self.assertEqual(agent.judge.provider, "aliyun")
            self.assertEqual(agent.judge.model, "deepseek-v4.1-flash")
            self.assertEqual(state.api_calls, 9)

    def test_stream_interrupt_resumes_json_without_replaying_work(self):
        from unittest.mock import Mock
        from deepsearchagent.models import ResearchPlan
        from test_streaming import chunk

        stage, backend, budget, events, _ = self.setup_transport(
            [], max_continuations=2
        )
        backend.trace = events.append
        first, second = Mock(), Mock()
        first.__iter__ = Mock(
            return_value=iter([chunk(content='{"objective":"Kept",', tokens=30)])
        )
        second.__iter__ = Mock(
            return_value=iter([chunk(content='"tasks":[]}', finish="stop", tokens=20)])
        )
        backend.client.chat.completions.create = Mock(side_effect=[first, second])
        self.assertEqual(
            stage.structured(ResearchPlan, "Plan", "Question").objective, "Kept"
        )
        self.assertEqual(
            (budget.calls, budget.tokens, budget.pending_tokens), (2, 50, 0)
        )
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertEqual(len([e for e in events if e["kind"] == "llm_continuation"]), 1)

    def test_failed_continuation_keeps_only_valid_source_backed_partial_findings(self):
        from deepsearchagent.llm import IncompleteResponse

        class TruncatedWorker(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                result = super().structured(schema, instructions, payload, **options)
                if schema is EvidenceExtraction:
                    text = result.model_dump_json()
                    # Completed findings, followed by an unfinished top-level field.
                    raise IncompleteResponse(
                        text[: text.index(',"contradictions"')] + ',"contradictions":['
                    )
                return result

        with tempfile.TemporaryDirectory() as root:
            state, report = ResearchAgent(
                TruncatedWorker(),
                Config(session_dir=root, max_continuations=0),
                SearchRouter(FakeAsyncSearch(delay=0)),
                judge=FakeJudge(),
            ).run("Facts")
            self.assertEqual(len(state.findings), 3)
            self.assertEqual(state.status, "PARTIAL")
            self.assertIn("Some sources have not been fully assessed", report)
            self.assertIn("[S", report)
            files = list(Path(state.facts_path).parent.glob("generation-*.json"))
            self.assertEqual(len(files), 3)
            self.assertTrue(
                all(not json.loads(f.read_text())["complete"] for f in files)
            )

    def test_llm_concurrency_is_shared_by_stage_adapters(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from deepsearchagent.budget import RunBudget, StageBackend
        from deepsearchagent.models import ResearchPlan

        class SlowBackend:
            active = peak = 0
            lock = threading.Lock()

            def structured(self, *args, **kwargs):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    time.sleep(0.02)
                    return ResearchPlan(objective="Test", tasks=[])
                finally:
                    with self.lock:
                        self.active -= 1

        backend = SlowBackend()
        budget = RunBudget(Config(llm_concurrency=2))
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(
                    StageBackend(backend, budget, 1000, 12000).structured,
                    ResearchPlan,
                    "Plan",
                    "Question",
                )
                for _ in range(5)
            ]
            self.assertTrue(all(f.result().objective == "Test" for f in futures))
        self.assertEqual(backend.peak, 2)
        self.assertEqual((budget.calls, budget.pending_tokens), (5, 0))
        self.assertFalse(budget.token_usage_complete)
