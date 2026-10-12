#!/usr/bin/env python3
"""Run existing unittest suites with outbound isolation and CI-friendly evidence."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
SUITES = {
    "unit": [
        "test_config_prompt",
        "test_references",
        "test_pricing",
        "test_model_catalog",
        "test_properties",
        "test_reporting",
        "test_consolidated_report",
        "test_smoke_control",
    ],
    "contract": [
        "test_provider_contracts",
        "test_mcp",
        "test_multi_agent.ContractTests",
        "test_multi_agent.JudgeTransportTests",
        "test_judge_boundaries",
    ],
    "integration": [
        "test_history",
        "test_ui",
        "test_multi_agent",
        "test_storage_reliability",
        "test_reliability",
        "test_research_experience",
    ],
    "network": [
        "test_diagnostics",
        "test_mcp",
        "test_provider_contracts",
        "test_retry_policy",
        "test_research_experience",
    ],
    "agent": [
        "test_agent",
        "test_multi_agent",
        "test_cancellation",
        "test_reliability",
        "test_research_experience",
        "test_orchestration_edges",
    ],
    "evaluation": ["test_benchmark", "test_quality"],
    "security": [
        "test_security",
        "test_storage_reliability",
        "test_diagnostics",
        "test_references",
    ],
    "performance": ["test_properties", "test_reliability"],
    "frontend": ["test_web", "test_streaming", "test_ui"],
    "e2e": ["test_multi_agent.WorkflowTests", "test_reliability", "test_web"],
    "smoke": ["test_smoke"],
}


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.current = None

    def startTest(self, test):
        super().startTest(test)
        self.current = {
            "id": test.id(),
            "status": "passed",
            "details": [],
            "started": time.monotonic(),
        }

    def record_problem(self, test, status, detail):
        if self.current is None:
            self.records.append(
                {
                    "id": test.id(),
                    "status": status,
                    "details": [detail],
                    "duration_seconds": 0,
                }
            )
        else:
            self.current["status"] = status
            self.current["details"].append(detail)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record_problem(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record_problem(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record_problem(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record_problem(
            test, "expected_failure", self._exc_info_to_string(err, test)
        )

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record_problem(
            test, "unexpected_success", "An expected failure unexpectedly passed"
        )

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record_problem(
                test, "failed", f"{subtest.id()}\n{self._exc_info_to_string(err, test)}"
            )

    def stopTest(self, test):
        self.current["duration_seconds"] = time.monotonic() - self.current.pop(
            "started"
        )
        self.records.append(self.current)
        self.current = None
        super().stopTest(test)


def write_reports(records, output, metadata):
    output.mkdir(parents=True, exist_ok=True)
    counts = {
        status: sum(r["status"] == status for r in records)
        for status in (
            "passed",
            "failed",
            "error",
            "skipped",
            "expected_failure",
            "unexpected_success",
        )
    }
    payload = {
        **metadata,
        "total_tests": len(records),
        **counts,
        "flaky": None,
        "flaky_note": "No retries are used; a single run cannot estimate flakiness.",
        "tests": records,
    }
    (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    root = ET.Element(
        "testsuite",
        name=metadata["suite"],
        tests=str(len(records)),
        failures=str(counts["failed"] + counts["unexpected_success"]),
        errors=str(counts["error"]),
        skipped=str(counts["skipped"] + counts["expected_failure"]),
        time=str(metadata["duration_seconds"]),
    )
    for record in records:
        module, _, name = record["id"].rpartition(".")
        item = ET.SubElement(
            root,
            "testcase",
            classname=module,
            name=name,
            time=str(record["duration_seconds"]),
        )
        tag = {
            "failed": "failure",
            "error": "error",
            "skipped": "skipped",
            "expected_failure": "skipped",
            "unexpected_success": "failure",
        }.get(record["status"])
        if tag:
            ET.SubElement(item, tag).text = "\n".join(record["details"])
    ET.ElementTree(root).write(
        output / "junit.xml", encoding="utf-8", xml_declaration=True
    )
    rows = [
        "# QA execution report",
        "",
        f"Suite: {metadata['suite']}. UTC: {metadata['timestamp']}.",
        "",
        "| Total | Passed | Failed | Errors | Skipped | Duration |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {len(records)} | {counts['passed']} | {counts['failed']} | {counts['error']} | {counts['skipped']} | {metadata['duration_seconds']:.2f}s |",
        "",
        "Flakiness: unmeasured (no automatic retries). Live functionality is not established by these results.",
        "",
        "| Module | Passed | Failed/errors | Skipped |",
        "| --- | --- | --- | --- |",
    ]
    modules = sorted({r["id"].split(".")[0] for r in records})
    for module in modules:
        subset = [r for r in records if r["id"].split(".")[0] == module]
        rows.append(
            f"| {module} | {sum(r['status'] == 'passed' for r in subset)} | {sum(r['status'] in ('failed', 'error', 'unexpected_success') for r in subset)} | {sum(r['status'] == 'skipped' for r in subset)} |"
        )
    for record in records:
        if record["status"] != "passed":
            rows += [
                "",
                f"## {record['status']}: {record['id']}",
                "",
                "```text",
                "\n".join(record["details"]),
                "```",
            ]
    if metadata.get("coverage"):
        rows += [
            "",
            "## Coverage",
            "",
            f"Overall line coverage: {metadata['coverage']['line_percent']:.2f}%. Overall branch coverage: {metadata['coverage']['branch_percent']:.2f}%.",
            "",
            "| Component | Line % | Branch % | Recommended branch target |",
            "| --- | --- | --- | --- |",
        ]
        for name, value in metadata["coverage"]["critical"].items():
            rows.append(
                f"| {name} | {value['line_percent']:.2f} | {value['branch_percent']:.2f} | {value['target']} |"
            )
        rows += [
            "",
            f"Recommended release coverage gates: {'PASS' if metadata['coverage']['gates_passed'] else 'UNMET'}. See coverage HTML and JSON for missing paths.",
        ]
    rows += [
        "",
        "## Separate scorecards",
        "",
        "- AI answer correctness / semantic citation quality / calibrated false READY rate: unverified; human labels and live predictions required.",
        "- Performance: run scripts/qa_load.py; deterministic fake-provider timings only.",
        "- Security: tests validate boundaries; dependency scan is a separate artifact.",
        "- Browser: npm run test:browser; browser report is a separate artifact.",
        "- Production readiness: do not infer release readiness from a passing mocked suite.",
        "",
    ]
    (output / "QUALITY_REPORT.md").write_text("\n".join(rows))
    return payload


def coverage_summary(data):
    totals = data["totals"]
    ratio = lambda hit, total: 100 * hit / total if total else 100.0
    critical = {}
    for filename, item in data["files"].items():
        name = Path(filename).stem
        if name in ("orchestrator", "budget", "judge", "evidence_store"):
            summary = item["summary"]
            critical[name] = {
                "line_percent": ratio(
                    summary["covered_lines"], summary["num_statements"]
                ),
                "branch_percent": ratio(
                    summary["covered_branches"], summary["num_branches"]
                ),
                "target": 90 if name == "evidence_store" else 95,
            }
    line = ratio(totals["covered_lines"], totals["num_statements"])
    return {
        "line_percent": line,
        "branch_percent": ratio(totals["covered_branches"], totals["num_branches"]),
        "critical": critical,
        "gates_passed": line >= 85
        and len(critical) == 4
        and all(v["branch_percent"] >= v["target"] for v in critical.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["all", *SUITES], default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/qa")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--require-release-coverage", action="store_true")
    parser.add_argument("--require-database", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable only smoke tests; at most 3 paid requests",
    )
    args = parser.parse_args()
    if args.live and args.suite != "smoke":
        parser.error("--live is restricted to --suite smoke")
    if args.require_release_coverage and (not args.coverage or args.suite != "all"):
        parser.error("Release coverage requires --suite all --coverage")
    if args.require_database and not os.getenv("TEST_DATABASE_URL"):
        parser.error(
            "TEST_DATABASE_URL is required; CI database checks must not silently skip"
        )
    if args.suite in ("all", "frontend", "e2e") and not shutil.which("node"):
        parser.error("Node is required for the frontend checks")
    if args.coverage and not importlib.util.find_spec("coverage"):
        parser.error("Install development dependencies: uv sync --group dev")
    args.output.mkdir(parents=True, exist_ok=True)
    cov = None
    if args.coverage:
        import coverage

        cov = coverage.Coverage(
            branch=True,
            source=[str(ROOT / "src/deepsearchagent")],
            data_file=str(args.output / ".coverage"),
        )
        cov.start()
    loader = unittest.TestLoader()
    discovered = loader.discover(str(ROOT / "tests"))
    tests = list(flatten(discovered))
    if args.suite != "all":
        tests = [
            t
            for t in tests
            if any(t.id().startswith(prefix + ".") for prefix in SUITES[args.suite])
        ]
    if not tests:
        parser.error("No tests selected")
    original_connect, original_resolve = socket.socket.connect, socket.getaddrinfo

    def local_host(host):
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(sock, address):
        if isinstance(address, tuple) and not local_host(address[0]):
            raise AssertionError("Offline QA attempted outbound network access")
        return original_connect(sock, address)

    def resolve(host, *values, **kwargs):
        if host is not None and not local_host(host):
            raise AssertionError("Offline QA attempted external DNS lookup")
        return original_resolve(host, *values, **kwargs)

    started = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(
            patch("deepsearchagent.config.dotenv_values", return_value={})
        )
        if args.live:
            stack.enter_context(patch.dict(os.environ, {"QA_LIVE_ENABLED": "1"}))
        else:
            stack.enter_context(patch.dict(os.environ, {"QA_LIVE_ENABLED": "0"}))
            stack.enter_context(patch("socket.socket.connect", connect))
            stack.enter_context(patch("socket.getaddrinfo", resolve))
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(
            unittest.TestSuite(tests)
        )
    metadata = {
        "suite": args.suite,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": time.monotonic() - started,
        "live_enabled": args.live,
        "coverage": None,
    }
    if cov:
        cov.stop()
        cov.save()
        cov.xml_report(outfile=str(args.output / "coverage.xml"))
        cov.json_report(outfile=str(args.output / "coverage.json"))
        cov.html_report(directory=str(args.output / "coverage-html"))
        metadata["coverage"] = coverage_summary(
            json.loads((args.output / "coverage.json").read_text())
        )
    write_reports(result.records, args.output, metadata)
    print(f"QA artifacts: {args.output}")
    passed = result.wasSuccessful() and (
        not args.require_release_coverage or metadata["coverage"]["gates_passed"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
