"""Targeted regressions for final-answer quality and session diagnostics."""

import json
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from deepsearchagent.config import Config
from deepsearchagent.diagnostics import ErrorHistory, failure_from_exception
from deepsearchagent.judge import OpenAICompatibleJudge, validate_quality
from deepsearchagent.llm import ResponseError
from deepsearchagent.models import QualityEvaluation, QuerySpec
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge, plan


def evaluation_fixture():
    requirements = plan().research_requirements
    evidence = {
        "findings": [
            {
                "id": f"F{i}",
                "requirement_id": r.id,
                "source_id": f"S{i}",
                "evidence_status": "SUPPORTED",
                "evidence": "measured 42",
                "claim": "42",
            }
            for i, r in enumerate(requirements, 1)
        ],
        "sources": [
            {"id": f"S{i}", "url": f"https://example.com/{i}"} for i in range(1, 4)
        ],
    }
    result = FakeJudge().evaluate_answer(
        "Compare three facts", requirements, "42", evidence, {}
    )
    return result, requirements, evidence


class AnswerQualityContracts(unittest.TestCase):
    def test_default_models_provider_and_documented_caps(self):
        from deepsearchagent.capabilities import capabilities

        config = Config()
        self.assertEqual(
            (
                config.model,
                config.final_writer_provider,
                config.final_writer_model,
                config.judge_provider,
                config.judge_model,
            ),
            ("deepseek-v4.1-flash", "aliyun", "qwen3.8-flash", "aliyun", "qwen3.8-max"),
        )
        self.assertEqual(capabilities("aliyun", "qwen3.8-flash").output_tokens, 131072)
        with self.assertRaises(ValueError):
            replace(config, quality_max_iterations=6)
        with self.assertRaises(ValueError):
            replace(config, quality_min_primary_score=float("nan"))

    def test_relevance_and_grounding_gates_reject_false_passes(self):
        # Representative false-pass cases: polished drift, missing country,
        # unsupported quantitative claim, broken citation, incomplete output.
        for field in (
            "relevance",
            "requirement_coverage",
            "factual_grounding",
            "citation_quality",
        ):
            result, requirements, evidence = evaluation_fixture()
            setattr(result.scores, field, 2)
            gated = validate_quality(
                result,
                requirements,
                evidence,
                Config(),
                answer_claim_ids=["F1", "F2", "F3"],
            )
            self.assertEqual(gated.decision, "REVISE")
            self.assertLess(gated.overall_score, 5)
        result, requirements, evidence = evaluation_fixture()
        for changes in (
            {"citation_issues": ["S1 does not support the associated number"]},
            {"unsupported_claims": ["Unverified: claimed 99 instead of 42"]},
            {"missing_requirements": ["R3"]},
        ):
            gated = validate_quality(
                result.model_copy(update=changes),
                requirements,
                evidence,
                Config(),
                answer_claim_ids=["F1", "F2", "F3"],
            )
            self.assertEqual(gated.decision, "REVISE")
        self.assertEqual(
            validate_quality(
                result, requirements, evidence, Config(), answer_claim_ids=["F1", "F2"]
            ).decision,
            "REVISE",
        )

    def test_strict_contract_references_and_nonfinite_scores(self):
        result, requirements, evidence = evaluation_fixture()
        for changes in (
            {"unexpected": True},
            {"overall_score": float("nan")},
            {"decision": "READY"},
        ):
            with self.assertRaises(ValidationError):
                QualityEvaluation.model_validate(
                    {**result.model_dump(), **changes}, strict=True
                )
        result.requirements_coverage[0].supporting_claim_ids = ["invented"]
        with self.assertRaises(ResponseError):
            validate_quality(result, requirements, evidence, Config())
        result, requirements, evidence = evaluation_fixture()
        result.requirements_coverage.pop()
        with self.assertRaises(ResponseError):
            validate_quality(result, requirements, evidence, Config())

    def test_provider_receives_question_requirements_actual_answer_and_completion(self):
        result, requirements, evidence = evaluation_fixture()
        client = Mock()
        client.chat.completions.create.return_value = NS(
            usage=None,
            choices=[
                NS(
                    finish_reason="stop",
                    message=NS(content=result.model_dump_json(), refusal=None),
                )
            ],
        )
        with patch("deepsearchagent.judge.require_api_key", return_value="OFFLINE"):
            judge = OpenAICompatibleJudge(Config(), client=client)
            judge.evaluate_answer(
                "Compare A, B, C; only 2026 data",
                requirements,
                "Final report 42",
                evidence,
                {"stop_reason": "coverage"},
            )
        call = client.chat.completions.create.call_args.kwargs
        self.assertEqual(call["model"], "qwen3.8-max")
        self.assertEqual(judge.base_url, Config().aliyun_base_url)
        payload = json.loads(call["messages"][1]["content"])
        self.assertEqual(payload["final_answer"], "Final report 42")
        self.assertIn("only 2026", payload["original_query"])
        self.assertEqual(len(payload["requirements"]), 3)
        self.assertEqual(payload["research_completion"]["stop_reason"], "coverage")
        self.assertIn("Prioritize relevance", call["messages"][0]["content"])


