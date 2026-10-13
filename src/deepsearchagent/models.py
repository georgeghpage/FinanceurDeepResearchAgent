"""Typed contracts shared by the research stages."""

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from uuid import uuid4


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(Contract):
    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=1000)
    critical: bool
    weight: float = Field(gt=0, le=100)


class PlannedQuery(Contract):
    id: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=500)
    requirement_ids: list[str] = Field(min_length=1)


class ResearchGroup(Contract):
    id: str
    theme: str
    query_ids: list[str] = Field(min_length=1, max_length=5)


class PlanningBrief(Contract):
    objective: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(min_length=1, max_length=5)
    research_requirements: list[Requirement] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def concise_plan(self):
        if any(not s.strip() or len(s) > 300 for s in self.steps):
            raise ValueError("Plan steps must be short, concrete sentences")
        if len({r.id for r in self.research_requirements}) != len(self.research_requirements):
            raise ValueError("Requirement identifiers must be unique")
        return self


class QuerySpec(Contract):
    query: str = Field(min_length=1, max_length=500)
    requirement_ids: list[str] = Field(min_length=1)


class QueryBatch(Contract):
    queries: list[QuerySpec] = Field(min_length=1, max_length=15)


class EvidencePlan(Contract):
    objective: str = ""
    steps: list[str] = Field(default_factory=list)
    research_requirements: list[Requirement] = Field(min_length=1, max_length=20)
    research_groups: list[ResearchGroup] = Field(min_length=1, max_length=3)
    search_queries: list[PlannedQuery] = Field(min_length=1, max_length=15)
    requirement_query_mapping: dict[str, list[str]]

    @model_validator(mode="after")
    def validate_mapping(self):
        requirements = {r.id for r in self.research_requirements}
        queries = {q.id for q in self.search_queries}
        if len(requirements) != len(self.research_requirements) or len(queries) != len(self.search_queries):
            raise ValueError("Plan identifiers must be unique")
        if len({g.id for g in self.research_groups}) != len(self.research_groups):
            raise ValueError("Group identifiers must be unique")
        grouped = [qid for g in self.research_groups for qid in g.query_ids]
        if len(grouped) != len(queries) or set(grouped) != queries:
            raise ValueError("Each query must belong to exactly one group")
        if len({" ".join(q.query.casefold().split()) for q in self.search_queries}) != len(queries):
            raise ValueError("Search queries must be distinct")
        if any(not q.query.strip() or not set(q.requirement_ids) <= requirements for q in self.search_queries):
            raise ValueError("Queries must reference known requirements")
        expected = {rid: [q.id for q in self.search_queries if rid in q.requirement_ids] for rid in requirements}
        if set(self.requirement_query_mapping) != requirements or any(
            not expected[rid] or set(self.requirement_query_mapping[rid]) != set(expected[rid])
            for rid in requirements
        ):
            raise ValueError("Requirement mapping must exactly match queries and cover every requirement")
        return self


class EvidenceFinding(Contract):
    claim_id: str
    requirement_id: str
    claim: str = Field(min_length=1, max_length=1500)
    source_id: str
    source_url: str
    supporting_excerpt: str = Field(max_length=2000)
    publication_date: str | None
    evidence_status: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED", "UNVERIFIED"]
    limitations: list[str]


class EvidenceExtraction(Contract):
    findings: list[EvidenceFinding] = Field(max_length=40)
    contradictions: list[str]
    uncertainties: list[str]


class RequirementEvaluation(Contract):
    requirement_id: str
    status: Literal["COVERED", "PARTIAL", "MISSING", "CONFLICTED"]
    supporting_claim_ids: list[str]
    supporting_source_ids: list[str]
    missing_evidence: list[str]
    critical: bool


