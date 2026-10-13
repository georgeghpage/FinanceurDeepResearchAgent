"""Fault injection: attribute stops to the correct provider, stage and cause."""

import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import APIStatusError, APITimeoutError

from deepsearchagent.config import Config
from deepsearchagent.diagnostics import DiagnosticJournal, failure_from_exception, research_outcome, run_metadata
from deepsearchagent.llm import BudgetExceeded, KimiBackend, ResponseError
from deepsearchagent.models import ResearchPlan, ResearchState
from deepsearchagent.orchestrator import SequentialResearchAgent as ResearchAgent
from deepsearchagent.tools.search import SearchError, SearchRouter
from deepsearchagent.tools.tavily import TavilyMCPSearch
from test_agent import FakeSearch, ScriptedBackend, TASK, extraction, verification, draft
from test_streaming import chunk
from support_fixtures import plan

SECRET = "PRIVATE-API-KEY-AND-RESPONSE"


class OutcomeTests(unittest.TestCase):
    def test_stop_reasons_and_report_failure_explain_unfinished_research(self):
        for reason, expected in (("round_limit", "round limit"), ("budget", "limit"),
                                 ("no_tasks", "follow-up"), ("cancelled", "user's request"), ("error", "stopped")):
            with self.subTest(reason=reason):
                self.assertIn(expected, research_outcome(ResearchState(question="Facts", stop_reason=reason)))
        state = ResearchState(question="Facts", stop_reason="coverage", report_status="best_effort_assembled")
        self.assertIn("report generation", research_outcome(state))
        state.report_status = "generated"
        state.warnings = ["Some searches were unavailable."]
        self.assertIn("with limitations", research_outcome(state))
        state.warnings = []
        self.assertIn("completed", research_outcome(state))

    def test_unavailable_evaluation_is_distinct_from_no_followups(self):
        state = ResearchState(question="Facts", plan=plan())
        self.assertIn("evaluation did not finish", research_outcome(state))


def status_error(status):
    request = httpx.Request("POST", f"https://example.com/?key={SECRET}", headers={"Authorization": SECRET})
    response = httpx.Response(status, request=request, json={"error": SECRET}, headers={"x-request-id": "req_123"})
    return httpx.HTTPStatusError(SECRET, request=request, response=response)


class FailureClassificationTests(unittest.TestCase):
    def test_generic_search_wrapper_does_not_hide_typed_network_cause(self):
        exc = SearchError(SECRET)
        exc.__cause__ = httpx.ConnectError(SECRET)
        failure = failure_from_exception(exc, provider="documentation", stage="search", operation="search")
        self.assertEqual(failure.category, "connection_error")
        self.assertNotIn(SECRET, failure.model_dump_json())

    def test_mcp_exception_groups_preserve_http_status_without_secrets(self):
        for status, category in ((401, "authentication"), (403, "authentication"),
                                 (429, "rate_limit"), (503, "provider_unavailable"), (400, "invalid_request")):
            with self.subTest(status=status):
                exc = ExceptionGroup(SECRET, [ExceptionGroup(SECRET, [status_error(status)])])
                result = failure_from_exception(exc, provider="tavily", stage="search", operation="initialize")
                self.assertEqual(result.category, category)
                self.assertEqual(result.http_status, status)
                self.assertEqual(result.exception_type, "HTTPStatusError")
                self.assertNotIn(SECRET, result.model_dump_json())

    def test_typed_causes_distinguish_timeout_tls_proxy_and_protocol(self):
        tls = httpx.ConnectError(SECRET)
        tls.__cause__ = ssl.SSLError(SECRET)
        for exc, category in ((httpx.ReadTimeout(SECRET), "timeout"), (tls, "tls_error"),
                              (httpx.ProxyError(SECRET), "proxy_error"),
                              (httpx.ConnectError(SECRET), "connection_error"),
                              (httpx.RemoteProtocolError(SECRET), "protocol_error"),
                              (RuntimeError(SECRET), "unexpected_error")):
            with self.subTest(category=category):
                result = failure_from_exception(ExceptionGroup(SECRET, [exc]), provider="tavily", stage="search", operation="call_tool")
                self.assertEqual(result.category, category)
                self.assertNotIn(SECRET, result.model_dump_json())

    def test_kimi_http_error_keeps_safe_request_id(self):
        response = status_error(429).response
        exc = APIStatusError(SECRET, response=response, body={"error": SECRET})
        result = failure_from_exception(exc, provider="kimi", stage="verify", operation="chat_completion", call_id=3)
        self.assertEqual((result.category, result.http_status, result.call_id), ("rate_limit", 429, 3))
        self.assertEqual(result.request_id, "req_123")
        self.assertNotIn(SECRET, result.model_dump_json())


