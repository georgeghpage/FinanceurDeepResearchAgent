"""Deterministic asynchronous research orchestration; models never own routing."""

import asyncio
import concurrent.futures
import json
import random
import threading
import time
from collections.abc import Callable

from . import planner, researcher, writer
from .budget import RunBudget, StageBackend, stage_prompt, token_bound
from .cancellation import Cancellation, CancellableBackend, ResearchCancelled
from .config import Config
from .diagnostics import failure_from_exception
from .evidence_store import EvidenceStore, atomic_write
from .judge import ResearchModelJudge, OpenAICompatibleJudge, evidence_snapshot, ready_eligible, validate_decision
from .llm import Backend, BudgetExceeded, IncompleteResponse, ResponseError
from .models import EvidencePlan, JudgeDecision, PlannedQuery, ResearchState, Verification
from .retrieval import TRANSIENT, retrieve
from .sequential import ResearchAgent as SequentialResearchAgent
from .tools.search import SearchRouter
from .tools.retry import cancellable_wait


async def blocking_call(operation, timeout, cancellation=None):
    """Deadline-aware daemon I/O; late results cannot enter the evidence store."""
    future = concurrent.futures.Future()

    def work():
        try:
            result = operation()
            if not future.cancelled():
                future.set_result(result)
        except BaseException as exc:
            if not future.cancelled():
                future.set_exception(exc)

    threading.Thread(target=work, daemon=True).start()
    wrapped = asyncio.wrap_future(future)
    deadline = time.monotonic() + timeout
    try:
        while not wrapped.done():
            if cancellation:
                cancellation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("Stage execution timeout reached.", category="request_timeout")
            await asyncio.wait({wrapped}, timeout=min(0.025, remaining))
        if cancellation:
            cancellation.check()
        return await wrapped
    finally:
        if not wrapped.done():
            wrapped.cancel()


