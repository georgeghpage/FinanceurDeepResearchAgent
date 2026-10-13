"""Real SDK HTTP serialization with controlled transport responses; never live proof."""

import json
import socket
import ssl
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from deepsearchagent.config import Config
from deepsearchagent.diagnostics import failure_from_exception
from deepsearchagent.judge import OpenAICompatibleJudge
from deepsearchagent.llm import KimiBackend, ResponseError
from deepsearchagent.models import EvidencePlan
from deepsearchagent.tools.fetch import parse_tavily_result
from deepsearchagent.tools.search import SearchError
from support_fixtures import FakeJudge, plan


def completion(content, *, finish="stop", usage=True):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        if usage
        else None,
    }


class ProviderContractTests(unittest.TestCase):
    def client(self, handler):
        client = OpenAI(
            api_key="CANARY-KEY",
            base_url="https://fixture.invalid/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(client.close)
        return client

    def test_kimi_and_aliyun_real_sdk_serialization_and_usage(self):
        for provider, model in [
            ("kimi", "kimi-k3"),
            ("kimi", "older-model"),
            ("aliyun", "qwen-plus"),
            ("aliyun", "qwen3.8-max"),
            ("aliyun", "qwen3.7-plus"),
        ]:
            with self.subTest(provider=provider, model=model):
                seen = []

                def respond(request):
                    seen.append(request)
                    return httpx.Response(
                        200, json=completion(plan().model_dump_json())
                    )

                backend = KimiBackend(
                    Config(provider=provider, model=model), "CANARY-KEY"
                )
                backend.client.close()
                backend.client = self.client(respond)
                result = backend.structured(
                    EvidencePlan, "Plan", '{"question":"中文"}', output_tokens=1234
                )
                request = seen[0]
                body = json.loads(request.content)
                self.assertEqual(request.headers["authorization"], "Bearer CANARY-KEY")
                self.assertEqual(
                    (body["model"], body["max_tokens"], body.get("response_format")),
                    (
                        model,
                        1234,
                        None if model == "older-model" else {"type": "json_object"},
                    ),
                )
                self.assertNotIn("tools", body)
                self.assertNotIn("CANARY-KEY", json.dumps(body))
                self.assertEqual(backend.usage_for_thread()["tokens"], 30)
                self.assertEqual(len(result.search_queries), 15)
                self.assertEqual(
                    body.get("reasoning_effort"), "low" if model == "kimi-k3" else None
                )
                self.assertEqual(
                    body.get("enable_thinking"), False if model == "qwen-plus" else None
                )

    def test_sdk_http_status_matrix_is_bounded_and_secret_safe(self):
        for status in (
            301,
            302,
            307,
            308,
            400,
            401,
            403,
            404,
            408,
            409,
            422,
            429,
            500,
            502,
            503,
            504,
        ):
            with self.subTest(status=status):
                seen = []

                def respond(request):
                    seen.append(request)
                    return httpx.Response(
                        status, json={"error": {"message": "CANARY-KEY"}}
                    )

                backend = KimiBackend(Config(), "offline")
                backend.client.close()
                backend.client = self.client(respond)
                with self.assertRaises(Exception):
                    backend.structured(EvidencePlan, "Plan", "{}")
                self.assertEqual(len(seen), 1)
                expected = (
                    "authentication"
                    if status in (401, 403)
                    else "rate_limit"
                    if status == 429
                    else "timeout"
                    if status == 408
                    else "invalid_request"
                    if status in (400, 404, 422)
                    else "provider_unavailable"
                    if status >= 500
                    else "http_error"
                )
                self.assertEqual(backend.last_failure.category, expected)
                self.assertEqual(backend.last_failure.http_status, status)
                self.assertNotIn("CANARY-KEY", backend.last_failure.model_dump_json())

    def test_transport_error_classes_are_preserved_without_secrets(self):
        cases = [
            (httpx.ConnectTimeout, "timeout"),
            (httpx.ReadTimeout, "timeout"),
            (httpx.WriteTimeout, "timeout"),
            (httpx.PoolTimeout, "timeout"),
            (httpx.ConnectError, "connection_error"),
            (httpx.ReadError, "connection_error"),
            (httpx.RemoteProtocolError, "protocol_error"),
            (httpx.ProxyError, "proxy_error"),
        ]
        for error, expected in cases:
            with self.subTest(error=error.__name__):
                exc = error("CANARY-KEY")
                failure = failure_from_exception(
                    exc, provider="tavily", stage="search", operation="connect"
                )
                self.assertEqual(failure.category, expected)
                self.assertNotIn("CANARY-KEY", failure.model_dump_json())
        self.assertEqual(
            failure_from_exception(
                ssl.SSLError("CANARY"),
                provider="tavily",
                stage="search",
                operation="tls",
            ).category,
            "tls_error",
        )
        self.assertEqual(
            failure_from_exception(
                socket.gaierror(-2, "CANARY"),
                provider="tavily",
                stage="search",
                operation="dns",
            ).category,
            "dns_error",
        )

    def test_valid_and_incomplete_pai_real_sdk_outputs(self):
        decision = FakeJudge().evaluate("q", [], {"findings": []}, {}).model_dump_json()
        for content, finish, valid in [
            (decision, "stop", True),
            (decision, "length", False),
            ("{", "stop", False),
            ("", "stop", False),
            ("{}", "stop", False),
        ]:
            with self.subTest(content=content[:5], finish=finish):
                captured = []

                def respond(request):
                    captured.append(json.loads(request.content))
                    return httpx.Response(200, json=completion(content, finish=finish))

                judge = OpenAICompatibleJudge(
                    Config(judge_provider="pai", judge_model="pai-judge"),
                    client=self.client(respond),
                )
                if valid:
                    self.assertEqual(judge.evaluate("q", [], {}, {}).decision, "READY")
                else:
                    with self.assertRaises((ValidationError, ResponseError)):
                        judge.evaluate("q", [], {}, {})
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["model"], "pai-judge")
                self.assertEqual(
                    captured[0]["response_format"], {"type": "json_object"}
                )
                self.assertNotIn("tools", captured[0])
                self.assertIn("untrusted data", captured[0]["messages"][0]["content"])
                self.assertEqual(judge.usage["tokens"], 30)

    def test_tavily_malformed_duplicates_and_multilingual_payloads(self):
        for text in [
            "中文 42 万元",
            "Русский 42 рубля",
            "한국어 42 원",
            "English &amp; mixed 中文",
        ]:
            entry = {"url": "https://example.com/doc", "raw_content": text * 9000}
            result = parse_tavily_result(
                NS(
                    isError=False,
                    structuredContent={
                        "results": [
                            None,
                            {},
                            entry,
                            entry,
                            {"url": "javascript:alert(1)", "content": "x"},
                        ]
                    },
                )
            )
            self.assertEqual(len(result.sources), 1)
            self.assertEqual(result.documents[0]["content"], entry["raw_content"])
            self.assertFalse(result.documents[0]["truncated"])
            self.assertEqual(result.sources[0].title, "example.com")
            self.assertIsNone(result.sources[0].published_at)
        for data in (
            NS(isError=True),
            NS(content=[NS(type="text", text="{")]),
            NS(content=[]),
        ):
            with self.assertRaises(SearchError):
                parse_tavily_result(data)
