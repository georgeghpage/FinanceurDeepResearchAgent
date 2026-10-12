"""Metrics must be reproducible and must not substitute model labels for humans."""

import unittest

from deepsearchagent.benchmark import review_queue, score


class BenchmarkTests(unittest.TestCase):
    def test_225_unlabeled_snapshots_cover_nine_categories(self):
        records = list(review_queue())
        self.assertEqual(len(records), 225)
        self.assertEqual(len({r["category"] for r in records}), 9)
        self.assertTrue(all(r["human_decision"] is None and r["reviewer"] is None for r in records))
        self.assertEqual(len({r["id"] for r in records}), 225)
        with self.assertRaises(ValueError):
            score(records, [])

    def test_known_confusion_and_gap_metrics(self):
        gold = [
            {"id": "a", "human_decision": "RESEARCH_MORE", "human_gap_requirement_ids": ["R2"], "reviewer": "human"},
            {"id": "b", "human_decision": "INSUFFICIENT_EVIDENCE", "human_gap_requirement_ids": ["R1", "R2"], "reviewer": "human"},
            {"id": "c", "human_decision": "READY", "human_gap_requirement_ids": [], "reviewer": "human"},
            {"id": "d", "human_decision": "READY", "human_gap_requirement_ids": [], "reviewer": "human"},
        ]
        predictions = [
            {"id": "a", "decision": "READY", "gap_requirement_ids": [], "latency_seconds": 1, "tokens": 10, "cost_usd": 0.01},
            {"id": "b", "decision": "INSUFFICIENT_EVIDENCE", "gap_requirement_ids": ["R1"], "latency_seconds": 2, "tokens": 20, "cost_usd": 0.02},
            {"id": "c", "decision": "RESEARCH_MORE", "gap_requirement_ids": ["R1"], "latency_seconds": 3, "tokens": 30, "cost_usd": 0.03},
            {"id": "d", "decision": "READY", "gap_requirement_ids": [], "latency_seconds": 4, "tokens": 40, "cost_usd": 0.04},
        ]
        metrics = score(gold, predictions)
        self.assertEqual(metrics["false_ready_rate"], 0.5)
        self.assertEqual(metrics["incorrect_approval_rate"], 0.5)
        self.assertEqual(metrics["false_research_more_rate"], 0.5)
        self.assertEqual(metrics["decision_agreement"], 0.5)
        self.assertEqual(metrics["gap_precision"], 0.5)
        self.assertAlmostEqual(metrics["gap_recall"], 1/3)
        self.assertEqual(metrics["total_tokens"], 100)
        self.assertEqual(metrics["mean_latency_seconds"], 2.5)
        self.assertIsNone(metrics["follow_up_query_utility"])

    def test_label_requires_named_human_reviewer(self):
        with self.assertRaises(ValueError):
            score([{"id": "a", "human_decision": "READY", "human_gap_requirement_ids": [], "reviewer": None}], [{"id": "a", "decision": "READY"}])
