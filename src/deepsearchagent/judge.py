"""Interchangeable, tool-free classifiers and deterministic READY eligibility."""

import json
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from .budget import stage_prompt, token_bound
from .config import environment_values, require_api_key
from .capabilities import inference_options, model_limits
from .diagnostics import safe_request_id
from .llm import BudgetExceeded, ResponseError
from .models import JudgeDecision, QualityEvaluation


class EvidenceJudge(Protocol):
    provider: str
    model: str

    def evaluate(self, question, requirements, evidence, research_state) -> JudgeDecision: ...

    def evaluate_answer(self, question, requirements, answer, evidence, research_state) -> QualityEvaluation: ...


def validate_decision(decision, requirements, findings, sources):
    decision = JudgeDecision.model_validate(decision.model_dump() if isinstance(decision, JudgeDecision) else decision, strict=True)
    expected = {r.id: r for r in requirements}
    evaluated = {r.requirement_id: r for r in decision.requirements}
    if len(evaluated) != len(decision.requirements) or set(evaluated) != set(expected):
        raise ResponseError("Judge must evaluate every requirement exactly once.", category="evidence_reference")
    known_claims = {f.id: f for f in findings}
    known_sources = {s.id for s in sources}
    for rid, entry in evaluated.items():
        if entry.critical != expected[rid].critical:
            raise ResponseError("Judge changed requirement criticality.", category="evidence_reference")
        if not set(entry.supporting_claim_ids) <= set(known_claims) or not set(entry.supporting_source_ids) <= known_sources:
            raise ResponseError("Judge references unknown claims or sources.", category="evidence_reference")
        claims = [known_claims[fid] for fid in entry.supporting_claim_ids]
        if any(f.requirement_id != rid for f in claims) or {f.source_id for f in claims} != set(entry.supporting_source_ids):
            raise ResponseError("Judge references do not match requirement evidence.", category="evidence_reference")
    if decision.decision == "RESEARCH_MORE" and (not decision.suggested_queries or not any(
        r.critical and r.status != "COVERED" for r in decision.requirements
    )):
        raise ResponseError("Research-more requires actionable critical gaps.")
    if decision.decision != "RESEARCH_MORE" and decision.suggested_queries:
        raise ResponseError("Queries supplied when no further research is recommended.")
    if decision.decision == "INSUFFICIENT_EVIDENCE" and not decision.critical_gaps:
        raise ResponseError("Insufficiency requires explicit missing information.")
    return decision


def ready_eligible(decision, requirements, findings, sources, threshold=0.9, unresolved_contradictions=()):
    if decision.decision != "READY" or decision.coverage_score < threshold or decision.contradictions or unresolved_contradictions:
        return False
    evaluations = {r.requirement_id: r for r in decision.requirements}
    known = {f.id: f for f in findings}
    source_ids = {s.id for s in sources}
    if len(evaluations) != len(requirements) or not any(r.critical for r in requirements):
        return False
    weighted = sum(r.weight * (1 if evaluations.get(r.id) and evaluations[r.id].status == "COVERED" else 0) for r in requirements)
    if weighted / sum(r.weight for r in requirements) < threshold or decision.critical_gaps:
        return False
    for requirement in requirements:
        entry = evaluations.get(requirement.id)
        if entry is None or entry.critical != requirement.critical:
            return False
        if requirement.critical:
            if entry.status != "COVERED" or entry.missing_evidence or not entry.supporting_claim_ids:
                return False
            if any(f.requirement_id == requirement.id and f.evidence_status in {"UNVERIFIED", "CONTRADICTED"} for f in findings):
                return False
            for fid in entry.supporting_claim_ids:
                finding = known.get(fid)
                if (finding is None or finding.requirement_id != requirement.id or not finding.evidence.strip()
                        or finding.evidence_status != "SUPPORTED" or finding.source_id not in source_ids
                        or finding.source_id not in entry.supporting_source_ids):
                    return False
    return True


def evidence_snapshot(state, max_bytes):
    """Round-robin requirement sampling keeps every requirement visible under a byte cap."""
    requirements = state.plan.research_requirements
    snapshot = {"findings": [], "sources": [], "contradictions": state.contradictions,
                "unresolved_questions": state.unresolved, "evidence_version": state.evidence_version,
                "omitted_findings": len(state.findings)}
    selected_sources = {}
    by_requirement = {r.id: [f for f in state.findings if f.requirement_id == r.id] for r in requirements}
    sources = {s.id: s for s in state.sources}
    for index in range(max((len(fs) for fs in by_requirement.values()), default=0)):
        for requirement in requirements:
            fs = by_requirement[requirement.id]
            if index >= len(fs):
                continue
            finding = fs[index]
            data = finding.model_dump()
            source = sources[finding.source_id]
            candidate_sources = {**selected_sources, source.id: {"id": source.id, "url": source.url,
                "title": source.title[:200], "published_at": source.published_at, "retrieved_at": source.retrieved_at}}
            candidate = {**snapshot, "findings": snapshot["findings"] + [data], "sources": list(candidate_sources.values()),
                         "omitted_findings": snapshot["omitted_findings"] - 1}
            if token_bound(json.dumps(candidate, ensure_ascii=False)) <= max_bytes:
                snapshot, selected_sources = candidate, candidate_sources
    if token_bound(json.dumps(snapshot, ensure_ascii=False)) > max_bytes:
        raise BudgetExceeded("Evidence metadata exceeds classifier input limit.", category="input_budget")
    return snapshot


