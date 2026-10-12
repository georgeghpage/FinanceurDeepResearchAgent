#!/usr/bin/env python3
"""Consolidate executed QA evidence; absent or failed checks never become passes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_artifact(root, name):
    path = root / name
    if not path.is_file():
        return {"status": "missing", "path": name}, None
    raw = path.read_bytes()
    evidence = {
        "status": "available",
        "path": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        return evidence, json.loads(raw)
    except (ValueError, UnicodeError):
        return {**evidence, "status": "invalid"}, None


def browser_summary(data):
    tests = []

    def visit(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                results = test.get("results", [])
                tests.append(
                    {
                        "project": test.get("projectName"),
                        "title": spec["title"],
                        "status": results[-1]["status"] if results else "missing",
                        "attempts": len(results),
                    }
                )
        for child in suite.get("suites", []):
            visit(child)

    for suite in data.get("suites", []):
        visit(suite)
    return {
        "total": len(tests),
        "passed": sum(t["status"] == "passed" for t in tests),
        "failed": sum(t["status"] not in ("passed", "skipped") for t in tests),
        "skipped": sum(t["status"] == "skipped" for t in tests),
        "projects": sorted({t["project"] for t in tests}),
        "global_errors": data.get("errors", []),
        "retried": sum(t["attempts"] > 1 for t in tests),
        "tests": tests,
    }


def consolidate(root):
    evidence, values = {}, {}
    names = {
        "backend": "results.json",
        "browser": "browser/results.json",
        "visual": "visual/results.json",
        "evaluation": "evaluation.json",
        "python_audit": "python-audit.json",
        "npm_audit": "npm-audit.json",
        **{
            f"load_{name}": f"load/{name}.json"
            for name in ("c1", "c5", "c10", "c25", "c50", "soak")
        },
    }
    for key, name in names.items():
        evidence[key], values[key] = read_artifact(root, name)
    gates, metrics = {}, {}
    backend = values["backend"]
    gates["backend"] = bool(
        backend
        and backend.get("suite") == "all"
        and backend.get("total_tests", 0)
        and not any(
            backend.get(s, 0) for s in ("failed", "error", "unexpected_success")
        )
        and not backend.get("live_enabled")
    )
    database = [
        r
        for r in (backend or {}).get("tests", [])
        if r["id"].startswith("test_history.HistoryTests.")
    ]
    gates["database"] = bool(
        database and all(r["status"] == "passed" for r in database)
    )
    gates["release_coverage"] = bool(
        backend and (backend.get("coverage") or {}).get("gates_passed")
    )
    metrics["backend"] = {k: v for k, v in (backend or {}).items() if k != "tests"}
    for key in ("browser", "visual"):
        summary = browser_summary(values[key]) if values[key] else None
        metrics[key] = summary
        expected = {"chromium", "firefox", "webkit"} if key == "browser" else {"visual"}
        gates[key] = bool(
            summary
            and summary["total"]
            and summary["passed"] == summary["total"]
            and not summary["global_errors"]
            and not summary["retried"]
            and set(summary["projects"]) == expected
        )
    evaluation = values["evaluation"]
    metrics["evaluation"] = evaluation
    gates["evaluation"] = bool(
        evaluation
        and evaluation.get("gate_cases")
        and evaluation.get("workflow_cases")
        and all(c["eligible"] == c["expected"] for c in evaluation["gate_cases"])
        and all(c["passed"] for c in evaluation["workflow_cases"])
    )
    metrics["performance"] = []
    for key in names:
        if key.startswith("load_"):
            data = values[key]
            gates[key] = bool(
                data
                and data.get("workload") == "scripted_providers_no_live_billing"
                and data.get("sessions", 0)
                and data.get("failed") == 0
                and data.get("passed") == data.get("sessions")
                and data.get("temporary_snapshots_left") == 0
            )
            if data:
                metrics["performance"].append(
                    {k: v for k, v in data.items() if k != "results"}
                )
    python = values["python_audit"]
    dependencies = (python or {}).get("dependencies", [])
    metrics["python_audit"] = {
        "audited": sum("skip_reason" not in d for d in dependencies),
        "skipped": [d for d in dependencies if "skip_reason" in d],
        "findings": [
            {"package": d["name"], "version": d.get("version"), **v}
            for d in dependencies
            for v in d.get("vulns", [])
        ],
    }
    # The local editable application has no PyPI advisory identity; its source
    # is checked by the security suite. Any other skipped dependency fails.
    gates["python_audit"] = bool(
        dependencies
        and not metrics["python_audit"]["findings"]
        and all(
            d["name"] == "deepsearchagent" for d in metrics["python_audit"]["skipped"]
        )
    )
    npm = values["npm_audit"]
    metrics["npm_audit"] = (npm or {}).get("metadata", {}).get("vulnerabilities")
    gates["npm_audit"] = bool(
        npm
        and not npm.get("error")
        and metrics["npm_audit"] is not None
        and metrics["npm_audit"].get("total") == 0
    )
    metrics["answer_quality"] = {
        "semantic_accuracy": None,
        "human_labeled_false_ready_rate": None,
        "live_provider_compatibility": None,
        "production_performance": None,
        "flaky_rate": None,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence": evidence,
        "gates": gates,
        "metrics": metrics,
        "deterministic_checks_passed": all(
            v for k, v in gates.items() if k != "release_coverage"
        ),
        "release_ready": False,
        "release_note": "Live providers, human quality labels and production SLOs remain unverified; inspect coverage gate.",
        "missing_or_invalid": [
            k for k, v in evidence.items() if v["status"] != "available"
        ],
    }


def markdown(report):
    lines = [
        "# Consolidated QA report",
        "",
        f"Generated UTC: {report['generated_at']}",
        "",
        "Production readiness: **NOT ESTABLISHED**. " + report["release_note"],
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    lines += [
        f"| {k} | {'PASS' if v else 'UNMET / MISSING'} |"
        for k, v in report["gates"].items()
    ]
    backend = report["metrics"]["backend"]
    if backend:
        lines += [
            "",
            f"Backend: {backend['total_tests']} total, {backend['passed']} passed, {backend['failed']} failed, {backend['error']} errors, {backend['skipped']} skipped in {backend['duration_seconds']:.2f}s.",
        ]
        coverage = backend.get("coverage")
        if coverage:
            lines += [
                "",
                f"Overall line coverage: {coverage['line_percent']:.2f}%; branch coverage: {coverage['branch_percent']:.2f}%.",
                "",
                "| Component | Line % | Branch % | Branch target |",
                "| --- | --- | --- | --- |",
            ]
            lines += [
                f"| {k} | {v['line_percent']:.2f} | {v['branch_percent']:.2f} | {v['target']} |"
                for k, v in coverage["critical"].items()
            ]
    lines += [
        "",
        "| Load concurrency | Sessions | Failed | p50 seconds | p95 seconds |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {v['concurrency']} | {v['sessions']} | {v['failed']} | {v['p50_seconds']:.3f} | {v['p95_seconds']:.3f} |"
        for v in report["metrics"]["performance"]
    ]
    lines += [
        "",
        "Load results use scripted providers and local storage, with no live billing. Resource snapshots are observations, not proof of absence of leaks.",
        "",
        "Answer correctness, semantic citation support, live integrations, human false READY rate, production latency/cost and flakiness: **unverified**.",
        "",
        "The JSON companion contains component results, security scan details and SHA-256 hashes of every input artifact.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "reports/qa")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="Require complete passing deterministic artifacts; coverage reported separately",
    )
    parser.add_argument(
        "--require-release",
        action="store_true",
        help="Require release coverage and verified release evidence",
    )
    args = parser.parse_args()
    report = consolidate(args.input)
    output = args.output or args.input / "consolidated"
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "QUALITY_REPORT.md").write_text(markdown(report))
    print(f"Consolidated QA report: {output / 'QUALITY_REPORT.md'}")
    return int(
        (args.require_all and not report["deterministic_checks_passed"])
        or (args.require_release and not report["release_ready"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
