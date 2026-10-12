#!/usr/bin/env python3
"""Run deterministic gate/workflow regressions, never call paid models."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from support_quality import dataset, gate_case, workflow_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/qa/evaluation.json"
    )
    args = parser.parse_args()
    gates = [
        {"id": c["id"], "eligible": gate_case(c), "expected": c["expected_eligible"]}
        for c in dataset("classifier_gates.v1.json")
    ]
    with tempfile.TemporaryDirectory() as root:
        workflows = [workflow_case(c, root) for c in dataset("workflows.v1.json")]
    negatives = [c for c in gates if not c["expected"]]
    result = {
        "fixture_version": 1,
        "label_origin": "deterministic_contracts_not_human_review",
        "gate_cases": gates,
        "workflow_cases": workflows,
        "deterministic_false_ready_rate": sum(c["eligible"] for c in negatives)
        / len(negatives),
        "workflow_completion_rate": sum(c["checks"]["termination"] for c in workflows)
        / len(workflows),
        "workflow_check_pass_rate": sum(c["passed"] for c in workflows)
        / len(workflows),
        "semantic_answer_accuracy": None,
        "live_classifier_accuracy": None,
        "human_reviewed_labels": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in ("gate_cases", "workflow_cases")
            },
            indent=2,
        )
    )
    return (
        0
        if all(c["eligible"] == c["expected"] for c in gates)
        and all(c["passed"] for c in workflows)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
