"""Normalize Tavily MCP's JSON or text results into evidence with source URLs."""

import json
import re
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..models import SearchResult, Source
from ..provider_errors import provider_error_category
from .search import SearchError

MAX_CONTENT_CHARS = 8_000


def _normalize(entries: list[dict]) -> SearchResult:
    valid = [entry for entry in entries if isinstance(entry, dict)]
    if entries and not valid:
        raise SearchError("Tavily returned malformed result entries.", category="result_format")
    entries = valid
    sources: dict[str, Source] = {}
    documents: list[dict] = []
    for entry in entries[:5]:
        url = entry.get("url", "")
        content = entry.get("raw_content") or entry.get("content") or ""
        if not isinstance(url, str) or not isinstance(content, str) or not content.strip():
            continue
        try:
            source = Source(
                id="", url=url, title=entry.get("title") or urlsplit(url).hostname or url,
                published_at=entry.get("published_date"),
            )
        except (ValueError, ValidationError):
            continue
        if url in sources:
            continue
        sources[url] = source
        documents.append({
            "url": url, "title": source.title, "published_at": source.published_at,
            "snippet": str(entry.get("content") or "")[:MAX_CONTENT_CHARS],
            "content": content, "truncated": False,
            "metadata": {key: entry[key] for key in ("score", "published_date") if key in entry},
        })
    return SearchResult(text=json.dumps(documents, ensure_ascii=False), sources=list(sources.values()), documents=documents)


def parse_tavily_result(result: object) -> SearchResult:
    structured = getattr(result, "structuredContent", None)
    error_payload = None
    if isinstance(structured, dict) and (structured.get("error") or structured.get("success") is False):
        error_payload = structured
    if getattr(result, "isError", False):
        error_payload = structured or " ".join(str(getattr(b, "text", "")) for b in getattr(result, "content", []))
    if error_payload is not None:
        category = provider_error_category(error_payload) or "tool_error"
        raise SearchError("Tavily MCP returned a tool error.", category=category)
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        return _normalize(structured["results"])

    entries: list[dict] = []
    recognized = False
    for block in getattr(result, "content", []):
        if getattr(block, "type", None) != "text":
            continue
        text = block.text
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and (data.get("error") or data.get("success") is False):
            raise SearchError("Tavily MCP returned a tool error.", category=provider_error_category(data) or "tool_error")
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            recognized = True
            entries.extend(data["results"])
            continue
        # Tavily's official server currently returns formatted text as well as JSON
        # in some deployments. Only parse the detailed-result section's fields.
        if "Detailed Results:" not in text:
            continue
        recognized = True
        body = text.split("Detailed Results:", 1)[1].split("\nImages:", 1)[0].strip()
        for chunk in re.split(r"\n\n(?=(?:Title:|ID:|URL:))", body):
            match = re.search(r"(?m)^URL:\s*(https?://\S+)\s*$", chunk)
            if match is None:
                continue
            title = re.search(r"(?m)^Title:\s*(.+)$", chunk)
            raw = re.search(r"(?ms)^Raw Content:\s*(.*)", chunk)
            snippet = re.search(r"(?ms)^Content:\s*(.*)", chunk)
            entries.append({
                "url": match.group(1), "title": title.group(1) if title else "",
                "content": raw.group(1) if raw else snippet.group(1) if snippet else "",
            })
    if not recognized:
        raise SearchError("Tavily MCP returned an unsupported search-result format.", category="result_format")
    return _normalize(entries)
