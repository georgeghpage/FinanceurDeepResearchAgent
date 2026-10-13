"""Regression coverage for lightweight planning and forward progress after limits."""

import json
import tempfile
import threading
import unittest

from deepsearchagent import planner
from deepsearchagent.config import Config
from deepsearchagent.llm import BudgetExceeded, IncompleteResponse
from deepsearchagent.models import (
    EvidenceExtraction,
    PlanningBrief,
    QueryBatch,
    ReportDraft,
)
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge, plan


class FastPipelineTests(unittest.TestCase):
    def run_agent(self, backend, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        events = []
        agent = ResearchAgent(
            backend,
            Config(session_dir=directory.name, search_requests_per_second=10000, **options),
            SearchRouter(FakeAsyncSearch(delay=0)),
            judge=FakeJudge(),
            trace=events.append,
        )
        state, report = agent.run("Establish facts 1, 2 and 3 in 2026")
        return agent, state, report, events

    def test_plan_is_separate_and_queries_disable_thinking_only_for_their_call(self):
        backend = AutoBackend()
        _, state, _, events = self.run_agent(backend, reasoning_effort="max")
        calls = dict(backend.limits)
        self.assertEqual(
            [n for n, _ in backend.limits][:2], ["PlanningBrief", "QueryBatch"]
        )
        self.assertEqual(calls["PlanningBrief"]["reasoning_effort"], "low")
        self.assertFalse(calls["QueryBatch"]["thinking_enabled"])
        self.assertNotIn("thinking_enabled", calls["EvidenceExtraction"])
        self.assertFalse(calls["ReportAudit"]["thinking_enabled"])
        self.assertLessEqual(calls["ReportAudit"]["timeout"], 20)
        self.assertLessEqual(calls["ReportAudit"]["output_tokens"], 4096)
        self.assertEqual(state.status, "COMPLETE", state.warnings)
        self.assertTrue(state.plan.objective)
        self.assertTrue(state.plan.steps)
        self.assertEqual(
            [e["query_count"] for e in events if e["kind"] == "plan"], [0, 15]
        )

    def test_planner_timeout_still_searches_and_writes(self):
        class TimeoutPlanner(AutoBackend):
            def structured(self, schema, *args, **kwargs):
                if schema is PlanningBrief:
                    raise BudgetExceeded("planner deadline", category="request_timeout")
                return super().structured(schema, *args, **kwargs)

        _, state, report, events = self.run_agent(TimeoutPlanner())
        self.assertGreater(state.search_calls, 0)
        self.assertGreater(len(state.findings), 0)
        self.assertIn("42", report)
        self.assertIsNone(state.failure)
        self.assertNotIn("Planning limited", report)
        recovery = next(e for e in events if e["kind"] == "planning_recovered")
        self.assertEqual(recovery["diagnostic"]["category"], "request_timeout")
        self.assertEqual(recovery["schema"], "PlanningBrief")
        self.assertTrue(recovery["recovered"])
        self.assertNotIn(recovery["diagnostic"]["message"], report)

    def test_partial_queries_are_used_once_without_continuation(self):
        class PartialQueries(AutoBackend):
            attempts = 0

            def structured(self, schema, *args, **kwargs):
                result = super().structured(schema, *args, **kwargs)
                if schema is QueryBatch:
                    self.attempts += 1
                    raise IncompleteResponse(
                        '{"queries":['
                        + result.queries[0].model_dump_json()
                        + ',{"query":"unfinished'
                    )
                return result

        backend = PartialQueries()
        _, state, report, events = self.run_agent(backend, max_continuations=5)
        self.assertEqual(backend.attempts, 1)
        self.assertEqual(state.search_calls, 3)
        self.assertEqual(
            state.plan.search_queries[0].query, plan().search_queries[0].query
        )
        self.assertIn("42", report)
        recovery = next(e for e in events if e["kind"] == "planning_recovered")
        self.assertEqual(recovery["schema"], "QueryBatch")
        self.assertEqual(recovery["diagnostic"]["category"], "output_limit")

    def test_one_worker_timeout_does_not_skip_successes_or_classifier(self):
        class TimeoutWorker(AutoBackend):
            def structured(self, schema, instructions, payload, **kwargs):
                if (
                    schema is EvidenceExtraction
                    and json.loads(payload)["requirements"][0]["id"] == "R1"
                ):
                    raise BudgetExceeded("worker deadline", category="request_timeout")
                return super().structured(schema, instructions, payload, **kwargs)

        agent, state, report, _ = self.run_agent(TimeoutWorker())
        self.assertEqual(agent.judge.calls, 1)
        self.assertEqual(len(state.findings), 2)
        self.assertEqual(state.report_status, "generated")
        self.assertIn("42", report)
        self.assertIsNone(state.failure)

    def test_stream_checkpoint_survives_outer_deadline_and_late_worker(self):
        release = threading.Event()

        class InterruptedWorker(AutoBackend):
            def structured(self, schema, instructions, payload, **kwargs):
                result = super().structured(schema, instructions, payload, **kwargs)
                if schema is EvidenceExtraction:
                    text = result.model_dump_json()
                    kwargs["on_partial"](
                        text[: text.index(',"contradictions"')] + ',"contradictions":['
                    )
                    release.wait(2)
                return result

        try:
            _, state, report, _ = self.run_agent(
                InterruptedWorker(), request_timeout=0.15
            )
            self.assertEqual(len(state.findings), 3, state.warnings)
            self.assertIn("42", report)
            self.assertEqual(state.status, "PARTIAL")
        finally:
            release.set()

    def test_simple_question_needs_one_query_without_padding(self):
        brief = planner.recover_brief("What is a compiler?")
        result = planner.assemble_plan(brief, [], 15)
        self.assertEqual(len(result.search_queries), 1)
        self.assertEqual(len(result.research_groups), 1)

    def test_extractor_receives_evidence_beyond_document_navigation(self):
        from deepsearchagent.researcher import group_payload, EXTRACTION_PROMPT
        from deepsearchagent.models import SearchResult
        from deepsearchagent.budget import stage_prompt, token_bound

        content = "Navigation and links. " * 60 + "The search tool is tavily-search."
        result = SearchResult(
            text=content,
            sources=[],
            documents=[
                {
                    "url": "https://example.com/docs",
                    "source_id": "S1",
                    "content": content,
                }
            ],
        )
        payload = group_payload(
            "Which search tool?",
            plan().research_requirements,
            plan().search_queries[:5],
            [result],
            24000,
        )
        self.assertIn(
            "The search tool is tavily-search.", payload["documents"][0]["content"]
        )
        self.assertLessEqual(
            token_bound(stage_prompt(EXTRACTION_PROMPT, EvidenceExtraction))
            + token_bound(json.dumps(payload, ensure_ascii=False))
            + 64,
            24000,
        )

    def test_low_search_limit_keeps_requirement_mapping_valid(self):
        brief = PlanningBrief(
            objective="Facts 1, 2 and 3",
            steps=["Collect evidence"],
            research_requirements=plan().research_requirements,
        )
        result = planner.assemble_plan(brief, [], 1)
        self.assertEqual(len(result.search_queries), 1)
        self.assertEqual(
            set(result.search_queries[0].requirement_ids), {"R1", "R2", "R3"}
        )

    def test_truncated_writer_keeps_complete_paragraphs_and_audits(self):
        class PartialWriter(AutoBackend):
            def structured(self, schema, *args, **kwargs):
                result = super().structured(schema, *args, **kwargs)
                if schema is ReportDraft:
                    result.sections[0].paragraphs = result.sections[0].paragraphs[:1]
                    text = result.model_dump_json()
                    raise IncompleteResponse(text[:-2] + ',{"title":"unfinished')
                return result

        backend = PartialWriter()
        _, state, report, _ = self.run_agent(backend)
        self.assertEqual(
            state.citation_validation_status,
            "semantic_and_references_passed",
            state.warnings,
        )
        self.assertTrue(any(n == "ReportAudit" for n, _ in backend.limits))
        self.assertIn("42", report)
        self.assertIn("Retained paragraphs require review", report)
        self.assertNotIn("retained complete paragraphs", report)

    def test_total_time_exhaustion_assembles_existing_evidence_without_new_requests(
        self,
    ):
        class ExhaustedJudge(FakeJudge):
            def evaluate(self, *args):
                raise BudgetExceeded("time up", category="time_budget")

        with tempfile.TemporaryDirectory() as directory:
            backend = AutoBackend()
            agent = ResearchAgent(
                backend,
                Config(session_dir=directory),
                SearchRouter(FakeAsyncSearch(delay=0)),
                judge=ExhaustedJudge(),
            )
            # Writer is also denied; stored evidence must still form a cited report.
            from unittest.mock import patch

            with patch(
                "deepsearchagent.writer.write_evidence",
                side_effect=BudgetExceeded("time up", category="time_budget"),
            ):
                state, report = agent.run("Facts")
            self.assertEqual(state.report_status, "best_effort_assembled")
            self.assertIn("[S", report)
            self.assertIn("42", report)
