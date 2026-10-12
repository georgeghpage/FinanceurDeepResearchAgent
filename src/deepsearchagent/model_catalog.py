"""Read account catalogs, including Aliyun's native model-list API."""

from urllib.parse import urlsplit, urlunsplit

import httpx
from httpx import Client as CatalogClient
from openai import APIConnectionError, APIStatusError, OpenAI

from .config import Config
from .diagnostics import DiagnosticError


def _native_urls(config: Config) -> list[str]:
    """Only use known Aliyun hosts; keep workspace and region selection intact."""
    parsed = urlsplit(config.base_url)
    host = parsed.hostname or ""
    known_host = host in {"maas.qianwenaiapi.com", "dashscope.aliyuncs.com",
                          "dashscope-intl.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"}
    workspace_host = host.endswith(".maas.aliyuncs.com")
    if (config.provider != "aliyun" or parsed.scheme != "https"
            or parsed.path.rstrip("/") != "/compatible-mode/v1"
            or not (known_host or workspace_host)):
        return []
    urls = [urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/models", "", ""))]
    if host == "maas.qianwenaiapi.com":
        urls.append("https://dashscope.aliyuncs.com/api/v1/models")
    return urls


def _model_ids(values) -> list[str]:
    return sorted({value for value in values
                   if isinstance(value, str) and 0 < len(value) <= 200
                   and not any(char.isspace() for char in value)})


def _can_fallback(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, (APIStatusError, httpx.HTTPStatusError)):
        status = exc.status_code if isinstance(exc, APIStatusError) else exc.response.status_code
        return status in {404, 405} or status >= 500
    return isinstance(exc, DiagnosticError) and exc.category == "catalog_response"


def _native_models(url: str, api_key: str, timeout: float) -> list[str]:
    """Native catalogs use output.models[].model and numbered pages."""
    values = []
    seen_pages = set()
    with CatalogClient(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}) as client:
        for page in range(1, 101):
            response = client.get(url, params={"page_no": page, "page_size": 100})
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                raise DiagnosticError("Invalid model catalog JSON", category="catalog_response") from None
            output = payload.get("output") if isinstance(payload, dict) else None
            models = output.get("models") if isinstance(output, dict) else None
            total = output.get("total") if isinstance(output, dict) else None
            if (not isinstance(payload, dict) or payload.get("success") is False or not isinstance(models, list)
                    or any(not isinstance(model, dict) for model in models)
                    or not isinstance(total, int) or isinstance(total, bool) or total < 0):
                raise DiagnosticError("Invalid native model catalog", category="catalog_response")
            ids = [model.get("model") for model in models]
            signature = tuple(_model_ids(ids))
            if len(values) < total and (not models or signature in seen_pages):
                raise DiagnosticError("Incomplete native model catalog", category="catalog_response")
            seen_pages.add(signature)
            values.extend(ids)
            if len(values) >= total:
                return _model_ids(values)
    raise DiagnosticError("Model catalog exceeded pagination limit", category="catalog_response")


def list_models(config: Config, api_key: str) -> list[str]:
    """Fetch model IDs without making completion requests or exposing credentials."""
    timeout = min(config.request_timeout, 15)
    try:
        with OpenAI(api_key=api_key, base_url=config.base_url, max_retries=0, timeout=timeout) as client:
            models = client.models.list()
            if not isinstance(models.data, list):
                raise DiagnosticError("Invalid compatible model catalog", category="catalog_response")
            return _model_ids(model.id for model in models)
    except Exception as exc:
        urls = _native_urls(config)
        if not urls or not _can_fallback(exc):
            raise
    for index, url in enumerate(urls):
        try:
            return _native_models(url, api_key, timeout)
        except Exception as exc:
            if index == len(urls) - 1 or not _can_fallback(exc):
                raise
    raise AssertionError("No model catalog endpoint")
