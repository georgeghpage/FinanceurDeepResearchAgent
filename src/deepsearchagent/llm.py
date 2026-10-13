"""OpenAI-compatible Chat Completions adapter with validated JSON and shared run budgets."""

import time
import threading
from uuid import uuid4
from collections.abc import Callable
from typing import Protocol, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from .cancellation import Cancellation
from .config import Config
from .capabilities import capabilities, inference_options, model_limits
from .diagnostics import DiagnosticError, failure_from_exception
from .system_prompt import build_system_prompt

T = TypeVar("T", bound=BaseModel)

class BudgetExceeded(DiagnosticError):
    def __init__(self, message: str, *, category: str = "budget") -> None:
        super().__init__(message, category=category)


class ResponseError(DiagnosticError):
    def __init__(self, message: str, *, category: str = "invalid_response") -> None:
        super().__init__(message, category=category)


class IncompleteResponse(ResponseError):
    """Internal checkpoint; never serialized into metadata logs."""
    def __init__(self, content: str, *, category: str = "output_limit") -> None:
        super().__init__("Model generation is incomplete.", category=category)
        self.partial_content = content
        self.extraction_checkpoint = None


def continuation_payload(payload: str, prefix: str) -> str:
    import json
    return payload + "\nUntrusted generation checkpoint: " + json.dumps(prefix) + (
        "\nContinue the JSON document from exactly its last character. Return only the missing suffix. "
        "Do not repeat, revise or contradict checkpoint content or its citations. No Markdown fences.")


def append_suffix(prefix: str, suffix: str) -> str:
    # Exact overlaps only; never fuzzy-match or remove repeated factual sentences.
    if prefix and suffix.startswith(prefix):
        return suffix
    for size in range(min(len(prefix), len(suffix), 4096), 15, -1):
        if prefix.endswith(suffix[:size]):
            return prefix + suffix[size:]
    return prefix + suffix


class Backend(Protocol):
    calls: int
    tokens_used: int
    started: float

    def structured(self, schema: type[T], instructions: str, payload: str, *,
                   output_tokens: int | None = None, timeout: float | None = None,
                   system_prompt: str | None = None, continuation: str = "",
                   thinking_enabled: bool | None = None, reasoning_effort: str | None = None,
                   on_partial: Callable[[str], None] | None = None) -> T: ...


