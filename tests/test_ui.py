"""Offline HTTP integration checks for the UI and its live progress stream."""

import http.client
import json
import os
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from deepsearchagent.models import ResearchPlan, ResearchState
from deepsearchagent.config import Config
from deepsearchagent.llm import KimiBackend, ResponseError
from deepsearchagent.ui import ResearchRun, ResearchServer
from deepsearchagent.orchestrator import SequentialResearchAgent
from test_agent import FakeSearch, ScriptedBackend, TASK, draft, extraction, verification
from test_streaming import chunk


class RunIssueTests(unittest.TestCase):
    def test_search_recovery_preserves_cause_and_only_updates_its_own_invocation(self):
        run = ResearchRun(str(uuid4()), "Facts")
        diagnostic = {"provider": "tavily", "stage": "search", "operation": "search",
                      "category": "timeout", "message": "Provider request timed out."}
        for search_id in (1, 2):
            run.emit({"type": "trace", "kind": "search_error", "search_id": search_id,
                      "invocation_id": f"search-{search_id}", "diagnostic": diagnostic})
        run.emit({"type": "trace", "kind": "search_recovered", "invocation_id": "search-1"})
        self.assertTrue(run.issues[0]["recovered"])
        self.assertFalse(run.issues[1]["recovered"])
        self.assertEqual(run.issues[0]["diagnostic"]["category"], "timeout")
        run.emit({"type": "trace", "kind": "planning_recovered", "schema": "PlanningBrief",
                  "diagnostic": diagnostic, "recovered": True, "recovery": "Using a compact plan."})
        self.assertEqual(run.issues[-1]["recovery"], "Using a compact plan.")


