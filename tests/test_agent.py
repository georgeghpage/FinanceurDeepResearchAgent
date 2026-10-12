"""Offline integration tests against the compiled LangGraph and provider contracts."""

import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from pydantic import ValidationError

from deepsearchagent.config import Config, require_api_key
from deepsearchagent.cli import main
from deepsearchagent.llm import BudgetExceeded, KimiBackend, ResponseError
from deepsearchagent.models import (
    Extraction, ExtractedFinding, ReportDraft, ReportParagraph, ReportSection,
    ResearchPlan, ResearchState, ResearchTask, SearchResult, Source, Verification,
)
from deepsearchagent.orchestrator import SequentialResearchAgent as ResearchAgent
from deepsearchagent.tools.documentation import DocumentationSearch
from deepsearchagent.tools.fetch import parse_tavily_result
from deepsearchagent.tools.search import SearchError, SearchRouter

URL = "https://example.com/filing"
TASK = ResearchTask(question="What was revenue?", query="company FY2025 revenue")


class ScriptedBackend:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
        self.tokens_used = 0
        self.started = time.monotonic()
        self.client = Mock()

    def structured(self, schema, instructions, payload):
        self.calls += 1
        self.tokens_used += 100
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, schema), (schema, result)
        return result.model_copy(deep=True)


class FakeSearch:
    def __init__(self):
        self.queries = []

    def search(self, query, *, timeout):
        self.queries.append(query)
        assert timeout > 0
        return SearchResult(text=f"{URL}: Revenue was USD 10 million in FY2025.",
                            sources=[Source(id="", url=URL, title="Company filing")])


def extraction(url=URL):
    return Extraction(findings=[ExtractedFinding(
        claim="Revenue was USD 10 million in FY2025.",
        evidence="The filing reports USD 10 million revenue for FY2025.",
        source_url=url, published_at=None,
    )])


def verification(sufficient=True, tasks=None):
    return Verification(sufficient=sufficient, supported_finding_ids=["F1"],
                        conflicts=[], gaps=[] if sufficient else ["Need comparison"],
                        follow_up_tasks=tasks or [])


def draft(ids=None):
    return ReportDraft(title="Revenue research", sections=[ReportSection(
        title="Findings", paragraphs=[ReportParagraph(
            text="Revenue was USD 10 million in FY2025.", finding_ids=ids or ["F1"],
        )],
    )])


