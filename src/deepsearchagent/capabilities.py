"""Provider request capabilities, independent of workflow routing.

DeepSeek limits verified against Aliyun's DeepSeek API and V4.1 Flash model
pages on 2026-10-12. Unknown models use conservative limits and local JSON
validation; they never receive unverified reasoning parameters.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    context_tokens: int = 32_768
    output_tokens: int = 8_192
    json_object: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    thinking_switch: bool = False
    thinking_default: bool = False


def capabilities(provider: str, model: str) -> ModelCapabilities:
    if provider == "aliyun" and model == "deepseek-v4.1-flash":
        return ModelCapabilities(1_000_000, 393_216, True, ("low", "high", "max"), True, True)
    # Aliyun model-detail and structured-output docs verified 2026-10-13.
    # Use only verified JSON/context limits; inference switches remain omitted.
    if provider == "aliyun" and model in {"qwen3.8-flash", "qwen3.8-max", "qwen3.7-plus"}:
        return ModelCapabilities(1_000_000, 131_072, True)
    if provider == "kimi" and (model.startswith("kimi-k3") or model in {"kimi-k2.6", "kimi-k2.5", "kimi-k2"}):
        return ModelCapabilities(262_144, 32_768, True,
                                 ("low", "high", "max") if model.startswith("kimi-k3") else ())
    if provider == "aliyun" and model in {"qwen-plus", "qwen-flash"}:
        return ModelCapabilities(json_object=True, thinking_switch=True)
    return ModelCapabilities()


def inference_options(config, *, thinking_enabled=None, reasoning_effort=None) -> dict:
    caps = capabilities(config.provider, config.model)
    effort = config.kimi_reasoning_effort if config.provider == "kimi" else config.reasoning_effort
    if reasoning_effort is not None:
        effort = reasoning_effort
    if thinking_enabled is False and not caps.thinking_switch and caps.reasoning_efforts:
        effort = "low"
    options = {}
    if caps.reasoning_efforts:
        if effort not in caps.reasoning_efforts:
            raise ValueError("Reasoning effort is unsupported by the selected model.")
        options["reasoning_effort"] = effort
    if caps.thinking_switch:
        enabled = config.thinking_enabled if thinking_enabled is None else thinking_enabled
        options["extra_body"] = {"enable_thinking": caps.thinking_default and enabled}
        if not options["extra_body"]["enable_thinking"]:
            options.pop("reasoning_effort", None)
    return options


def model_limits(config) -> tuple[int, int]:
    caps = capabilities(config.provider, config.model)
    # Overrides can lower a verified limit, never silently raise it.
    return min(config.context_tokens, caps.context_tokens), min(config.model_output_tokens, caps.output_tokens)
