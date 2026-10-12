"""Versioned deterministic contract scorecards, separate from human/model quality."""

import json
from pathlib import Path
from deepsearchagent.config import Config
from deepsearchagent.judge import evidence_snapshot, ready_eligible, validate_decision
from deepsearchagent.models import Finding, ResearchState, Source
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge, plan

FIXTURES = Path(__file__).parent / "fixtures"


def gate_case(case):
    state = ResearchState(
        question="What was the 2026 measurement?",
        plan=plan(),
        sources=[
            Source(id=f"S{i}", url=f"https://example.com/{i}", title="Synthetic")
            for i in range(1, 4)
        ],
        findings=[
            Finding(
                id=f"F{i}",
                requirement_id=f"R{i}",
                task_question=f"R{i}",
                claim="42 units in 2026",
                evidence="42 units in 2026",
                source_id=f"S{i}",
            )
            for i in range(1, 4)
        ],
    )
    decision = FakeJudge().evaluate(
        state.question,
        state.plan.research_requirements,
        evidence_snapshot(state, 10000),
        {},
    )
    for key, value in case["mutations"].items():
        if key == "requirement_status":
            decision.requirements[-1].status = value
        elif key == "evidence_status":
            state.findings[-1].evidence_status = value
        elif key == "excerpt":
            state.findings[-1].evidence = value
        elif key == "source_id":
            state.findings[-1].source_id = value
        elif key == "coverage_score":
            decision.coverage_score = value
        elif key == "criticality":
            decision.requirements[-1].critical = value
        elif key == "critical_gap":
            decision.critical_gaps.append(value)
        elif key == "contradiction":
            state.contradictions.append(value)
        elif key == "supporting_claim":
            decision.requirements[-1].supporting_claim_ids = [value]
        else:
            raise ValueError("Unknown fixture mutation")
    try:
        validate_decision(
            decision, state.plan.research_requirements, state.findings, state.sources
        )
    except Exception:
        return False
    return ready_eligible(
        decision,
        state.plan.research_requirements,
        state.findings,
        state.sources,
        unresolved_contradictions=state.contradictions,
    )


def workflow_case(case, root):
    state, report = ResearchAgent(
        AutoBackend(**case.get("backend", {})),
        Config(session_dir=root),
        SearchRouter(FakeAsyncSearch(empty=case.get("empty_search", False), delay=0)),
        judge=FakeJudge(case["decisions"], errors=case.get("judge_errors", 0)),
    ).run(case["question"])
    sources = {s.id for s in state.sources}
    checks = {
        "termination": state.status in {"COMPLETE", "PARTIAL", "FAILED"},
        "stop_reason": state.stop_reason == case["expected_stop"],
        "searches": state.search_calls == case["expected_searches"],
        "findings": len(state.findings) == case["expected_findings"],
        "required_terms": all(term in report for term in case["required_terms"]),
        "forbidden_terms": not any(term in report for term in case["forbidden_terms"]),
        "reference_integrity": all(f.source_id in sources for f in state.findings),
        "budgets": state.rounds <= 3
        and state.search_calls <= 25
        and state.api_calls <= 20
        and state.tokens_used <= 50000,
    }
    return {
        "id": case["id"],
        "checks": checks,
        "passed": all(checks.values()),
        "stop_reason": state.stop_reason,
        "rounds": state.rounds,
        "elapsed_seconds": state.elapsed_seconds,
        "tokens": state.tokens_used,
        "searches": state.search_calls,
        "report_status": state.report_status,
    }


def dataset(name):
    data = json.loads((FIXTURES / name).read_text())
    if data["schema_version"] != 1 or len({c["id"] for c in data["cases"]}) != len(
        data["cases"]
    ):
        raise ValueError("Unsupported or duplicate fixture IDs")
    return data["cases"]
