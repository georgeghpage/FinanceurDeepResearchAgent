"""Missing, skipped and globally failed artifacts cannot imply passing QA."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "qa_report", Path(__file__).resolve().parents[1] / "scripts/qa_report.py"
)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class ConsolidatedReportTests(unittest.TestCase):
    def test_missing_and_invalid_artifacts_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "results.json").write_text("not JSON")
            report = qa.consolidate(path)
            self.assertFalse(report["deterministic_checks_passed"])
            self.assertFalse(report["release_ready"])
            self.assertEqual(report["evidence"]["backend"]["status"], "invalid")
            self.assertFalse(report["gates"]["database"])
            self.assertIn("NOT ESTABLISHED", qa.markdown(report))
            self.assertIsNone(report["metrics"]["answer_quality"]["semantic_accuracy"])

    def test_browser_skips_retries_and_setup_errors_remain_visible(self):
        data = {
            "suites": [
                {
                    "specs": [
                        {
                            "title": "flow",
                            "tests": [
                                {
                                    "projectName": "chromium",
                                    "results": [
                                        {"status": "failed"},
                                        {"status": "passed"},
                                    ],
                                },
                                {
                                    "projectName": "firefox",
                                    "results": [{"status": "skipped"}],
                                },
                            ],
                        }
                    ]
                }
            ],
            "errors": [{"message": "server did not start"}],
        }
        summary = qa.browser_summary(data)
        self.assertEqual(
            (summary["passed"], summary["skipped"], summary["retried"]), (1, 1, 1)
        )
        self.assertTrue(summary["global_errors"])

    def test_security_scans_report_unscannable_dependencies(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "python-audit.json").write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {"name": "unknown-remote", "skip_reason": "unavailable"},
                            {
                                "name": "fixture",
                                "version": "1",
                                "vulns": [{"id": "CANARY-VULN", "fix_versions": ["2"]}],
                            },
                        ]
                    }
                )
            )
            (path / "npm-audit.json").write_text(
                json.dumps({"error": {"code": "NETWORK"}})
            )
            report = qa.consolidate(path)
            self.assertFalse(report["gates"]["python_audit"])
            self.assertFalse(report["gates"]["npm_audit"])
            self.assertEqual(
                report["metrics"]["python_audit"]["findings"][0]["id"], "CANARY-VULN"
            )
            self.assertEqual(
                report["metrics"]["python_audit"]["skipped"][0]["name"],
                "unknown-remote",
            )