class OpenAICompatibleJudge:
    def __init__(self, config, *, fallback=False, client=None):
        self.config = config
        self.provider = config.judge_fallback_provider if fallback else config.judge_provider
        self.model = config.judge_fallback_model if fallback else config.judge_model
        fallback_url = config.kimi_base_url if self.provider == "kimi" else (config.judge_base_url or "https://aiservice.cn-hangzhou.aliyuncs.com/v1") if self.provider == "pai" else config.aliyun_base_url
        self.base_url = (config.judge_fallback_base_url or fallback_url) if fallback else (config.judge_base_url or fallback_url)
        env = environment_values()
        token = env.get("JUDGE_MODEL_TOKEN", "").strip() if self.provider == "pai" else require_api_key(self.provider)
        if not token and client is None:
            raise ValueError("Set JUDGE_MODEL_TOKEN to enable PAI Judge.")
        # Alibaba's documented Python SDK value includes the full authorization string.
        self.client = client or OpenAI(api_key=f"Authorization: Bearer {token}" if self.provider == "pai" else token,
                                       base_url=self.base_url, max_retries=0)
        self.prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "evidence-judge.md").read_text()
        self.answer_prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "judge.md").read_text()
        self.usage = {"usage_observed": False}
        self.timeout = config.judge_timeout
        self.output_tokens = config.judge_output_tokens
        self.rates = ((config.judge_fallback_input_price_per_million, config.judge_fallback_output_price_per_million)
                      if fallback else (config.judge_input_price_per_million, config.judge_output_price_per_million))
        # Fallback compatibility is independently selected; schema mode is opt-in per deployment.
        self.json_mode = config.judge_json_mode if not fallback else "json_object"

    def request_payload(self, question, requirements, evidence, research_state):
        return json.dumps({"question": question, "requirements": [r.model_dump() for r in requirements],
                           "evidence": evidence, "research_state": research_state}, ensure_ascii=False)

    def evaluate(self, question, requirements, evidence, research_state):
        payload = self.request_payload(question, requirements, evidence, research_state)
        return self._request(JudgeDecision, self.prompt, payload)

    def evaluate_answer(self, question, requirements, answer, evidence, research_state):
        payload = json.dumps({"original_query": question, "requirements": [r.model_dump() for r in requirements],
                              "final_answer": answer, "evidence": evidence, "research_completion": research_state}, ensure_ascii=False)
        return self._request(QualityEvaluation, self.answer_prompt, payload)

    def _request(self, schema, prompt, payload):
        self.usage = {"usage_observed": False}
        self.request_id = None
        system = stage_prompt(prompt, schema)
        if token_bound(system) + token_bound(payload) + 64 > self.config.judge_input_tokens:
            raise BudgetExceeded("Judge input context limit reached.", category="input_budget")
        options = {}
        if self.json_mode == "json_object":
            options["response_format"] = {"type": "json_object"}
        elif self.json_mode == "json_schema":
            options["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "evidence_sufficiency", "strict": True, "schema": schema.model_json_schema()}}
        output_tokens = self.output_tokens
        if self.provider in {"aliyun", "kimi"}:
            provider_config = replace(self.config, provider=self.provider, model=self.model)
            options.update(inference_options(provider_config))
            context_limit, output_limit = model_limits(provider_config)
            output_tokens = min(output_tokens, output_limit, context_limit - token_bound(system) - token_bound(payload) - 64)
            if output_tokens <= 0:
                raise BudgetExceeded("Judge model context limit reached.", category="input_budget")
        response = self.client.chat.completions.create(model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": payload}],
            max_tokens=output_tokens, temperature=self.config.judge_temperature,
            timeout=self.timeout, **options)
        self.request_id = safe_request_id(getattr(response, "_request_id", None))
        if response.usage:
            self.usage = {"usage_observed": True, "tokens": response.usage.total_tokens,
                          "input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}
        if not response.choices:
            raise ResponseError("Judge returned no choices.", category="empty_response")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content or getattr(choice.message, "refusal", None):
            raise ResponseError("Judge did not return complete JSON.", category="output_limit" if choice.finish_reason == "length" else "invalid_response")
        return schema.model_validate_json(choice.message.content, strict=True)

    def close(self):
        self.client.close()


class ResearchModelJudge:
    """Use the configured research adapter and shared recovery/budget policy."""
    budget_managed = True

    def __init__(self, config, backend, budget):
        from .budget import StageBackend
        self.provider, self.model = config.provider, config.model
        self.prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "evidence-judge.md").read_text()
        self.answer_prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "judge.md").read_text()
        self.backend = StageBackend(backend, budget, config.judge_output_tokens, config.judge_input_tokens, timeout=config.judge_timeout)

    def evaluate(self, question, requirements, evidence, research_state):
        payload = json.dumps({"question": question, "requirements": [r.model_dump() for r in requirements],
                              "evidence": evidence, "research_state": research_state}, ensure_ascii=False)
        return self.backend.structured(JudgeDecision, self.prompt, payload)

    def evaluate_answer(self, question, requirements, answer, evidence, research_state):
        payload = json.dumps({"original_query": question, "requirements": [r.model_dump() for r in requirements],
                              "final_answer": answer, "evidence": evidence, "research_completion": research_state}, ensure_ascii=False)
        self.backend.writing = True
        return self.backend.structured(QualityEvaluation, self.answer_prompt, payload)

    def close(self):
        pass