class GraphTests(unittest.TestCase):
    def setUp(self):
        compat = patch("deepsearchagent.cli.ResearchAgent", ResearchAgent)
        compat.start()
        self.addCleanup(compat.stop)
        patcher = patch("deepsearchagent.config.dotenv_values", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_saves_report_and_evidence(self):
        backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), verification(), draft())
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.md"
            state_path = Path(directory) / "state.json"
            with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline-key", "TAVILY_API_KEY": "offline-key"}), \
                    patch("sys.argv", ["deepsearchagent", "Find revenue", "--quiet", "--output", str(report_path), "--state", str(state_path), "--log", str(Path(directory) / "diagnostic.jsonl")]), \
                    patch("deepsearchagent.cli.KimiBackend", return_value=backend) as factory, \
                    patch("deepsearchagent.cli.TavilyMCPSearch", return_value=FakeSearch()):
                main()
            self.assertIn(f"[S1: Company filing](<{URL}>)", report_path.read_text())
            self.assertEqual(json.loads(state_path.read_text())["stop_reason"], "coverage")
            config = factory.call_args.args[0]
            self.assertEqual(config.max_searches, 25)
            self.assertEqual(config.max_tasks * config.max_rounds, 10)

    def run_agent(self, *results, config=None, search=None):
        backend = ScriptedBackend(*results)
        router = SearchRouter(search or FakeSearch())
        agent = ResearchAgent(backend, config or Config(provider="kimi", model="kimi-k3"), router)
        state, report = agent.run("How much was revenue?")
        return state, report, backend, router

    def test_complete_graph_report(self):
        state, report, backend, router = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(), verification(), draft(),
        )
        self.assertEqual(state.stop_reason, "coverage")
        self.assertEqual(state.rounds, 1)
        self.assertEqual(backend.calls, 4)
        self.assertEqual(router.calls, 1)
        self.assertIn(f"[S1: Company filing](<{URL}>)", report)
        self.assertEqual(state.sources[0].published_at, None)
        self.assertEqual(ResearchState.model_validate_json(state.model_dump_json()).findings[0].id, "F1")

    def test_gap_routes_back_to_research_and_deduplicates_evidence(self):
        follow_up = ResearchTask(question="Check independent evidence", query="company independent FY2025 revenue")
        state, _, _, router = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(),
            verification(False, [follow_up]), extraction(), verification(), draft(),
        )
        self.assertEqual(state.rounds, 2)
        self.assertEqual(state.stop_reason, "coverage")
        self.assertEqual(router.calls, 2)
        self.assertEqual(len(state.sources), 1)
        self.assertEqual(len(state.findings), 1)

    def test_default_limits_allow_ten_searches_across_two_rounds(self):
        tasks = [ResearchTask(question=f"Evidence need {i}", query=f"company evidence {i}")
                 for i in range(10)]
        state, _, backend, router = self.run_agent(
            ResearchPlan(objective="Cover evidence needs", tasks=tasks[:5]),
            *[extraction() for _ in range(5)], verification(False, tasks[5:]),
            *[extraction() for _ in range(5)], verification(), draft(),
        )
        self.assertEqual(state.stop_reason, "coverage")
        self.assertIsNone(state.failure)
        self.assertEqual(state.rounds, 2)
        self.assertEqual(state.search_calls, 10)
        self.assertEqual(router.web.queries, [task.query for task in tasks])
        self.assertEqual(backend.calls, 14)

    def test_max_rounds_stops_follow_up(self):
        state, report, _, _ = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(),
            verification(False, [ResearchTask(question="More", query="new query")]), draft(),
            config=Config(max_rounds=1),
        )
        self.assertEqual(state.stop_reason, "round_limit")
        self.assertIn("Need comparison", report)

    def test_repeated_queries_do_not_loop(self):
        state, _, _, router = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK, TASK]), extraction(),
            verification(False, [TASK]), draft(),
        )
        self.assertEqual(state.stop_reason, "no_tasks")
        self.assertEqual(router.calls, 1)

    def test_budget_keeps_collected_evidence(self):
        state, report, backend, _ = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(),
            config=Config(max_calls=2),
        )
        self.assertEqual(state.stop_reason, "budget")
        self.assertEqual(backend.calls, 2)
        self.assertEqual(len(state.findings), 1)
        self.assertIn("no synthesized report", report)
        self.assertIn(f"[S1: Company filing](<{URL}>)", report)

    def test_invented_source_url_is_dropped(self):
        audit = verification()
        state, report, _, _ = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]),
            extraction("https://invented.example/filing"), audit,
        )
        self.assertEqual(state.findings, [])
        self.assertFalse(state.verification.sufficient)
        self.assertNotIn("invented.example", report)

    def test_unknown_report_references_produce_partial_report(self):
        state, report, _, _ = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), extraction(), verification(), draft(["F999"]),
        )
        self.assertEqual(state.stop_reason, "error")
        self.assertIn("no synthesized report", report)
        self.assertEqual(len(state.findings), 1)

    def test_provider_failure_is_reported_without_exception_body(self):
        failing = DocumentationSearch(lambda query, timeout: (_ for _ in ()).throw(SearchError("SECRET")))
        state, report, _, _ = self.run_agent(
            ResearchPlan(objective="Find revenue", tasks=[TASK]), search=failing,
        )
        self.assertEqual(state.stop_reason, "error")
        self.assertNotIn("SECRET", report)

    def test_documentation_provider_is_used_by_graph(self):
        docs = FakeSearch()
        web = FakeSearch()
        task = TASK.model_copy(update={"source": "documentation"})
        backend = ScriptedBackend(ResearchPlan(objective="Find docs", tasks=[task]),
                                  extraction(), verification(), draft())
        router = SearchRouter(web, DocumentationSearch(lambda query, timeout: docs.search(query, timeout=timeout)))
        state, report = ResearchAgent(backend, Config(provider="kimi", model="kimi-k3"), router).run("Look in documentation")
        self.assertEqual(state.stop_reason, "coverage")
        self.assertEqual(web.queries, [])
        self.assertEqual(docs.queries, [task.query])
        self.assertIn(f"[S1: Company filing](<{URL}>)", report)

    def test_unconfigured_docs_are_not_silently_routed_to_web(self):
        router = SearchRouter(FakeSearch())
        self.assertEqual(router.available_sources, ["web"])
        with self.assertRaises(SearchError):
            router.search(TASK.model_copy(update={"source": "documentation"}), timeout=10)