class ResearchAgent:
    def __init__(self, backend: Backend, config: Config, search: SearchRouter,
                 progress: Callable[[str], None] | None = None,
                 trace: Callable[[dict], None] | None = None,
                 cancellation: Cancellation | None = None, judge=None, fallback_judge=None):
        self.config, self.search = config, search
        self.cancellation = cancellation or Cancellation()
        if hasattr(backend, "cancellation"):
            backend.cancellation = self.cancellation
        self.backend = CancellableBackend(backend, cancellation) if cancellation else backend
        self.progress, self.trace = progress or (lambda message: None), trace or (lambda event: None)
        self.budget = RunBudget(config, self.cancellation)
        self.search.configure(config, self.budget)
        self.judge, self.fallback_judge = judge, fallback_judge
        self.owned_judges = []
        self.stage, self.provider = "initialize", "agent"
        self.search.cancellation, self.search.trace = self.cancellation, self.trace
        for provider in (search.web, search.documentation):
            if hasattr(provider, "trace"):
                provider.trace = self.trace
            if hasattr(provider, "cancellation"):
                provider.cancellation = self.cancellation

    def _stage(self, stage, provider=None):
        self.stage, self.provider = stage, provider or self.config.provider
        self.trace({"kind": "stage_start", "stage": stage, "provider": self.provider})
        self.progress({"plan": "Planning 15 research queries", "search": "Retrieving evidence concurrently",
                       "extract": "Extracting structured evidence", "verify": "Classifying evidence sufficiency",
                       "write": "Writing cited report", "validate": "Validating report citations"}.get(stage, stage))

    def _backend(self, output, context, writing=False):
        return StageBackend(self.backend, self.budget, output, context, writing=writing)

    async def _model(self, operation, writing=False):
        return await blocking_call(operation, min(self.config.request_timeout, self.budget.remaining_time(writing)), self.cancellation)

    def _usage(self, state):
        state.api_calls, state.search_calls = self.budget.calls, self.budget.searches
        state.tokens_used = self.budget.tokens + self.budget.pending_tokens
        state.total_cost_usd = round(self.budget.cost + self.budget.pending_cost, 8)
        state.cost_complete = self.budget.cost_complete and self.budget.pending_tokens == 0
        state.token_usage_complete = self.budget.token_usage_complete and self.budget.pending_tokens == 0
        state.elapsed_seconds = round(time.monotonic() - self.budget.started, 3)

    async def _round(self, state, store, queries, groups):
        state.rounds += 1
        state.status = "RETRIEVING"
        self._stage("search", "tavily")
        def evidence_keys():
            return {(f.requirement_id, " ".join(f.claim.casefold().split()), f.evidence_status)
                    for f in state.findings if f.evidence and f.evidence_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"}}
        before = evidence_keys()
        def retrieval_trace(event):
            if event.get("kind") == "search_start" and event["query"] not in state.executed_queries:
                state.executed_queries.append(event["query"])
            self.trace({"round": state.rounds, "source": "web", **event})
        results = await retrieve(queries, self.search, self.budget, retrieval_trace)
        usable = {}
        terminal_error = None
        for query, result in zip(queries, results):
            if isinstance(result, ResearchCancelled):
                raise result
            if isinstance(result, BaseException):
                terminal_error = result
                continue
            if result.failure:
                state.warnings.append(f"Source coverage incomplete for {query.id}: search unavailable ({result.failure.category}); continuing with available evidence.")
                continue
            store.add_result(query, result)
            usable[query.id] = result
        self._usage(state)
        store.persist()
        if not any(result.sources or result.documents for result in usable.values()):
            state.round_history.append({"round": state.rounds, "query_ids": [q.id for q in queries],
                                        "queries": [q.query for q in queries], "new_findings": 0,
                                        "source_count": len(state.sources), "evidence_version": state.evidence_version})
            store.persist()
            if terminal_error is not None:
                raise terminal_error
            return 0
        state.status = "EXTRACTING"
        self._stage("extract")

        async def extract(group):
            assigned = [q for q in queries if q.id in group]
            result_list = [usable[q.id] for q in assigned if q.id in usable]
            ids = {rid for q in assigned for rid in q.requirement_ids}
            requirements = [r for r in state.plan.research_requirements if r.id in ids]
            payload = researcher.group_payload(state.question, requirements, assigned, result_list,
                                               self.config.researcher_input_tokens, state.findings)
            if not assigned or not result_list:
                return None
            backend = self._backend(self.config.researcher_output_tokens, self.config.researcher_input_tokens)
            self.trace({"kind": "researcher_start", "stage": "extract", "query_ids": group,
                        "input_bytes": token_bound(json.dumps(payload, ensure_ascii=False)),
                        "max_output_tokens": self.config.researcher_output_tokens})
            try:
                output = await self._model(lambda: researcher.extract_group(backend, payload))
            except IncompleteResponse as exc:
                partial = researcher.partial_extraction(exc.partial_content)
                if partial is not None:
                    exc.extraction_checkpoint = (partial, ids, result_list, [q.id for q in assigned])
                raise
            return output, ids, result_list, [q.id for q in assigned]

        # All workers return immutable evidence; only this coroutine writes the store.
        async def guarded_extract(group):
            try:
                return await extract(group)
            except Exception as exc:
                return exc
        workers = [asyncio.create_task(guarded_extract(group)) for group in groups]
        try:
            for completed in asyncio.as_completed(workers):
                output = await completed
                if isinstance(output, ResearchCancelled):
                    raise output
                if isinstance(output, BaseException):
                    diagnostic = failure_from_exception(output, provider=self.config.provider, stage="extract", operation="extract")
                    state.warnings.append(f"Researcher unavailable: {diagnostic.category}.")
                    self.trace({"kind": "researcher_error", "diagnostic": diagnostic.model_dump()})
                    if isinstance(output, BudgetExceeded):
                        terminal_error = output
                    checkpoint = getattr(output, "extraction_checkpoint", None)
                    if checkpoint is not None:
                        store.merge(*checkpoint)
                        self._usage(state)
                        store.persist()
                    continue
                if output is not None:
                    previous_count = len(state.findings)
                    store.merge(*output)
                    self._usage(state)
                    store.persist()
                    self.trace({"kind": "findings", "stage": "extract", "findings": [f.model_dump() for f in state.findings[previous_count:]]})
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        gain = len(evidence_keys() - before)
        state.round_history.append({"round": state.rounds, "query_ids": [q.id for q in queries],
                                    "queries": [q.query for q in queries], "new_findings": gain,
                                    "source_count": len(state.sources), "evidence_version": state.evidence_version})
        self._usage(state)
        store.persist()
        if terminal_error is not None:
            raise terminal_error
        return gain

    def _get_judge(self, fallback=False):
        attr = "fallback_judge" if fallback else "judge"
        judge = getattr(self, attr)
        if judge is None:
            judge = (ResearchModelJudge(self.config, self.backend, self.budget)
                     if not fallback and self.config.judge_provider == "research"
                     else OpenAICompatibleJudge(self.config, fallback=fallback))
            setattr(self, attr, judge)
            self.owned_judges.append(judge)
        return judge

    async def _evaluate(self, state, store):
        self._stage("verify", self.config.provider if self.config.judge_provider == "research" else self.config.judge_provider)
        state.status = "CLASSIFYING"
        requirements = state.plan.research_requirements
        for fallback in (False, True):
            if fallback and not (self.fallback_judge or self.config.judge_fallback_provider):
                break
            try:
                judge = self._get_judge(fallback)
            except Exception as exc:
                diagnostic = failure_from_exception(exc, provider=self.config.judge_provider, stage="verify", operation="initialize_judge")
                self.trace({"kind": "classifier_error", "diagnostic": diagnostic.model_dump()})
                continue
            if fallback:
                self.trace({"kind": "classifier_fallback", "provider": judge.provider, "model": judge.model})
            attempts = self.config.judge_retries + 1 if not fallback and not getattr(judge, "budget_managed", False) else 1
            for attempt in range(attempts):
                reservation = None
                metadata = None
                succeeded = False
                diagnostic = None
                attempt_started = time.monotonic()
                try:
                    research_context = {"round": state.rounds, "previously_executed_queries": state.executed_queries,
                                        "remaining_budget": self.budget.snapshot()}
                    prompt = getattr(judge, "prompt", "Evaluate evidence sufficiency.")
                    base = {"question": state.question, "requirements": [r.model_dump() for r in requirements],
                            "evidence": {}, "research_state": research_context}
                    overhead = token_bound(stage_prompt(prompt, JudgeDecision)) + token_bound(json.dumps(base, ensure_ascii=False)) + 128
                    snapshot = evidence_snapshot(state, max(0, self.config.judge_input_tokens - overhead))
                    payload = {**base, "evidence": snapshot}
                    input_tokens = token_bound(stage_prompt(prompt, JudgeDecision)) + token_bound(json.dumps(payload, ensure_ascii=False)) + 64
                    rates = getattr(judge, "rates", (self.config.judge_input_price_per_million, self.config.judge_output_price_per_million))
                    if not getattr(judge, "budget_managed", False):
                        reservation = self.budget.reserve_model(input_tokens, self.config.judge_output_tokens, rates)
                    judge.timeout = min(self.config.judge_timeout, self.budget.remaining_time())
                    judge.output_tokens = self.config.judge_output_tokens
                    judge.usage = {"usage_observed": False}
                    metadata = {"call_id": f"judge-{self.budget.calls}", "schema": "JudgeDecision", "stage": "verify",
                                "provider": judge.provider, "model": judge.model, "timeout_seconds": judge.timeout,
                                "max_output_tokens": judge.output_tokens,
                                "pricing": {"model": judge.model, "input_price_per_million": rates[0], "output_price_per_million": rates[1]}}
                    if not getattr(judge, "budget_managed", False):
                        self.trace({"kind": "llm_start", **metadata})
                    self.trace({"kind": "classifier_start", "stage": "verify", "provider": judge.provider,
                                "model": judge.model, "attempt": attempt, "evidence_version": state.evidence_version})
                    result = await blocking_call(lambda: judge.evaluate(state.question, requirements, snapshot, research_context),
                                                 judge.timeout, self.cancellation)
                    # Validate against the exact bounded snapshot, not hidden claims the judge never received.
                    visible = {f["id"] for f in snapshot["findings"]}
                    visible_sources = {s["id"] for s in snapshot["sources"]}
                    result = validate_decision(result, requirements,
                        [f for f in state.findings if f.id in visible], [s for s in state.sources if s.id in visible_sources])
                    advisory = result.model_dump()
                    if result.decision == "READY" and not ready_eligible(result, requirements, state.findings, state.sources,
                                                                         self.config.ready_coverage_threshold, state.contradictions):
                        # An unsafe advisory READY never triggers additional model-controlled loops.
                        state.warnings.append("Classifier READY was rejected by deterministic evidence gates.")
                        self.trace({"kind": "classifier_ready_rejected", "stage": "verify"})
                        result = result.model_copy(update={"decision": "INSUFFICIENT_EVIDENCE",
                            "critical_gaps": ["Deterministic evidence checks rejected completeness."],
                            "suggested_queries": [], "reason": "Proposed READY lacked eligible requirement-level evidence."})
                    state.judge = result
                    record = store.save_evaluation(payload, result, judge.provider, judge.model, advisory)
                    self.trace({"kind": "verification", "stage": "verify", **record,
                                "sufficient": result.decision == "READY", "gaps": result.critical_gaps,
                                "coverage_score": result.coverage_score, "evidence_score": result.evidence_score,
                                "reliability_score": result.reliability_score})
                    succeeded = True
                    return result
                except ResearchCancelled:
                    raise
                except Exception as exc:
                    diagnostic = failure_from_exception(exc, provider=judge.provider, stage="verify", operation="classify")
                    self.trace({"kind": "classifier_error", "stage": "verify", "provider": judge.provider,
                                "model": judge.model, "attempt": attempt, "diagnostic": diagnostic.model_dump()})
                    if isinstance(exc, BudgetExceeded):
                        raise
                    recoverable = diagnostic.category in TRANSIENT | {"validation_error", "empty_response", "output_limit", "invalid_response", "evidence_reference"}
                    if not recoverable:
                        break
                finally:
                    if reservation is not None:
                        usage = getattr(judge, "usage", {"usage_observed": False})
                        self.budget.settle(reservation, usage)
                        if metadata:
                            self.trace({"kind": "llm_complete" if succeeded else "llm_error", **metadata, **usage,
                                        "duration_seconds": round(time.monotonic() - attempt_started, 3),
                                        "finish_reason": "stop" if succeeded else None,
                                        "diagnostic": diagnostic.model_dump() if diagnostic else None})
                if diagnostic is not None and attempt + 1 < attempts:
                    base_delay = min(self.config.llm_max_delay, self.config.llm_backoff * 2 ** attempt)
                    delay = min(self.config.llm_max_delay, base_delay + random.uniform(0, base_delay * self.config.llm_jitter))
                    delay = max(delay, diagnostic.retry_after_seconds or 0)
                    if delay >= self.budget.remaining_time():
                        raise BudgetExceeded("Classifier retry deadline reached.", category="time_budget")
                    self.trace({"kind": "llm_retry", "stage": "verify", "provider": judge.provider,
                                "category": diagnostic.category, "retry_delay_seconds": delay, "next_attempt": attempt + 2})
                    await cancellable_wait(asyncio.sleep(delay), self.cancellation)
        state.warnings.append("Evidence classification was unavailable; this is a qualified best-effort answer.")
        return None

    def _verification(self, state):
        # Preserve renderer/UI compatibility without treating provider failure as evidence insufficiency.
        supported = [f.id for f in state.findings if f.evidence_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"} and f.evidence]
        state.verification = Verification(sufficient=bool(state.judge and state.judge.decision == "READY"),
            supported_finding_ids=supported, conflicts=state.judge.contradictions if state.judge else state.contradictions,
            gaps=state.judge.critical_gaps if state.judge else ["Classification unavailable."], follow_up_tasks=[])

    async def _write(self, state):
        self._stage("write")
        state.status = "WRITING"
        self._verification(state)
        draft = None
        try:
            if state.verification.supported_finding_ids:
                backend = self._backend(self.config.writer_output_tokens, self.config.writer_input_tokens, writing=True)
                draft = await self._model(lambda: writer.write_evidence(backend, state, self.config.writer_input_tokens), writing=True)
                self._stage("validate")
                audit_backend = self._backend(self.config.audit_output_tokens, self.config.writer_input_tokens, writing=True)
                await self._model(lambda: writer.audit_report(audit_backend, state, draft), writing=True)
                state.citation_validation_status = "semantic_and_references_passed"
                state.report_status = "generated"
            else:
                state.warnings.append("No eligible source-backed evidence was collected.")
        except ResearchCancelled:
            raise
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider=self.config.provider, stage=self.stage, operation="write_or_validate")
            self.trace({"kind": "report_error", "diagnostic": diagnostic.model_dump()})
            state.warnings.append(f"Final generation unavailable ({diagnostic.category}); report assembled from stored evidence.")
            draft = None
        if draft is None:
            draft = writer.evidence_draft(state)
            if any(s.paragraphs for s in draft.sections):
                writer.validate_draft(state, draft)
            state.citation_validation_status = "extractive_references_passed"
            state.report_status = "best_effort_assembled"
        return draft

    async def arun(self, question):
        if not question.strip():
            raise ValueError("Research question must not be empty.")
        state = ResearchState(question=question.strip())
        self._latest = state
        store = EvidenceStore(state, self.config.session_dir)
        def checkpoint(schema, content, complete):
            atomic_write(store.directory / f"generation-{threading.get_ident()}-{schema}.json",
                         json.dumps({"schema": schema, "content": content, "complete": complete,
                                     "session_id": state.session_id, "evidence_version": state.evidence_version}, ensure_ascii=False))
        self.budget.checkpoint = checkpoint
        draft = None
        self.trace({"kind": "session_initialized", "session_id": state.session_id, "query": state.question, "facts_path": state.facts_path})
        try:
            self._stage("plan")
            state.status = "PLANNING"
            state.plan = await self._model(lambda: planner.plan_evidence(self._backend(self.config.planner_output_tokens, self.config.planner_input_tokens), state.question))
            state.plan = EvidencePlan.model_validate(state.plan.model_dump())
            self.trace({"kind": "plan", **state.plan.model_dump(), "query_count": 15})
            store.persist()
            queries = state.plan.search_queries
            groups = [g.query_ids for g in state.plan.research_groups]
            for round_index in range(self.config.max_rounds + 1):
                gain = await self._round(state, store, queries, groups)
                decision = await self._evaluate(state, store)
                self._usage(state)
                store.persist()
                if decision is None:
                    state.stop_reason = "no_tasks"
                    break
                if decision.decision == "READY":
                    state.stop_reason = "coverage"
                    break
                if decision.decision == "INSUFFICIENT_EVIDENCE":
                    state.stop_reason = "no_tasks"
                    break
                if round_index >= self.config.max_rounds:
                    state.stop_reason = "round_limit"
                    break
                if round_index > 0 and gain < self.config.min_information_gain:
                    state.stop_reason = "no_tasks"
                    state.warnings.append("Targeted research stopped because marginal evidence gain was negligible.")
                    break
                missing = [r.requirement_id for r in decision.requirements if r.critical and r.status != "COVERED"]
                seen = {" ".join(q.casefold().split()) for q in state.executed_queries}
                queries = []
                for text in decision.suggested_queries:
                    normalized = " ".join(text.casefold().split())
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    # Every follow-up is explicitly attached to unresolved critical requirements.
                    queries.append(PlannedQuery(id=f"Q{15 + round_index * 5 + len(queries) + 1}", query=text.strip(), requirement_ids=missing))
                    if len(queries) >= min(5, self.config.max_tasks):
                        break
                if not queries:
                    state.stop_reason = "no_tasks"
                    state.warnings.append("No useful new targeted queries remained.")
                    break
                groups = [[q.id for q in queries]]
        except (ResearchCancelled, asyncio.CancelledError):
            self.cancellation.cancel()
            state.stop_reason = "cancelled"
            state.warnings.append("Research stopped by the user.")
        except Exception as exc:
            state.stop_reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
            diagnostic = failure_from_exception(exc, provider=self.provider, stage=self.stage, operation=self.stage)
            state.warnings.append(f"Research terminated at {diagnostic.stage}: {diagnostic.category}.")
            self.trace({"kind": "research_error", "diagnostic": diagnostic.model_dump()})
            if not isinstance(exc, BudgetExceeded):
                state.failure = diagnostic
        try:
            if state.stop_reason != "cancelled":
                draft = await self._write(state)
        except (ResearchCancelled, asyncio.CancelledError):
            self.cancellation.cancel()
            state.stop_reason = "cancelled"
            draft = None
        finally:
            self._usage(state)
            complete = (state.stop_reason == "coverage" and state.report_status == "generated"
                        and not state.failure and not state.warnings)
            state.status = ("CANCELLED" if state.stop_reason == "cancelled" else "COMPLETE" if complete
                            else "PARTIAL" if state.findings else "FAILED")
            store.persist()
            for judge in self.owned_judges:
                try:
                    judge.close()
                except Exception as exc:
                    self.trace({"kind": "cleanup_error", "diagnostic": failure_from_exception(
                        exc, provider=judge.provider, stage="cleanup", operation="judge_close").model_dump()})
        report = writer.render(state, draft)
        self._stage("finalize", "agent")
        atomic_write(store.directory / "report.md", report)
        self.trace({"kind": "run_complete" if state.status == "COMPLETE" else "run_stopped",
                    "session_id": state.session_id, "stage": self.stage, "provider": "agent",
                    "stop_reason": state.stop_reason, "status": state.status, "failure": state.failure.model_dump() if state.failure else None,
                    "api_calls": state.api_calls, "search_calls": state.search_calls, "tokens_used": state.tokens_used,
                    "total_cost_usd": state.total_cost_usd, "cost_complete": state.cost_complete,
                    "source_count": len(state.sources), "finding_count": len(state.findings),
                    "refinement_rounds": max(0, state.rounds - 1), "elapsed_seconds": state.elapsed_seconds,
                    "report_status": state.report_status, "citation_validation_status": state.citation_validation_status})
        return state, report

    def run(self, question):
        return asyncio.run(self.arun(question))