class KimiDiagnosticTests(unittest.TestCase):
    def backend(self, chunks=(), **config):
        events = []
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3", **config), SECRET, trace=events.append)
        self.addCleanup(backend.client.close)
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter(chunks))
        backend.client.chat.completions.create = Mock(return_value=stream)
        return backend, stream, events

    def test_response_failure_matrix_including_reasoning_only_truncation(self):
        cases = [
            ([chunk(reasoning="Thinking", finish="length", tokens=256)], "output_limit"),
            ([chunk(content='{"objective":', finish="length")], "output_limit"),
            ([chunk(content='{"objective":"Test","tasks":[]}')], "missing_finish"),
            ([chunk(refusal=SECRET, finish="stop")], "refusal"),
            ([chunk(finish="content_filter")], "refusal"),
            ([chunk(finish="stop")], "empty_response"),
            ([chunk(content='{"wrong":"PRIVATE-API-KEY-AND-RESPONSE"}', finish="stop")], "validation_error"),
        ]
        for chunks, category in cases:
            with self.subTest(category=category):
                backend, stream, events = self.backend(chunks, max_output_tokens=256)
                with self.assertRaises(Exception):
                    backend.structured(ResearchPlan, "Plan", "Question")
                event = events[-1]
                self.assertEqual(event["diagnostic"]["category"], category)
                self.assertEqual(event["diagnostic"]["provider"], "kimi")
                self.assertEqual(event["diagnostic"]["stage"], "plan")
                self.assertEqual(event["max_output_tokens"], 256)
                self.assertIn("usage_observed", event)
                self.assertNotIn(SECRET, json.dumps(event))
                stream.close.assert_called_once()

    def test_request_timeout_and_run_deadline_are_distinct(self):
        for runtime, category in ((5, "request_timeout"), (100, "time_budget")):
            with self.subTest(category=category):
                backend, stream, events = self.backend([chunk(reasoning="Late", tokens=12)], max_seconds=60, request_timeout=3)
                backend.started = 0
                # structured/_options, stream start, run-time check, request-time
                # check, then failure duration; no real sleep or network required.
                with patch("deepsearchagent.llm.time.monotonic", side_effect=[0, 0, runtime, runtime, runtime]):
                    with self.assertRaises(BudgetExceeded):
                        backend.structured(ResearchPlan, "Plan", "Question")
                self.assertEqual(events[-1]["diagnostic"]["category"], category)
                self.assertEqual(backend.tokens_used, 12)

    def test_provider_request_timeout_is_not_reported_as_tavily_or_run_budget(self):
        backend, _, events = self.backend()
        backend.client.chat.completions.create.side_effect = APITimeoutError(request=httpx.Request("POST", "https://example.com"))
        with self.assertRaises(APITimeoutError):
            backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(events[-1]["diagnostic"]["category"], "timeout")
        self.assertFalse(events[-1]["usage_observed"])

    def test_usage_and_output_limit_metadata_on_success(self):
        usage = NS(total_tokens=120, prompt_tokens=100, completion_tokens=20,
                   completion_tokens_details=NS(reasoning_tokens=15))
        backend, _, events = self.backend([
            chunk(reasoning="Check", content='{"objective":"Test","tasks":[]}', finish="stop"),
            NS(usage=usage, choices=[]),
        ])
        backend.structured(ResearchPlan, "Plan", "Question")
        event = events[-1]
        self.assertEqual((event["input_tokens"], event["output_tokens"], event["reasoning_tokens"]), (100, 20, 15))
        self.assertEqual(event["finish_reason"], "stop")
        self.assertEqual(event["reasoning_chars"], 5)
        self.assertGreater(event["output_chars"], 0)
        self.assertIsNotNone(event["time_to_first_delta_seconds"])

    def test_nonstreamed_cli_requests_have_the_same_failure_diagnostics(self):
        events = []
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), SECRET, diagnostic_trace=events.append)
        self.addCleanup(backend.client.close)
        response = NS(usage=NS(total_tokens=12), choices=[NS(finish_reason="length", message=NS(content="{", refusal=None))])
        backend.client.chat.completions.create = Mock(return_value=response)
        with self.assertRaises(ResponseError):
            backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(events[-1]["diagnostic"]["category"], "output_limit")
        self.assertEqual(backend.tokens_used, 12)
        self.assertNotIn("stream", backend.client.chat.completions.create.call_args.kwargs)


