"""Safety decisions and provider modes at the classifier boundary."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from deepsearchagent.config import Config
from deepsearchagent.judge import (
    OpenAICompatibleJudge,
    evidence_snapshot,
    ready_eligible,
    validate_decision,
)
from deepsearchagent.llm import BudgetExceeded, ResponseError
from deepsearchagent.models import ResearchState
import test_multi_agent as contracts
from support_fixtures import plan


class JudgeBoundaryTests(unittest.TestCase):
    def test_non_ready_decisions_require_actionable_gaps(self):
        decision, reqs, findings, sources = contracts.ContractTests().fixture()
        for changes in (
            {"decision": "RESEARCH_MORE", "suggested_queries": []},
            {"decision": "RESEARCH_MORE", "suggested_queries": ["follow up"]},
            {"suggested_queries": ["unneeded search"]},
            {"decision": "INSUFFICIENT_EVIDENCE", "critical_gaps": []},
        ):
            with self.subTest(changes=changes), self.assertRaises(ResponseError):
                validate_decision(
                    decision.model_copy(update=changes), reqs, findings, sources
                )
        decision.decision = "INSUFFICIENT_EVIDENCE"
        decision.critical_gaps = ["Missing public evidence"]
        self.assertEqual(
            validate_decision(decision, reqs, findings, sources).decision,
            "INSUFFICIENT_EVIDENCE",
        )

    def test_ready_gate_missing_assessments_and_evidence_mismatch(self):
        for mutation in (
            "missing_assessment",
            "no_critical",
            "wrong_id",
            "criticality",
            "missing_evidence",
            "source_mismatch",
        ):
            decision, reqs, findings, sources = contracts.ContractTests().fixture()
            if mutation == "missing_assessment":
                decision.requirements.pop()
            elif mutation == "no_critical":
                for r, entry in zip(reqs, decision.requirements):
                    r.critical = entry.critical = False
            elif mutation == "wrong_id":
                decision.requirements[-1].requirement_id = "R99"
            elif mutation == "criticality":
                decision.requirements[-1].critical = False
            elif mutation == "missing_evidence":
                decision.requirements[-1].missing_evidence = ["Missing year"]
            elif mutation == "source_mismatch":
                decision.requirements[-1].supporting_source_ids = ["S1"]
            with self.subTest(mutation=mutation):
                self.assertFalse(ready_eligible(decision, reqs, findings, sources))
        decision, reqs, findings, sources = contracts.ContractTests().fixture()
        reqs[-1].critical = decision.requirements[-1].critical = False
        self.assertTrue(ready_eligible(decision, reqs, findings, sources))

    def test_snapshot_bound_keeps_round_robin_and_fails_closed_on_metadata(self):
        _, _, findings, sources = contracts.ContractTests().fixture()
        state = ResearchState(
            question="q", plan=plan(), findings=findings * 2, sources=sources
        )
        full = evidence_snapshot(state, 10000)
        self.assertEqual(len(full["findings"]), 6)
        bounded = evidence_snapshot(state, 900)
        self.assertGreater(bounded["omitted_findings"], 0)
        with self.assertRaises(BudgetExceeded):
            evidence_snapshot(state, 1)

    def test_provider_modes_custom_prompt_and_fallback_configuration(self):
        decision, reqs, *_ = contracts.ContractTests().fixture()
        for mode in ("json_schema", "none"):
            client = Mock()
            client.chat.completions.create.return_value = NS(
                usage=None,
                choices=[
                    NS(
                        finish_reason="stop",
                        message=NS(content=decision.model_dump_json(), refusal=None),
                    )
                ],
            )
            with (
                tempfile.TemporaryDirectory() as root,
                patch("deepsearchagent.judge.require_api_key", return_value="OFFLINE"),
            ):
                prompt = Path(root) / "prompt.md"
                prompt.write_text("Custom rubric: evidence only.")
                config = Config(
                    judge_provider="aliyun",
                    judge_model="qwen-flash",
                    judge_json_mode=mode,
                    judge_prompt_path=str(prompt),
                    judge_fallback_provider="kimi",
                )
                judge = OpenAICompatibleJudge(config, client=client)
                self.assertEqual(judge.evaluate("q", reqs, {}, {}).decision, "READY")
                options = client.chat.completions.create.call_args.kwargs
                self.assertIn("Custom rubric", options["messages"][0]["content"])
                self.assertEqual(options["extra_body"], {"enable_thinking": False})
                if mode == "none":
                    self.assertNotIn("response_format", options)
                else:
                    self.assertEqual(options["response_format"]["type"], "json_schema")
                fallback = OpenAICompatibleJudge(config, fallback=True, client=client)
                self.assertEqual(
                    (fallback.provider, fallback.base_url, fallback.json_mode),
                    ("kimi", config.kimi_base_url, "json_object"),
                )
                judge.close()
                client.close.assert_called_once()

    def test_missing_credentials_no_choices_and_input_limit_never_pass(self):
        with (
            patch("deepsearchagent.judge.environment_values", return_value={}),
            self.assertRaises(ValueError),
        ):
            OpenAICompatibleJudge(Config(judge_provider="pai"))
        client = Mock()
        client.chat.completions.create.return_value = NS(usage=None, choices=[])
        judge = OpenAICompatibleJudge(Config(judge_provider="pai"), client=client)
        with self.assertRaises(ResponseError) as failure:
            judge.evaluate("q", plan().research_requirements, {}, {})
        self.assertEqual(failure.exception.category, "empty_response")
        judge.config = replace(judge.config, judge_input_tokens=1)
        client.chat.completions.create.reset_mock()
        with self.assertRaises(BudgetExceeded):
            judge.evaluate("q", plan().research_requirements, {}, {})
        client.chat.completions.create.assert_not_called()