class OpenAICompatibleBackend:
    def __init__(self, config: Config, api_key: str,
                 trace: Callable[[dict], None] | None = None,
                 diagnostic_trace: Callable[[dict], None] | None = None) -> None:
        self.trace_namespace = uuid4().hex
        self.config = config
        # Retries are disabled so a run's request count and timeout stay explicit.
        self.client = OpenAI(api_key=api_key, base_url=config.base_url, max_retries=0)
        self.calls = 0
        self.tokens_used = 0
        self.started = time.monotonic()
        self.trace = trace
        self.diagnostic_trace = diagnostic_trace
        self.last_failure = None
        self.cancellation: Cancellation | None = None
        self._stream_lock = threading.Lock()
        self._active_stream = None
        self._streams: dict[int, object] = {}
        self._budget_lock = threading.RLock()
        self._local = threading.local()

    def for_model(self, model: str, provider: str | None = None):
        from dataclasses import replace
        config = replace(self.config, provider=provider or self.config.provider, model=model, research_model=None,
                         input_price_per_million=self.config.final_writer_input_price_per_million,
                         output_price_per_million=self.config.final_writer_output_price_per_million,
                         cached_price_per_million=None, cache_write_price_per_million=None)
        from .config import require_api_key
        key = self.client.api_key if config.provider == self.config.provider else require_api_key(config.provider)
        backend = type(self)(config, key, self.trace, self.diagnostic_trace)
        backend.cancellation = self.cancellation
        return backend

    def close_stream(self) -> None:
        with self._stream_lock:
            streams = list(self._streams.values())
            self._streams.clear()
            self._active_stream = None
        for stream in streams:
            stream.close()

    def _check_cancelled(self) -> None:
        if self.cancellation:
            self.cancellation.check()

    def _emit(self, event: dict) -> None:
        self._check_cancelled()
        from .diagnostics import CALL_CONTEXT
        call_id = event.get("call_id", getattr(self._local, "call_id", None))
        context = CALL_CONTEXT.get()
        event = {**context, **event}
        if call_id is not None:
            event["span_id"] = f"{self.trace_namespace}:{call_id}"
        if self.trace is not None:
            self.trace(event)
        if (self.diagnostic_trace is not None and self.diagnostic_trace != self.trace
                and event.get("kind") not in {"thinking_delta", "output_delta"}):
            self.diagnostic_trace(event)

    def _options(self) -> dict:
        self._check_cancelled()
        remaining_time = self.config.max_seconds - (time.monotonic() - self.started)
        remaining_tokens = self.config.max_tokens - self.tokens_used
        if self.calls >= self.config.max_calls:
            raise BudgetExceeded("The model request budget was reached.", category="call_budget")
        if remaining_time <= 0:
            raise BudgetExceeded("The research time budget was reached.", category="time_budget")
        if remaining_tokens <= 0:
            raise BudgetExceeded("The observed model token budget was reached.", category="token_budget")
        self.calls += 1
        options = {
            "model": self.config.model,
            "max_tokens": min(self.config.max_output_tokens, remaining_tokens),
            "timeout": min(self.config.request_timeout, remaining_time),
        }
        options.update(inference_options(self.config))
        return options

    def _record(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        self._local.usage = self._usage(usage)
        if usage is not None:
            with self._budget_lock:
                self.tokens_used += usage.total_tokens

    def usage_for_thread(self) -> dict:
        return getattr(self._local, "usage", {"usage_observed": False})

    def structured(self, schema: type[T], instructions: str, payload: str,
                   *, output_tokens: int | None = None, timeout: float | None = None,
                   system_prompt: str | None = None, continuation: str = "",
                   thinking_enabled: bool | None = None, reasoning_effort: str | None = None,
                   on_partial: Callable[[str], None] | None = None) -> T:
        self._local.usage = {"usage_observed": False}
        self.last_failure = None
        stage = {"PlanningBrief": "plan", "QueryBatch": "plan", "ResearchPlan": "plan", "EvidencePlan": "plan", "Extraction": "extract", "EvidenceExtraction": "extract",
                 "Verification": "verify", "JudgeDecision": "verify", "ReportDraft": "write", "ReportAudit": "validate", "QualityEvaluation": "validate"}.get(schema.__name__, "model")
        try:
            with self._budget_lock:
                options = self._options()
                self._local.call_id = self.calls
            if output_tokens is not None:
                options["max_tokens"] = min(output_tokens, self.config.max_tokens - self.tokens_used)
            if timeout is not None:
                options["timeout"] = min(timeout, options["timeout"])
            if thinking_enabled is not None or reasoning_effort is not None:
                options.pop("reasoning_effort", None)
                options.pop("extra_body", None)
                options.update(inference_options(self.config, thinking_enabled=thinking_enabled,
                                                 reasoning_effort=reasoning_effort))
        except BudgetExceeded as exc:
            self.last_failure = failure_from_exception(exc, provider="agent", stage=stage, operation="budget_check")
            self._emit({"kind": "llm_error", "diagnostic": self.last_failure.model_dump(),
                        "message": self.last_failure.message, "provider": "agent", "stage": stage})
            raise
        request = dict(
            **options, messages=[
                {"role": "system", "content": system_prompt or build_system_prompt(instructions, schema)},
                {"role": "user", "content": payload},
            ],
        )
        context_limit, output_limit = model_limits(self.config)
        if continuation:
            request["messages"][1]["content"] = continuation_payload(payload, continuation)
        elif capabilities(self.config.provider, self.config.model).json_object:
            request["response_format"] = {"type": "json_object"}
        count = sum(len(m["content"].encode("utf-8")) for m in request["messages"]) + 64
        request["max_tokens"] = min(request["max_tokens"], output_limit, context_limit - count)
        if request["max_tokens"] <= 0:
            raise BudgetExceeded("Model context window reached.", category="input_budget")
        self._local.continuation = continuation
        self._local.on_partial = on_partial
        if self.trace is not None or on_partial is not None:
            return self._streamed(schema, request)
        started = time.monotonic()
        meta = self._start_metadata(schema, request)
        self._emit({"kind": "llm_start", **meta})
        finish_reason = None
        stats = {"tokens": 0, "usage_observed": False, "output_chars": 0, "reasoning_chars": 0}
        try:
            response = self.client.chat.completions.create(**request)
            self._check_cancelled()
            self._record(response)
            from .diagnostics import safe_request_id
            meta["request_id"] = safe_request_id(getattr(response, "_request_id", None))
            stats.update(self._usage(getattr(response, "usage", None)))
            if not response.choices:
                raise ResponseError(f"{self.config.provider.title()} returned no choices.", category="empty_response")
            choice = response.choices[0]
            finish_reason = choice.finish_reason
            stats["output_chars"] = len(choice.message.content or "")
            stats["reasoning_chars"] = len(getattr(choice.message, "reasoning_content", "") or "")
            content = append_suffix(continuation, choice.message.content or "")
            if on_partial and content:
                on_partial(content)
            if not getattr(choice.message, "refusal", None) and finish_reason in {"length", None}:
                raise IncompleteResponse(content, category="output_limit" if finish_reason == "length" else "missing_finish")
            self._validate_completion(finish_reason, choice.message.content, getattr(choice.message, "refusal", None))
            result = schema.model_validate_json(content, strict=True)
            self._emit({"kind": "llm_complete", **meta, **stats, "finish_reason": "stop",
                        "duration_seconds": round(time.monotonic() - started, 3)})
            return result
        except Exception as exc:
            self._failed(exc, meta, stats, started, finish_reason)
            raise

    def _start_metadata(self, schema, request: dict) -> dict:
        return {"call_id": getattr(self._local, "call_id", self.calls), "schema": schema.__name__, "provider": self.config.provider,
                "stage": {"PlanningBrief": "plan", "QueryBatch": "plan", "ResearchPlan": "plan", "EvidencePlan": "plan", "Extraction": "extract", "EvidenceExtraction": "extract",
                          "Verification": "verify", "JudgeDecision": "verify", "ReportDraft": "write", "ReportAudit": "validate", "QualityEvaluation": "validate"}.get(schema.__name__, "model"),
                "model": self.config.model, "operation": "chat_completion",
                "timeout_seconds": request["timeout"], "max_output_tokens": request["max_tokens"],
                "reasoning_effort": request.get("reasoning_effort"),
                "thinking_enabled": request.get("extra_body", {}).get("enable_thinking")}

    @staticmethod
    def _usage(usage) -> dict:
        if usage is None:
            return {"tokens": 0, "usage_observed": False}
        return {"tokens": usage.total_tokens, "usage_observed": True,
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
                "cached_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None),
                "cache_write_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cache_write_tokens", None),
                "reasoning_tokens": getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)}

    def _validate_completion(self, finish_reason, content, refused) -> None:
        if refused or finish_reason == "content_filter":
            raise ResponseError(f"{self.config.provider.title()} refused the response.", category="refusal")
        if finish_reason == "length":
            raise ResponseError(f"{self.config.provider.title()} reached the output token limit.", category="output_limit")
        if finish_reason is None:
            raise ResponseError(f"{self.config.provider.title()} omitted the completion finish reason.", category="missing_finish")
        if finish_reason != "stop":
            raise ResponseError(f"{self.config.provider.title()} returned an unsupported finish reason.")
        if not content:
            raise ResponseError(f"{self.config.provider.title()} returned no JSON content.", category="empty_response")

    def _failed(self, exc, meta, stats, started, finish_reason) -> None:
        self._check_cancelled()
        failure = failure_from_exception(
            exc, provider=self.config.provider, stage=meta["stage"], operation="chat_completion",
            call_id=meta["call_id"], finish_reason=finish_reason)
        from .diagnostics import CALL_CONTEXT
        failure = failure.model_copy(update={"correlation_id": CALL_CONTEXT.get().get("invocation_id"),
                                             "upstream_span_id": f"{self.trace_namespace}:{meta['call_id']}"})
        exc.research_diagnostic = failure
        self.last_failure = failure
        self._emit({"kind": "llm_error", **meta, **stats,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "finish_reason": failure.finish_reason,
                    "diagnostic": failure.model_dump(), "message": failure.message})

    def _streamed(self, schema: type[T], request: dict) -> T:
        """Relay provider deltas while validating only the completed JSON content."""
        emit = self._emit
        meta = self._start_metadata(schema, request)
        call_id = meta["call_id"]
        started = time.monotonic()
        emit({"kind": "llm_start", **meta})
        stream = None
        registered = False
        content: list[str] = []
        finish_reason = None
        refused = False
        observed_tokens = 0
        stats = {"tokens": 0, "usage_observed": False, "output_chars": 0, "reasoning_chars": 0,
                 "time_to_first_delta_seconds": None}
        try:
            stream = self.client.chat.completions.create(
                **request, stream=True, stream_options={"include_usage": True})
            from .diagnostics import safe_request_id
            meta["request_id"] = safe_request_id(getattr(getattr(stream, "response", None), "headers", {}).get("x-request-id"))
            with self._stream_lock:
                self._active_stream = stream
                self._streams[threading.get_ident()] = stream
                registered = True
            self._check_cancelled()
            for chunk in stream:
                self._check_cancelled()
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    with self._budget_lock:
                        self.tokens_used += max(0, usage.total_tokens - observed_tokens)
                    self._local.usage = self._usage(usage)
                    observed_tokens = max(observed_tokens, usage.total_tokens)
                    stats.update(self._usage(usage))
                    stats["tokens"] = observed_tokens
                    emit({"kind": "llm_usage", "call_id": call_id, **stats})
                if time.monotonic() - self.started >= self.config.max_seconds:
                    raise BudgetExceeded("The research time budget was reached during a model call.", category="time_budget")
                if time.monotonic() - started >= request["timeout"]:
                    raise BudgetExceeded("The model request time limit was reached.", category="request_timeout")
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    delta = choice.delta
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        stats["reasoning_chars"] += len(reasoning)
                        emit({"kind": "thinking_delta", "call_id": call_id, "text": reasoning})
                    if delta.content:
                        stats["output_chars"] += len(delta.content)
                        content.append(delta.content)
                        on_partial = getattr(self._local, "on_partial", None)
                        if on_partial:
                            on_partial(append_suffix(getattr(self._local, "continuation", ""), "".join(content)))
                        emit({"kind": "output_delta", "call_id": call_id, "text": delta.content})
                    if (reasoning or delta.content) and stats["time_to_first_delta_seconds"] is None:
                        stats["time_to_first_delta_seconds"] = round(time.monotonic() - started, 3)
                    refused = refused or bool(getattr(delta, "refusal", None))
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
            text = append_suffix(getattr(self._local, "continuation", ""), "".join(content))
            if not refused and finish_reason in {"length", None}:
                raise IncompleteResponse(text, category="output_limit" if finish_reason == "length" else "missing_finish")
            self._validate_completion(finish_reason, "".join(content), refused)
            result = schema.model_validate_json(text, strict=True)
            emit({"kind": "llm_complete", **meta, **stats, "finish_reason": "stop",
                  "duration_seconds": round(time.monotonic() - started, 3),
                  "tokens": observed_tokens})
            return result
        except Exception as exc:
            self._failed(exc, meta, stats, started, finish_reason)
            if not refused and content:
                diagnostic = failure_from_exception(exc, provider=self.config.provider, stage=meta["stage"], operation="stream")
                if diagnostic.category in {"connection_error", "protocol_error", "timeout", "request_timeout", "time_budget"}:
                    raise IncompleteResponse(append_suffix(getattr(self._local, "continuation", ""), "".join(content)),
                                             category=diagnostic.category) from exc
            raise
        finally:
            if stream is not None:
                try:
                    with self._stream_lock:
                        owned = self._streams.pop(threading.get_ident(), None)
                        if self._active_stream is stream:
                            self._active_stream = None
                    if owned is stream or not registered:
                        stream.close()
                except Exception as exc:
                    diagnostic = failure_from_exception(exc, provider=self.config.provider, stage=meta["stage"], operation="stream_cleanup")
                    if not self.cancellation or not self.cancellation.event.is_set():
                        emit({"kind": "cleanup_error", "diagnostic": diagnostic.model_dump(), "call_id": call_id})


# Keep the original import contract for existing integrations.
KimiBackend = OpenAICompatibleBackend


class AliyunBackend(OpenAICompatibleBackend):
    """AliYun Model Studio (DashScope) OpenAI-compatible transport."""
