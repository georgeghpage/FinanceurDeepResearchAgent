"""Offline checks of rate snapshots and provider cache accounting."""
import math
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from deepsearchagent.config import Config
from deepsearchagent.llm import KimiBackend
from deepsearchagent.pricing import pricing_metadata


class PricingTests(unittest.TestCase):
    def test_unknown_models_are_not_assigned_flagship_prices(self):
        rates = pricing_metadata(Config(model="custom"))
        self.assertIsNone(rates["input_price_per_million"])
        self.assertEqual(pricing_metadata(Config(provider="kimi", model="kimi-k3"))["cached_price_per_million"], .3)

    def test_overrides_accept_free_accounts_and_reject_invalid_rates(self):
        with patch.dict(os.environ, {"KIMI_INPUT_PRICE_PER_MILLION": "0", "TAVILY_PRICE_PER_SEARCH": "0"}), \
             patch("deepsearchagent.config.dotenv_values", return_value={}):
            rates = pricing_metadata(Config.from_env(provider="kimi"))
        self.assertEqual(rates["input_price_per_million"], 0)
        self.assertEqual(rates["tavily_price_per_search"], 0)
        for rate in (-1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                Config(input_price_per_million=rate)

    def test_provider_usage_preserves_cache_and_reasoning_subsets(self):
        usage = NS(total_tokens=1200, prompt_tokens=1000, completion_tokens=200,
                   prompt_tokens_details=NS(cached_tokens=600, cache_write_tokens=100),
                   completion_tokens_details=NS(reasoning_tokens=150))
        observed = KimiBackend._usage(usage)
        self.assertEqual(observed["tokens"], 1200)
        self.assertEqual(observed["cached_tokens"], 600)
        self.assertEqual(observed["cache_write_tokens"], 100)
        self.assertEqual(observed["reasoning_tokens"], 150)
        self.assertFalse(KimiBackend._usage(None)["usage_observed"])
