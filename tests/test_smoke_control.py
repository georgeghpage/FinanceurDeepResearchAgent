"""Opt-in evaluated at execution, without making live requests in regressions."""

import os
import unittest
from unittest.mock import patch
import test_smoke


class SmokeControlTests(unittest.TestCase):
    def test_runtime_opt_in_works_after_module_discovery(self):
        case = test_smoke.LiveSmokeTests("test_tavily_single_search")
        with patch.dict(os.environ, {"QA_LIVE_ENABLED": "0"}):
            with self.assertRaises(unittest.SkipTest):
                case.setUp()
        with patch.dict(os.environ, {"QA_LIVE_ENABLED": "1"}):
            case.setUp()  # no provider call; only checks opt-in
