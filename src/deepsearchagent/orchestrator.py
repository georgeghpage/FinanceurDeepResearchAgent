"""Deterministic asynchronous research orchestration; models never own routing."""

import asyncio
from contextvars import copy_context
import concurrent.futures
import json
import random
import threading
import time
from pathlib import Path
from uuid import uuid4
from collections.abc import Callable

from . import planner, researcher, writer
from .budget import RunBudget, StageBackend, stage_prompt, token_bound
from .cancellation import Cancellation, CancellableBackend, ResearchCancelled
from .config import Config
from .capabilities import model_limits
from .diagnostics import CALL_CONTEXT, failure_from_exception
from .evidence_store import EvidenceStore, atomic_write
from .judge import ResearchModelJudge, OpenAICompatibleJudge, evidence_snapshot, ready_eligible, validate_decision, validate_quality
from .llm import Backend, BudgetExceeded, IncompleteResponse, ResponseError
from .models import QualityEvaluation, JudgeDecision, PlannedQuery, ResearchState, Verification
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

    context = copy_context()
    threading.Thread(target=lambda: context.run(work), daemon=True).start()
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
        elif not wrapped.cancelled():
            # Cancellation may win after a worker has already failed.
            wrapped.exception()


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
        self.writer_backend = None
        self.stage, self.provider = "initialize", "agent"
        self.search.cancellation, self.search.trace = self.cancellation, self.trace
        for provider in (search.web, search.documentation):
            if hasattr(provider, "trace"):
                provider.trace = self.trace
            if hasattr(provider, "cancellation"):
                provider.cancellation = self.cancellation

    def _stage(self, stage, provider=None):
        self.stage, self.provider = stage, provider or self.config.provider
        task_id = uuid4().hex
        CALL_CONTEXT.set({"parent_id": task_id, "stage": stage})
        self.trace({"kind": "stage_start", "stage": stage, "provider": self.provider,
                    "task_id": task_id, "parent_id": getattr(getattr(self, "_latest", None), "session_id", None)})
        self.progress({"plan": "Planning research", "search": "Retrieving evidence concurrently",
                       "extract": "Extracting structured evidence", "verify": "Classifying evidence sufficiency",
                       "write": "Writing cited report", "validate": "Validating report citations"}.get(stage, stage))

    def _backend(self, output, context, writing=False, **options):
        return StageBackend(self.writer_backend if writing and self.writer_backend else self.backend,
                            self.budget, output, context, writing=writing, **options)

    async def _model(self, operation, writing=False, backend=None):
        timeout = backend.timeout if backend else self.config.request_timeout
        try:
            return await blocking_call(operation, min(timeout, self.budget.remaining_time(writing)), self.cancellation)
        except BudgetExceeded as exc:
            if backend and backend.partial_content:
                if self.budget.checkpoint:
                    self.budget.checkpoint("PartialStage", backend.partial_content, False)
                raise IncompleteResponse(backend.partial_content, category=exc.category) from exc
            raise

    async def _plan(self, state, store):
        backend = self._backend(min(self.config.planner_output_tokens, 4096), self.config.planner_input_tokens,
                                reasoning_effort="low", max_continuations=0,
                                timeout=min(30, self.config.request_timeout))
        try:
            brief = await self._model(lambda: planner.plan_evidence(backend, state.question), backend=backend)
        except ResearchCancelled:
            raise
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider=self.config.provider, stage="plan", operation="plan")
            if diagnostic.category not in {"output_limit", "missing_finish", "timeout", "request_timeout",
                                            "time_budget", "token_budget", "input_budget", "call_budget",
                                            "cost_budget", "validation_error", "empty_response"}:
                raise
            brief = planner.recover_brief(state.question, getattr(exc, "partial_content", backend.partial_content))
            recovery = "Continuing with a compact evidence plan recovered from available output or the question."
            state.warnings.append(f"Planning limited ({diagnostic.category}): {diagnostic.message} {recovery}")
            self.trace({"kind": "planning_recovered", "stage": "plan", "schema": "PlanningBrief",
                        "diagnostic": diagnostic.model_dump(), "recovered": True, "recovery": recovery})
        # Persist a usable plan before making another provider request.
        state.plan = planner.assemble_plan(brief, [], min(15, self.config.max_searches))
        await store.apersist()
        self.trace({"kind": "plan", **state.plan.model_dump(), "query_count": 0})
        backend = self._backend(min(self.config.planner_output_tokens, 4096), self.config.planner_input_tokens,
                                thinking_enabled=False, max_continuations=0,
                                timeout=min(30, self.config.request_timeout))
        try:
            batch = await self._model(lambda: planner.generate_queries(backend, state.question, brief,
                                     min(15, self.config.max_searches)), backend=backend)
            queries = batch.queries
        except ResearchCancelled:
            raise
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider=self.config.provider, stage="plan", operation="queries")
            if diagnostic.category in {"authentication", "permission", "invalid_request"}:
                raise
            queries = planner.recover_queries(getattr(exc, "partial_content", backend.partial_content))
            recovery = "Using available queries and generating missing searches from plan requirements."
            state.warnings.append(f"Query generation limited ({diagnostic.category}): {diagnostic.message} {recovery}")
            self.trace({"kind": "planning_recovered", "stage": "plan", "schema": "QueryBatch",
                        "diagnostic": diagnostic.model_dump(), "recovered": True, "recovery": recovery})
        state.plan = planner.assemble_plan(brief, queries, min(15, self.config.max_searches))
        self.trace({"kind": "plan", **state.plan.model_dump(), "query_count": len(state.plan.search_queries)})
        await store.apersist()

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
        usable = {}
        terminal_errors = []
        ready = {qid: asyncio.Event() for group in groups for qid in group}

        async def accept_result(query, result):
            # The event-loop owner persists evidence before releasing dependants.
            if isinstance(result, BaseException):
                terminal_errors.append(result)
                diagnostic = failure_from_exception(result, provider="tavily", stage="search", operation="search")
                state.warnings.append(f"Search {query.id} limited ({diagnostic.category}): {diagnostic.message} Continuing with available evidence.")
                self.trace({"kind": "retrieval_error", "query_id": query.id, "diagnostic": diagnostic.model_dump()})
            elif result.failure:
                state.warnings.append(f"Source coverage incomplete for {query.id}: {result.failure.message} Continuing with available evidence.")
            else:
                store.add_result(query, result)
                usable[query.id] = result
            self._usage(state)
            await store.apersist()
            if query.id in ready:
                ready[query.id].set()

        async def extract(group):
            await asyncio.gather(*(ready[qid].wait() for qid in group if qid in ready))
            self.cancellation.check()
            assigned = [q for q in queries if q.id in group]
            result_list = [usable[q.id] for q in assigned if q.id in usable]
            if not any(result.documents for result in result_list):
                return None
            ids = {rid for q in assigned for rid in q.requirement_ids}
            requirements = [r for r in state.plan.research_requirements if r.id in ids]
            payload = researcher.group_payload(state.question, requirements, assigned, result_list,
                                               self.config.researcher_input_tokens, state.findings)
            if not assigned or not result_list:
                return None
            state.status = "EXTRACTING"
            self._stage("extract")
            backend = self._backend(self.config.researcher_output_tokens, self.config.researcher_input_tokens,
                                    max_continuations=0)
            self.trace({"kind": "researcher_start", "stage": "extract", "query_ids": group,
                        "input_bytes": token_bound(json.dumps(payload, ensure_ascii=False)),
                        "max_output_tokens": self.config.researcher_output_tokens})
            try:
                output = await self._model(lambda: researcher.extract_group(backend, payload), backend=backend)
            except IncompleteResponse as exc:
                partial = researcher.partial_extraction(exc.partial_content)
                if partial is not None:
                    exc.extraction_checkpoint = (partial, ids, result_list, [q.id for q in assigned])
                raise
            return output, ids, result_list, [q.id for q in assigned]

        # Only the event-loop owner consolidates evidence; disk workers get frozen snapshots.
        async def guarded_extract(group):
            try:
                return await extract(group)
            except Exception as exc:
                return exc
        async def retrieval_worker():
            try:
                await retrieve(queries, self.search, self.budget, retrieval_trace, accept_result)
                if any(isinstance(error, ResearchCancelled) for error in terminal_errors):
                    raise ResearchCancelled("Research stopped by the user.")
                return None
            finally:
                for event in ready.values():
                    event.set()

        workers = [asyncio.create_task(retrieval_worker())]
        workers += [asyncio.create_task(guarded_extract(group)) for group in groups]
        try:
            for completed in asyncio.as_completed(workers):
                output = await completed
                if isinstance(output, ResearchCancelled):
                    raise output
                if isinstance(output, BaseException):
                    diagnostic = failure_from_exception(output, provider=self.config.provider, stage="extract", operation="extract")
                    state.warnings.append(f"Researcher unavailable ({diagnostic.category}): {diagnostic.message}")
                    self.trace({"kind": "researcher_error", "diagnostic": diagnostic.model_dump()})
                    # A worker's limit must not prevent its siblings or the classifier from finishing.
                    checkpoint = getattr(output, "extraction_checkpoint", None)
                    if checkpoint is not None:
                        store.merge(*checkpoint, persist=False)
                        self._usage(state)
                        await store.apersist()
                    continue
                if output is not None:
                    previous_count = len(state.findings)
                    store.merge(*output, persist=False)
                    self._usage(state)
                    await store.apersist()
                    self.trace({"kind": "findings", "stage": "extract", "findings": [f.model_dump() for f in state.findings[previous_count:]]})
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if not usable and terminal_errors:
            raise terminal_errors[0]
        gain = len(evidence_keys() - before)
        state.round_history.append({"round": state.rounds, "query_ids": [q.id for q in queries],
                                    "queries": [q.query for q in queries], "new_findings": gain,
                                    "source_count": len(state.sources), "evidence_version": state.evidence_version})
        self._usage(state)
        await store.apersist()
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
        invocation = f"evidence-{uuid4().hex}"
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
                    judge.request_id = None
                    metadata = {"call_id": f"judge-{self.budget.calls}", "invocation_id": invocation, "attempt": attempt + 1, "parent_id": CALL_CONTEXT.get().get("parent_id"), "schema": "JudgeDecision", "stage": "verify",
                                "provider": judge.provider, "model": judge.model, "timeout_seconds": judge.timeout,
                                "max_output_tokens": judge.output_tokens,
                                "pricing": {"model": judge.model, "input_price_per_million": rates[0], "output_price_per_million": rates[1]}}
                    if not getattr(judge, "budget_managed", False):
                        self.trace({"kind": "llm_start", **metadata})
                    self.trace({"kind": "classifier_start", "stage": "verify", "provider": judge.provider,
                                "model": judge.model, "attempt": attempt, "evidence_version": state.evidence_version})
                    result = await blocking_call(lambda judge=judge, snapshot=snapshot, research_context=research_context: judge.evaluate(state.question, requirements, snapshot, research_context),
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
                    record = await store.asave_evaluation(payload, result, judge.provider, judge.model, advisory)
                    self.trace({"kind": "verification", "stage": "verify", **record,
                                "sufficient": result.decision == "READY", "gaps": result.critical_gaps,
                                "coverage_score": result.coverage_score, "evidence_score": result.evidence_score,
                                "reliability_score": result.reliability_score})
                    succeeded = True
                    if attempt:
                        self.trace({"kind": "llm_recovered", **metadata, "recovered": True})
                    return result
                except ResearchCancelled:
                    raise
                except Exception as exc:
                    diagnostic = failure_from_exception(exc, provider=judge.provider, stage="verify", operation="classify")
                    self.trace({"kind": "classifier_error", "stage": "verify", "provider": judge.provider,
                                "model": judge.model, "attempt": attempt + 1, "invocation_id": invocation, "diagnostic": diagnostic.model_dump()})
                    if isinstance(exc, BudgetExceeded):
                        raise
                    recoverable = diagnostic.category in (TRANSIENT - {"timeout", "request_timeout"}) | {"validation_error", "empty_response", "invalid_response", "evidence_reference"}
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
                                        "diagnostic": diagnostic.model_dump() if diagnostic else None,
                                        "request_id": (diagnostic.request_id if diagnostic else getattr(judge, "request_id", None))})
                if diagnostic is not None and attempt + 1 < attempts:
                    base_delay = min(self.config.llm_max_delay, self.config.llm_backoff * 2 ** attempt)
                    delay = min(self.config.llm_max_delay, base_delay + random.uniform(0, base_delay * self.config.llm_jitter))
                    delay = max(delay, diagnostic.retry_after_seconds or 0)
                    if delay >= self.budget.remaining_time():
                        raise BudgetExceeded("Classifier retry deadline reached.", category="time_budget")
                    self.trace({"kind": "llm_retry", "invocation_id": invocation, "stage": "verify", "provider": judge.provider,
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

    async def _write(self, state, previous_draft=None, feedback=None):
        previous_report_status, previous_citation_status = state.report_status, state.citation_validation_status
        self._stage("write", self.config.final_writer_provider)
        state.status = "WRITING"
        self._verification(state)
        draft = None
        synthesis_started = time.monotonic()
        interrupted = False
        last_preview = 0.0
        self._partial_report = None
        loop = asyncio.get_running_loop()
        preview_open = True

        def publish_preview(content):
            nonlocal last_preview
            if not preview_open or self.cancellation.event.is_set():
                return
            now = time.monotonic()
            if now - last_preview < 0.5:
                return
            last_preview = now
            partial = writer.partial_draft(state, content, fill_missing=False)
            if partial:
                self._partial_report = partial
                snapshot = state.model_copy(update={"report_status": "incomplete"})
                rendered = writer.render(snapshot, partial)
                atomic_write(Path(state.facts_path).parent / "report.partial.md", rendered)
                self.trace({"kind": "report_preview", "stage": "write", "report": rendered,
                            "incomplete": True, "citation_validation_status": "references_only"})

        def preview(content):
            # Provider callbacks run on worker threads. Publish only on the
            # controller loop, while this synthesis call still owns the report.
            if not loop.is_closed():
                loop.call_soon_threadsafe(publish_preview, content)

        try:
            model = self.config.final_writer_model
            if model and (model != self.config.model or self.config.final_writer_provider != self.config.provider) and self.writer_backend is None:
                factory = getattr(self.backend, "for_model", None)
                if factory is None:
                    raise ValueError("The injected backend does not support model selection")
                self.writer_backend = factory(model, provider=self.config.final_writer_provider)
            if state.verification.supported_finding_ids:
                writer_config = getattr(self.writer_backend or self.backend, "config", self.config)
                context_limit, output_limit = model_limits(writer_config)
                input_limit = min(self.config.writer_input_tokens, context_limit - min(self.config.writer_output_tokens, output_limit) - 128)
                backend = self._backend(self.config.writer_output_tokens, input_limit,
                                        writing=True, max_continuations=0, on_partial=preview)
                try:
                    draft = await self._model(lambda: writer.write_evidence(backend, state, input_limit, previous_draft, feedback),
                                              writing=True, backend=backend)
                except IncompleteResponse as exc:
                    interrupted = True
                    draft = writer.partial_draft(state, exc.partial_content)
                    if draft is None:
                        raise
                    state.warnings.append("Report generation was interrupted; retained complete paragraphs and stored evidence.")
                finally:
                    preview_open = False
                self._stage("validate")
                audit_backend = self._backend(min(self.config.audit_output_tokens, 4096), self.config.writer_input_tokens,
                                              writing=True, thinking_enabled=False, max_continuations=0,
                                              timeout=min(20, self.config.request_timeout))
                await self._model(lambda: writer.audit_report(audit_backend, state, draft), writing=True, backend=audit_backend)
                state.citation_validation_status = "semantic_and_references_passed"
                state.report_status = "incomplete" if interrupted else "generated"
            else:
                state.warnings.append("No eligible source-backed evidence was collected.")
        except ResearchCancelled:
            raise
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider=self.provider, stage=self.stage, operation="write_or_validate")
            self.trace({"kind": "report_error", "diagnostic": diagnostic.model_dump()})
            state.warnings.append(f"Final generation unavailable ({diagnostic.category}): {diagnostic.message} Report assembled from stored evidence.")
            draft = previous_draft
            if draft is not None:
                state.report_status, state.citation_validation_status = previous_report_status, previous_citation_status
        finally:
            preview_open = False
        if draft is None:
            draft = writer.evidence_draft(state)
            if any(s.paragraphs for s in draft.sections):
                writer.validate_draft(state, draft)
            state.citation_validation_status = "extractive_references_passed"
            state.report_status = "best_effort_assembled"
        self.trace({"kind": "synthesis_complete", "stage": "write",
                    "model": self.config.final_writer_model or self.config.model,
                    "duration_seconds": round(time.monotonic() - synthesis_started, 3),
                    "report_status": state.report_status})
        return draft

    async def _assess_answer(self, state, draft, store):
        """Evaluate the actual rendered answer; API failure leaves quality unverified."""
        self._stage("validate", self.config.judge_provider)
        requirements = state.plan.research_requirements
        answer = writer.render(state, draft)
        context = {"status": state.status, "stop_reason": state.stop_reason,
                   "report_status": state.report_status, "citation_validation_status": state.citation_validation_status,
                   "remaining_budget": self.budget.snapshot(), "unresolved_questions": state.unresolved,
                   "contradictions": state.contradictions,
                   "thresholds": {"primary": self.config.quality_min_primary_score,
                                  "grounding": self.config.quality_min_grounding_score,
                                  "overall": self.config.quality_min_overall_score}}
        invocation = f"quality-{uuid4().hex}"
        for fallback in (False, True):
            if fallback and not (self.fallback_judge or self.config.judge_fallback_provider):
                break
            for attempt in range(self.config.judge_retries + 1):
                reservation = None
                metadata = None
                diagnostic = None
                started = time.monotonic()
                judge = None
                try:
                    judge = self._get_judge(fallback)
                    prompt = getattr(judge, "answer_prompt", "Evaluate answer relevance, requirements and factual grounding.")
                    base = {"original_query": state.question, "requirements": [r.model_dump() for r in requirements],
                            "final_answer": answer, "evidence": {}, "research_completion": context}
                    overhead = token_bound(stage_prompt(prompt, QualityEvaluation)) + token_bound(json.dumps(base, ensure_ascii=False)) + 128
                    snapshot = evidence_snapshot(state, max(0, self.config.judge_input_tokens - overhead))
                    payload = {**base, "evidence": snapshot}
                    managed = getattr(judge, "budget_managed", False)
                    rates = getattr(judge, "rates", (self.config.judge_input_price_per_million, self.config.judge_output_price_per_million))
                    if not managed:
                        reservation = self.budget.reserve_model(overhead + token_bound(json.dumps(snapshot, ensure_ascii=False)),
                                                               self.config.judge_output_tokens, rates, writing=True)
                    judge.timeout = min(self.config.judge_timeout, self.budget.remaining_time(writing=True))
                    judge.output_tokens = self.config.judge_output_tokens
                    judge.usage = {"usage_observed": False}
                    judge.request_id = None
                    metadata = {"call_id": f"quality-call-{uuid4().hex}", "invocation_id": invocation, "attempt": attempt + 1,
                                "schema": "QualityEvaluation", "stage": "validate", "operation": "evaluate_answer",
                                "provider": judge.provider, "model": judge.model, "parent_id": state.session_id,
                                "timeout_seconds": judge.timeout, "max_output_tokens": judge.output_tokens,
                                "pricing": {"model": judge.model, "input_price_per_million": rates[0], "output_price_per_million": rates[1]}}
                    if not managed:
                        self.trace({"kind": "llm_start", **metadata})
                    result = await blocking_call(lambda judge=judge, snapshot=snapshot: judge.evaluate_answer(state.question, requirements, answer, snapshot, context),
                                                 judge.timeout, self.cancellation)
                    advisory = result.model_dump() if isinstance(result, QualityEvaluation) else result
                    hard_issues = []
                    try:
                        writer.validate_draft(state, draft)
                    except ResponseError:
                        hard_issues.append("The answer failed deterministic evidence-reference validation.")
                    if state.report_status != "generated":
                        hard_issues.append("The answer is incomplete or assembled from evidence without completed synthesis.")
                    cited = {fid for section in draft.sections for paragraph in section.paragraphs for fid in paragraph.finding_ids}
                    result = validate_quality(result, requirements, snapshot, self.config,
                                              answer_claim_ids=cited, hard_issues=hard_issues)
                    state.quality_evaluation = result
                    state.quality_status = result.decision.lower()
                    await store.asave_quality_evaluation(payload, result, judge.provider, judge.model, advisory)
                    self.trace({"kind": "quality_evaluation", **metadata, "decision": result.decision,
                                "scores": result.scores.model_dump(), "overall_score": result.overall_score,
                                "rejection_reasons": result.critical_issues, "confidence": result.confidence,
                                "duration_seconds": round(time.monotonic() - started, 3)})
                    if attempt:
                        self.trace({"kind": "llm_recovered", **metadata, "recovered": True,
                                    "recovery": "A subsequent structured evaluation succeeded."})
                    return result
                except ResearchCancelled:
                    raise
                except Exception as exc:
                    state.quality_status = "unverified"
                    diagnostic = failure_from_exception(exc, provider=judge.provider if judge else self.config.judge_provider,
                                                        stage="validate", operation="evaluate_answer")
                    self.trace({"kind": "quality_error", **(metadata or {"invocation_id": invocation}),
                                "diagnostic": diagnostic.model_dump()})
                    if isinstance(exc, BudgetExceeded):
                        break
                    if diagnostic.category not in TRANSIENT | {"validation_error", "invalid_response", "empty_response", "evidence_reference"}:
                        break
                    if attempt < self.config.judge_retries:
                        delay = max(self.config.llm_backoff, diagnostic.retry_after_seconds or 0)
                        if delay >= self.budget.remaining_time(writing=True):
                            break
                        self.trace({"kind": "llm_retry", **(metadata or {}), "invocation_id": invocation,
                                    "next_attempt": attempt + 2, "retry_delay_seconds": delay,
                                    "recovery": "Retrying the structured evaluation within the configured limit."})
                        await cancellable_wait(asyncio.sleep(delay), self.cancellation)
                finally:
                    if reservation is not None:
                        usage = getattr(judge, "usage", {"usage_observed": False})
                        self.budget.settle(reservation, usage)
                        self.trace({"kind": "llm_error" if diagnostic else "llm_complete", **metadata, **usage,
                                    "duration_seconds": round(time.monotonic() - started, 3),
                                    "diagnostic": diagnostic.model_dump() if diagnostic else None,
                                        "request_id": (diagnostic.request_id if diagnostic else getattr(judge, "request_id", None))})
        state.research_limitations.append("The final answer's relevance and completeness could not be independently verified.")
        return None

    async def _quality_loop(self, state, draft, store):
        if not any(section.paragraphs for section in draft.sections):
            state.quality_status = "insufficient_evidence"
            state.research_limitations.append("No source-backed evidence is available to answer the request reliably.")
            self.trace({"kind": "quality_rejected", "stage": "validate", "decision": "INSUFFICIENT_EVIDENCE",
                        "rejection_reasons": ["The answer contains no evidence-backed paragraphs."]})
            return draft
        for iteration in range(self.config.quality_max_iterations + 1):
            result = await self._assess_answer(state, draft, store)
            await store.apersist()
            if result is None or result.decision == "PASS":
                return draft
            if result.decision == "INSUFFICIENT_EVIDENCE":
                for rid in result.missing_requirements:
                    requirement = next(r for r in state.plan.research_requirements if r.id == rid)
                    state.research_limitations.append(f"Insufficient evidence to answer: {requirement.question}")
                state.research_limitations.extend(result.unsupported_claims)
                if not result.missing_requirements and not result.unsupported_claims:
                    state.research_limitations.append("Available evidence does not support a reliable answer to all requested questions.")
                if result.unsupported_claims or result.citation_issues:
                    draft = writer.evidence_draft(state)
                    state.report_status = "best_effort_assembled"
                    state.citation_validation_status = "extractive_references_passed"
                return draft
            if iteration >= self.config.quality_max_iterations:
                state.quality_status = "iteration_limit"
                state.research_limitations.append("The answer requires further review to fully satisfy the request.")
                return draft
            if result.decision == "MORE_RESEARCH":
                seen = {" ".join(q.casefold().split()) for q in state.executed_queries}
                queries = []
                for query in result.suggested_queries:
                    normalized = " ".join(query.query.casefold().split())
                    if normalized not in seen:
                        seen.add(normalized)
                        queries.append(PlannedQuery(id=f"quality-{iteration}-{len(queries)}", **query.model_dump()))
                if not queries:
                    state.quality_status = "insufficient_evidence"
                    state.research_limitations.extend(f"No additional evidence for: {r.question}" for r in state.plan.research_requirements if r.id in result.missing_requirements)
                    return draft
                gain = await self._round(state, store, queries, [[q.id for q in queries]])
                if gain < self.config.min_information_gain:
                    state.quality_status = "insufficient_evidence"
                    state.research_limitations.extend(f"Missing evidence for: {r.question}" for r in state.plan.research_requirements if r.id in result.missing_requirements)
                    return draft
                # Reassess evidence from the preserved store, never repeat the initial plan.
                await self._evaluate(state, store)
            draft = await self._write(state, previous_draft=draft, feedback=result.model_dump())
        return draft

    async def arun(self, question):
        if not question.strip():
            raise ValueError("Research question must not be empty.")
        state = ResearchState(question=question.strip())
        self._latest = state
        store = EvidenceStore(state, self.config.session_dir, persist=False)
        def checkpoint(schema, content, complete):
            atomic_write(store.directory / f"generation-{uuid4().hex}-{schema}.json",
                         json.dumps({"schema": schema, "content": content, "complete": complete,
                                     "session_id": state.session_id, "evidence_version": state.evidence_version}, ensure_ascii=False))
        self.budget.checkpoint = checkpoint
        draft = None
        self.trace({"kind": "session_initialized", "session_id": state.session_id, "query": state.question, "facts_path": state.facts_path})
        try:
            await store.apersist()
            self._stage("plan")
            state.status = "PLANNING"
            await self._plan(state, store)
            queries = state.plan.search_queries
            groups = [g.query_ids for g in state.plan.research_groups]
            for round_index in range(self.config.max_rounds + 1):
                gain = await self._round(state, store, queries, groups)
                decision = await self._evaluate(state, store)
                self._usage(state)
                await store.apersist()
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
                    queries.append(PlannedQuery(id=f"Q{len(state.plan.search_queries) + round_index * 5 + len(queries) + 1}", query=text.strip(), requirement_ids=missing))
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
            state.warnings.append(f"Research limited at {diagnostic.stage} ({diagnostic.category}): {diagnostic.message} Finishing with collected evidence.")
            self.trace({"kind": "research_error", "diagnostic": diagnostic.model_dump()})
            if not isinstance(exc, BudgetExceeded):
                state.failure = diagnostic
        try:
            if state.stop_reason != "cancelled":
                draft = await self._write(state)
                draft = await self._quality_loop(state, draft, store)
        except (ResearchCancelled, asyncio.CancelledError):
            self.cancellation.cancel()
            state.stop_reason = "cancelled"
            draft = getattr(self, "_partial_report", None)
            if draft is not None:
                state.report_status = "incomplete"
        except Exception as exc:
            state.quality_status = "unverified"
            state.research_limitations.append("Some requested information could not be independently verified.")
            self.trace({"kind": "quality_error", "diagnostic": failure_from_exception(
                exc, provider=self.provider, stage=self.stage, operation="quality_recovery").model_dump()})
        finally:
            self._usage(state)
            complete = (state.stop_reason == "coverage" and state.report_status == "generated"
                        and state.quality_status == "pass" and not state.failure and not state.research_limitations
                        and not state.unresolved and not state.contradictions)
            state.status = ("CANCELLED" if state.stop_reason == "cancelled" else "COMPLETE" if complete
                            else "PARTIAL" if state.findings else "FAILED")
            await store.apersist()
            if self.writer_backend is not None:
                try:
                    self.writer_backend.close_stream()
                    self.writer_backend.client.close()
                except Exception:
                    pass
            for judge in self.owned_judges:
                try:
                    judge.close()
                except Exception as exc:
                    self.trace({"kind": "cleanup_error", "diagnostic": failure_from_exception(
                        exc, provider=judge.provider, stage="cleanup", operation="judge_close").model_dump()})
        report = writer.render(state, draft)
        self._stage("finalize", "agent")
        await store.awrite("report.md", report)
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
