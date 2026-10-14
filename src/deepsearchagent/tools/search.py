"""Provider-neutral search routing; documentation search is an injectable plug."""

from typing import Protocol
import time
import asyncio
from asyncio import sleep as rate_wait
import threading
import concurrent.futures
from contextvars import ContextVar

from ..cancellation import ResearchCancelled
from ..models import FailureDiagnostic, ResearchTask, SearchResult
from ..diagnostics import DiagnosticError, failure_from_exception

SEARCH_CONTEXT: ContextVar[dict] = ContextVar("search_trace_context", default={})


class SearchError(DiagnosticError):
    def __init__(self, message: str, *, category: str = "search_error", diagnostic=None) -> None:
        super().__init__(message, category=category, diagnostic=diagnostic)


class SearchProvider(Protocol):
    def search(self, query: str, *, timeout: float) -> SearchResult: ...


class SearchRouter:
    def __init__(self, web: SearchProvider, documentation: SearchProvider | None = None) -> None:
        self.web = web
        self.documentation = documentation
        self.cancellation = None
        self.calls = 0
        self.trace = lambda event: None
        self.last_failure: FailureDiagnostic | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._calls_lock = threading.Lock()
        self._cache = {}
        self._inflight = {}
        self._waiters = {}
        self._next_request = 0.0
        self._active = 0

    def provider_name(self, source: str) -> str:
        provider = self.web if source == "web" else self.documentation
        return getattr(provider, "provider_name", source)

    @property
    def available_sources(self) -> list[str]:
        return ["web", "documentation"] if self.documentation else ["web"]

    def configure(self, config, budget=None):
        from .retry import RetryPolicy
        self.retry_policy = RetryPolicy(config)
        self.budget = budget
        self._semaphore = None
        self.session_started = time.monotonic()
        self._cache.clear()
        self._inflight.clear()
        self._next_request = 0.0

    def search(self, task: ResearchTask, *, timeout: float) -> SearchResult:
        # The sync workflow shares exactly the async transport/retry behavior.
        return asyncio.run(self.asearch(task, timeout=timeout))

    async def _once(self, provider, query, timeout):
        from .retry import cancellable_wait
        method = getattr(provider, "asearch_once", None) or getattr(provider, "asearch", None)
        if method:
            # The retry policy cancels this whole attempt. Await directly so
            # no extra task scheduling separates pacing from transport entry.
            return await method(query, timeout=timeout)
        # A daemon adapter cannot hold asyncio's executor open after a deadline.
        future = concurrent.futures.Future()
        def work():
            try:
                value = provider.search(query, timeout=timeout)
                if not future.cancelled():
                    future.set_result(value)
            except BaseException as exc:
                if not future.cancelled():
                    future.set_exception(exc)
        threading.Thread(target=work, daemon=True).start()
        return await cancellable_wait(asyncio.wrap_future(future), self.cancellation)

    async def asearch(self, task: ResearchTask, *, timeout: float) -> SearchResult:
        from .retry import cancellable_wait
        if self.cancellation:
            self.cancellation.check()
        key = (task.source, " ".join(task.query.split()))
        if key in self._cache:
            self.trace({"kind": "search_cache_hit", "stage": "search", "query": task.query})
            return self._cache[key].model_copy(deep=True)
        shared = self._inflight.get(key)
        if shared is None:
            shared = asyncio.create_task(self._execute(task, timeout=timeout))
            self._inflight[key] = shared
        self._waiters[shared] = self._waiters.get(shared, 0) + 1
        try:
            result = await cancellable_wait(asyncio.shield(shared), self.cancellation)
            self._cache[key] = result.model_copy(deep=True)
            return result.model_copy(deep=True)
        finally:
            self._waiters[shared] -= 1
            if self._waiters[shared] == 0:
                self._waiters.pop(shared)
                if not shared.done():
                    shared.cancel()
                await asyncio.gather(shared, return_exceptions=True)
            if shared.done() and self._inflight.get(key) is shared:
                self._inflight.pop(key, None)

    async def aclose(self):
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    async def _execute(self, task: ResearchTask, *, timeout: float) -> SearchResult:
        import hashlib
        from uuid import uuid4
        from .retry import RetryPolicy
        from ..config import Config
        if not hasattr(self, "retry_policy"):
            self.retry_policy = RetryPolicy(Config())
            self.budget = None
            self._semaphore = None
            self.session_started = time.monotonic()
        # Sync search creates a fresh loop; async fan-out uses one loop per run.
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.retry_policy.config.search_concurrency)
            self._pace_lock = asyncio.Lock()
            self._semaphore_loop = loop
        provider = self.web if task.source == "web" else self.documentation
        if provider is None:
            raise SearchError("Documentation search has no provider plugged in.")
        self.last_failure = None
        from .retry import cancellable_wait
        started = time.monotonic()
        timeout = min(timeout, self.retry_policy.config.max_seconds - (started - self.session_started))
        from ..diagnostics import CALL_CONTEXT
        context = {**CALL_CONTEXT.get(), **SEARCH_CONTEXT.get(), "operation": "search", "invocation_id": str(uuid4()), "query_ref": hashlib.sha256(task.query.encode()).hexdigest()[:16]}

        async def operation(remaining, meta):
            from .retry import cancellable_wait
            async with self._semaphore:
                # Pace actual transport starts, including retries. Reserving
                # slots before semaphore admission can release queued bursts.
                async with self._pace_lock:
                    while True:
                        delay = max(self._next_request, self.retry_policy.cooldown_until) - time.monotonic()
                        if delay <= 0:
                            break
                        await cancellable_wait(rate_wait(delay), self.cancellation)
                    if self.cancellation:
                        self.cancellation.check()
                remaining = min(timeout, self.retry_policy.config.max_seconds - (time.monotonic() - self.session_started))
                if self.budget:
                    remaining = min(remaining, self.budget.remaining_time())
                    meta["search_id"] = self.budget.reserve_search()
                meta["timeout_seconds"] = remaining
                self._active += 1
                self.trace({"kind": "search_concurrency", "active": self._active,
                            "limit": self.retry_policy.config.search_concurrency})
                try:
                    # timeout() preserves the current task; wait_for() would
                    # enqueue transport behind other ready admissions.
                    async with asyncio.timeout(remaining):
                        return await attempt(remaining, meta)
                finally:
                    self._active -= 1

        async def attempt(remaining, meta):
            with self._calls_lock:
                if not self.budget and self.calls >= self.retry_policy.config.max_searches:
                    from ..llm import BudgetExceeded
                    raise BudgetExceeded("Search request budget reached.", category="search_budget")
                self.calls += 1
                call_id = self.calls
            meta.setdefault("search_id", call_id)
            attempt_started = time.monotonic()
            self.trace({"kind": "search_start", **meta, "query": task.query, "source": task.source})
            token = SEARCH_CONTEXT.set(meta)
            try:
                # Trace callbacks may perform slow synchronous storage. Start
                # the interval after those callbacks, at transport entry.
                if self.cancellation:
                    self.cancellation.check()
                if self.budget:
                    self.budget.remaining_time()
                self._next_request = time.monotonic() + 1 / self.retry_policy.config.search_requests_per_second
                result = await self._once(provider, task.query, remaining)
                if result.failure:
                    raise SearchError(result.failure.message, diagnostic=result.failure)
                self.trace({"kind": "search_complete", **meta, "query": task.query,
                            "source_count": len(result.sources), "sources": [s.model_dump() for s in result.sources],
                            "duration_seconds": round(time.monotonic() - attempt_started, 3)})
                return result
            finally:
                SEARCH_CONTEXT.reset(token)

        try:
            return await self.retry_policy.run(operation,
                    timeout=self.retry_policy.config.max_seconds - (started - self.session_started),
                    started=started, reserve_in_operation=True,
                    provider=self.provider_name(task.source), trace=self.trace, context=context,
                    cancellation=self.cancellation, budget=self.budget)
        except ResearchCancelled:
            raise
        except Exception as exc:
            self.last_failure = failure_from_exception(exc, provider=self.provider_name(task.source),
                                                       stage="search", operation="search")
            raise
