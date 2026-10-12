"""LangGraph workflow: plan → research → verify → follow up or write."""

import time
from collections.abc import Callable
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from . import planner, researcher, verifier, writer
from .cancellation import Cancellation, CancellableBackend, ResearchCancelled
from .config import Config
from .diagnostics import failure_from_exception
from .llm import Backend, BudgetExceeded
from .models import ReportDraft, ResearchState, ResearchTask
from .tools.search import SearchError, SearchRouter


class GraphState(TypedDict):
    research_state: ResearchState
    tasks: list[ResearchTask]
    seen: list[tuple[str, str]]
    draft: ReportDraft | None


class ResearchAgent:
    """A compiled graph with injected LLM and search providers for one run."""

    def __init__(self, backend: Backend, config: Config, search: SearchRouter,
                 progress: Callable[[str], None] | None = None,
                 trace: Callable[[dict], None] | None = None,
                 cancellation: Cancellation | None = None) -> None:
        self.cancellation = cancellation
        if hasattr(backend, "cancellation"):
            backend.cancellation = cancellation
        self.backend = CancellableBackend(backend, cancellation) if cancellation else backend
        self.config = config
        self.search = search
        self.search.configure(config)
        self.progress = progress or (lambda message: None)
        sink = trace or (lambda event: None)

        def emit(event: dict) -> None:
            if event.get("kind") == "stage_start":
                self.stage, self.provider = event["stage"], event["provider"]
            sink(event)

        self.trace = emit
        self._latest: ResearchState | None = None
        self.stage = "plan"
        self.provider = config.provider
        self.search.cancellation = cancellation
        self.search.trace = self.trace
        # Providers that support transport diagnostics share the run's trace.
        for provider in (self.search.web, self.search.documentation):
            if hasattr(provider, "trace"):
                provider.trace = self.trace
            if hasattr(provider, "cancellation"):
                provider.cancellation = cancellation
        builder = StateGraph(GraphState)
        builder.add_node("plan", self._plan)
        builder.add_node("research", self._research)
        builder.add_node("verify", self._verify)
        builder.add_node("write", self._write)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges("plan", self._after_plan, {"research": "research", "write": "write"})
        builder.add_edge("research", "verify")
        builder.add_conditional_edges("verify", self._after_verify, {"research": "research", "write": "write"})
        builder.add_edge("write", END)
        self.graph = builder.compile()

    def _state(self, graph: GraphState) -> ResearchState:
        self._latest = graph["research_state"]
        return self._latest

    def _remaining_time(self) -> float:
        if self.cancellation:
            self.cancellation.check()
        remaining = self.config.max_seconds - (time.monotonic() - self.backend.started)
        if remaining <= 0:
            raise BudgetExceeded("The research time budget was reached.", category="time_budget")
        if self.backend.calls >= self.config.max_calls:
            raise BudgetExceeded("The model request budget was reached.", category="call_budget")
        if self.backend.tokens_used >= self.config.max_tokens:
            raise BudgetExceeded("The observed model token budget was reached.", category="token_budget")
        return min(remaining, self.config.request_timeout)

    def _stage(self, stage: str, provider: str) -> None:
        self.stage, self.provider = stage, provider
        self.trace({"kind": "stage_start", "stage": stage, "provider": provider})

    def _plan(self, graph: GraphState) -> dict:
        self._stage("plan", self.config.provider)
        state = self._state(graph)
        self._remaining_time()
        self.progress("Planning research")
        state.plan = planner.plan(self.backend, state.question, self.config.max_tasks, self.search.available_sources)
        self.trace({"kind": "plan", **state.plan.model_dump()})
        return {"research_state": state, "tasks": state.plan.tasks}

    def _after_plan(self, graph: GraphState) -> Literal["research", "write"]:
        return "research" if graph["tasks"] else "write"

    def _research(self, graph: GraphState) -> dict:
        state = self._state(graph)
        state.rounds += 1
        seen = set(graph["seen"])
        for task in graph["tasks"][:self.config.max_tasks]:
            key = (task.source, task.query.strip().casefold())
            if key in seen:
                continue
            self._stage("search", self.search.provider_name(task.source))
            timeout = self._remaining_time()
            if self.search.calls >= self.config.max_searches:
                raise BudgetExceeded("The research search budget was reached.", category="search_budget")
            seen.add(key)
            self.progress(f"Round {state.rounds} ({task.source}): {task.question}")
            try:
                researcher.research(self.backend, self.search, state, task, timeout, self.trace)
            except SearchError as exc:
                failure = failure_from_exception(exc, provider=self.search.provider_name(task.source), stage="search", operation="search")
                state.warnings.append(f"Source coverage incomplete: search unavailable ({failure.category}); continuing with available evidence.")
        return {"research_state": state, "seen": list(seen)}

    def _verify(self, graph: GraphState) -> dict:
        self._stage("verify", self.config.provider)
        state = self._state(graph)
        self._remaining_time()
        self.progress("Checking evidence and coverage")
        state.verification = verifier.verify(self.backend, state, self.config.max_tasks, self.search.available_sources)
        self.trace({"kind": "verification", **state.verification.model_dump()})
        seen = set(graph["seen"])
        tasks = [task for task in state.verification.follow_up_tasks
                 if (task.source, task.query.strip().casefold()) not in seen]
        if state.verification.sufficient:
            state.stop_reason = "coverage"
        elif state.rounds >= self.config.max_rounds:
            state.stop_reason = "round_limit"
        else:
            state.stop_reason = "no_tasks"
        return {"research_state": state, "tasks": tasks}

    def _after_verify(self, graph: GraphState) -> Literal["research", "write"]:
        state = graph["research_state"]
        if state.stop_reason != "coverage" and state.rounds < self.config.max_rounds and graph["tasks"]:
            return "research"
        return "write"

    def _write(self, graph: GraphState) -> dict:
        self._stage("write", self.config.provider)
        state = self._state(graph)
        if state.verification and state.verification.supported_finding_ids:
            self._remaining_time()
            self.progress("Writing cited report")
            return {"draft": writer.write(self.backend, state)}
        return {"draft": None}

    def run(self, question: str) -> tuple[ResearchState, str]:
        if not question.strip():
            raise ValueError("Research question must not be empty.")
        state = ResearchState(question=question.strip())
        self._latest = state
        draft = None
        try:
            output = self.graph.invoke(
                {"research_state": state, "tasks": [], "seen": [], "draft": None},
                config={"recursion_limit": 3 * self.config.max_rounds + 5},
            )
            if self.cancellation:
                self.cancellation.check()
            state, draft = output["research_state"], output["draft"]
        except ResearchCancelled:
            state = self._latest
            state.stop_reason = "cancelled"
            state.failure = None
            state.warnings.append("Research stopped by the user. No further model or search calls were made. Usage for an interrupted request may be unavailable.")
        except Exception as exc:
            state = self._latest
            state.stop_reason = "budget" if isinstance(exc, BudgetExceeded) else "error"
            model_failure = getattr(self.backend, "last_failure", None)
            if not isinstance(exc, SearchError) and model_failure is not None:
                state.failure = model_failure
            else:
                state.failure = failure_from_exception(
                    exc, provider="agent" if isinstance(exc, BudgetExceeded) else self.provider,
                    stage=self.stage, operation="budget_check" if isinstance(exc, BudgetExceeded) else self.stage)
            failure = state.failure
            state.warnings.append(f"Stopped at {failure.stage}: {failure.provider} / {failure.category}. {failure.message}")
        finally:
            state.api_calls = self.backend.calls
            state.search_calls = self.search.calls
            state.tokens_used = self.backend.tokens_used
            state.elapsed_seconds = round(time.monotonic() - self.backend.started, 3)
        state.status = ("CANCELLED" if state.stop_reason == "cancelled" else
                        "COMPLETE" if state.stop_reason == "coverage" and draft is not None and not state.failure
                        else "PARTIAL" if state.findings else "FAILED")
        report = writer.render(state, draft)
        self.trace({"kind": "run_stopped" if state.failure or state.stop_reason == "cancelled" else "run_complete",
                    "stage": state.failure.stage if state.failure else self.stage,
                    "provider": state.failure.provider if state.failure else "agent",
                    "stop_reason": state.stop_reason,
                    "failure": state.failure.model_dump() if state.failure else None,
                    "api_calls": state.api_calls, "search_calls": state.search_calls,
                    "tokens_used": state.tokens_used, "source_count": len(state.sources),
                    "elapsed_seconds": state.elapsed_seconds})
        return state, report