def validate_quality(result, requirements, evidence, config, *, answer_claim_ids=(), hard_issues=()):
    """Reject invalid references; apply relevance/grounding gates independently of prose scores."""
    result = QualityEvaluation.model_validate(result.model_dump() if isinstance(result, QualityEvaluation) else result, strict=True)
    expected = {r.id: r for r in requirements}
    assessed = {r.requirement_id: r for r in result.requirements_coverage}
    if len(assessed) != len(result.requirements_coverage) or set(assessed) != set(expected):
        raise ResponseError("Quality judge must assess every requirement exactly once.", category="evidence_reference")
    claims = {f["id"]: f for f in evidence["findings"]}
    sources = {s["id"] for s in evidence["sources"]}
    for rid, entry in assessed.items():
        if not set(entry.supporting_claim_ids) <= set(claims) or not set(entry.supporting_source_ids) <= sources:
            raise ResponseError("Quality judge invented evidence references.", category="evidence_reference")
        selected = [claims[fid] for fid in entry.supporting_claim_ids]
        if any(f["requirement_id"] != rid for f in selected) or {f["source_id"] for f in selected} != set(entry.supporting_source_ids):
            raise ResponseError("Quality evidence references mismatch requirements.", category="evidence_reference")
    if not set(result.missing_requirements) <= set(expected):
        raise ResponseError("Quality judge invented requirements.", category="evidence_reference")
    for query in result.suggested_queries:
        if not set(query.requirement_ids) <= set(result.missing_requirements):
            raise ResponseError("Follow-up query must target missing requirements.", category="evidence_reference")
    score = result.scores
    result.overall_score = round(score.relevance * .3 + score.requirement_coverage * .25 + score.factual_grounding * .2
                                 + score.citation_quality * .1 + score.analytical_quality * .1 + score.uncertainty_handling * .05, 3)
    failures = list(hard_issues)
    for rid, entry in assessed.items():
        if expected[rid].critical and (entry.status != "COVERED" or not entry.supporting_claim_ids
                                      or not set(entry.supporting_claim_ids) <= set(answer_claim_ids)):
            failures.append(f"Critical requirement {rid} lacks covered, cited answer evidence.")
        if entry.status == "COVERED" and any(f["evidence_status"] != "SUPPORTED" or not f["evidence"] for f in (claims[fid] for fid in entry.supporting_claim_ids)):
            failures.append(f"Requirement {rid} relies on incomplete or unverified evidence.")
    if result.decision == "PASS":
        if (min(score.relevance, score.requirement_coverage) < config.quality_min_primary_score
                or min(score.factual_grounding, score.citation_quality) < config.quality_min_grounding_score
                or result.overall_score < config.quality_min_overall_score):
            failures.append("Mandatory relevance, coverage or grounding score threshold failed.")
        failures.extend(result.critical_issues + result.unsupported_claims + result.citation_issues + result.missing_requirements)
        if failures:
            result = result.model_copy(update={"decision": "REVISE", "critical_issues": list(dict.fromkeys(failures)),
                "actionable_feedback": list(dict.fromkeys([*result.actionable_feedback, *failures])),
                "recommended_next_action": "Correct the identified deficiencies using verified evidence; communicate remaining evidence gaps."})
    return result