class UITests(unittest.TestCase):
    def test_failed_verification_reaches_ui_and_persistent_log_with_provider_and_stage(self):
        backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), ResponseError("PRIVATE-ERROR", category="output_limit"))
        with tempfile.TemporaryDirectory() as directory:
            self.server.log_dir = Path(directory)
            with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                    patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                    patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
                response = self.post({"question": "Find revenue"})
                events = [json.loads(line) for line in response.read().splitlines()]
            result = events[-1]
            self.assertEqual(result["type"], "result")
            self.assertEqual((result["failure"]["provider"], result["failure"]["stage"]), ("kimi", "verify"))
            self.assertEqual(result["failure"]["category"], "output_limit")
            self.assertEqual(result["sources"], 1)
            self.assertIn("verify", result["stop_explanation"])
            self.assertIn("output_limit", [issue["diagnostic"]["category"] for issue in result["issues"]])
            self.assertTrue(result["warnings"])
            text = (Path(directory) / f'{result["run_id"]}.jsonl').read_text()
            self.assertNotIn("PRIVATE-ERROR", text)
            self.assertNotIn("Revenue was", text)
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(records[-1]["failure"], result["failure"])
            self.assertEqual(records[-1]["stop_reason"], "error")
            self.assertIn("run_stopped", [record.get("kind") for record in records])

    def setUp(self):
        # Keep transport/history regression scenarios on the compatibility workflow.
        # The new default is covered by test_multi_agent.py, including CLI and HTTP.
        compat = patch("deepsearchagent.ui.ResearchAgent", SequentialResearchAgent)
        compat.start()
        self.addCleanup(compat.stop)
        patcher = patch("deepsearchagent.config.dotenv_values", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ResearchServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, payload, headers=None):
        self.connection.request("POST", "/api/research", json.dumps(payload), headers or {})
        return self.connection.getresponse()

    def test_assets_and_unknown_paths(self):
        for path, content_type in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            self.connection.request("GET", path)
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(content_type, response.getheader("Content-Type"))
            self.assertTrue(response.read())
        self.connection.request("GET", "/../config.py")
        response = self.connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_invalid_questions_do_not_start_research(self):
        for payload in ({}, {"question": "  "}, {"question": 12}, [], {"question": "x" * 8001}):
            response = self.post(payload)
            self.assertEqual(response.status, 400)
            self.assertIn("research question", json.loads(response.read())["error"])

    def test_missing_credentials_shown_without_starting(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.post({"question": "Find revenue"})
            self.assertEqual(response.status, 400)
            self.assertIn("ALIYUN_API_KEY", json.loads(response.read())["error"])
        self.assertFalse(self.server.run_lock.locked())

    def test_status_reads_current_credentials_without_exposing_them(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("deepsearchagent.config.dotenv_values", return_value={}) as settings, \
                patch("deepsearchagent.ui.KimiBackend") as backend:
            self.connection.request("GET", "/api/status")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            status = json.loads(response.read())
            self.assertFalse(status["configured"])
            self.assertEqual(status["missing_keys"], ["ALIYUN_API_KEY", "TAVILY_API_KEY"])
            settings.return_value = {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "secret-kimi", "TAVILY_API_KEY": "secret-tavily"}
            self.connection.request("GET", "/api/status")
            response = self.connection.getresponse()
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(body)["configured"])
            self.assertNotIn("secret", body)
            backend.assert_not_called()

    def test_status_rejects_foreign_host(self):
        self.connection.request("GET", "/api/status", headers={"Host": "foreign.example"})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()

    def test_foreign_origin_and_host_are_rejected(self):
        for headers in ({"Origin": "https://foreign.example"}, {"Host": "foreign.example"}):
            response = self.post({"question": "Find revenue"}, headers)
            self.assertEqual(response.status, 403)
            response.read()

    def test_overlapping_run_is_rejected(self):
        self.server.run_lock.acquire()
        try:
            response = self.post({"question": "Find revenue"})
            self.assertEqual(response.status, 409)
            response.read()
        finally:
            self.server.run_lock.release()

    def test_actual_graph_streams_before_completion_and_returns_citations(self):
        started, release = threading.Event(), threading.Event()

        class BlockingBackend(ScriptedBackend):
            def structured(self, *args):
                if not self.calls:
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("Test did not release backend")
                return super().structured(*args)

        backend = BlockingBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), verification(), draft())
        backend.client = unittest.mock.Mock()
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            try:
                response = self.post({"question": "Find revenue"})
                self.assertEqual(response.status, 200)
                self.assertIn("application/x-ndjson", response.getheader("Content-Type"))
                progress = []
                while len(progress) < 2:
                    event = json.loads(response.readline())
                    if event["type"] == "progress":
                        progress.append(event)
                first, second = progress
                self.assertEqual(first["message"], "Starting research")
                self.assertEqual(second["message"], "Planning research")
                self.assertTrue(started.wait(2))
            finally:
                release.set()
            events = [json.loads(line) for line in response.read().splitlines()]
        self.assertIn("Checking evidence and coverage", [event.get("message") for event in events])
        self.assertIn("Writing cited report", [event.get("message") for event in events])
        kinds = [event.get("kind") for event in events]
        self.assertIn("plan", kinds)
        self.assertIn("search_start", kinds)
        self.assertIn("search_complete", kinds)
        self.assertIn("findings", kinds)
        self.assertIn("verification", kinds)
        search_event = next(event for event in events if event.get("kind") == "search_start")
        self.assertEqual(search_event["query"], TASK.query)
        source_event = next(event for event in events if event.get("kind") == "search_complete")
        self.assertEqual(source_event["sources"][0]["url"], "https://example.com/filing")
        result = events[-1]
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["stop_reason"], "coverage")
        self.assertEqual(result["sources"], 1)
        self.assertIn("[S1: Company filing](<https://example.com/filing>)", result["report"])
        self.assertFalse(self.server.run_lock.locked())
        backend.client.close.assert_called_once()

    def test_model_reasoning_reaches_http_client_while_call_is_still_running(self):
        release = threading.Event()
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), "offline-key")
        self.addCleanup(backend.client.close)
        results = iter([ResearchPlan(objective="Find revenue", tasks=[TASK]),
                        extraction(), verification(), draft()])

        def create(**kwargs):
            result = next(results)

            def chunks():
                yield chunk(reasoning="Checking revenue sources.")
                if isinstance(result, ResearchPlan) and not release.wait(5):
                    raise RuntimeError("Test did not release model stream")
                yield chunk(content=result.model_dump_json(), finish="stop")
                yield chunk(tokens=100)

            stream = unittest.mock.Mock()
            stream.__iter__ = unittest.mock.Mock(return_value=chunks())
            return stream

        backend.client.chat.completions.create = create
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Find revenue"})
            early = []
            try:
                while not early or early[-1].get("kind") != "thinking_delta":
                    early.append(json.loads(response.readline()))
                self.assertEqual(early[-1]["kind"], "thinking_delta")
                self.assertEqual(early[-1]["text"], "Checking revenue sources.")
                self.assertFalse(release.is_set())
            finally:
                release.set()
            events = early + [json.loads(line) for line in response.read().splitlines()]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["tokens_used"], 400)
        self.assertEqual(events[-1]["api_calls"], 4)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(event.get("kind") == "llm_complete" for event in events), 4)

    def test_unexpected_errors_are_sanitized_and_unlock_server(self):
        backend = ScriptedBackend(RuntimeError("SECRET-API-KEY"))
        backend.client = unittest.mock.Mock()
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Find revenue"})
            body = response.read().decode()
        self.assertNotIn("SECRET-API-KEY", body)
        result = json.loads(body.splitlines()[-1])
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["stop_reason"], "error")
        self.assertEqual(result["failure"]["category"], "unexpected_error")
        self.assertFalse(self.server.run_lock.locked())

    def test_partial_report_is_returned_after_provider_failure(self):
        backend = unittest.mock.Mock()
        state = ResearchState(question="Find revenue", stop_reason="error")
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.ResearchAgent") as agent:
            agent.return_value.run.return_value = (state, "# Partial report\n\n## Limitations\n")
            response = self.post({"question": "Find revenue"})
            events = [json.loads(line) for line in response.read().splitlines()]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["stop_reason"], "error")
        self.assertIn("Partial report", events[-1]["report"])

    def test_initialization_errors_are_sanitized_and_unlock_server(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", side_effect=RuntimeError("SECRET")):
            response = self.post({"question": "Find revenue"})
            self.assertEqual(response.status, 500)
            self.assertNotIn("SECRET", response.read().decode())
        self.assertFalse(self.server.run_lock.locked())

    def test_disconnect_does_not_cancel_research_and_cursor_replays_final_output(self):
        release = threading.Event()

        class BlockingBackend(ScriptedBackend):
            def structured(self, *args):
                if not self.calls and not release.wait(5):
                    raise RuntimeError("Test did not release backend")
                return super().structured(*args)

        backend = BlockingBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), verification(), draft())
        backend.client = unittest.mock.Mock()
        run_id = str(uuid4())
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend) as factory, \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Find revenue", "run_id": run_id})
            try:
                early = [json.loads(response.readline()), json.loads(response.readline())]
                self.assertEqual(early[-1]["sequence"], 2)
                response.close()
                self.connection.close()
                self.assertTrue(self.server.run_lock.locked())
            finally:
                release.set()
            resumed = http.client.HTTPConnection(*self.server.server_address, timeout=5)
            try:
                resumed.request("GET", f"/api/research/{run_id}?after=2")
                response = resumed.getresponse()
                self.assertEqual(response.status, 200)
                events = [json.loads(line) for line in response.read().splitlines()]
            finally:
                resumed.close()
        self.assertEqual(events[0]["sequence"], 3)
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["stop_reason"], "coverage")
        self.assertIn("Company filing", events[-1]["report"])
        self.assertEqual(backend.calls, 4)
        factory.assert_called_once()
        backend.client.close.assert_called_once()
        self.assertFalse(self.server.run_lock.locked())

    def test_repeated_post_is_idempotent_and_question_mismatch_is_rejected(self):
        backend = ScriptedBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), verification(), draft())
        backend.client = unittest.mock.Mock()
        payload = {"question": "Find revenue", "run_id": str(uuid4())}
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend) as factory, \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            first = self.post(payload).read()
            second = self.post(payload).read()
            self.assertEqual(first, second)
            response = self.post({**payload, "question": "A different question"})
            self.assertEqual(response.status, 409)
            response.read()
        factory.assert_called_once()
        self.assertEqual(backend.calls, 4)

    def test_repeated_post_while_running_attaches_to_the_original_run(self):
        release = threading.Event()

        class BlockingBackend(ScriptedBackend):
            def structured(self, *args):
                if not self.calls and not release.wait(5):
                    raise RuntimeError("Test did not release backend")
                return super().structured(*args)

        backend = BlockingBackend(ResearchPlan(objective="Find revenue", tasks=[TASK]),
                                  extraction(), verification(), draft())
        backend.client = unittest.mock.Mock()
        payload = {"question": "Find revenue", "run_id": str(uuid4())}
        attached = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend) as factory, \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            first = self.post(payload)
            try:
                first_event = json.loads(first.readline())
                attached.request("POST", "/api/research", json.dumps(payload))
                second = attached.getresponse()
                self.assertEqual(second.status, 200)
                self.assertEqual(json.loads(second.readline()), first_event)
                self.assertTrue(self.server.run_lock.locked())
            finally:
                release.set()
            first_rest = first.read()
            second_rest = second.read()
            attached.close()
        self.assertEqual(first_rest, second_rest)
        self.assertEqual(backend.calls, 4)
        factory.assert_called_once()

    def test_cleanup_failure_does_not_lose_output_or_leave_server_busy(self):
        backend = ScriptedBackend(ResearchPlan(objective="Nothing to research", tasks=[]))
        backend.client = unittest.mock.Mock()
        backend.client.close.side_effect = RuntimeError("SECRET-CLEANUP-ERROR")
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            body = self.post({"question": "Find revenue"}).read().decode()
        self.assertEqual(json.loads(body.splitlines()[-1])["type"], "result")
        self.assertNotIn("SECRET-CLEANUP-ERROR", body)
        self.assertFalse(self.server.run_lock.locked())

    def test_resume_rejects_unknown_run_invalid_cursor_and_foreign_origin(self):
        run = ResearchRun(str(uuid4()), "Question")
        run.emit({"type": "progress", "message": "Planning"})
        run.done = True
        self.server.runs[run.id] = run
        for path, status, headers in [
            ("/api/research/unknown", 404, {}),
            (f"/api/research/{run.id}?after=-1", 400, {}),
            (f"/api/research/{run.id}?after=2", 400, {}),
            (f"/api/research/{run.id}?after=abc", 400, {}),
            (f"/api/research/{run.id}?after=", 400, {}),
            (f"/api/research/{run.id}", 403, {"Origin": "https://foreign.example"}),
        ]:
            self.connection.request("GET", path, headers=headers)
            response = self.connection.getresponse()
            self.assertEqual(response.status, status)
            response.read()

    def test_invalid_run_ids_do_not_initialize_backend(self):
        with patch("deepsearchagent.ui.KimiBackend") as factory:
            for run_id in (None, [], {}, 123, "not-a-uuid"):
                response = self.post({"question": "Find revenue", "run_id": run_id})
                self.assertEqual(response.status, 400)
                response.read()
            factory.assert_not_called()

    def test_long_wait_sends_heartbeat_without_consuming_trace_sequence(self):
        run = ResearchRun(str(uuid4()), "Question")
        self.server.runs[run.id] = run
        with patch("deepsearchagent.ui.HEARTBEAT_SECONDS", 0.02):
            self.connection.request("GET", f"/api/research/{run.id}")
            response = self.connection.getresponse()
            try:
                self.assertEqual(json.loads(response.readline()), {"type": "heartbeat"})
                self.assertEqual(run.events, [])
                run.emit({"type": "result", "report": "Ready"})
            finally:
                with run.condition:
                    run.done = True
                    run.condition.notify_all()
            result = json.loads(response.readline())
            self.assertEqual(result["type"], "result")
            self.assertEqual(result["sequence"], 1)
            self.assertEqual(response.read(), b"")

    def test_recent_completed_traces_are_retained_with_bounded_history(self):
        from deepsearchagent.ui import RETAINED_RUNS

        old_ids = []
        for _ in range(RETAINED_RUNS):
            run = ResearchRun(str(uuid4()), "Old question")
            run.done = True
            self.server.runs[run.id] = run
            old_ids.append(run.id)
        backend = ScriptedBackend(ResearchPlan(objective="Nothing to research", tasks=[]))
        backend.client = unittest.mock.Mock()
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Find revenue"})
            response.read()
        self.assertEqual(len(self.server.runs), RETAINED_RUNS)
        self.assertNotIn(old_ids[0], self.server.runs)
        self.assertIn(old_ids[-1], self.server.runs)

    def test_failed_stream_retains_reasoning_partial_json_and_partial_report(self):
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), "offline-key")
        self.addCleanup(backend.client.close)
        stream = unittest.mock.Mock()
        stream.__iter__ = unittest.mock.Mock(return_value=iter([
            chunk(reasoning="Check the filing."),
            chunk(content='{"objective":', finish="length", tokens=18),
        ]))
        backend.client.chat.completions.create = unittest.mock.Mock(return_value=stream)
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Find revenue"})
            events = [json.loads(line) for line in response.read().splitlines()]
        self.assertIn("thinking_delta", [event.get("kind") for event in events])
        self.assertIn("output_delta", [event.get("kind") for event in events])
        self.assertIn("llm_error", [event.get("kind") for event in events])
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["stop_reason"], "error")
        self.assertEqual(events[-1]["tokens_used"], 18)
        self.assertIn("Limitations", events[-1]["report"])
        stream.close.assert_called_once()

    def test_aliyun_selection_uses_its_own_key_and_records_the_model(self):
        backend = ScriptedBackend(ResearchPlan(objective="Nothing to research", tasks=[]))
        backend.client = unittest.mock.Mock()
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "aliyun-key", "TAVILY_API_KEY": "offline"}, clear=True), \
                patch("deepsearchagent.ui.AliyunBackend", return_value=backend) as factory, \
                patch("deepsearchagent.ui.KimiBackend") as kimi, \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            events = [json.loads(line) for line in self.post({
                "question": "Revenue", "provider": "aliyun", "model": "qwen-custom"}).read().splitlines()]
            config, key = factory.call_args.args
            self.assertEqual((config.provider, config.model, key), ("aliyun", "qwen-custom", "aliyun-key"))
            kimi.assert_not_called()
            self.assertEqual(events[0]["provider"], "aliyun")
            self.assertEqual(events[0]["model"], "qwen-custom")
            self.assertIsNone(events[0]["pricing"]["input_price_per_million"])
            stages = [event for event in events if event.get("kind") == "stage_start"]
            self.assertTrue(all(event["provider"] == "aliyun" for event in stages))

    def test_provider_model_validation_and_retry_conflict(self):
        with patch("deepsearchagent.ui.KimiBackend") as factory, patch("deepsearchagent.ui.AliyunBackend") as aliyun:
            for selection in ({"provider": []}, {"provider": "unknown"}, {"model": 7},
                              {"model": ""}, {"model": "bad model"}):
                response = self.post({"question": "Revenue", **selection})
                self.assertEqual(response.status, 400)
                response.read()
            factory.assert_not_called()
            aliyun.assert_not_called()
        run = ResearchRun(str(uuid4()), "Revenue")
        run.config = Config(model="kimi-custom")
        run.done = True
        self.server.runs[run.id] = run
        response = self.post({"question": "Revenue", "run_id": run.id, "model": "different"})
        self.assertEqual(response.status, 409)
        response.read()

    def test_stop_interrupts_reasoning_releases_server_and_replays_final_result(self):
        release = threading.Event()
        entered = threading.Event()
        exited = threading.Event()
        backend = KimiBackend(Config(provider="kimi", model="kimi-k3"), "offline-key")
        backend.client.close = unittest.mock.Mock(wraps=backend.client.close)
        stream = unittest.mock.Mock()
        def chunks():
            yield chunk(reasoning="Checking sources", tokens=8)
            entered.set()
            try:
                release.wait(5)
                exited.set()
                yield chunk(content='{"objective":"Late","tasks":[]}', finish="stop")
            finally:
                exited.set()
        stream.__iter__ = unittest.mock.Mock(return_value=chunks())
        stream.close.side_effect = release.set
        backend.client.chat.completions.create = unittest.mock.Mock(return_value=stream)
        run_id = str(uuid4())
        stopper = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        self.addCleanup(stopper.close)
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "offline", "TAVILY_API_KEY": "offline"}), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            response = self.post({"question": "Revenue", "run_id": run_id})
            early = []
            try:
                while not early or early[-1].get("kind") != "thinking_delta":
                    early.append(json.loads(response.readline()))
                self.assertTrue(entered.wait(2))
                for _ in range(2):
                    stopper.request("POST", f"/api/research/{run_id}/stop")
                    stopped = stopper.getresponse()
                    self.assertEqual(stopped.status, 200)
                    self.assertIn(json.loads(stopped.read())["status"], {"stopping", "finished"})
                events = early + [json.loads(line) for line in response.read().splitlines()]
            finally:
                release.set()
            self.assertEqual(events[-1]["type"], "result")
            self.assertEqual(events[-1]["stop_reason"], "cancelled")
            self.assertEqual(events[-1]["tokens_used"], 8)
            self.assertIsNone(events[-1]["failure"])
            self.assertFalse(self.server.run_lock.locked())
            self.assertTrue(self.server.runs[run_id].done)
            self.assertTrue(exited.wait(1))
            self.assertEqual(sum(e.get("kind") == "cancellation_requested" for e in events), 1)
            self.assertEqual(backend.client.chat.completions.create.call_count, 1)
            backend.client.close.assert_called_once()
            stream.close.assert_called_once()
            stopper.request("GET", f"/api/research/{run_id}")
            replay = [json.loads(line) for line in stopper.getresponse().read().splitlines()]
            self.assertEqual(replay, events)

    def test_stop_checks_local_origin_and_unknown_runs(self):
        run = ResearchRun(str(uuid4()), "Revenue")
        self.server.runs[run.id] = run
        for path, headers, expected in [
            (f"/api/research/{run.id}/stop", {"Origin": "https://foreign.example"}, 403),
            (f"/api/research/{uuid4()}/stop", {}, 404),
            ("/api/research/invalid/stop", {}, 400),
        ]:
            self.connection.request("POST", path, headers=headers)
            response = self.connection.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
        self.assertFalse(run.cancellation.event.is_set())
        run.done = True
        self.connection.request("POST", f"/api/research/{run.id}/stop")
        result = json.loads(self.connection.getresponse().read())
        self.assertEqual(result["status"], "finished")
        self.assertFalse(run.cancellation.event.is_set())

    def test_model_catalog_uses_aliyun_account_and_returns_only_fetched_ids(self):
        models = ["kimi/kimi-k3", "qwen3.8-max"]
        with patch.dict(os.environ, {"ALIYUN_API_KEY": "secret-aliyun"}, clear=True), \
                patch("deepsearchagent.ui.list_models", return_value=models) as discovery, \
                patch("deepsearchagent.ui.AliyunBackend") as backend:
            self.connection.request("GET", "/api/models?provider=aliyun")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read().decode()
            self.assertEqual(json.loads(body), {"provider": "aliyun", "models": models})
            config, key = discovery.call_args.args
            self.assertEqual(key, "secret-aliyun")
            self.assertEqual(config.base_url, "https://maas.qianwenaiapi.com/compatible-mode/v1")
            self.assertNotIn("secret-aliyun", body)
            self.assertFalse(self.server.run_lock.locked())
            backend.assert_not_called()

    def test_model_catalog_uses_kimi_account_and_status_contains_no_static_models(self):
        models = ["account-kimi", "kimi-k3"]
        with patch.dict(os.environ, {"LLM_PROVIDER": "kimi", "KIMI_API_KEY": "secret-kimi", "ALIYUN_API_KEY": "secret-aliyun",
                                    "KIMI_BASE_URL": "https://api.moonshot.ai/v1"}, clear=True), \
                patch("deepsearchagent.ui.list_models", return_value=models) as discovery, \
                patch("deepsearchagent.ui.KimiBackend") as backend:
            self.connection.request("GET", "/api/status")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            status = json.loads(response.read())
            self.assertTrue(all(provider["models"] == [] for provider in status["providers"]))
            discovery.assert_not_called()
            self.connection.request("GET", "/api/models?provider=kimi")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read().decode()
            self.assertEqual(json.loads(body), {"provider": "kimi", "models": models})
            config, key = discovery.call_args.args
            self.assertEqual(key, "secret-kimi")
            self.assertEqual(config.base_url, "https://api.moonshot.ai/v1")
            self.assertNotIn("secret-kimi", body)
            self.assertNotIn("secret-aliyun", body)
            backend.assert_not_called()

    def test_model_catalog_rejects_foreign_origin_missing_key_and_invalid_provider(self):
        with patch.dict(os.environ, {}, clear=True), patch("deepsearchagent.ui.list_models") as discovery:
            for path, headers, expected in [
                ("/api/models?provider=aliyun", {"Origin": "https://foreign.example"}, 403),
                ("/api/models?provider=aliyun", {}, 400),
                ("/api/models?provider=unknown", {}, 400),
            ]:
                self.connection.request("GET", path, headers=headers)
                response = self.connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            discovery.assert_not_called()

    def test_model_catalog_errors_are_safe_and_do_not_block_research(self):
        import httpx
        for error, category in ((httpx.ReadTimeout("SECRET"), "timeout"), (RuntimeError("SECRET"), "unexpected_error")):
            with patch.dict(os.environ, {"ALIYUN_API_KEY": "secret-aliyun"}, clear=True), \
                    patch("deepsearchagent.ui.list_models", side_effect=error):
                self.connection.request("GET", "/api/models?provider=aliyun")
                response = self.connection.getresponse()
                self.assertEqual(response.status, 502)
                body = response.read().decode()
                self.assertNotIn("SECRET", body)
                self.assertNotIn("secret-aliyun", body)
                self.assertEqual(json.loads(body)["failure"]["category"], category)
                self.assertFalse(self.server.run_lock.locked())


if __name__ == "__main__":
    unittest.main()
