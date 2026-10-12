"""An explicit extension point for documentation search, with no assumed vendor."""

from collections.abc import Callable

from ..models import SearchResult


class DocumentationSearch:
    """Wrap a local index, REST client, or MCP tool in the common search contract.

    The callback accepts (query, timeout_seconds) and returns SearchResult with
    retrieved source text and authoritative source URLs.
    """

    def __init__(self, callback: Callable[[str, float], SearchResult]) -> None:
        self.callback = callback

    def search(self, query: str, *, timeout: float) -> SearchResult:
        return self.callback(query, timeout)
