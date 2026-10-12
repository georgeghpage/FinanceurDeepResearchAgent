#!/usr/bin/env python3
"""Scoped formatting/type checks and repository-wide fatal lint checks."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
QA_FILES = [
    "scripts/run_tests.py",
    "scripts/qa_checks.py",
    "scripts/qa_evaluate.py",
    "scripts/qa_load.py",
    "scripts/qa_report.py",
    "tests/support_fixtures.py",
    "tests/support_quality.py",
    "tests/browser/server.py",
    "tests/test_storage_reliability.py",
    "tests/test_properties.py",
    "tests/test_reliability.py",
    "tests/test_provider_contracts.py",
    "tests/test_retry_policy.py",
    "tests/test_research_experience.py",
    "tests/test_security.py",
    "tests/test_reporting.py",
    "tests/test_smoke.py",
    "tests/test_smoke_control.py",
    "tests/test_quality.py",
    "tests/test_judge_boundaries.py",
    "tests/test_orchestration_edges.py",
    "tests/test_consolidated_report.py",
]


def main():
    commands = [
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "src",
            "tests",
            "scripts",
            "--select",
            "E9,F63,F7,F82",
        ],
        [sys.executable, "-m", "ruff", "format", "--check", *QA_FILES],
        [
            sys.executable,
            "-m",
            "mypy",
            "src/deepsearchagent/config.py",
            "src/deepsearchagent/models.py",
            "--follow-imports=silent",
        ],
    ]
    statuses = [subprocess.run(cmd, cwd=ROOT).returncode for cmd in commands]
    return int(any(statuses))


if __name__ == "__main__":
    raise SystemExit(main())
