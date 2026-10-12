"""Safe failure classification and compact, persistent run diagnostics.

Never serialize exception messages, response bodies, headers, prompts, or URLs.
The browser's full trace remains separate from these metadata-only JSONL logs.
"""

import json
import os
import re
import ssl
import socket
import math
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from .models import FailureDiagnostic
from .pricing import pricing_metadata

MESSAGES = {
    "authentication": "Provider rejected authentication or access (check the key and endpoint).",
    "rate_limit": "Provider temporarily rate limited requests.",
    "quota_exhausted": "Provider credits or quota are exhausted.",
    "account_restricted": "Provider account access is restricted.",
    "invalid_request": "Provider rejected request parameters.",
    "provider_unavailable": "Provider returned a server error.",
    "http_error": "Provider returned an unsuccessful HTTP status.",
    "timeout": "Provider request timed out.",
    "request_timeout": "Model stream exceeded the per-request time limit.",
    "tls_error": "TLS connection failed; check certificates and the proxy route.",
    "proxy_error": "The configured proxy connection failed.",
    "dns_error": "Provider hostname resolution failed. Check DNS and the configured endpoint.",
    "connection_error": "Provider connection failed; check network and proxy routing.",
    "protocol_error": "Provider connection or stream ended with a protocol error.",
    "output_limit": "Model reached the output token limit before completing JSON.",
    "refusal": "Model refused the requested response.",
    "missing_finish": "Model stream ended without a completion finish reason.",
    "empty_response": "Model returned no usable response content.",
    "invalid_response": "Model returned an unsupported completion finish reason.",
    "validation_error": "Model output failed JSON or schema validation.",
    "catalog_response": "Provider returned an invalid or incomplete model list.",
    "evidence_reference": "Report output contained missing or unsupported evidence references.",
    "empty_report": "Model output contained no evidence-backed report paragraphs.",
    "tool_error": "Tavily MCP returned a tool error.",
    "result_format": "Tavily returned an unsupported search-result format.",
    "search_error": "The search provider failed.",
    "time_budget": "The overall research time budget was reached.",
    "call_budget": "The model request budget was reached.",
    "token_budget": "The observed model token budget was reached.",
    "search_budget": "The search request budget was reached.",
    "budget": "A research run limit was reached.",
    "cost_budget": "The configured monetary budget was reached.",
    "unpriced_model": "Account prices are required to enforce the monetary budget.",
    "context_overflow": "Provider context window was exceeded.",
    "circuit_open": "Search temporarily paused after repeated provider failures.",
    "input_budget": "The stage input context limit was reached.",
    "unexpected_error": "An unexpected application error stopped this operation.",
}