class TavilyParserTests(unittest.TestCase):
    def test_structured_results_preserve_url_and_content(self):
        result = NS(isError=False, structuredContent={"results": [
            {"url": URL, "title": "Filing", "content": "snippet", "raw_content": "actual page content"},
        ]}, content=[])
        parsed = parse_tavily_result(result)
        self.assertEqual(parsed.sources[0].url, URL)
        self.assertIn("actual page content", parsed.text)

    def test_formatted_mcp_output(self):
        result = NS(isError=False, structuredContent=None, content=[NS(type="text", text=
                    f"Detailed Results:\n\nTitle: Filing\nID: 123\nURL: {URL}\nContent: snippet\nRaw Content: actual page content")])
        parsed = parse_tavily_result(result)
        self.assertEqual(len(parsed.sources), 1)
        self.assertIn("actual page content", parsed.text)

    def test_json_text_and_empty_results(self):
        result = NS(isError=False, structuredContent=None,
                    content=[NS(type="text", text=json.dumps({"results": []}))])
        self.assertEqual(parse_tavily_result(result).sources, [])

    def test_error_payload_is_not_accepted_as_evidence(self):
        with self.assertRaises(SearchError):
            parse_tavily_result(NS(isError=True))
        with self.assertRaises(SearchError):
            parse_tavily_result(NS(content=[NS(type="text", text="Unauthorized")]))

    def test_unsafe_urls_and_empty_content_are_dropped(self):
        result = NS(structuredContent={"results": [
            {"url": "javascript:alert(1)", "content": "fake"},
            {"url": URL, "content": ""},
        ]})
        self.assertEqual(parse_tavily_result(result).sources, [])


class KimiTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("deepsearchagent.config.dotenv_values", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def backend(self, content, finish_reason="stop"):
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), "offline-key")
        completion = NS(usage=NS(total_tokens=25), choices=[NS(
            finish_reason=finish_reason, message=NS(content=content, refusal=None),
        )])
        backend.client.chat.completions.create = lambda **kwargs: self.capture(backend, completion, kwargs)
        return backend

    def capture(self, backend, response, kwargs):
        backend.sent = kwargs
        return response

    def test_kimi_endpoint_model_and_json_validation(self):
        backend = self.backend(ResearchPlan(objective="Test", tasks=[]).model_dump_json())
        result = backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(result.objective, "Test")
        self.assertEqual(str(backend.client.base_url), Config.kimi_base_url + "/")
        self.assertEqual(backend.sent["model"], "kimi-k3")
        self.assertEqual(backend.sent["response_format"], {"type": "json_object"})
        self.assertEqual(backend.sent["reasoning_effort"], "low")
        system_message = backend.sent["messages"][0]
        self.assertEqual(system_message["role"], "system")
        self.assertIn("search-query designer", system_message["content"])
        self.assertIn("task.query directly to Tavily's tavily_search", system_message["content"])
        self.assertIn("Current stage instructions:\nPlan", system_message["content"])
        self.assertEqual(backend.tokens_used, 25)
        self.assertNotIn("store", backend.sent)

    def test_invalid_or_truncated_json_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.backend('{"wrong": true}').structured(ResearchPlan, "Plan", "Question")
        with self.assertRaises(ResponseError):
            self.backend('{"objective":', finish_reason="length").structured(ResearchPlan, "Plan", "Question")

    def test_budget_prevents_next_request(self):
        backend = self.backend('{}')
        backend.calls = backend.config.max_calls
        with self.assertRaises(BudgetExceeded):
            backend.structured(ResearchPlan, "Plan", "Question")

    def test_kimi_environment_settings_and_key_alias(self):
        with patch.dict(os.environ, {"KIMI_MODEL": "chosen-model", "KIMI_BASE_URL": "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY": "offline-key"}, clear=True):
            config = Config.from_env(provider="kimi")
            self.assertEqual(config.model, "chosen-model")
            self.assertEqual(config.kimi_base_url, "https://api.moonshot.cn/v1")
            self.assertEqual(require_api_key("kimi"), "offline-key")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            require_api_key()

    def test_invalid_limits(self):
        for changes in ({"max_rounds": -1}, {"max_seconds": float("nan")}, {"max_searches": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config(**changes)


if __name__ == "__main__":
    unittest.main()
