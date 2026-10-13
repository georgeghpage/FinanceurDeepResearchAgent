"""Local credential loading and the system prompt used in each stage."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from deepsearchagent.config import Config, require_api_key, require_tavily_key
from deepsearchagent.models import ResearchPlan
from deepsearchagent.system_prompt import SYSTEM_PROMPT, build_system_prompt


class ConfigurationTests(unittest.TestCase):
    def test_local_env_supplies_settings_and_keys_without_mutating_environment(self):
        settings = {
            "LLM_PROVIDER": "kimi", "KIMI_MODEL": "kimi-k2.6", "KIMI_BASE_URL": "https://api.moonshot.cn/v1",
            "KIMI_API_KEY": "local-test-key", "TAVILY_API_KEY": "local-test-tavily-key",
        }
        with patch.dict(os.environ, {}, clear=True), \
                patch("deepsearchagent.config.dotenv_values", return_value=settings) as reader:
            config = Config.from_env()
            self.assertEqual(config.model, "kimi-k2.6")
            self.assertEqual(config.kimi_base_url, "https://api.moonshot.cn/v1")
            self.assertEqual(require_api_key("kimi"), "local-test-key")
            self.assertEqual(require_tavily_key(), "local-test-tavily-key")
            reader.assert_called_with(Path.cwd() / ".env")
            self.assertNotIn("KIMI_API_KEY", os.environ)

    def test_exported_values_and_explicit_overrides_take_precedence(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_MODEL": "exported-model", "MOONSHOT_API_KEY": "exported-test-key"}, clear=True), \
                patch("deepsearchagent.config.dotenv_values", return_value={"KIMI_MODEL": "file-model", "KIMI_API_KEY": "file-test-key"}):
            self.assertEqual(Config.from_env().model, "exported-model")
            self.assertEqual(Config.from_env(model="explicit-model").model, "explicit-model")
            self.assertEqual(require_api_key("kimi"), "exported-test-key")


class SystemPromptTests(unittest.TestCase):
    def test_stage_instructions_and_schema_are_added_to_the_shared_prompt(self):
        prompt = build_system_prompt("Create a research plan.", ResearchPlan)
        self.assertTrue(prompt.startswith(SYSTEM_PROMPT))
        self.assertIn("Current stage instructions:\nCreate a research plan.", prompt)
        self.assertIn('"objective"', prompt)
        self.assertIn('"tasks"', prompt)
        self.assertIn("allowed source URLs", prompt)
        self.assertIn("finding_ids", prompt)
        self.assertIn("retrieved documents", prompt.lower())

class AliyunConfigurationTests(unittest.TestCase):
    def test_provider_defaults_overrides_and_independent_keys(self):
        settings = {"LLM_PROVIDER": "aliyun", "DASHSCOPE_API_KEY": "aliyun-only",
                    "KIMI_MODEL": "kimi-custom", "ALIYUN_MODEL": "qwen-custom",
                    "ALIYUN_BASE_URL": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                    "KIMI_INPUT_PRICE_PER_MILLION": "99", "ALIYUN_INPUT_PRICE_PER_MILLION": "1.5"}
        with patch.dict(os.environ, {}, clear=True), patch("deepsearchagent.config.dotenv_values", return_value=settings):
            config = Config.from_env()
            self.assertEqual((config.provider, config.model), ("aliyun", "qwen-custom"))
            self.assertEqual(config.base_url, settings["ALIYUN_BASE_URL"])
            self.assertEqual(config.input_price_per_million, 1.5)
            self.assertEqual(require_api_key("aliyun"), "aliyun-only")
            with self.assertRaises(ValueError):
                require_api_key("kimi")
            self.assertEqual(Config.from_env(provider="kimi").model, "kimi-custom")
            self.assertEqual(Config.from_env(provider="aliyun", model="explicit-model").model, "explicit-model")

    def test_dashscope_export_overrides_file_alias_and_no_kimi_key_fallback(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "exported"}, clear=True), \
                patch("deepsearchagent.config.dotenv_values", return_value={"ALIYUN_API_KEY": "file"}):
            self.assertEqual(require_api_key("aliyun"), "exported")
        with patch.dict(os.environ, {"KIMI_API_KEY": "kimi-only"}, clear=True), \
                patch("deepsearchagent.config.dotenv_values", return_value={}):
            self.assertEqual(Config.from_env(provider="aliyun").model, "deepseek-v4.1-flash")
            with self.assertRaises(ValueError):
                require_api_key("aliyun")

    def test_invalid_provider_model_and_endpoint_are_rejected(self):
        for overrides in ({"provider": "unknown"}, {"model": "bad model"},
                          {"aliyun_base_url": "https://key@example.com/v1"}):
            with self.assertRaises(ValueError):
                Config(**overrides)

    def test_migrated_defaults_and_environment_capabilities(self):
        with patch.dict(os.environ, {}, clear=True), patch("deepsearchagent.config.dotenv_values", return_value={}):
            config = Config.from_env()
        self.assertEqual((config.provider, config.model, config.judge_provider), ("aliyun", "deepseek-v4.1-flash", "aliyun"))
        self.assertEqual(config.reasoning_effort, "low")
        self.assertTrue(config.thinking_enabled)
        from deepsearchagent.capabilities import inference_options, model_limits
        self.assertEqual(inference_options(config), {"reasoning_effort": "low", "extra_body": {"enable_thinking": True}})
        self.assertEqual(model_limits(config), (1000000, 393216))
        self.assertEqual(inference_options(Config(model="unverified-model")), {})
        self.assertEqual(model_limits(Config(model="unverified-model")), (32768, 8192))
        self.assertEqual(inference_options(Config(thinking_enabled=False)), {"extra_body": {"enable_thinking": False}})
