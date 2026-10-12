"""Provider streaming, validation, usage accounting, and resource cleanup."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from pydantic import ValidationError

from deepsearchagent.config import Config
from deepsearchagent.llm import BudgetExceeded, KimiBackend, ResponseError
from deepsearchagent.models import ResearchPlan


def chunk(content=None, reasoning=None, finish=None, tokens=None, refusal=None):
    return NS(usage=NS(total_tokens=tokens) if tokens is not None else None,
              choices=[NS(index=0, finish_reason=finish,
                          delta=NS(content=content, reasoning_content=reasoning, refusal=refusal))])


class StreamingTests(unittest.TestCase):
    def backend(self, chunks):
        events = []
        backend = KimiBackend(Config(), "offline-key", trace=events.append)
        self.addCleanup(backend.client.close)
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter(chunks))
        backend.client.chat.completions.create = Mock(return_value=stream)
        return backend, stream, events

    def test_reasoning_and_json_stream_separately_with_usage_only_once(self):
        backend, stream, events = self.backend([
            chunk(reasoning="Plan "), chunk(reasoning="the search."),
            chunk(content='{"objective":"Test",'), chunk(content='"tasks":[]}', finish="stop", tokens=25),
            NS(usage=NS(total_tokens=25), choices=[]),
        ])
        result = backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(result.objective, "Test")
        self.assertEqual(backend.tokens_used, 25)
        self.assertEqual(backend.calls, 1)
        self.assertEqual("".join(e["text"] for e in events if e["kind"] == "thinking_delta"), "Plan the search.")
        self.assertEqual("".join(e["text"] for e in events if e["kind"] == "output_delta"), result.model_dump_json())
        self.assertEqual(events[0]["kind"], "llm_start")
        self.assertEqual(events[-1]["kind"], "llm_complete")
        self.assertTrue(all(e["call_id"] == 1 for e in events))
        sent = backend.client.chat.completions.create.call_args.kwargs
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        stream.close.assert_called_once()

    def test_stream_without_reasoning_is_supported(self):
        backend, stream, events = self.backend([
            chunk(content='{"objective":"Test","tasks":[]}', finish="stop", tokens=12)])
        backend.structured(ResearchPlan, "Plan", "Question")
        self.assertNotIn("thinking_delta", [e["kind"] for e in events])
        self.assertEqual(backend.tokens_used, 12)
        stream.close.assert_called_once()

    def test_truncated_missing_finish_refusal_and_invalid_json_are_rejected(self):
        for chunks, error in [
            ([chunk(content='{"objective":', finish="length", tokens=9)], ResponseError),
            ([chunk(content='{"objective":"Test","tasks":[]}')], ResponseError),
            ([chunk(content='{"objective":"Test","tasks":[]}', refusal="No", finish="stop")], ResponseError),
            ([chunk(content='{"wrong":true}', finish="stop")], ValidationError),
        ]:
            with self.subTest(chunks=chunks):
                backend, stream, events = self.backend(chunks)
                with self.assertRaises(error):
                    backend.structured(ResearchPlan, "Plan", "Question")
                self.assertEqual(events[-1]["kind"], "llm_error")
                stream.close.assert_called_once()

    def test_interrupted_stream_keeps_observed_usage_and_sanitizes_error(self):
        def interrupted():
            yield chunk(reasoning="Checking evidence", tokens=8)
            raise RuntimeError("SECRET-PROVIDER-ERROR")

        backend, stream, events = self.backend(interrupted())
        with self.assertRaises(RuntimeError):
            backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(backend.tokens_used, 8)
        self.assertNotIn("SECRET-PROVIDER-ERROR", str(events))
        self.assertEqual(events[-1]["kind"], "llm_error")
        stream.close.assert_called_once()

    def test_total_deadline_is_checked_during_streaming(self):
        backend, stream, events = self.backend([])

        def expired():
            backend.started -= backend.config.max_seconds
            yield chunk(reasoning="Late reasoning", tokens=8)

        stream.__iter__.return_value = expired()
        with self.assertRaises(BudgetExceeded):
            backend.structured(ResearchPlan, "Plan", "Question")
        self.assertEqual(backend.tokens_used, 8)
        self.assertEqual(events[-1]["kind"], "llm_error")
        stream.close.assert_called_once()

class AliyunStreamingTests(unittest.TestCase):
    def test_selected_endpoint_model_json_mode_and_provider_metadata(self):
        from deepsearchagent.llm import AliyunBackend
        events = []
        backend = AliyunBackend(Config(provider="aliyun", model="qwen-plus"), "offline-key", trace=events.append)
        self.addCleanup(backend.client.close)
        self.assertEqual(str(backend.client.base_url).rstrip("/"), backend.config.aliyun_base_url)
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([
            chunk(content='{"objective":"Test","tasks":[]}', finish="stop", tokens=12)]))
        backend.client.chat.completions.create = Mock(return_value=stream)
        result = backend.structured(ResearchPlan, "Plan", "Question")
        sent = backend.client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["model"], "qwen-plus")
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["extra_body"], {"enable_thinking": False})
        self.assertNotIn("reasoning_effort", sent)
        self.assertEqual(result.objective, "Test")
        self.assertEqual(backend.tokens_used, 12)
        self.assertEqual(events[0]["provider"], "aliyun")
        stream.close.assert_called_once()

    def test_aliyun_hosted_kimi_does_not_receive_moonshot_options_or_prices(self):
        from deepsearchagent.pricing import pricing_metadata
        backend = KimiBackend(Config(provider="aliyun", model="kimi-k3"), "offline-key")
        self.addCleanup(backend.client.close)
        self.assertNotIn("reasoning_effort", backend._options())
        self.assertIsNone(pricing_metadata(backend.config)["input_price_per_million"])
