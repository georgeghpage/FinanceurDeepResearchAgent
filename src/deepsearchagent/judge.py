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
from .llm import BudgetExceeded, ResponseError
from .models import JudgeDecision


class EvidenceJudge(Protocol):
    provider: str
    model: str

    def evaluate(self, question, requirements, evidence, research_state) -> JudgeDecision: ...


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
            data["claim"] = finding.claim[:700]
            data["evidence"] = finding.evidence[:600]
            data["limitations"] = finding.limitations[:3]
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
        fallback_url = config.kimi_base_url if self.provider == "kimi" else config.judge_base_url if self.provider == "pai" else config.aliyun_base_url
        self.base_url = (config.judge_fallback_base_url or fallback_url) if fallback else config.judge_base_url
        env = environment_values()
        token = env.get("JUDGE_MODEL_TOKEN", "").strip() if self.provider == "pai" else require_api_key(self.provider)
        if not token and client is None:
            raise ValueError("Set JUDGE_MODEL_TOKEN to enable PAI Judge.")
        # Alibaba's documented Python SDK value includes the full authorization string.
        self.client = client or OpenAI(api_key=f"Authorization: Bearer {token}" if self.provider == "pai" else token,
                                       base_url=self.base_url, max_retries=0)
        self.prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "judge.md").read_text()
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
        self.usage = {"usage_observed": False}
        system = stage_prompt(self.prompt, JudgeDecision)
        payload = self.request_payload(question, requirements, evidence, research_state)
        if token_bound(system) + token_bound(payload) + 64 > self.config.judge_input_tokens:
            raise BudgetExceeded("Judge input context limit reached.", category="input_budget")
        options = {}
        if self.json_mode == "json_object":
            options["response_format"] = {"type": "json_object"}
        elif self.json_mode == "json_schema":
            options["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "evidence_sufficiency", "strict": True, "schema": JudgeDecision.model_json_schema()}}
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
        if response.usage:
            self.usage = {"usage_observed": True, "tokens": response.usage.total_tokens,
                          "input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}
        if not response.choices:
            raise ResponseError("Judge returned no choices.", category="empty_response")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content or getattr(choice.message, "refusal", None):
            raise ResponseError("Judge did not return complete JSON.", category="output_limit" if choice.finish_reason == "length" else "invalid_response")
        return JudgeDecision.model_validate_json(choice.message.content, strict=True)

    def close(self):
        self.client.close()


class ResearchModelJudge:
    """Use the configured research adapter and shared recovery/budget policy."""
    budget_managed = True

    def __init__(self, config, backend, budget):
        from .budget import StageBackend
        self.provider, self.model = config.provider, config.model
        self.prompt = Path(config.judge_prompt_path).read_text() if config.judge_prompt_path else files("deepsearchagent").joinpath("prompts", "judge.md").read_text()
        self.backend = StageBackend(backend, budget, config.judge_output_tokens, config.judge_input_tokens, timeout=config.judge_timeout)

    def evaluate(self, question, requirements, evidence, research_state):
        payload = json.dumps({"question": question, "requirements": [r.model_dump() for r in requirements],
                              "evidence": evidence, "research_state": research_state}, ensure_ascii=False)
        return self.backend.structured(JudgeDecision, self.prompt, payload)

    def close(self):
        pass
