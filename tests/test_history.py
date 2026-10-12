"""Real PostgreSQL integration checks, isolated in a disposable test schema."""

import http.client
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from deepsearchagent.history import ChatStore
from deepsearchagent.models import ResearchPlan
from deepsearchagent.ui import ResearchRun, ResearchServer
from test_agent import FakeSearch, ScriptedBackend


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostgreSQL checks")
class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "test_" + uuid4().hex
        with psycopg.connect(self.url) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        schema = self.schema

        class IsolatedStore(ChatStore):
            def connect(store):
                return psycopg.connect(store.url, row_factory=dict_row,
                                       options=f"-c search_path={schema} -c statement_timeout=5000")

        self.store = IsolatedStore(self.url)
        self.store.initialize()
        self.server = ResearchServer(("127.0.0.1", 0), store=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        with psycopg.connect(self.url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, data

    def result(self, chat_id, reason="coverage"):
        run = ResearchRun(chat_id, "Question", store=self.store)
        run.emit({"type": "progress", "message": "Planning"})
        run.emit({"type": "result", "report": "# Answer\n\nSaved 收入", "stop_reason": reason})
        return run.events

    def test_messages_trace_and_status_survive_new_store_instance(self):
        chat_id = str(uuid4())
        self.store.create(chat_id, "What's revenue? <script>")
        events = self.result(chat_id, "error")
        fresh_store = type(self.store)(self.url)
        fresh_store.initialize()
        chat = fresh_store.get(chat_id)
        self.assertEqual(chat["status"], "partial")
        self.assertEqual(chat["events"], events)
        self.assertEqual(chat["messages"], [{"role": "user", "content": "What's revenue? <script>"},
                                             {"role": "assistant", "content": "# Answer\n\nSaved 收入"}])

    def test_history_paginates_beyond_in_memory_retention(self):
        ids = []
        for i in range(7):
            chat_id = str(uuid4())
            ids.append(chat_id)
            self.store.create(chat_id, f"Question {i}")
        first = self.store.list(limit=3)
        second = self.store.list(offset=3, limit=4)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual([chat["id"] for chat in first["chats"] + second["chats"]], ids[::-1])
        self.assertEqual(self.store.list(offset=7)["chats"], [])

    def test_archived_run_replays_and_repeated_post_never_calls_model(self):
        chat_id = str(uuid4())
        self.store.create(chat_id, "Question")
        events = self.result(chat_id)
        with patch("deepsearchagent.ui.KimiBackend") as backend:
            status, data = self.request("POST", "/api/research", {"run_id": chat_id, "question": "Question"})
            self.assertEqual(status, 200)
            self.assertEqual([json.loads(line) for line in data.splitlines()], events)
            backend.assert_not_called()
        status, data = self.request("GET", f"/api/research/{chat_id}?after=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), events[-1])
        status, _ = self.request("POST", "/api/research", {"run_id": chat_id, "question": "Changed"})
        self.assertEqual(status, 409)

    def test_research_is_logged_before_stream_and_readable_in_history_api(self):
        backend = ScriptedBackend(ResearchPlan(objective="No sources", tasks=[]))
        backend.client = Mock()
        with patch("deepsearchagent.ui.require_api_key", return_value="offline"), \
                patch("deepsearchagent.ui.require_tavily_key", return_value="offline"), \
                patch("deepsearchagent.ui.KimiBackend", return_value=backend), \
                patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()):
            status, data = self.request("POST", "/api/research", {"question": "Find revenue"})
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in data.splitlines()]
        chat_id = events[-1]["run_id"]
        status, data = self.request("GET", "/api/chats?offset=0")
        self.assertEqual(json.loads(data)["chats"][0]["id"], chat_id)
        status, data = self.request("GET", f"/api/chats/{chat_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["events"], events)
        self.assertEqual(len(json.loads(data)["messages"]), 2)

    def test_restart_marks_only_unfinished_chats_and_retains_partial_trace(self):
        unfinished, complete = str(uuid4()), str(uuid4())
        self.store.create(unfinished, "Question")
        self.store.save_event(unfinished, {"type": "progress", "sequence": 1, "message": "Planning"})
        self.store.create(complete, "Done")
        self.result(complete)
        self.store.interrupt_unfinished()
        self.store.interrupt_unfinished()
        chat = self.store.get(unfinished)
        self.assertEqual(chat["status"], "interrupted")
        self.assertEqual(len(chat["events"]), 2)
        self.assertEqual(chat["events"][-1]["type"], "error")
        self.assertEqual(self.store.get(complete)["status"], "completed")

    def test_history_errors_and_foreign_origins_are_rejected_safely(self):
        for path, status in [("/api/chats?offset=-1", 400), ("/api/chats?offset=abc", 400),
                             ("/api/chats/not-a-uuid", 400), (f"/api/chats/{uuid4()}", 404)]:
            self.assertEqual(self.request("GET", path)[0], status)
        self.assertEqual(self.request("GET", "/api/chats", headers={"Origin": "https://foreign.example"})[0], 403)
        with patch.object(self.store, "list", side_effect=RuntimeError("SECRET-DB-PASSWORD")):
            status, data = self.request("GET", "/api/chats")
        self.assertEqual(status, 503)
        self.assertNotIn(b"SECRET-DB-PASSWORD", data)

    def test_database_write_failure_keeps_live_report_and_warns_user(self):
        run = ResearchRun(str(uuid4()), "Question", store=self.store)
        with patch.object(self.store, "save_event", side_effect=RuntimeError("SECRET")):
            run.emit({"type": "result", "report": "Report"})
        self.assertEqual(run.events[-1]["report"], "Report")
        self.assertIn("history_error", run.events[-1])
        self.assertNotIn("SECRET", json.dumps(run.events))


if __name__ == "__main__":
    unittest.main()

    def test_explicit_partial_failed_and_cancelled_results_survive_storage(self):
        for state, reason, expected in (("PARTIAL", "coverage", "partial"), ("FAILED", "error", "failed"), ("CANCELLED", "cancelled", "cancelled")):
            chat_id = str(uuid4())
            self.store.create(chat_id, "Question")
            self.store.save_event(chat_id, {"type": "result", "sequence": 1, "report": "# Result", "status": state, "stop_reason": reason})
            self.assertEqual(self.store.get(chat_id)["status"], expected)
