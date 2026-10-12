"""Snapshot reference prices in each run so history does not change with rates."""

# Published USD reference rates verified 2026-10-12. Local endpoint/account bills
# may differ. Unknown models deliberately have no inferred rate.
REFERENCE_RATES = {
    "kimi-k3": (3.0, 15.0, 0.30, 3.0),
    "kimi-k2.6": (0.95, 4.0, 0.16, None),
}


def pricing_metadata(config) -> dict:
    defaults = REFERENCE_RATES.get(config.model, (None, None, None, None)) if config.provider == "kimi" else (None, None, None, None)
    names = ("input_price_per_million", "output_price_per_million",
             "cached_price_per_million", "cache_write_price_per_million")
    rates = {name: getattr(config, name) if getattr(config, name) is not None else default
             for name, default in zip(names, defaults)}
    custom = any(getattr(config, name) is not None for name in names)
    return {**rates, "currency": "USD", "provider": config.provider, "model": config.model,
            "basis": "custom" if custom else "published reference" if config.provider == "kimi" and config.model in REFERENCE_RATES else "unpriced",
            "verified_at": "2026-10-12", "cache_ttl": "5m",
            "model_source": "https://platform.kimi.ai/docs/pricing/chat" if config.provider == "kimi" else "https://www.alibabacloud.com/help/en/model-studio/model-pricing",
            "tavily_source": "https://docs.tavily.com/documentation/api-credits",
            "tavily_price_per_search": config.tavily_price_per_search}
