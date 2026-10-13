"""Default workflow contracts, asynchronous integration and adversarial routing."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from pydantic import ValidationError

from deepsearchagent.budget import RunBudget, StageBackend
from deepsearchagent.cancellation import Cancellation
from deepsearchagent.config import Config
from deepsearchagent.evidence_store import EvidenceStore
from deepsearchagent.judge import OpenAICompatibleJudge, ready_eligible, validate_decision
from deepsearchagent.llm import BudgetExceeded, ResponseError
from deepsearchagent.models import (EvidenceExtraction, EvidenceFinding, EvidencePlan, Finding, JudgeDecision,
    ParagraphAudit, PlannedQuery, ReportAudit, ReportDraft, ReportParagraph, ReportSection,
    Requirement, RequirementEvaluation, ResearchGroup, ResearchState, SearchResult, Source)
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter


from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge, plan


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(session_dir=self.directory.name, search_requests_per_second=10000)

    def run_agent(self, judge=None, backend=None, search=None, config=None, fallback=None):
        backend, search = backend or AutoBackend(), search or FakeAsyncSearch()
        events = []
        state, report = ResearchAgent(backend, config or self.config, SearchRouter(search), trace=events.append,
            judge=judge or FakeJudge(), fallback_judge=fallback).run("Establish facts 1, 2 and 3 in 2026.")
        return state, report, backend, search, events

    def test_initial_flow_parallel_budgets_and_atomic_session(self):
        backend = AutoBackend()
        backend.extraction_barrier = threading.Barrier(3)
        state, report, backend, search, events = self.run_agent(backend=backend)
        self.assertEqual(state.stop_reason, "coverage", state.warnings)
        self.assertEqual(len(search.queries), 15)
        self.assertEqual(search.peak, 5)
        self.assertEqual(backend.extractions, 3)
        self.assertEqual(backend.peak, 3)
        self.assertEqual(len(state.plan.research_groups), 3)
        self.assertEqual(state.rounds, 1)
        self.assertEqual(state.citation_validation_status, "semantic_and_references_passed")
        self.assertEqual(state.report_status, "generated")
        self.assertTrue(all(options["output_tokens"] == self.config.researcher_output_tokens for name, options in backend.limits if name == "EvidenceExtraction"))
        self.assertTrue(any(options["output_tokens"] == self.config.writer_output_tokens for name, options in backend.limits if name == "ReportDraft"))
        facts = Path(state.facts_path)
        self.assertTrue(facts.exists())
        self.assertIn("Original question", facts.read_text())
        self.assertIn("Latest classifier evaluation", facts.read_text())
        self.assertEqual(json.loads((facts.parent / "state.json").read_text())["session_id"], state.session_id)
        saved = json.loads((facts.parent / "evaluation-1.json").read_text())
        self.assertEqual(saved["evidence_version"], 3)
        self.assertIn("snapshot_sha256", saved)
        self.assertIn("[S", report)
        self.assertEqual(events[-1]["report_status"], "generated")

    def test_refinement_only_runs_targeted_query_then_ready(self):
        state, _, backend, search, _ = self.run_agent(judge=FakeJudge(("RESEARCH_MORE", "READY")))
        self.assertEqual(state.stop_reason, "coverage", state.warnings)
        self.assertEqual(len(search.queries), 16)
        self.assertEqual(search.queries[-1], "targeted additional evidence")
        self.assertEqual(backend.extractions, 4)
        self.assertEqual(state.rounds, 2)
        self.assertEqual(len(state.judge_history), 2)

    def test_maximum_refinement_rounds_and_queries(self):
        queries = [f"targeted evidence {i}" for i in range(8)]
        state, report, _, search, _ = self.run_agent(judge=FakeJudge(("RESEARCH_MORE",), queries=queries))
        self.assertLessEqual(len(search.queries), 25)
        self.assertLessEqual(state.rounds, 3)
        self.assertIn("Unresolved requirement R3", report)
        self.assertEqual(state.report_status, "generated")

    def test_two_full_refinement_rounds(self):
        class ChangingJudge(FakeJudge):
            def evaluate(self, *args):
                self.queries = [f"targeted round {self.calls} query {i}" for i in range(5)]
                return super().evaluate(*args)
        state, _, _, search, _ = self.run_agent(judge=ChangingJudge(("RESEARCH_MORE",)))
        self.assertEqual(state.rounds, 3)
        self.assertEqual(state.stop_reason, "round_limit")
        self.assertEqual(len(search.queries), 25)

    def test_insufficiency_writes_transparent_best_effort(self):
        state, report, _, search, _ = self.run_agent(judge=FakeJudge(("INSUFFICIENT_EVIDENCE",)))
        self.assertEqual(len(search.queries), 15)
        self.assertEqual(state.report_status, "generated")
        self.assertIn("Need fact 3", report)
        self.assertIn("Unresolved requirement R3", report)

    def test_classifier_recovery_and_failure_degrade_without_ready(self):
        for errors, expected in ((1, "coverage"), (2, "no_tasks")):
            with self.subTest(errors=errors):
                judge = FakeJudge(errors=errors)
                state, report, _, _, _ = self.run_agent(judge=judge)
                self.assertEqual(judge.calls, 2)
                self.assertEqual(state.stop_reason, expected)
                self.assertEqual(state.report_status, "generated")
                if errors == 2:
                    self.assertIsNone(state.judge)
                    self.assertTrue(any("classification was unavailable" in w for w in state.warnings))
                self.assertNotIn("classification was unavailable", report)

    def test_fallback_uses_same_validation(self):
        primary, fallback = FakeJudge(errors=2), FakeJudge()
        state, _, _, _, events = self.run_agent(judge=primary, fallback=fallback)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(state.stop_reason, "coverage")
        self.assertIn("classifier_fallback", [e["kind"] for e in events])

    def test_unknown_judge_reference_is_rejected(self):
        judge = FakeJudge(bad_reference=True)
        state, report, _, _, _ = self.run_agent(judge=judge)
        self.assertEqual(judge.calls, 2)
        self.assertIsNone(state.judge)
        self.assertNotEqual(state.stop_reason, "coverage")
        self.assertTrue(any("classification was unavailable" in w for w in state.warnings))
        self.assertNotIn("classification was unavailable", report)

    def test_invented_excerpt_cannot_pass_ready(self):
        state, report, _, _, events = self.run_agent(backend=AutoBackend(wrong_excerpt=True))
        self.assertTrue(all(f.evidence_status == "UNVERIFIED" for f in state.findings))
        self.assertNotEqual(state.stop_reason, "coverage")
        self.assertIn("classifier_ready_rejected", [e["kind"] for e in events])
        self.assertNotIn("Measured value was", report)

    def test_empty_retrieval_still_terminates_and_writes(self):
        state, report, backend, search, _ = self.run_agent(search=FakeAsyncSearch(empty=True))
        self.assertEqual(backend.extractions, 0)
        self.assertEqual(state.findings, [])
        self.assertEqual(state.report_status, "best_effort_assembled")
        self.assertIn("Unresolved requirement R1", report)
        self.assertEqual(len(search.queries), 15)

    def test_duplicate_sources_have_stable_ids(self):
        state, _, _, _, _ = self.run_agent(search=FakeAsyncSearch(duplicate=True))
        self.assertEqual(len(state.sources), 1)
        self.assertEqual(len(state.sources[0].query_ids), 15)
        self.assertEqual({f.source_id for f in state.findings}, {"S1"})
        self.assertEqual(len({f.id for f in state.findings}), len(state.findings))

    def test_plan_respects_small_search_budget_and_finishes(self):
        state, report, _, search, _ = self.run_agent(config=replace(self.config, max_searches=5))
        self.assertEqual(len(search.queries), 5)
        self.assertEqual(state.stop_reason, "coverage")
        self.assertTrue(state.findings)
        self.assertIn("42", report)
        self.assertIn(state.report_status, {"generated", "best_effort_assembled"})

    def test_invalid_report_reference_or_semantic_addition_uses_safe_fallback(self):
        for options in ({"unsupported_report": True}, {"audit_reject": True}):
            with self.subTest(options=options):
                state, report, _, _, _ = self.run_agent(backend=AutoBackend(**options))
                self.assertEqual(state.report_status, "best_effort_assembled")
                self.assertNotIn("F999", report)
                self.assertEqual(state.citation_validation_status, "extractive_references_passed")

    def test_time_budget_always_returns_bounded_report(self):
        config = replace(self.config, max_seconds=0.05, writer_reserve_seconds=0.01)
        started = time.monotonic()
        state, report, _, _, _ = self.run_agent(config=config, search=FakeAsyncSearch(delay=0.03))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(state.stop_reason, "budget")
        self.assertTrue(report)

    def test_sessions_are_isolated(self):
        first = self.run_agent()[0]
        second = self.run_agent()[0]
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.facts_path, second.facts_path)

    def test_prompt_injection_stays_in_user_payload(self):
        backend = AutoBackend()
        state, _, _, _, _ = self.run_agent(backend=backend)
        for _, options in backend.limits:
            self.assertIn("untrusted data", options["system_prompt"])

    def test_cancelled_work_does_not_write_report_or_more_calls(self):
        cancellation = Cancellation()
        cancellation.cancel()
        backend = AutoBackend()
        state, _ = ResearchAgent(backend, self.config, SearchRouter(FakeAsyncSearch()), cancellation=cancellation, judge=FakeJudge()).run("Question")
        self.assertEqual(state.stop_reason, "cancelled")
        self.assertEqual(backend.calls, 0)
        self.assertEqual(state.report_status, "pending")


class ContractTests(unittest.TestCase):
    def test_plan_exact_counts_mapping_and_uniqueness(self):
        for change in (lambda p: p["search_queries"].pop(),
                       lambda p: p["research_groups"][0]["query_ids"].__setitem__(0, "Q6"),
                       lambda p: p["requirement_query_mapping"].__setitem__("R1", ["Q15"]),
                       lambda p: p["search_queries"][0].__setitem__("query", p["search_queries"][1]["query"])):
            data = plan().model_dump()
            change(data)
            with self.assertRaises(ValidationError):
                EvidencePlan.model_validate(data)

    def fixture(self):
        requirements = plan().research_requirements
        sources = [Source(id=f"S{i}", url=f"https://example.com/{i}", title="Official") for i in range(1, 4)]
        findings = [Finding(id=f"F{i}", task_question=f"R{i}", requirement_id=f"R{i}", claim="Fact", evidence="Excerpt", source_id=f"S{i}") for i in range(1, 4)]
        state = ResearchState(question="Question", plan=plan(), findings=findings, sources=sources)
        from deepsearchagent.judge import evidence_snapshot
        decision = FakeJudge().evaluate("Question", requirements, evidence_snapshot(state, 10000), {})
        return decision, requirements, findings, sources

    def test_schema_rejects_missing_scores_unknown_fields_nan_and_bad_queries(self):
        decision, *_ = self.fixture()
        mutations = ({"coverage_score": 1.1}, {"evidence_score": float("nan")}, {"decision": "WRITE"},
                     {"unexpected": True}, {"suggested_queries": [""]}, {"coverage_score": "0.99"})
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                JudgeDecision.model_validate({**decision.model_dump(), **change}, strict=True)
        data = decision.model_dump()
        del data["reason"]
        with self.assertRaises(ValidationError):
            JudgeDecision.model_validate(data)

    def test_ready_gate_critical_missing_unsupported_contradictions_weighted_coverage(self):
        decision, reqs, findings, sources = self.fixture()
        self.assertTrue(ready_eligible(decision, reqs, findings, sources))
        for status in ("MISSING", "PARTIAL", "CONFLICTED"):
            copy = decision.model_copy(deep=True)
            copy.requirements[-1].status = status
            self.assertFalse(ready_eligible(copy, reqs, findings, sources))
        self.assertFalse(ready_eligible(decision.model_copy(update={"contradictions": ["Conflict"]}), reqs, findings, sources))
        self.assertFalse(ready_eligible(decision, reqs, findings[:-1], sources))
        findings[-1].evidence_status = "UNVERIFIED"
        self.assertFalse(ready_eligible(decision, reqs, findings, sources))

    def test_judge_ids_duplicates_criticality_and_cross_requirement_refs(self):
        decision, reqs, findings, sources = self.fixture()
        for change in (lambda d: d.requirements.pop(),
                       lambda d: d.requirements.append(d.requirements[0]),
                       lambda d: setattr(d.requirements[0], "critical", False),
                       lambda d: setattr(d.requirements[0], "supporting_claim_ids", ["F999"]),
                       lambda d: setattr(d.requirements[0], "supporting_claim_ids", ["F2"]),
                       lambda d: setattr(d.requirements[0], "supporting_source_ids", ["S999"])):
            copy = decision.model_copy(deep=True)
            change(copy)
            with self.assertRaises(ResponseError):
                validate_decision(copy, reqs, findings, sources)

    def test_input_budget_rejects_before_call(self):
        backend = AutoBackend()
        budget = RunBudget(Config())
        with self.assertRaises(BudgetExceeded):
            StageBackend(backend, budget, 4000, 100).structured(EvidencePlan, "Plan", "{}")
        self.assertEqual(backend.calls, 0)

    def test_token_reservations_atomic_under_parallel_pressure(self):
        budget = RunBudget(Config(max_tokens=1000, writer_reserve_tokens=0))
        successes = []
        def reserve():
            try:
                successes.append(budget.reserve_model(100, 300, (0, 0)))
            except BudgetExceeded:
                pass
        threads = [threading.Thread(target=reserve) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(successes), 2)
        self.assertEqual(budget.pending_tokens, 800)

    def test_cost_budget_and_unknown_pricing(self):
        budget = RunBudget(Config(max_cost_usd=0.01, writer_reserve_tokens=0))
        with self.assertRaises(BudgetExceeded):
            budget.reserve_model(100, 100, (None, None))
        with self.assertRaises(BudgetExceeded):
            budget.reserve_model(100, 100, (100, 100))
        self.assertEqual(budget.calls, 0)
        budget.reserve_model(100, 100, (0, 0))
        with self.assertRaises(BudgetExceeded):
            budget.reserve_search()

    def test_default_and_config_researcher_output_enforcement(self):
        self.assertEqual(Config().researcher_output_tokens, 16384)
        self.assertEqual(Config().max_searches, 25)
        self.assertEqual(Config(max_rounds=0).max_rounds, 0)
        for values in ({"researcher_output_tokens": 0}, {"judge_retries": 2}, {"search_concurrency": 0},
                       {"judge_provider": "pai", "judge_json_mode": "json_schema"}):
            with self.assertRaises(ValueError):
                Config(**values)


class JudgeTransportTests(unittest.TestCase):
    def test_documented_pai_sdk_auth_and_custom_json_prompt(self):
        decision = ContractTests().fixture()[0]
        client = Mock()
        client.chat.completions.create.return_value = NS(usage=NS(total_tokens=50, prompt_tokens=30, completion_tokens=20),
            choices=[NS(finish_reason="stop", message=NS(content=decision.model_dump_json(), refusal=None))])
        with patch("deepsearchagent.judge.OpenAI", return_value=client) as factory, \
             patch("deepsearchagent.config.dotenv_values", return_value={}), \
             patch.dict(os.environ, {"JUDGE_MODEL_TOKEN": "offline-judge"}, clear=True):
            judge = OpenAICompatibleJudge(Config(judge_provider="pai"))
            result = judge.evaluate("Question", plan().research_requirements, {}, {})
        self.assertEqual(factory.call_args.kwargs["api_key"], "Authorization: Bearer offline-judge")
        self.assertEqual(factory.call_args.kwargs["base_url"], "https://aiservice.cn-hangzhou.aliyuncs.com/v1")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in request["messages"]], ["system", "user"])
        self.assertNotIn("tools", request)
        self.assertEqual(result.decision, "READY")

    def test_truncation_malformed_json_and_empty_are_never_ready(self):
        for finish, content in (("length", "{}"), ("stop", ""), ("stop", "not JSON")):
            client = Mock()
            client.chat.completions.create.return_value = NS(usage=None, choices=[NS(finish_reason=finish, message=NS(content=content, refusal=None))])
            judge = OpenAICompatibleJudge(Config(judge_provider="pai"), client=client)
            with self.assertRaises((ResponseError, ValidationError)):
                judge.evaluate("Question", plan().research_requirements, {}, {})


class EntrypointTests(unittest.TestCase):
    def test_cli_uses_default_multi_agent_flow(self):
        from deepsearchagent.cli import main
        backend, search, judge = AutoBackend(), FakeAsyncSearch(), FakeJudge()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ALIYUN_API_KEY": "offline", "TAVILY_API_KEY": "offline", "SESSION_DIR": directory}, clear=True), \
             patch("deepsearchagent.config.dotenv_values", return_value={}), \
             patch("deepsearchagent.cli.AliyunBackend", return_value=backend), \
             patch("deepsearchagent.cli.TavilyMCPSearch", return_value=search), \
             patch("deepsearchagent.orchestrator.OpenAICompatibleJudge", return_value=judge), \
             patch("sys.argv", ["deepsearchagent", "Establish facts", "--quiet", "--output", str(Path(directory) / "answer.md"),
                                "--state", str(Path(directory) / "result.json"), "--log", str(Path(directory) / "trace.jsonl")]):
            main()
            state = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(state["stop_reason"], "coverage")
            self.assertEqual(len(search.queries), 15)
            self.assertEqual(backend.extractions, 3)
            self.assertTrue(Path(state["facts_path"]).exists())

    def test_http_stream_uses_new_workflow_and_returns_session_artifact(self):
        import http.client
        from deepsearchagent.ui import ResearchServer
        backend, search, judge = AutoBackend(), FakeAsyncSearch(), FakeJudge()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ALIYUN_API_KEY": "offline", "TAVILY_API_KEY": "offline", "SESSION_DIR": directory}, clear=True), \
             patch("deepsearchagent.config.dotenv_values", return_value={}), \
             patch("deepsearchagent.ui.AliyunBackend", return_value=backend), \
             patch("deepsearchagent.ui.TavilyMCPSearch", return_value=search), \
             patch("deepsearchagent.orchestrator.OpenAICompatibleJudge", return_value=judge):
            server = ResearchServer(("127.0.0.1", 0))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("POST", "/api/research", json.dumps({"question": "Establish facts"}))
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                events = [json.loads(line) for line in response.read().splitlines()]
                result = events[-1]
                self.assertEqual(result["type"], "result")
                self.assertEqual(result["stop_reason"], "coverage")
                self.assertEqual(result["report_status"], "generated")
                self.assertTrue(Path(result["facts_path"]).exists())
                self.assertEqual(len(search.queries), 15)
                self.assertEqual(backend.extractions, 3)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                worker.join()


class AdditionalFailureTests(unittest.TestCase):
    def config(self, root, **changes):
        return Config(session_dir=root, **changes)

    def test_async_retrieval_retries_transient_errors_and_counts_attempts(self):
        from deepsearchagent.tools.search import SearchError
        class TransientSearch(FakeAsyncSearch):
            def __init__(self):
                super().__init__()
                self.seen = set()
            async def asearch(self, query, *, timeout):
                if query not in self.seen:
                    self.seen.add(query)
                    raise SearchError("Transient", category="rate_limit")
                return await super().asearch(query, timeout=timeout)
        with tempfile.TemporaryDirectory() as root:
            search = TransientSearch()
            state, _ = ResearchAgent(AutoBackend(), self.config(root, max_searches=30, search_backoff=0),
                SearchRouter(search), judge=FakeJudge()).run("Establish facts")
        self.assertEqual(state.stop_reason, "coverage", state.warnings)
        self.assertEqual(state.search_calls, 30)
        self.assertEqual(len(search.queries), 15)
        self.assertEqual(len(state.sources), 15)

    def test_auth_failure_is_not_retried_and_fallback_is_validated(self):
        class AuthenticationJudge(FakeJudge):
            def evaluate(self, *args):
                self.calls += 1
                raise ResponseError("Authentication failed", category="authentication")
        with tempfile.TemporaryDirectory() as root:
            primary, fallback = AuthenticationJudge(), FakeJudge()
            state, _ = ResearchAgent(AutoBackend(), self.config(root), SearchRouter(FakeAsyncSearch()),
                judge=primary, fallback_judge=fallback).run("Question")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(state.stop_reason, "coverage")

    def test_known_unresolved_contradiction_cannot_be_hidden_by_judge(self):
        class ContradictoryBackend(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                result = super().structured(schema, instructions, payload, **options)
                if schema is EvidenceExtraction:
                    result.contradictions = ["Two dated records disagree on the key conclusion."]
                return result
        with tempfile.TemporaryDirectory() as root:
            state, report = ResearchAgent(ContradictoryBackend(), self.config(root), SearchRouter(FakeAsyncSearch()), judge=FakeJudge()).run("Question")
        self.assertNotEqual(state.stop_reason, "coverage")
        self.assertIn("records disagree", report)
        self.assertEqual(state.judge_history[0]["advisory_decision"]["decision"], "READY")
        self.assertEqual(state.judge_history[0]["decision"]["decision"], "INSUFFICIENT_EVIDENCE")

    def test_negligible_gain_terminates_before_round_limit(self):
        class RepeatingSearch(FakeAsyncSearch):
            async def asearch(self, query, *, timeout):
                return await super().asearch(query.replace("targeted", "repeat"), timeout=timeout)
        class ChangingJudge(FakeJudge):
            def evaluate(self, *args):
                self.queries = [f"targeted query round {self.calls}"]
                return super().evaluate(*args)
        with tempfile.TemporaryDirectory() as root:
            state, report = ResearchAgent(AutoBackend(), self.config(root), SearchRouter(RepeatingSearch()),
                judge=ChangingJudge(("RESEARCH_MORE",))).run("Question")
        self.assertEqual(state.rounds, 2)
        self.assertEqual(state.stop_reason, "no_tasks")
        self.assertTrue(any("marginal evidence gain" in w for w in state.warnings))
        self.assertNotIn("marginal evidence gain", report)

    def test_zero_refinement_budget_still_writes(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = ResearchAgent(AutoBackend(), self.config(root, max_rounds=0), SearchRouter(FakeAsyncSearch()),
                judge=FakeJudge(("RESEARCH_MORE",))).run("Question")
        self.assertEqual(state.rounds, 1)
        self.assertEqual(state.stop_reason, "round_limit")
        self.assertEqual(state.report_status, "generated")

    def test_evidence_store_upgrade_keeps_claim_id_and_serialized_writes(self):
        with tempfile.TemporaryDirectory() as root:
            state = ResearchState(question="Question", plan=plan())
            store = EvidenceStore(state, root)
            query = state.plan.search_queries[0]
            url = "https://example.com/real"
            result = SearchResult(text="Actual excerpt", sources=[Source(id="", title="Source", url=url)],
                documents=[{"url": url, "content": "Actual excerpt"}])
            store.add_result(query, result)
            finding = EvidenceFinding(claim_id="worker-1", requirement_id="R1", claim="Evidence claim", source_id="S1", source_url=url,
                supporting_excerpt="Invented", publication_date=None, evidence_status="SUPPORTED", limitations=[])
            output = EvidenceExtraction(findings=[finding], contradictions=[], uncertainties=[])
            store.merge(output, {"R1"}, [result], [query.id])
            self.assertEqual(state.findings[0].evidence_status, "UNVERIFIED")
            finding.supporting_excerpt = "Actual excerpt"
            store.merge(output, {"R1"}, [result], [query.id])
            self.assertEqual(state.findings[0].id, "F1")
            self.assertEqual(state.findings[0].evidence_status, "SUPPORTED")
            self.assertEqual(len(state.findings), 1)
            self.assertEqual(state.evidence_version, 2)
            self.assertFalse(list(Path(state.facts_path).parent.glob(".snapshot-*")))


class FinalRequirementTests(unittest.TestCase):
    def test_writer_omission_uses_extractively_complete_fallback(self):
        class OmittingWriter(AutoBackend):
            def structured(self, schema, instructions, payload, **options):
                result = super().structured(schema, instructions, payload, **options)
                if schema is ReportDraft:
                    result.sections[0].paragraphs = result.sections[0].paragraphs[:1]
                return result
        with tempfile.TemporaryDirectory() as root:
            state, report = ResearchAgent(OmittingWriter(), Config(session_dir=root), SearchRouter(FakeAsyncSearch()), judge=FakeJudge()).run("Question")
        self.assertEqual(state.report_status, "best_effort_assembled")
        self.assertIn("Establish fact 1", report)
        self.assertIn("Establish fact 2", report)
        self.assertIn("Establish fact 3", report)


class NativeTavilyConcurrencyTests(unittest.TestCase):
    def test_concurrent_transport_errors_keep_independent_phases(self):
        from deepsearchagent.tools.tavily import TavilyMCPSearch
        from deepsearchagent.tools.search import SearchError
        web = TavilyMCPSearch("https://example.com/mcp", "offline")
        async def fail(query, timeout):
            web._step(query)
            await asyncio.sleep(0.005 if query == "initialize" else 0.001)
            raise TimeoutError("Private error")
        web._search = fail
        async def run():
            return await asyncio.gather(web.asearch("initialize", timeout=1), web.asearch("call_tool", timeout=1), return_exceptions=True)
        results = asyncio.run(run())
        self.assertTrue(all(isinstance(r, SearchError) for r in results))
        self.assertEqual([r.diagnostic.operation for r in results], ["initialize", "call_tool"])


class SynchronousAdapterDeadlineTests(unittest.TestCase):
    def test_uncooperative_sync_search_cannot_hold_run_open(self):
        release = threading.Event()
        class SlowSyncSearch:
            def search(self, query, *, timeout):
                release.wait(5)
                return SearchResult(text="", sources=[])
        with tempfile.TemporaryDirectory() as root:
            started = time.monotonic()
            try:
                state, _ = ResearchAgent(AutoBackend(), Config(session_dir=root, max_seconds=0.06,
                    writer_reserve_seconds=0.01, search_retries=0), SearchRouter(SlowSyncSearch()), judge=FakeJudge()).run("Question")
                # Must release well before the blocked provider's five seconds;
                # allow instrumented filesystem/scheduler overhead on the host.
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertEqual(state.stop_reason, "budget")
                self.assertEqual(state.report_status, "best_effort_assembled")
            finally:
                release.set()
