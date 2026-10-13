"""Independent bounded search fan-out; tool failures are explicit evidence gaps."""

import asyncio

from .cancellation import ResearchCancelled
from .diagnostics import failure_from_exception
from .llm import BudgetExceeded
from .models import ResearchTask, SearchResult
from .tools.retry import TRANSIENT as TRANSIENT
from .tools.search import SEARCH_CONTEXT


async def retrieve(queries, router, budget, trace, on_result=None):
    if getattr(router, "budget", None) is not budget:
        router.configure(budget.config, budget)
    router.trace = trace

    async def one(query):
        context = SEARCH_CONTEXT.set({**SEARCH_CONTEXT.get(), "query_id": query.id})
        try:
            timeout = min(budget.config.request_timeout, budget.remaining_time())
            return await router.asearch(ResearchTask(question=", ".join(query.requirement_ids), query=query.query), timeout=timeout)
        except (ResearchCancelled, BudgetExceeded):
            raise
        except Exception as exc:
            diagnostic = failure_from_exception(exc, provider=router.provider_name("web"), stage="search", operation="search")
            return SearchResult(text="", sources=[], failure=diagnostic)
        finally:
            SEARCH_CONTEXT.reset(context)

    async def completed(query):
        try:
            result = await one(query)
        except Exception as exc:
            result = exc
        if on_result:
            on_result(query, result)
        return result

    tasks = [asyncio.create_task(completed(q)) for q in queries]
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await router.aclose()
