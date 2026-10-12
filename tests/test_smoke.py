"""Explicitly opt-in, at most one paid request per configured provider."""

import os
import unittest
from deepsearchagent.config import Config, require_api_key, require_tavily_key
from deepsearchagent.judge import OpenAICompatibleJudge
from deepsearchagent.llm import AliyunBackend, KimiBackend
from deepsearchagent.models import ReportAudit
from deepsearchagent.tools.tavily import TavilyMCPSearch


class LiveSmokeTests(unittest.TestCase):
    def setUp(self):
        if os.getenv("QA_LIVE_ENABLED") != "1":
            self.skipTest("Live smoke disabled; use run_tests.py --suite smoke --live")

    def test_research_model_tiny_json(self):
        provider = os.getenv("LLM_PROVIDER", "kimi")
        try:
            key = require_api_key(provider)
        except ValueError:
            self.skipTest("Research provider credential unavailable")
        config = Config.from_env(
            max_calls=1, max_tokens=2000, max_output_tokens=128, request_timeout=20
        )
        backend_type = AliyunBackend if provider == "aliyun" else KimiBackend
        backend = backend_type(config, key)
        try:
            result = backend.structured(
                ReportAudit, 'Return JSON: {"paragraphs": []}', "{}"
            )
            self.assertEqual(result.paragraphs, [])
            self.assertEqual(backend.calls, 1)
        finally:
            backend.client.close()

    def test_tavily_single_search(self):
        try:
            key = require_tavily_key()
        except ValueError:
            self.skipTest("Tavily credential unavailable")
        result = TavilyMCPSearch(Config.from_env().tavily_mcp_url, key).search(
            "Python official documentation", timeout=20
        )
        self.assertTrue(result.sources)

    def test_pai_single_small_classifier(self):
        if not os.getenv("JUDGE_MODEL_TOKEN"):
            self.skipTest("PAI credential unavailable")
        from deepsearchagent.models import Requirement

        config = Config.from_env(
            judge_provider="pai",
            judge_retries=0,
            judge_timeout=20,
            judge_output_tokens=512,
        )
        judge = OpenAICompatibleJudge(config)
        try:
            decision = judge.evaluate(
                "Establish unavailable private fact",
                [
                    Requirement(
                        id="R1", question="Private fact", critical=True, weight=1
                    )
                ],
                {"findings": [], "sources": [], "contradictions": []},
                {"round": 1},
            )
            self.assertNotEqual(decision.decision, "READY")
        finally:
            judge.close()