class QualityLoopTests(unittest.TestCase):
    def run_agent(self, judge, backend=None, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        events = []
        search = FakeAsyncSearch(delay=0)
        backend = backend or AutoBackend()
        agent = ResearchAgent(
            backend,
            Config(
                session_dir=directory.name, search_requests_per_second=10000, **options
            ),
            SearchRouter(search),
            judge=judge,
            trace=events.append,
        )
        state, report = agent.run("Compare facts 1, 2 and 3 in 2026")
        return state, report, events, backend, search

    def test_revision_uses_targeted_feedback_and_never_repeats_initial_search(self):
        class ReviseJudge(FakeJudge):
            answer_calls = 0

            def evaluate_answer(self, *args):
                self.answer_calls += 1
                result = super().evaluate_answer(*args)
                if self.answer_calls == 1:
                    result.decision = "REVISE"
                    result.actionable_feedback = [
                        "Explain the comparison in the established facts section."
                    ]
                    result.recommended_next_action = result.actionable_feedback[0]
                return result

        class CaptureWriter(AutoBackend):
            revisions = []

            def structured(self, schema, instructions, payload, **options):
                data = json.loads(payload)
                if "targeted_feedback" in data:
                    self.revisions.append(data)
                return super().structured(schema, instructions, payload, **options)

        judge, backend = ReviseJudge(), CaptureWriter()
        state, _, events, _, search = self.run_agent(judge, backend)
        self.assertEqual(state.quality_status, "pass", state.research_limitations)
        self.assertEqual(judge.answer_calls, 2)
        self.assertEqual(len(search.queries), 15)
        self.assertEqual(len(backend.revisions), 1)
        self.assertIn(
            "Explain the comparison",
            backend.revisions[0]["targeted_feedback"]["actionable_feedback"][0],
        )
        self.assertEqual(
            [e["decision"] for e in events if e["kind"] == "quality_evaluation"],
            ["REVISE", "PASS"],
        )

    def test_more_research_only_searches_missing_requirements(self):
        class MoreJudge(FakeJudge):
            answer_calls = 0

            def evaluate_answer(self, *args):
                self.answer_calls += 1
                result = super().evaluate_answer(*args)
                if self.answer_calls == 1:
                    result.decision = "MORE_RESEARCH"
                    result.missing_requirements = ["R3"]
                    result.actionable_feedback = ["Research the missing R3 comparison."]
                    result.suggested_queries = [
                        QuerySpec(
                            query="targeted additional R3 evidence",
                            requirement_ids=["R3"],
                        )
                    ]
                return result

        state, _, _, backend, search = self.run_agent(MoreJudge())
        self.assertEqual(state.quality_status, "pass", state.research_limitations)
        self.assertEqual(len(search.queries), 16)
        self.assertEqual(search.queries[-1], "targeted additional R3 evidence")
        self.assertEqual(sum(n == "PlanningBrief" for n, _ in backend.limits), 1)
        self.assertGreater(len(state.findings), 3)

    def test_judge_failure_is_not_pass_and_cannot_contaminate_report(self):
        class FailedJudge(FakeJudge):
            answer_calls = 0

            def evaluate_answer(self, *args):
                self.answer_calls += 1
                raise ResponseError(
                    "CANARY_API_KEY internal prompt", category="invalid_response"
                )

        judge = FailedJudge()
        state, report, events, _, _ = self.run_agent(judge, llm_backoff=0)
        self.assertEqual(state.quality_status, "unverified")
        self.assertEqual(state.status, "PARTIAL")
        self.assertEqual(judge.answer_calls, 2)
        self.assertNotIn("CANARY", report + json.dumps(events))
        self.assertNotIn("invalid_response", report)
        self.assertIn("could not be independently verified", report)

    def test_iteration_limit_and_insufficient_evidence_are_explicit(self):
        class NeverPass(FakeJudge):
            answer_calls = 0

            def evaluate_answer(self, *args):
                self.answer_calls += 1
                result = super().evaluate_answer(*args)
                result.decision = "REVISE"
                result.actionable_feedback = ["Correct the requested comparison."]
                return result

        judge = NeverPass()
        state, report, _, _, search = self.run_agent(judge, quality_max_iterations=1)
        self.assertEqual(judge.answer_calls, 2)
        self.assertEqual(state.quality_status, "iteration_limit")
        self.assertEqual(len(search.queries), 15)
        self.assertIn("further review", report)

        class Insufficient(FakeJudge):
            def evaluate_answer(self, *args):
                result = super().evaluate_answer(*args)
                result.decision = "INSUFFICIENT_EVIDENCE"
                result.missing_requirements = ["R3"]
                result.actionable_feedback = [
                    "Acknowledge missing reliable fact 3 evidence."
                ]
                return result

        state, report, _, _, search = self.run_agent(Insufficient())
        self.assertEqual(state.quality_status, "insufficient_evidence")
        self.assertIn("Insufficient evidence to answer: Establish fact 3", report)
        self.assertEqual(len(search.queries), 15)


class SessionDiagnosticTests(unittest.TestCase):
    def test_retries_group_and_recovery_updates_preserve_history(self):
        diagnostic = failure_from_exception(
            httpx.ReadTimeout("secret"),
            provider="tavily",
            stage="search",
            operation="search",
        ).model_dump()
        history = ErrorHistory("run")
        first = history.observe(
            {
                "kind": "search_error",
                "invocation_id": "query-1",
                "attempt": 1,
                "diagnostic": diagnostic,
            }
        )
        history.observe(
            {
                "kind": "search_retry",
                "invocation_id": "query-1",
                "next_attempt": 2,
                "retry_delay_seconds": 1,
            }
        )
        history.observe(
            {
                "kind": "search_error",
                "invocation_id": "query-1",
                "attempt": 2,
                "diagnostic": diagnostic,
            }
        )
        recovered = history.observe(
            {"kind": "search_recovered", "invocation_id": "query-1", "attempt": 3}
        )
        self.assertEqual(first["error_id"], recovered["error_id"])
        self.assertEqual(len(history.issues), 1)
        self.assertEqual(recovered["resolution"], "recovered")
        self.assertEqual(recovered["attempt"], 3)
        self.assertNotIn("secret", json.dumps(recovered))

    def test_actual_provider_status_code_cause_and_unknown_do_not_expose_body(self):
        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"code": "RateLimit", "message": "Bearer CANARY"}},
        )
        diagnostic = failure_from_exception(
            httpx.HTTPStatusError("CANARY", request=request, response=response),
            provider="aliyun",
            stage="write",
            operation="synthesize",
        )
        self.assertEqual(
            (
                diagnostic.http_status,
                diagnostic.provider_code,
                diagnostic.cause_confidence,
            ),
            (429, "RateLimit", "confirmed"),
        )
        self.assertNotIn("CANARY", diagnostic.model_dump_json())
        unknown = failure_from_exception(
            RuntimeError("CANARY"),
            provider="agent",
            stage="write",
            operation="synthesize",
        )
        self.assertEqual(unknown.root_cause, "Unknown")

    def test_downstream_consequences_retain_original_cause_and_stream_deltas_do_not_repeat_notices(
        self,
    ):
        original = failure_from_exception(
            httpx.ReadTimeout("secret request"),
            provider="aliyun",
            stage="write",
            operation="chat_completion",
        )
        original = original.model_copy(
            update={"correlation_id": "writer-op", "upstream_span_id": "writer:1"}
        )
        exc = RuntimeError("secret wrapper")
        exc.research_diagnostic = original
        consequence = failure_from_exception(
            exc, provider="agent", stage="finalize", operation="assemble_report"
        )
        self.assertEqual(consequence.origin, "downstream")
        self.assertEqual(consequence.provider, "aliyun")
        self.assertEqual(
            (consequence.stage, consequence.operation), ("write", "chat_completion")
        )
        self.assertEqual(consequence.upstream_span_id, "writer:1")
        self.assertEqual(original.downstream_effects, [])
        history = ErrorHistory("run")
        first = history.observe(
            {
                "kind": "llm_error",
                "invocation_id": "writer-op",
                "diagnostic": original.model_dump(),
            }
        )
        update = history.observe(
            {"kind": "report_error", "diagnostic": consequence.model_dump()}
        )
        self.assertEqual(update["error_id"], first["error_id"])
        self.assertEqual(update["diagnostic"]["operation"], "chat_completion")
        self.assertEqual(update["downstream_effects"], ["finalize / assemble_report"])
        exc.research_diagnostic = consequence
        propagated = failure_from_exception(
            exc, provider="agent", stage="session", operation="complete"
        )
        exc.research_diagnostic = propagated
        propagated = failure_from_exception(
            exc, provider="agent", stage="session", operation="complete"
        )
        first_downstream = ErrorHistory("late").observe(
            {"kind": "session_error", "diagnostic": propagated.model_dump()}
        )
        self.assertEqual(first_downstream["diagnostic"]["operation"], "chat_completion")
        self.assertEqual(
            first_downstream["downstream_effects"],
            ["finalize / assemble_report", "session / complete"],
        )
        for _ in range(20):
            self.assertIsNone(
                history.observe(
                    {
                        "kind": "output_delta",
                        "invocation_id": "writer-op",
                        "text": "secret",
                    }
                )
            )
        self.assertEqual(len(history.issues), 1)
        self.assertNotIn("secret", json.dumps(update))
