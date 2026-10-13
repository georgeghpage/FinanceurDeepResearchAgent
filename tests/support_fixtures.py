"""Version 1 scripted providers shared by integration, browser and load tests."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from pydantic import ValidationError

from deepsearchagent.budget import RunBudget, StageBackend
from deepsearchagent.cancellation import Cancellation
from deepsearchagent.config import Config
from deepsearchagent.evidence_store import EvidenceStore
from deepsearchagent.judge import (
    OpenAICompatibleJudge,
    ready_eligible,
    validate_decision,
)
from deepsearchagent.llm import BudgetExceeded, ResponseError
from deepsearchagent.models import (
    QualityEvaluation,
    QualityScores,
    AnswerRequirement,
    EvidenceExtraction,
    EvidenceFinding,
    EvidencePlan,
    PlanningBrief,
    QueryBatch,
    QuerySpec,
    Finding,
    JudgeDecision,
    ParagraphAudit,
    PlannedQuery,
    ReportAudit,
    ReportDraft,
    ReportParagraph,
    ReportSection,
    Requirement,
    RequirementEvaluation,
    ResearchGroup,
    ResearchState,
    SearchResult,
    Source,
)
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.tools.search import SearchRouter


def plan():
    requirements = [
        Requirement(id=f"R{i}", question=f"Establish fact {i}", critical=True, weight=1)
        for i in range(1, 4)
    ]
    queries = [
        PlannedQuery(
            id=f"Q{i}",
            query=f"official evidence fact {(i - 1) // 5 + 1} variant {i}",
            requirement_ids=[f"R{(i - 1) // 5 + 1}"],
        )
        for i in range(1, 16)
    ]
    groups = [
        ResearchGroup(
            id=f"G{i}",
            theme=f"Fact {i}",
            query_ids=[q.id for q in queries[(i - 1) * 5 : i * 5]],
        )
        for i in range(1, 4)
    ]
    return EvidencePlan(
        research_requirements=requirements,
        research_groups=groups,
        search_queries=queries,
        requirement_query_mapping={
            r.id: [q.id for q in queries if r.id in q.requirement_ids]
            for r in requirements
        },
    )


class FakeAsyncSearch:
    provider_name = "tavily"

    def __init__(self, empty=False, duplicate=False, delay=0.004):
        self.queries = []
        self.active = self.peak = 0
        self.empty, self.duplicate, self.delay = empty, duplicate, delay

    async def asearch(self, query, *, timeout):
        self.queries.append(query)
        number = len(self.queries) if not self.duplicate else 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.empty:
                return SearchResult(text="", sources=[])
            url = f"https://example.com/source/{number}"
            content = "Official publication reports a measured value of 42 in 2026."
            if "targeted" in query:
                content += (
                    f" Supplementary measurement was {number + 100} units in 2026."
                )
            document = {
                "url": url,
                "content": content,
                "title": "Official publication",
                "published_at": "2026-10-01",
            }
            return SearchResult(
                text=json.dumps([document]),
                sources=[
                    Source(
                        id="",
                        url=url,
                        title="Official publication",
                        published_at="2026-10-01",
                    )
                ],
                documents=[document],
            )
        finally:
            self.active -= 1


class AutoBackend:
    def __init__(
        self, wrong_excerpt=False, unsupported_report=False, audit_reject=False
    ):
        self.calls = self.tokens_used = 0
        self.started = time.monotonic()
        self.client = Mock()
        self.lock = threading.Lock()
        self.local = threading.local()
        self.extractions = self.active = self.peak = 0
        self.extraction_barrier = None
        self.limits = []
        self.wrong_excerpt, self.unsupported_report, self.audit_reject = (
            wrong_excerpt,
            unsupported_report,
            audit_reject,
        )

    def for_model(self, model, provider=None):
        return self

    def close_stream(self):
        pass

    def usage_for_thread(self):
        return self.local.usage

    def structured(self, schema, instructions, payload, **options):
        data = json.loads(payload)
        with self.lock:
            self.calls += 1
            self.tokens_used += 100
            self.limits.append((schema.__name__, options))
        self.local.usage = {
            "usage_observed": True,
            "tokens": 100,
            "input_tokens": 80,
            "output_tokens": 20,
        }
        if schema is PlanningBrief:
            return PlanningBrief(
                objective="Establish facts",
                steps=["Find primary evidence for each fact."],
                research_requirements=plan().research_requirements,
            )
        if schema is QueryBatch:
            return QueryBatch(
                queries=[
                    QuerySpec(query=q.query, requirement_ids=q.requirement_ids)
                    for q in plan().search_queries
                ]
            )
        if schema is EvidencePlan:
            return plan()
        if schema is EvidenceExtraction:
            with self.lock:
                self.extractions += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if self.extraction_barrier is not None:
                    self.extraction_barrier.wait(timeout=5)
                time.sleep(0.02)
                findings = []
                for requirement in data["requirements"]:
                    if not data["documents"]:
                        continue
                    document = data["documents"][0]
                    findings.append(
                        EvidenceFinding(
                            claim_id="local-1",
                            requirement_id=requirement["id"],
                            claim=document["content"],
                            source_id=document["source_id"],
                            source_url=document["url"],
                            supporting_excerpt="Invented excerpt"
                            if self.wrong_excerpt
                            else document["content"],
                            publication_date=None,
                            evidence_status="SUPPORTED",
                            limitations=[],
                        )
                    )
                return EvidenceExtraction(
                    findings=findings, contradictions=[], uncertainties=[]
                )
            finally:
                with self.lock:
                    self.active -= 1
        if schema is QualityEvaluation:
            return FakeJudge().evaluate_answer(
                data["original_query"],
                plan().research_requirements,
                data["final_answer"],
                data["evidence"],
                data["research_completion"],
            )
        if schema is ReportDraft:
            findings = data["evidence"]["findings"]
            return ReportDraft(
                title="Evidence report",
                sections=[
                    ReportSection(
                        title="Established facts",
                        paragraphs=[
                            ReportParagraph(
                                text=f["claim"],
                                finding_ids=[
                                    "F999" if self.unsupported_report else f["id"]
                                ],
                            )
                            for f in findings
                        ],
                    )
                ],
            )
        if schema is ReportAudit:
            return ReportAudit(
                paragraphs=[
                    ParagraphAudit(
                        paragraph_id=p["paragraph_id"],
                        supported=not self.audit_reject,
                        reason="checked",
                    )
                    for p in data["paragraphs"]
                ]
            )
        raise AssertionError(schema)


class FakeJudge:
    provider, model = "pai", "pai-judge"
    rates = (0, 0)
    prompt = "Evaluate evidence sufficiency."

    def __init__(
        self, decisions=("READY",), errors=0, queries=None, bad_reference=False
    ):
        self.decisions, self.errors = list(decisions), errors
        self.calls = 0
        self.queries = queries or ["targeted additional evidence"]
        self.bad_reference = bad_reference
        self.snapshots = []

    def evaluate(self, question, requirements, evidence, research_state):
        self.calls += 1
        self.usage = {
            "usage_observed": True,
            "tokens": 100,
            "input_tokens": 80,
            "output_tokens": 20,
        }
        self.snapshots.append(evidence)
        if self.calls <= self.errors:
            raise ResponseError("Malformed JSON", category="invalid_response")
        decision = self.decisions[
            min(self.calls - self.errors - 1, len(self.decisions) - 1)
        ]
        evaluations = []
        for requirement in requirements:
            findings = [
                f for f in evidence["findings"] if f["requirement_id"] == requirement.id
            ]
            covered = bool(findings) and (decision == "READY" or requirement.id != "R3")
            evaluations.append(
                RequirementEvaluation(
                    requirement_id=requirement.id,
                    status="COVERED" if covered else "MISSING",
                    supporting_claim_ids=(
                        ["F999"] if self.bad_reference else [f["id"] for f in findings]
                    )
                    if covered
                    else [],
                    supporting_source_ids=list(
                        dict.fromkeys(f["source_id"] for f in findings)
                    )
                    if covered
                    else [],
                    missing_evidence=[] if covered else ["Need fact 3"],
                    critical=requirement.critical,
                )
            )
        return JudgeDecision(
            decision=decision,
            coverage_score=1 if decision == "READY" else 0.6,
            evidence_score=0.9,
            reliability_score=0.9,
            requirements=evaluations,
            critical_gaps=[] if decision == "READY" else ["Need fact 3"],
            contradictions=[],
            suggested_queries=self.queries if decision == "RESEARCH_MORE" else [],
            reason="Requirement-level evaluation",
        )

    def evaluate_answer(self, question, requirements, answer, evidence, research_state):
        # Offline boundary fixture; dedicated quality tests cover rejected decisions.
        self.usage = {
            "usage_observed": True,
            "tokens": 100,
            "input_tokens": 80,
            "output_tokens": 20,
        }
        return QualityEvaluation(
            decision="PASS",
            scores=QualityScores(**{key: 5 for key in QualityScores.model_fields}),
            overall_score=5,
            requirements_coverage=[
                AnswerRequirement(
                    requirement_id=r.id,
                    status="COVERED",
                    assessment="Cited evidence in answer",
                    supporting_claim_ids=[
                        f["id"]
                        for f in evidence["findings"]
                        if f["requirement_id"] == r.id
                    ],
                    supporting_source_ids=list(
                        dict.fromkeys(
                            f["source_id"]
                            for f in evidence["findings"]
                            if f["requirement_id"] == r.id
                        )
                    ),
                )
                for r in requirements
            ],
            missing_requirements=[],
            unsupported_claims=[],
            citation_issues=[],
            irrelevant_content=[],
            critical_issues=[],
            actionable_feedback=[],
            recommended_next_action="Finalize",
            confidence=0.9,
            suggested_queries=[],
        )