class RunFailureTests(unittest.TestCase):
    def test_successful_tavily_search_then_kimi_verification_truncation(self):
        events = []
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), SECRET, trace=events.append)
        self.addCleanup(backend.client.close)
        streams = []
        for result in (ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(), None):
            stream = Mock()
            stream.__iter__ = Mock(return_value=iter([chunk(content=result.model_dump_json(), finish="stop", tokens=100)] if result else [chunk(reasoning="Checking evidence", finish="length", tokens=4000)]))
            streams.append(stream)
        backend.client.chat.completions.create = Mock(side_effect=streams)
        web = FakeSearch()
        web.provider_name = "tavily"
        state, report = ResearchAgent(backend, Config(provider="kimi", model="kimi-k3"), SearchRouter(web), trace=events.append).run("Find revenue")
        self.assertEqual((state.failure.provider, state.failure.stage, state.failure.call_id), ("kimi", "verify", 3))
        self.assertEqual(state.failure.category, "output_limit")
        self.assertEqual((len(state.sources), state.search_calls, state.api_calls), (1, 1, 3))
        self.assertNotIn("kimi / output", report)
        self.assertEqual(events[-1]["kind"], "run_stopped")
        self.assertEqual(events[-1]["failure"]["finish_reason"], "length")
        self.assertIn("search_complete", [e["kind"] for e in events])

    def test_tavily_failures_skip_extraction_and_continue_with_mcp_diagnostics(self):
        for phase, exc, category in (
            ("initialize", ExceptionGroup(SECRET, [status_error(401)]), "authentication"),
            ("call_tool", TimeoutError(SECRET), "timeout"),
            ("parse_result", SearchError(SECRET, category="tool_error"), "tool_error"),
        ):
            with self.subTest(phase=phase):
                events = []
                web = TavilyMCPSearch("https://example.com/mcp", SECRET)
                async def fail_search(query, timeout):
                    web._step(phase)
                    raise exc
                web._search = AsyncMock(side_effect=fail_search)
                backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]), verification(sufficient=False).model_copy(update={"supported_finding_ids": []}))
                state, report = ResearchAgent(backend, Config(search_backoff=0), SearchRouter(web), trace=events.append).run("Find revenue")
                self.assertIsNone(state.failure)
                failure = next(e["diagnostic"] for e in events if e.get("kind") == "tavily_error")
                self.assertEqual((failure["provider"], failure["stage"], failure["operation"]), ("tavily", "search", phase))
                self.assertEqual(failure["category"], category)
                self.assertEqual(backend.calls, 2)
                self.assertEqual(state.sources, [])
                self.assertTrue(any("Source coverage incomplete" in w for w in state.warnings))
                self.assertNotIn("Source coverage incomplete", report)
                self.assertNotIn(SECRET, json.dumps(events) + state.model_dump_json() + report)

    def test_budget_matrix_is_attributed_to_agent_not_to_search_provider(self):
        cases = [
            (Config(max_calls=1), "call_budget"),
            (Config(max_tokens=100), "token_budget"),
        ]
        for config, category in cases:
            with self.subTest(category=category):
                backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]))
                state, _ = ResearchAgent(backend, config, SearchRouter(FakeSearch())).run("Find revenue")
                self.assertEqual(state.stop_reason, "budget")
                self.assertEqual((state.failure.provider, state.failure.category), ("agent", category))
                self.assertEqual(state.search_calls, 0)
        config = Config(max_searches=1)
        other = TASK.model_copy(update={"query": "another query"})
        backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK, other]), extraction())
        state, _ = ResearchAgent(backend, config, SearchRouter(FakeSearch())).run("Find revenue")
        self.assertEqual(state.failure.category, "search_budget")
        self.assertEqual(state.failure.provider, "agent")


class DiagnosticJournalTests(unittest.TestCase):
    def test_log_persists_metadata_and_omits_payloads_and_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            journal = DiagnosticJournal(path, "test-run")
            journal.record({"kind": "thinking_delta", "text": SECRET})
            journal.record({"kind": "output_delta", "text": SECRET})
            failure = failure_from_exception(status_error(401), provider="tavily", stage="search", operation="initialize")
            journal.record({"kind": "search_error", "query": SECRET, "headers": SECRET, "diagnostic": failure.model_dump()})
            journal.record({"type": "result", "report": SECRET, "failure": failure.model_dump(), "stop_reason": "error"})
            text = path.read_text()
            self.assertNotIn(SECRET, text)
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1]["failure"]["http_status"], 401)
            self.assertTrue(all(r["run_id"] == "test-run" and r["timestamp"] for r in records))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_proxy_metadata_omits_credentials_and_query_parameters(self):
        with patch("deepsearchagent.diagnostics.getproxies", return_value={"https": f"http://user:{SECRET}@127.0.0.1:1082/?key={SECRET}"}):
            result = run_metadata(Config(tavily_mcp_url=f"https://example.com/mcp?key={SECRET}"))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["proxies"], [{"scheme": "https", "host": "127.0.0.1", "port": 1082}])
        self.assertEqual(result["tavily_host"], "example.com")

    def test_log_write_failure_is_nonfatal_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "file"
            blocker.write_text("This is a file, not a directory")
            journal = DiagnosticJournal(blocker / "run.jsonl", "test-run")
            self.assertFalse(journal.record({"kind": "run_start"}))
            self.assertTrue(journal.failed)
