"""Transient retry delay/accounting contracts without wall-clock sleeps."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import httpx
from deepsearchagent.budget import RunBudget
from deepsearchagent.config import Config
from deepsearchagent.diagnostics import failure_from_exception
from deepsearchagent.models import PlannedQuery, SearchResult
from deepsearchagent.retrieval import retrieve
from deepsearchagent.tools.search import SearchError, SearchRouter


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_http_date_retry_after_is_bounded_and_only_used_for_retryable_status(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 12, 0, 0, 0, tzinfo=timezone.utc)

        for status, value, expected in (
            (503, "Mon, 12 Oct 2026 00:00:03 GMT", 3),
            (429, "Sun, 11 Oct 2026 00:00:00 GMT", 0),
            (401, "Mon, 12 Oct 2026 00:00:03 GMT", None),
        ):
            response = httpx.Response(
                status,
                headers={"Retry-After": value},
                request=httpx.Request("POST", "https://fixture.invalid"),
            )
            error = httpx.HTTPStatusError(
                "CANARY", request=response.request, response=response
            )
            with patch("deepsearchagent.diagnostics.datetime", FixedDateTime):
                failure = failure_from_exception(
                    error, provider="tavily", stage="search", operation="search"
                )
            self.assertEqual(failure.retry_after_seconds, expected)

    async def test_retry_after_is_respected_with_bounded_attempts(self):
        count = 0

        class Provider:
            async def asearch(self, query, *, timeout):
                nonlocal count
                count += 1
                if count == 1:
                    response = httpx.Response(
                        429,
                        headers={"Retry-After": "2"},
                        request=httpx.Request("POST", "https://fixture.invalid"),
                    )
                    error = httpx.HTTPStatusError(
                        "CANARY", request=response.request, response=response
                    )
                    raise SearchError(
                        "rate limit",
                        diagnostic=failure_from_exception(
                            error, provider="tavily", stage="search", operation="search"
                        ),
                    )
                return SearchResult(text="ok", sources=[])

        budget = RunBudget(Config(search_retries=1, search_backoff=0.1))
        sleeper = AsyncMock()
        with patch("deepsearchagent.tools.retry.asyncio.sleep", sleeper):
            results = await retrieve(
                [PlannedQuery(id="Q1", query="q", requirement_ids=["R1"])],
                SearchRouter(Provider()),
                budget,
                lambda e: None,
            )
        self.assertEqual((count, budget.searches), (2, 2))
        self.assertEqual(results[0].text, "ok")
        self.assertGreaterEqual(sleeper.call_args_list[0].args[0], 2)

    async def test_retry_after_beyond_deadline_does_not_sleep_or_call_again(self):
        response = httpx.Response(
            503,
            headers={"Retry-After": "999999"},
            request=httpx.Request("POST", "https://fixture.invalid"),
        )
        error = httpx.HTTPStatusError(
            "CANARY", request=response.request, response=response
        )

        class Provider:
            async def asearch(self, query, *, timeout):
                raise SearchError(
                    "unavailable",
                    diagnostic=failure_from_exception(
                        error, provider="tavily", stage="search", operation="search"
                    ),
                )

        budget = RunBudget(
            Config(search_backoff=0, max_seconds=5, writer_reserve_seconds=0)
        )
        with patch("deepsearchagent.tools.retry.asyncio.sleep", AsyncMock()) as sleeper:
            result = await retrieve(
                [PlannedQuery(id="Q1", query="q", requirement_ids=["R1"])],
                SearchRouter(Provider()),
                budget,
                lambda e: None,
            )
        self.assertEqual(budget.searches, 1)
        self.assertEqual(result[0].category, "time_budget")
        sleeper.assert_not_called()

    def test_malformed_retry_after_is_ignored_without_leaking_header(self):
        for value in ("CANARY-KEY", "-1", "NaN", "Infinity", "1e999"):
            response = httpx.Response(
                429,
                headers={"Retry-After": value},
                request=httpx.Request("POST", "https://fixture.invalid"),
            )
            error = httpx.HTTPStatusError(
                "CANARY", request=response.request, response=response
            )
            failure = failure_from_exception(
                error, provider="tavily", stage="search", operation="search"
            )
            self.assertIsNone(failure.retry_after_seconds)
            self.assertNotIn("CANARY", failure.model_dump_json())

    async def test_sustained_failures_open_shared_circuit_and_success_resets_it(self):
        from deepsearchagent.tools.retry import RetryPolicy

        calls = []
        recovered = False

        async def operation(timeout, metadata):
            calls.append(metadata)
            if not recovered:
                raise TimeoutError("CANARY")
            return SearchResult(text="ok", sources=[])

        policy = RetryPolicy(Config(search_retries=0, search_circuit_threshold=2))
        for _ in range(2):
            with self.assertRaises(SearchError):
                await policy.run(
                    operation, timeout=5, provider="tavily", trace=lambda e: None
                )
        with self.assertRaises(SearchError) as error:
            await policy.run(
                operation, timeout=5, provider="tavily", trace=lambda e: None
            )
        self.assertEqual(error.exception.diagnostic.category, "circuit_open")
        self.assertEqual(len(calls), 2)
        policy.circuit_until = 0
        recovered = True
        result = await policy.run(
            operation, timeout=5, provider="tavily", trace=lambda e: None
        )
        self.assertEqual(result.text, "ok")
        self.assertEqual(policy.failures, 0)
