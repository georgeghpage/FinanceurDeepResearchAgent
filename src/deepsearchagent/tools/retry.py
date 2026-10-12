"""One bounded, cancellable retry policy for all search entry points."""

import asyncio
import random
import time

from ..cancellation import ResearchCancelled
from ..diagnostics import DiagnosticError, failure_from_exception
from ..llm import BudgetExceeded

TRANSIENT = {"rate_limit", "provider_unavailable", "timeout", "request_timeout",
             "connection_error", "protocol_error", "dns_error", "result_format"}


async def cancellable_wait(operation, cancellation=None):
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            if cancellation:
                cancellation.check()
            await asyncio.wait({task}, timeout=0.025)
        if cancellation:
            cancellation.check()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class RetryPolicy:
    def __init__(self, config, *, sleep=None, uniform=None):
        self.config = config
        self.sleep = sleep or asyncio.sleep
        self.uniform = uniform or random.uniform
        self.cooldown_until = 0.0
        self.retries = 0
        self.failures = 0
        self.circuit_until = 0.0

    async def run(self, operation, *, timeout, provider, trace, context=None,
                  cancellation=None, budget=None, started=None):
        context = context or {}
        started = started or time.monotonic()
        deadline = started + timeout
        for attempt in range(self.config.search_retries + 1):
            if time.monotonic() < self.circuit_until:
                from .search import SearchError
                diagnostic = failure_from_exception(DiagnosticError("Search circuit open.", category="circuit_open"),
                    provider=provider, stage="search", operation="search")
                trace({"kind": "search_degraded", **context, "diagnostic": diagnostic.model_dump(),
                       "fallback_required": True})
                raise SearchError(diagnostic.message, diagnostic=diagnostic)
            if cancellation:
                cancellation.check()
            if budget:
                budget.remaining_time()
            # A provider-wide Retry-After gate also holds other concurrent requests.
            cooldown = self.cooldown_until - time.monotonic()
            if cooldown > 0:
                if cooldown >= deadline - time.monotonic():
                    raise BudgetExceeded("Search cooldown exceeds time budget.", category="time_budget")
                await cancellable_wait(self.sleep(cooldown), cancellation)
            remaining = deadline - time.monotonic()
            if budget:
                remaining = min(remaining, budget.remaining_time())
            if remaining <= 0:
                raise BudgetExceeded("Search request deadline reached.", category="time_budget")
            search_id = budget.reserve_search() if budget else None
            meta = {"stage": "search", "provider": provider, **context,
                    "attempt": attempt + 1, "timeout_seconds": remaining}
            if search_id is not None:
                meta["search_id"] = search_id
            trace({"kind": "search_attempt", **meta})
            attempt_started = time.monotonic()
            try:
                result = await cancellable_wait(asyncio.wait_for(operation(remaining, meta), remaining), cancellation)
                self.failures = 0
                self.circuit_until = 0.0
                trace({"kind": "search_recovered" if attempt else "search_succeeded", **meta,
                       "duration_seconds": round(time.monotonic() - attempt_started, 3),
                       "recovered": attempt > 0, "source_count": len(result.sources)})
                return result
            except (ResearchCancelled, BudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as exc:
                diagnostic = failure_from_exception(exc, provider=provider, stage="search", operation="search")
                trace({"kind": "search_error", **meta, "diagnostic": diagnostic.model_dump(),
                       "duration_seconds": round(time.monotonic() - attempt_started, 3)})
                retryable = diagnostic.category in TRANSIENT
                retry = retryable and attempt < self.config.search_retries
                if retry:
                    if budget:
                        retry = budget.reserve_search_retry()
                    else:
                        retry = self.retries < self.config.max_search_retries
                        if retry:
                            self.retries += 1
                if not retry:
                    if retryable:
                        self.failures += 1
                        if self.failures >= self.config.search_circuit_threshold:
                            self.circuit_until = time.monotonic() + self.config.search_circuit_seconds
                    trace({"kind": "search_degraded", **meta, "diagnostic": diagnostic.model_dump(),
                           "retryable": retryable, "fallback_required": True})
                    from .search import SearchError
                    raise SearchError(diagnostic.message, diagnostic=diagnostic) from None
                base = min(self.config.search_max_delay,
                           self.config.search_backoff * self.config.search_backoff_multiplier ** attempt)
                delay = min(self.config.search_max_delay, base + self.uniform(0, base * self.config.search_jitter))
                # Never shorten the provider's instruction; abort if it cannot fit.
                delay = max(delay, diagnostic.retry_after_seconds or 0)
                available = deadline - time.monotonic()
                if budget:
                    available = min(available, budget.remaining_time())
                if delay >= available:
                    if not budget:
                        from .search import SearchError
                        raise SearchError(diagnostic.message, diagnostic=diagnostic) from None
                    raise BudgetExceeded("Search retry would exceed time budget.", category="time_budget")
                if diagnostic.retry_after_seconds:
                    self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
                trace({"kind": "search_retry", **meta, "retry_delay_seconds": delay,
                       "category": diagnostic.category, "next_attempt": attempt + 2})
                await cancellable_wait(self.sleep(delay), cancellation)
