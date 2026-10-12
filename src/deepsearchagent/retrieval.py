"""Independent bounded search fan-out; tool failures are explicit evidence gaps."""

import asyncio

from .cancellation import ResearchCancelled
from .diagnostics import failure_from_exception
from .llm import BudgetExceeded
from .models import ResearchTask, SearchResult
from .tools.retry import TRANSIENT as TRANSIENT
from .tools.search import SEARCH_CONTEXT


async def retrieve(queries, router, budget, trace):
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

    return await asyncio.gather(*(one(q) for q in queries), return_exceptions=True)