class DiagnosticError(RuntimeError):
    def __init__(self, message: str, *, category: str = "unexpected_error",
                 diagnostic: FailureDiagnostic | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.diagnostic = diagnostic


def _exceptions(exc: BaseException):
    """Inspect typed causes inside MCP task groups without rendering their text."""
    pending = [exc]
    seen = set()
    while pending:
        item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend(getattr(item, "exceptions", ()))
        cause = item.__cause__ or item.__context__
        if cause is not None:
            pending.append(cause)


def failure_from_exception(exc: BaseException, *, provider: str, stage: str,
                           operation: str, call_id: int | None = None,
                           finish_reason: str | None = None) -> FailureDiagnostic:
    if isinstance(exc, DiagnosticError) and exc.diagnostic is not None:
        return exc.diagnostic.model_copy(update={"stage": stage})
    chain = list(_exceptions(exc))
    category = "unexpected_error"
    selected = exc
    status = None
    request_id = None
    retry_after_seconds = None
    # A local, explicit failure (e.g. output_limit) takes priority over context
    # left over from an earlier error handled by a provider library.
    explicit = next((e for e in chain if isinstance(e, DiagnosticError)
                     and e.category in MESSAGES and e.category not in {"unexpected_error", "search_error"}), None)
    statuses = [e for e in chain if isinstance(e, (APIStatusError, httpx.HTTPStatusError))]
    if explicit is not None:
        selected, category = explicit, explicit.category
    elif statuses:
        selected = statuses[0]
        status = selected.status_code if isinstance(selected, APIStatusError) else selected.response.status_code
        category = ("authentication" if status in {401, 403} else "rate_limit" if status == 429
                    else "timeout" if status == 408 else "invalid_request" if status in {400, 404, 405, 422, 501}
                    else "provider_unavailable" if status >= 500 else "http_error")
        if provider in {"tavily", "aliyun", "kimi"}:
            from .provider_errors import provider_error_category
            try:
                body = selected.response.text
            except httpx.ResponseNotRead:
                body = ""
            category = provider_error_category(body, status) or category
        # Retain only a validated numeric delay, never raw response headers.
        if status in {429, 503} and category in {"rate_limit", "provider_unavailable"}:
            value = selected.response.headers.get("Retry-After", "")
            try:
                delay = float(value)
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(value)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    delay = max(0, (retry_date - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    delay = -1
            if math.isfinite(delay) and delay >= 0:
                retry_after_seconds = delay
        candidate = getattr(selected, "request_id", None)
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate):
            request_id = candidate
    else:
        for types, code in (
            ((APITimeoutError, httpx.TimeoutException, TimeoutError), "timeout"),
            ((ssl.SSLError,), "tls_error"),
            ((httpx.ProxyError,), "proxy_error"),
            ((socket.gaierror,), "dns_error"),
            ((httpx.NetworkError, APIConnectionError, ConnectionError), "connection_error"),
            ((httpx.ProtocolError,), "protocol_error"),
            ((ValidationError,), "validation_error"),
        ):
            match = next((e for e in chain if isinstance(e, types)), None)
            if match is not None:
                selected, category = match, code
                break
        if category == "unexpected_error" and isinstance(exc, DiagnosticError) and exc.category in MESSAGES:
            selected, category = exc, exc.category
    return FailureDiagnostic(
        provider=provider, stage=stage, operation=operation, category=category,
        exception_type=type(selected).__name__, message=MESSAGES[category],
        http_status=status, request_id=request_id, call_id=call_id, retry_after_seconds=retry_after_seconds,
        finish_reason=finish_reason if finish_reason in {"stop", "length", "content_filter", "tool_calls", "function_call"} else None,
    )


def run_metadata(config) -> dict:
    """Endpoint/proxy hosts only; omit credentials, paths and query strings."""
    proxies = []
    for scheme, value in getproxies().items():
        if scheme not in {"http", "https", "all"}:
            continue
        try:
            parsed = urlsplit(value)
            proxies.append({"scheme": scheme, "host": parsed.hostname, "port": parsed.port})
        except ValueError:
            proxies.append({"scheme": scheme, "configured": True})
    return {"kind": "run_start", "provider": config.provider, "model": config.model, "pricing": pricing_metadata(config),
            "kimi_host": urlsplit(config.kimi_base_url).hostname if config.provider == "kimi" else None,
            "model_host": urlsplit(config.base_url).hostname,
            "tavily_host": urlsplit(config.tavily_mcp_url).hostname,
            "proxies": proxies, "request_timeout_seconds": config.request_timeout,
            "max_seconds": config.max_seconds, "max_calls": config.max_calls,
            "max_searches": config.max_searches, "max_tokens": config.max_tokens,
            "max_output_tokens": config.max_output_tokens}


class DiagnosticJournal:
    """Append one JSON object per event; intentionally exclude trace content."""

    FIELDS = {
        "kind", "type", "stage", "provider", "operation", "call_id", "schema", "model",
        "sequence", "elapsed_seconds", "duration_seconds", "time_to_first_delta_seconds",
        "timeout_seconds", "max_output_tokens", "tokens", "input_tokens", "output_tokens",
        "reasoning_tokens", "usage_observed", "finish_reason", "output_chars", "reasoning_chars",
        "source_count", "search_calls", "api_calls", "tokens_used", "stop_reason", "failure",
        "kimi_host", "model_host", "tavily_host", "proxies", "request_timeout_seconds", "max_seconds",
        "max_calls", "max_searches", "max_tokens", "diagnostic",
        "trust_env", "pricing", "cached_tokens", "cache_write_tokens", "search_id",
        "research_id", "invocation_id", "query_ref", "retry_delay_seconds", "next_attempt", "category", "recovered", "fallback_required", "retryable",
        "session_id", "evidence_version", "snapshot_sha256", "query_count", "attempt", "query_id",
        "finding_count", "refinement_rounds", "total_cost_usd", "cost_complete", "token_usage_complete",
        "report_status", "citation_validation_status", "coverage_score", "evidence_score", "reliability_score",
    }

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.failed = False

    def record(self, event: dict) -> bool:
        if self.failed:
            return False
        if event.get("kind") in {"thinking_delta", "output_delta", "plan", "findings", "verification"} or event.get("type") == "progress":
            return True
        record = {key: value for key, value in event.items() if key in self.FIELDS}
        record.update(run_id=self.run_id, timestamp=datetime.now(timezone.utc).isoformat())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self.failed = True
            return False
        return True
