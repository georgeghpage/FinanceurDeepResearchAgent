"""A failing subtest must fail JSON/XML reports and release coverage gates."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location(
    "qa_runner", Path(__file__).resolve().parents[1] / "scripts/run_tests.py"
)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class ReportingTests(unittest.TestCase):
    def test_failed_subtest_and_skips_are_reported(self):
        class Cases(unittest.TestCase):
            def test_subtest(self):
                with self.subTest(value=1):
                    self.assertEqual(1, 2)

            @unittest.skip("optional live credentials absent")
            def test_skip(self):
                pass

        result = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=qa.RecordedResult
        ).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        self.assertFalse(result.wasSuccessful())
        with tempfile.TemporaryDirectory() as root:
            report = qa.write_reports(
                result.records,
                Path(root),
                {"suite": "test", "timestamp": "fixed", "duration_seconds": 0.1},
            )
            self.assertEqual(
                (report["failed"], report["skipped"], report["passed"]), (1, 1, 0)
            )
            xml = ET.parse(Path(root) / "junit.xml")
            self.assertEqual(len(xml.findall(".//failure")), 1)
            self.assertEqual(len(xml.findall(".//skipped")), 1)
            self.assertEqual(
                json.loads((Path(root) / "results.json").read_text())["flaky"], None
            )

    def test_all_requested_suites_are_mapped_to_real_tests(self):
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
        ids = [test.id() for test in qa.flatten(suite)]
        for name, prefixes in qa.SUITES.items():
            with self.subTest(suite=name):
                self.assertTrue(
                    any(
                        any(identifier.startswith(p + ".") for p in prefixes)
                        for identifier in ids
                    )
                )

    def test_release_coverage_fails_when_critical_components_missing(self):
        summary = qa.coverage_summary(
            {
                "totals": {
                    "covered_lines": 100,
                    "num_statements": 100,
                    "covered_branches": 100,
                    "num_branches": 100,
                },
                "files": {},
            }
        )
        self.assertFalse(summary["gates_passed"])
