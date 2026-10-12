"""Account catalogs use the configured endpoint and only make model-list calls."""

import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from deepsearchagent.config import Config
from deepsearchagent.diagnostics import DiagnosticError
from deepsearchagent.model_catalog import _native_urls, list_models


class ModelCatalogTests(unittest.TestCase):
    def test_kimi_catalog_uses_selected_endpoint_and_account_key(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "kimi-k3", "object": "model"},
                {"id": "account-kimi", "object": "model"},
            ]})

        for base_url in ("https://api.moonshot.cn/v1", "https://api.moonshot.ai/v1"):
            config = Config(provider="kimi", kimi_base_url=base_url)
            client = OpenAI(api_key="offline-kimi", base_url=base_url,
                            http_client=httpx.Client(transport=httpx.MockTransport(respond)))
            with patch("deepsearchagent.model_catalog.OpenAI", return_value=client), \
                    patch("deepsearchagent.model_catalog.CatalogClient") as native:
                self.assertEqual(list_models(config, "offline-kimi"), ["account-kimi", "kimi-k3"])
                native.assert_not_called()
            self.assertEqual(str(requests[-1].url), base_url + "/models")
            self.assertEqual(requests[-1].headers["Authorization"], "Bearer offline-kimi")
            self.assertEqual(requests[-1].method, "GET")
            self.assertTrue(client.is_closed())
        self.assertEqual(len(requests), 2)

    def test_models_are_fetched_with_account_key_from_selected_endpoint(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "qwen3.8-max", "object": "model"},
                {"id": "kimi/kimi-k3", "object": "model"},
                {"id": "qwen3.8-max", "object": "model"},
                {"id": "bad model", "object": "model"},
                {"id": "", "object": "model"},
            ]})
        client = OpenAI(api_key="offline-account-key", base_url=Config.aliyun_base_url,
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        with patch("deepsearchagent.model_catalog.OpenAI", return_value=client) as factory:
            models = list_models(Config(provider="aliyun", model="qwen-plus"), "offline-account-key")
        self.assertEqual(models, ["kimi/kimi-k3", "qwen3.8-max"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(str(requests[0].url), "https://maas.qianwenaiapi.com/compatible-mode/v1/models")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer offline-account-key")
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["timeout"], 15)
        self.assertTrue(client.is_closed())

    def test_empty_catalog_stays_empty_and_custom_endpoint_is_used(self):
        client = OpenAI(api_key="offline", base_url="https://custom.example/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json={"object": "list", "data": []}))))
        config = Config(provider="aliyun", aliyun_base_url="https://custom.example/v1")
        with patch("deepsearchagent.model_catalog.OpenAI", return_value=client) as factory:
            self.assertEqual(list_models(config, "offline"), [])
        self.assertEqual(factory.call_args.kwargs["base_url"], config.aliyun_base_url)

    def test_unavailable_compatible_route_falls_back_and_fetches_all_native_pages(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/compatible-mode/v1/models":
                return httpx.Response(404, json={"error": {"message": "unsupported route"}})
            page = int(request.url.params["page_no"])
            return httpx.Response(200, json={"success": True, "output": {
                "total": 3, "models": [{"model": "qwen-plus"}, {"model": "bad model"}]
                if page == 1 else [{"model": "kimi/kimi-k3"}]}})

        client = OpenAI(api_key="offline", base_url=Config.aliyun_base_url,
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        native_client = httpx.Client(transport=httpx.MockTransport(respond),
                                    headers={"Authorization": "Bearer offline"})
        with patch("deepsearchagent.model_catalog.OpenAI", return_value=client), \
                patch("deepsearchagent.model_catalog.CatalogClient", return_value=native_client):
            self.assertEqual(list_models(Config(provider="aliyun"), "offline"), ["kimi/kimi-k3", "qwen-plus"])
        self.assertEqual([request.url.path for request in requests],
                         ["/compatible-mode/v1/models", "/api/v1/models", "/api/v1/models"])
        self.assertEqual([request.url.params["page_no"] for request in requests[1:]], ["1", "2"])
        self.assertTrue(all(request.headers["Authorization"] == "Bearer offline" for request in requests))
        self.assertTrue(client.is_closed())
        self.assertTrue(native_client.is_closed)

    def test_connection_failure_tries_same_region_alternate_host(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.host == "maas.qianwenaiapi.com":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"output": {"total": 1, "models": [{"model": "qwen-plus"}]}})

        client = OpenAI(api_key="offline", base_url=Config.aliyun_base_url, max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        original_client = httpx.Client
        with patch("deepsearchagent.model_catalog.OpenAI", return_value=client), \
                patch("deepsearchagent.model_catalog.CatalogClient",
                      side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs)):
            self.assertEqual(list_models(Config(provider="aliyun"), "offline"), ["qwen-plus"])
        self.assertEqual([request.url.host for request in requests],
                         ["maas.qianwenaiapi.com", "maas.qianwenaiapi.com", "dashscope.aliyuncs.com"])

    def test_authentication_and_quota_failures_do_not_try_other_endpoints(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                client = OpenAI(api_key="offline", base_url=Config.aliyun_base_url, max_retries=0,
                                http_client=httpx.Client(transport=httpx.MockTransport(
                                    lambda request: httpx.Response(status, json={"error": {"message": "denied"}}))))
                with patch("deepsearchagent.model_catalog.OpenAI", return_value=client), \
                        patch("deepsearchagent.model_catalog.CatalogClient") as native:
                    with self.assertRaises(Exception) as raised:
                        list_models(Config(provider="aliyun"), "offline")
                    self.assertEqual(raised.exception.status_code, status)
                    native.assert_not_called()

    def test_fallback_preserves_workspace_region_and_custom_hosts(self):
        for host in ("workspace.cn-beijing.maas.aliyuncs.com", "workspace.ap-southeast-1.maas.aliyuncs.com",
                     "workspace.eu-central-1.maas.aliyuncs.com", "dashscope-intl.aliyuncs.com"):
            config = Config(provider="aliyun", aliyun_base_url=f"https://{host}/compatible-mode/v1/")
            self.assertEqual(_native_urls(config), [f"https://{host}/api/v1/models"])
        self.assertEqual(_native_urls(Config(provider="aliyun", aliyun_base_url="https://custom.example/v1")), [])
        self.assertEqual(_native_urls(Config(provider="kimi")), [])

    def test_native_invalid_or_repeated_pages_are_errors_not_empty_catalogs(self):
        for payload in ([], {"output": {}}, {"success": False, "output": {"total": 0, "models": []}},
                        {"output": {"total": 2, "models": [{"model": "qwen-plus"}]}}):
            with self.subTest(payload=payload):
                config = Config(provider="aliyun", aliyun_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
                client = OpenAI(api_key="offline", base_url=config.base_url, max_retries=0,
                                http_client=httpx.Client(transport=httpx.MockTransport(
                                    lambda request: httpx.Response(404, json={"error": {"message": "missing"}}))))
                native_client = httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload)))
                with patch("deepsearchagent.model_catalog.OpenAI", return_value=client), \
                        patch("deepsearchagent.model_catalog.CatalogClient", return_value=native_client):
                    with self.assertRaises(DiagnosticError) as raised:
                        list_models(config, "offline")
                    self.assertEqual(raised.exception.category, "catalog_response")
