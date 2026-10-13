"""Safe failure classification and compact, persistent run diagnostics.

Never serialize exception messages, response bodies, headers, prompts, or URLs.
The browser's full trace remains separate from these metadata-only JSONL logs.
"""

import json
import hashlib
from contextvars import ContextVar
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
from .capabilities import inference_options, model_limits

CALL_CONTEXT: ContextVar[dict] = ContextVar("model_trace_context", default={})

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
    "storage_error": "Session history or diagnostic storage is unavailable; research can continue.",
    "unexpected_error": "An unexpected application error stopped this operation.",
}


class DiagnosticError(RuntimeError):
    def __init__(self, message: str, *, category: str = "unexpected_error",
                 diagnostic: FailureDiagnostic | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.diagnostic = diagnostic


def research_outcome(state) -> str:
    """Explain the actual stopping condition, including qualified coverage results."""
    if state.stop_reason == "cancelled":
        return "Research stopped at the user's request; the full plan was not completed."
    if state.failure:
        return f"Research stopped during {state.failure.stage}: {state.failure.message}"
    if state.stop_reason == "coverage":
        if state.report_status in {"incomplete", "best_effort_assembled"}:
            return "Evidence coverage was reached, but report generation or validation did not finish successfully."
        if state.warnings:
            return "Evidence coverage was reached with limitations; some research steps required recovery or could not finish."
        return "Research completed with sufficient evidence coverage."
    if state.stop_reason == "round_limit":
        return "The research round limit was reached before all evidence requirements were covered."
    if state.stop_reason == "budget":
        return "A research time, request, token, or cost limit was reached before the full plan finished."
    if state.stop_reason == "no_tasks":
        if hasattr(state.plan, "research_requirements") and state.judge is None:
            return "Evidence evaluation did not finish, so sufficient coverage could not be confirmed."
        if state.judge and state.judge.decision == "INSUFFICIENT_EVIDENCE":
            return "The evidence evaluation found insufficient support to complete the research plan."
        return "Research ended without sufficient coverage because no useful follow-up searches remained."
    return "Research stopped before the full plan finished."


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
    original = getattr(exc, "research_diagnostic", None)
    if original is None and isinstance(exc, DiagnosticError):
        original = exc.diagnostic
    if original is not None:
        # A wrapper describes a consequence, not a new provider failure. Keep
        # the original transport phase and correlation metadata intact.
        if (stage, operation) == (original.stage, original.operation):
            return original.model_copy()
        effect = f"{stage} / {operation}"
        return original.model_copy(update={"origin": "downstream",
            "downstream_effects": list(dict.fromkeys([*original.downstream_effects, effect]))})
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
    provider_code = None
    if status is not None:
        try:
            body = selected.response.json()
            error = body.get("error", body) if isinstance(body, dict) else {}
            candidate = error.get("code") if isinstance(error, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate):
                provider_code = candidate
        except (ValueError, AttributeError, httpx.ResponseNotRead):
            pass
    confirmed = category in {"validation_error", "output_limit", "empty_response", "evidence_reference", "input_budget", "time_budget", "call_budget", "token_budget", "cost_budget"}
    cause = MESSAGES[category] if confirmed else "Unknown"
    confidence = "confirmed" if confirmed else "unknown"
    if status == 429 and category == "rate_limit":
        cause, confidence = "The provider rejected the request with HTTP 429 (rate limit).", "confirmed"
    elif category == "context_overflow" and status is not None:
        cause, confidence = "The provider reported that the context length was exceeded.", "confirmed"
    elif status is not None:
        cause, confidence = MESSAGES[category], "suspected"
    return FailureDiagnostic(
        provider=provider, stage=stage, operation=operation, category=category,
        exception_type=type(selected).__name__, message=MESSAGES[category],
        http_status=status, request_id=request_id, call_id=call_id, provider_code=provider_code, root_cause=cause, cause_confidence=confidence, retry_after_seconds=retry_after_seconds,
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
    options = inference_options(config)
    context_limit, output_limit = model_limits(config)
    return {"kind": "run_start", "reasoning_effort": options.get("reasoning_effort"),
            "thinking_enabled": options.get("extra_body", {}).get("enable_thinking"),
            "context_tokens": context_limit, "model_output_tokens": output_limit, "provider": config.provider, "model": config.model, "pricing": pricing_metadata(config),
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
        "kind", "type", "stage", "status", "reasoning_effort", "thinking_enabled", "context_tokens", "model_output_tokens", "provider", "operation", "call_id", "schema", "model",
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
        "error_id", "timestamp", "severity", "resolution", "parent_id", "span_id", "task_id", "recovery", "outcome", "decision", "scores", "overall_score", "rejection_reasons", "confidence", "quality_status",
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


class ErrorHistory:
    """Session-scoped logical issues; retries update one record and preserve history."""
    def __init__(self, run_id):
        self.run_id = run_id
        self.issues = {}

    def observe(self, event):
        if event.get("type") == "diagnostic":
            return None
        diagnostic = event.get("diagnostic") or event.get("failure")
        identity = event.get("invocation_id") or (diagnostic or {}).get("correlation_id") or event.get("span_id")
        if identity is None and diagnostic:
            identity = event.get("call_id") or event.get("search_id") or event.get("query_id") or diagnostic.get("call_id")
            identity = identity or f"{diagnostic.get('provider')}:{diagnostic.get('stage')}:{diagnostic.get('operation')}"
        if identity is None:
            return None
        error_id = hashlib.sha256(f"{self.run_id}:{identity}".encode()).hexdigest()[:24]
        previous = self.issues.get(error_id)
        kind = event.get("kind", "")
        if diagnostic is None and (previous is None or kind not in {
                "llm_start", "search_start", "llm_retry", "llm_continuation", "search_retry",
                "llm_recovered", "search_recovered", "llm_complete", "search_complete", "search_degraded"}):
            return None
        now = event.get("timestamp") or datetime.now(timezone.utc).isoformat()
        record = {**(previous or {}), "error_id": error_id, "timestamp": previous.get("timestamp", now) if previous else now,
                  "updated_at": now, "invocation_id": identity,
                  "attempt": max(event.get("next_attempt") or event.get("attempt") or 1, (previous or {}).get("attempt", 1))}
        for key in ("call_id", "span_id", "search_id", "query_id", "stage", "schema", "parent_id"):
            if key in event:
                record[key] = event[key]
        if diagnostic:
            if diagnostic.get("downstream_effects"):
                record["downstream_effects"] = list(dict.fromkeys([
                    *record.get("downstream_effects", []), *diagnostic["downstream_effects"]]))
            if not previous or diagnostic.get("origin") != "downstream":
                record.update(diagnostic=diagnostic)
            record.update(severity="warning", resolution="active")
        if kind in {"llm_retry", "llm_continuation", "search_retry"}:
            record.update(resolution="retrying", recovery=event.get("recovery") or (
                "Continuing from the saved output checkpoint." if kind == "llm_continuation"
                else f"Retrying after {event.get('retry_delay_seconds', 0):g} seconds within the configured limit."))
        if event.get("recovered") or kind in {"llm_recovered", "search_recovered", "llm_complete", "search_complete"}:
            record.update(resolution="recovered", recovery=event.get("recovery") or "A subsequent attempt succeeded.", outcome="Recovered")
        elif event.get("fallback_required"):
            record.update(resolution="unrecovered", recovery=event.get("recovery") or "Continuing with the available evidence.", outcome="Partial recovery")
        if event.get("type") == "error" or (event.get("type") == "result" and event.get("status") == "FAILED"):
            record.update(severity="fatal", resolution="failed", outcome="Research cannot continue")
        record["recovered"] = record.get("resolution") == "recovered"
        if previous and {k: v for k, v in record.items() if k != "updated_at"} == {k: v for k, v in previous.items() if k != "updated_at"}:
            return None
        self.issues[error_id] = record
        return {"type": "diagnostic", **record}


def safe_request_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None