class JudgeDecision(Contract):
    decision: Literal["READY", "RESEARCH_MORE", "INSUFFICIENT_EVIDENCE"]
    coverage_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reliability_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    requirements: list[RequirementEvaluation]
    critical_gaps: list[str]
    contradictions: list[str]
    suggested_queries: list[str] = Field(max_length=15)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("suggested_queries")
    @classmethod
    def valid_queries(cls, values):
        if any(not v.strip() or len(v) > 500 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Invalid suggested query")
        return values


class QualityScores(Contract):
    relevance: float = Field(ge=0, le=5, allow_inf_nan=False)
    requirement_coverage: float = Field(ge=0, le=5, allow_inf_nan=False)
    factual_grounding: float = Field(ge=0, le=5, allow_inf_nan=False)
    citation_quality: float = Field(ge=0, le=5, allow_inf_nan=False)
    analytical_quality: float = Field(ge=0, le=5, allow_inf_nan=False)
    uncertainty_handling: float = Field(ge=0, le=5, allow_inf_nan=False)


class AnswerRequirement(Contract):
    requirement_id: str
    status: Literal["COVERED", "PARTIAL", "MISSING", "CONFLICTED"]
    assessment: str = Field(min_length=1, max_length=2000)
    supporting_claim_ids: list[str]
    supporting_source_ids: list[str]


class QualityEvaluation(Contract):
    decision: Literal["PASS", "REVISE", "MORE_RESEARCH", "INSUFFICIENT_EVIDENCE"]
    scores: QualityScores
    overall_score: float = Field(ge=0, le=5, allow_inf_nan=False)
    requirements_coverage: list[AnswerRequirement]
    missing_requirements: list[str]
    unsupported_claims: list[str]
    citation_issues: list[str]
    irrelevant_content: list[str]
    critical_issues: list[str]
    actionable_feedback: list[str]
    recommended_next_action: str = Field(min_length=1, max_length=3000)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    suggested_queries: list[QuerySpec] = Field(max_length=5)

    @model_validator(mode="after")
    def actionable_decision(self):
        if self.decision == "MORE_RESEARCH" and (not self.missing_requirements or not self.suggested_queries):
            raise ValueError("MORE_RESEARCH needs missing requirements and targeted queries")
        if self.decision != "MORE_RESEARCH" and self.suggested_queries:
            raise ValueError("Queries require MORE_RESEARCH")
        if self.decision != "PASS" and not self.actionable_feedback:
            raise ValueError("A rejected answer requires actionable feedback")
        return self


class ResearchTask(BaseModel):
    question: str
    query: str
    source: Literal["web", "documentation"] = "web"


class ResearchPlan(BaseModel):
    objective: str
    tasks: list[ResearchTask]


class Source(BaseModel):
    id: str
    url: str
    title: str
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    query_ids: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError("Sources require an HTTP(S) URL without credentials or whitespace.")
        return value


class SearchResult(BaseModel):
    failure: "FailureDiagnostic | None" = None
    text: str
    sources: list[Source]
    documents: list[dict] = Field(default_factory=list)


class ExtractedFinding(BaseModel):
    claim: str
    evidence: str
    source_url: str
    published_at: str | None


class Extraction(BaseModel):
    findings: list[ExtractedFinding]


class Finding(BaseModel):
    id: str
    task_question: str
    claim: str
    evidence: str
    source_id: str
    requirement_id: str = ""
    source_url: str = ""
    publication_date: str | None = None
    evidence_status: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED", "UNVERIFIED"] = "SUPPORTED"
    limitations: list[str] = Field(default_factory=list)
    query_ids: list[str] = Field(default_factory=list)


class ResearchNote(BaseModel):
    task: ResearchTask
    result: SearchResult


class Verification(BaseModel):
    sufficient: bool
    supported_finding_ids: list[str]
    conflicts: list[str]
    gaps: list[str]
    follow_up_tasks: list[ResearchTask]


class ReportParagraph(BaseModel):
    text: str
    finding_ids: list[str]


class ReportSection(BaseModel):
    title: str
    paragraphs: list[ReportParagraph]


class ReportDraft(BaseModel):
    title: str
    sections: list[ReportSection]


class FailureDiagnostic(BaseModel):
    provider: str
    stage: str
    operation: str
    category: str
    exception_type: str
    message: str
    http_status: int | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    request_id: str | None = None
    finish_reason: str | None = None
    call_id: int | str | None = None
    correlation_id: str | None = None
    upstream_span_id: str | None = None
    origin: Literal["call", "downstream"] = "call"
    downstream_effects: list[str] = Field(default_factory=list)
    provider_code: str | None = None
    root_cause: str = "Unknown"
    cause_confidence: Literal["confirmed", "suspected", "unknown"] = "unknown"


class ResearchState(BaseModel):
    question: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    facts_path: str | None = None
    status: str = "INITIALIZED"
    plan: EvidencePlan | ResearchPlan | None = None
    judge: JudgeDecision | None = None
    quality_evaluation: QualityEvaluation | None = None
    quality_history: list[dict] = Field(default_factory=list)
    quality_status: str = "pending"
    research_limitations: list[str] = Field(default_factory=list)
    evidence_version: int = 0
    contradictions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    executed_queries: list[str] = Field(default_factory=list)
    round_history: list[dict] = Field(default_factory=list)
    judge_history: list[dict] = Field(default_factory=list)
    total_cost_usd: float = 0
    cost_complete: bool = True
    token_usage_complete: bool = True
    report_status: str = "pending"
    citation_validation_status: str = "pending"
    notes: list[ResearchNote] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    verification: Verification | None = None
    rounds: int = 0
    stop_reason: Literal["coverage", "round_limit", "budget", "no_tasks", "error", "cancelled"] = "no_tasks"
    warnings: list[str] = Field(default_factory=list)
    failure: FailureDiagnostic | None = None
    api_calls: int = 0
    search_calls: int = 0
    tokens_used: int = 0
    elapsed_seconds: float = 0


class ParagraphAudit(Contract):
    paragraph_id: str
    supported: bool
    reason: str


class ReportAudit(Contract):
    paragraphs: list[ParagraphAudit]
