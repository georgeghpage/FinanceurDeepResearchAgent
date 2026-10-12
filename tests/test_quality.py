import tempfile
import unittest
from support_quality import dataset, gate_case, workflow_case


class QualityRegressionTests(unittest.TestCase):
    def test_curated_deterministic_ready_adversaries(self):
        for case in dataset("classifier_gates.v1.json"):
            with self.subTest(case=case["id"]):
                self.assertEqual(gate_case(case), case["expected_eligible"])

    def test_versioned_complete_workflow_regressions(self):
        with tempfile.TemporaryDirectory() as root:
            for case in dataset("workflows.v1.json"):
                with self.subTest(case=case["id"]):
                    result = workflow_case(case, root)
                    self.assertTrue(result["passed"], result)
