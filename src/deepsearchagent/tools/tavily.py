"""Tavily web search via a direct Streamable HTTP MCP client."""

import asyncio
import time
from datetime import timedelta
from contextvars import ContextVar

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..cancellation import ResearchCancelled
from ..config import Config
from ..models import SearchResult
from ..diagnostics import failure_from_exception
from .fetch import parse_tavily_result
from .search import SEARCH_CONTEXT, SearchError


class TavilyMCPSearch:
    provider_name = "tavily"

    def __init__(self, server_url: str, api_key: str, *, trust_env: bool = True, config: Config | None = None) -> None:
        self.config = config or Config()
        self.server_url = server_url
        self._api_key = api_key
        self.trust_env = trust_env
        self.trace = lambda event: None
        self.cancellation = None
        self.operation = "connect"
        self._operation = ContextVar("tavily_operation", default=None)
        self.last_failure = None

    def _step(self, operation: str) -> None:
        self.operation = operation
        phase = self._operation.get()
        if phase is not None:
            phase["operation"] = operation
        self.trace({"kind": "tavily_step", "provider": "tavily", "stage": "search",
                    "operation": operation, "trust_env": self.trust_env, **SEARCH_CONTEXT.get()})

    async def _search(self, query: str, timeout: float) -> SearchResult:
        self._step("connect")
        # One scoped session per search, so the synchronous CLI owns its lifecycle.
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout,
                trust_env=self.trust_env,
            ) as client:
                async with streamable_http_client(self.server_url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                        self._step("initialize")
                        await session.initialize()
                        self._step("call_tool")
                        result = await session.call_tool("tavily_search", arguments={
                            "query": query,
                            "search_depth": "advanced",
                            "max_results": 5,
                            "include_raw_content": True,
                            "include_images": False,
                        })
                        self._step("parse_result")
                        parsed = parse_tavily_result(result)
                        self._step("session_cleanup")
            return parsed

    async def _cancellable_search(self, query: str, timeout: float) -> SearchResult:
        task = asyncio.create_task(self._search(query, timeout))
        try:
            while not task.done():
                if self.cancellation:
                    self.cancellation.check()
                await asyncio.wait({task}, timeout=0.025)
            if self.cancellation:
                self.cancellation.check()
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def asearch_once(self, query: str, *, timeout: float) -> SearchResult:
        started = time.monotonic()
        context = self._operation.set({"operation": "connect"})
        self.last_failure = None
        try:
            result = await self._cancellable_search(query, timeout)
            self.trace({"kind": "tavily_complete", "provider": "tavily", "stage": "search",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "source_count": len(result.sources), **SEARCH_CONTEXT.get()})
            return result
        except ResearchCancelled:
            raise
        except Exception as exc:
            self.last_failure = failure_from_exception(exc, provider="tavily", stage="search", operation=self._operation.get()["operation"])
            self.trace({"kind": "tavily_error", "provider": "tavily", "stage": "search",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "timeout_seconds": timeout, "diagnostic": self.last_failure.model_dump(), **SEARCH_CONTEXT.get()})
            raise SearchError(self.last_failure.message, diagnostic=self.last_failure) from None
        finally:
            self._operation.reset(context)

    async def asearch(self, query: str, *, timeout: float) -> SearchResult:
        # Standalone integration checks receive the same retry policy as runs.
        from .retry import RetryPolicy
        policy = RetryPolicy(self.config)
        return await policy.run(lambda remaining, meta: self.asearch_once(query, timeout=remaining),
                                timeout=min(self.config.max_seconds, timeout * (self.config.search_retries + 1) + self.config.search_max_delay * self.config.search_retries),
                                request_timeout=timeout, provider="tavily", trace=self.trace,
                                context=SEARCH_CONTEXT.get(), cancellation=self.cancellation)

    def search(self, query: str, *, timeout: float) -> SearchResult:
        return asyncio.run(self.asearch(query, timeout=timeout))
