"""Environment configuration and explicit run limits."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values


def environment_values() -> dict[str, str]:
    """Read the working directory's .env without changing process environment."""
    values = {key: value for key, value in dotenv_values(Path.cwd() / ".env").items() if value is not None}
    values.update(os.environ)
    # Either exported key name takes precedence over credentials in the file.
    for canonical, alias in (("KIMI_API_KEY", "MOONSHOT_API_KEY"), ("ALIYUN_API_KEY", "DASHSCOPE_API_KEY")):
        exported_key = os.getenv(canonical) or os.getenv(alias)
        if exported_key:
            values[canonical] = exported_key
    return values


@dataclass(frozen=True)
class Config:
    provider: str = "aliyun"
    model: str = "deepseek-v4.1-flash"
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    aliyun_base_url: str = "https://maas.qianwenaiapi.com/compatible-mode/v1"
    kimi_reasoning_effort: str = "low"
    reasoning_effort: str = "max"
    thinking_enabled: bool = True
    context_tokens: int = 1_000_000
    model_output_tokens: int = 393_216
    planner_input_tokens: int = 24_000
    planner_output_tokens: int = 16_384
    audit_output_tokens: int = 8_192
    llm_concurrency: int = 3
    llm_retries: int = 2
    max_continuations: int = 2
    llm_backoff: float = 1.0
    llm_max_delay: float = 20.0
    llm_jitter: float = 0.2
    search_circuit_threshold: int = 8
    search_circuit_seconds: float = 30.0
    max_rounds: int = 2
    max_tasks: int = 5
    max_calls: int = 20
    max_tokens: int = 240_000
    max_seconds: float = 600
    max_output_tokens: int = 16_384
    request_timeout: float = 120
    max_searches: int = 25
    search_concurrency: int = 5
    search_retries: int = 3
    search_backoff: float = 1.0
    search_backoff_multiplier: float = 2.0
    search_max_delay: float = 15.0
    search_jitter: float = 0.2
    max_search_retries: int = 25
    researcher_input_tokens: int = 24_000
    researcher_output_tokens: int = 16_384
    writer_input_tokens: int = 64_000
    writer_output_tokens: int = 32_768
    writer_reserve_tokens: int = 48_000
    writer_reserve_seconds: float = 60
    max_cost_usd: float | None = None
    session_dir: str = "reports/sessions"
    min_information_gain: int = 1
    judge_provider: str = "research"
    judge_base_url: str = "https://aiservice.cn-hangzhou.aliyuncs.com/v1"
    judge_model: str = "pai-judge"
    judge_temperature: float = 0
    judge_output_tokens: int = 16_384
    judge_input_tokens: int = 32_000
    judge_timeout: float = 60
    judge_retries: int = 1
    judge_json_mode: str = "json_object"
    judge_prompt_path: str | None = None
    judge_fallback_provider: str | None = None
    judge_fallback_model: str = "qwen-flash"
    judge_fallback_base_url: str | None = None
    judge_input_price_per_million: float | None = None
    judge_output_price_per_million: float | None = None
    judge_fallback_input_price_per_million: float | None = None
    judge_fallback_output_price_per_million: float | None = None
    ready_coverage_threshold: float = 0.90
    tavily_mcp_url: str = "https://mcp.tavily.com/mcp/"
    # Optional account-specific USD rates; None uses a published reference rate.
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    cached_price_per_million: float | None = None
    cache_write_price_per_million: float | None = None
    tavily_price_per_search: float = 0.016

    def __post_init__(self) -> None:
        for name in ("input_price_per_million", "output_price_per_million", "cached_price_per_million",
                     "cache_write_price_per_million", "tavily_price_per_search", "max_cost_usd",
                     "judge_input_price_per_million", "judge_output_price_per_million",
                     "judge_fallback_input_price_per_million", "judge_fallback_output_price_per_million"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be nonnegative and finite")
        for name in ("max_tasks", "max_calls", "max_tokens", "max_output_tokens", "max_searches"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.max_rounds <= 2:
            raise ValueError("max_rounds must be between zero and two additional rounds")
        for name in ("max_seconds", "request_timeout"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("search_concurrency", "researcher_input_tokens", "researcher_output_tokens",
                     "writer_input_tokens", "writer_output_tokens", "judge_input_tokens", "judge_output_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("context_tokens", "model_output_tokens", "planner_input_tokens", "planner_output_tokens",
                     "audit_output_tokens", "llm_concurrency", "search_circuit_threshold"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("llm_retries", "max_continuations"):
            if not 0 <= getattr(self, name) <= 5:
                raise ValueError(f"{name} must be between zero and five")
        for name in ("llm_backoff", "llm_max_delay", "llm_jitter", "search_circuit_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.llm_jitter > 1 or self.llm_max_delay <= 0:
            raise ValueError("Invalid LLM retry delay settings")
        if self.reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("REASONING_EFFORT must be low, high, or max")
        for name in ("search_retries", "judge_retries", "writer_reserve_tokens", "min_information_gain", "max_search_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.judge_retries > 1 or self.search_retries > 3:
            raise ValueError("Judge recovery is limited to one retry; search retries to three")
        for name in ("search_backoff", "search_max_delay", "search_jitter", "search_backoff_multiplier", "writer_reserve_seconds", "judge_timeout", "judge_temperature"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.search_backoff_multiplier < 1 or self.search_max_delay <= 0 or self.search_jitter > 1:
            raise ValueError("Invalid search retry delay settings")
        if not 0 <= self.ready_coverage_threshold <= 1 or self.judge_timeout == 0:
            raise ValueError("Invalid judge threshold or timeout")
        if self.judge_provider not in {"research", "pai", "aliyun", "kimi"} or self.judge_fallback_provider not in {None, "pai", "aliyun", "kimi"}:
            raise ValueError("Invalid judge provider")
        if self.judge_json_mode not in {"json_object", "json_schema", "none"}:
            raise ValueError("Invalid judge JSON mode")
        if self.judge_provider == "pai" and self.judge_json_mode == "json_schema":
            raise ValueError("PAI strict JSON Schema support is not assumed")
        for name in ("judge_model", "judge_fallback_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(c.isspace() for c in value):
                raise ValueError(f"{name} must be a valid model ID")
        if self.provider not in {"kimi", "aliyun"}:
            raise ValueError("provider must be kimi or aliyun")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200 or any(char.isspace() for char in self.model):
            raise ValueError("model must be a model ID of 1–200 characters without whitespace")
        if self.kimi_reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("KIMI_REASONING_EFFORT must be low, high, or max")
        for name in ("kimi_base_url", "aliyun_base_url", "tavily_mcp_url", "judge_base_url", "judge_fallback_base_url"):
            value = getattr(self, name)
            if value is None:
                continue
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(char.isspace() for char in value):
                raise ValueError(f"{name} must be an HTTP(S) URL without credentials or whitespace")

    @property
    def base_url(self) -> str:
        return self.aliyun_base_url if self.provider == "aliyun" else self.kimi_base_url

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        env = environment_values()
        provider = overrides.get("provider") or env.get("LLM_PROVIDER", cls.provider)
        prefix = "ALIYUN" if provider == "aliyun" else "KIMI"
        # This mapping combines heterogeneous dataclass fields; each environment
        # field is parsed below and __post_init__ validates runtime constraints.
        values: dict[str, Any] = {"provider": provider,
                  "model": env.get(f"{prefix}_MODEL", cls.model if provider == "aliyun" else "kimi-k3"),
                  "kimi_base_url": env.get("KIMI_BASE_URL", cls.kimi_base_url),
                  "aliyun_base_url": env.get("ALIYUN_BASE_URL", cls.aliyun_base_url),
                  "kimi_reasoning_effort": env.get("KIMI_REASONING_EFFORT", cls.kimi_reasoning_effort),
                  "tavily_mcp_url": env.get("TAVILY_MCP_URL", cls.tavily_mcp_url)}
        values.update({key: value for key, value in overrides.items() if value is not None})
        # Every bounded-pipeline setting is configurable independently of model credentials.
        for name in ("context_tokens", "model_output_tokens", "planner_input_tokens", "planner_output_tokens", "audit_output_tokens",
                     "llm_concurrency", "llm_retries", "max_continuations", "search_circuit_threshold",
                     "search_concurrency", "search_retries", "max_search_retries", "researcher_input_tokens", "researcher_output_tokens",
                     "writer_input_tokens", "writer_output_tokens", "writer_reserve_tokens", "judge_input_tokens",
                     "judge_output_tokens", "judge_retries", "min_information_gain", "max_rounds", "max_tasks",
                     "max_calls", "max_tokens", "max_searches", "max_output_tokens"):
            if name.upper() in env and overrides.get(name) is None:
                values[name] = int(env[name.upper()])
        for name in ("llm_backoff", "llm_max_delay", "llm_jitter", "search_circuit_seconds", "max_cost_usd", "search_backoff", "search_backoff_multiplier", "search_max_delay", "search_jitter", "writer_reserve_seconds", "judge_timeout", "judge_temperature",
                     "ready_coverage_threshold", "judge_input_price_per_million", "judge_output_price_per_million",
                     "judge_fallback_input_price_per_million", "judge_fallback_output_price_per_million", "max_seconds", "request_timeout"):
            if name.upper() in env and overrides.get(name) is None:
                values[name] = float(env[name.upper()])
        for name in ("reasoning_effort", "session_dir", "judge_provider", "judge_base_url", "judge_model", "judge_json_mode",
                     "judge_prompt_path", "judge_fallback_provider", "judge_fallback_model", "judge_fallback_base_url"):
            if env.get(name.upper()) and overrides.get(name) is None:
                values[name] = env[name.upper()]
        for name, variable in {
            "input_price_per_million": "KIMI_INPUT_PRICE_PER_MILLION",
            "output_price_per_million": "KIMI_OUTPUT_PRICE_PER_MILLION",
            "cached_price_per_million": "KIMI_CACHED_PRICE_PER_MILLION",
            "cache_write_price_per_million": "KIMI_CACHE_WRITE_PRICE_PER_MILLION",
            "tavily_price_per_search": "TAVILY_PRICE_PER_SEARCH",
        }.items():
            if variable.startswith("KIMI_"):
                variable = variable.replace("KIMI_", f"{prefix}_", 1)
            if variable in env and overrides.get(name) is None:
                values[name] = float(env[variable])
        if "THINKING_ENABLED" in env and overrides.get("thinking_enabled") is None:
            raw = env["THINKING_ENABLED"].lower()
            if raw not in {"true", "false", "1", "0"}:
                raise ValueError("THINKING_ENABLED must be true or false")
            values["thinking_enabled"] = raw in {"true", "1"}
        return cls(**values)


def require_api_key(provider: str = "aliyun") -> str:
    env = environment_values()
    if provider == "aliyun":
        key = env.get("ALIYUN_API_KEY", "").strip() or env.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise ValueError("Set ALIYUN_API_KEY (or DASHSCOPE_API_KEY) before running AliYun research. See README.md.")
        return key
    if provider != "kimi":
        raise ValueError("provider must be kimi or aliyun")
    key = env.get("KIMI_API_KEY", "").strip() or env.get("MOONSHOT_API_KEY", "").strip()
    if not key:
        raise ValueError("Set KIMI_API_KEY (or MOONSHOT_API_KEY) before running live research. See README.md.")
    return key


def require_tavily_key() -> str:
    key = environment_values().get("TAVILY_API_KEY", "").strip()
    if not key:
        raise ValueError("Set TAVILY_API_KEY for Tavily MCP web search. See README.md.")
    return key
