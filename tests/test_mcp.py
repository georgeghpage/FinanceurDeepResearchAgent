"""Exercise the actual MCP HTTP handshake and tool call without network access."""

import unittest
from unittest.mock import patch

import httpx
from mcp.server.fastmcp import FastMCP

from deepsearchagent.tools.tavily import TavilyMCPSearch
from deepsearchagent.diagnostics import failure_from_exception


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_handshake_http_failure_keeps_status_and_phase(self):
        secret = "PRIVATE-MCP-ERROR"
        real_client = httpx.AsyncClient

        def deny(request):
            return httpx.Response(401, json={"error": secret}, request=request)

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(deny), **kwargs)

        provider = TavilyMCPSearch("https://example.com/mcp", secret)
        with patch("deepsearchagent.tools.tavily.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(Exception) as raised:
                await provider._search("Question", 10)
        failure = failure_from_exception(raised.exception, provider="tavily", stage="search", operation=provider.operation)
        self.assertEqual(failure.operation, "initialize")
        self.assertEqual(failure.category, "authentication")
        self.assertEqual(failure.http_status, 401)
        self.assertNotIn(secret, failure.model_dump_json())

    async def test_real_mcp_transport_with_in_process_server(self):
        server = FastMCP("offline-tavily", stateless_http=True, json_response=True, log_level="ERROR")
        seen = {}

        @server.tool(name="tavily_search")
        def tavily_search(query: str, search_depth: str, max_results: int,
                          include_raw_content: bool, include_images: bool) -> dict:
            seen.update(query=query, search_depth=search_depth, max_results=max_results,
                        include_raw_content=include_raw_content, include_images=include_images)
            return {"results": [{"title": "Filing", "url": "https://example.com/filing",
                                 "raw_content": "Revenue was USD 10 million in FY2025."}]}

        app = server.streamable_http_app()
        real_client = httpx.AsyncClient

        def client_factory(**kwargs):
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer offline-key")
            return real_client(transport=httpx.ASGITransport(app=app), **kwargs)

        async with app.router.lifespan_context(app):
            with patch("deepsearchagent.tools.tavily.httpx.AsyncClient", side_effect=client_factory):
                result = await TavilyMCPSearch("http://localhost:8000/mcp", "offline-key")._search("FY2025 revenue", 10)

        self.assertEqual(seen["query"], "FY2025 revenue")
        self.assertEqual(seen["search_depth"], "advanced")
        self.assertEqual(seen["max_results"], 5)
        self.assertTrue(seen["include_raw_content"])
        self.assertFalse(seen["include_images"])
        self.assertEqual(result.sources[0].url, "https://example.com/filing")
        self.assertIn("USD 10 million", result.text)
