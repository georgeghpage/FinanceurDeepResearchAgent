"""Loopback fixture UI: production HTTP/orchestrator, scripted paid boundaries."""

import asyncio
import json
import sys
import tempfile
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from deepsearchagent.config import Config
from deepsearchagent.llm import ResponseError
from deepsearchagent.models import EvidencePlan, EvidenceExtraction, ReportDraft
from deepsearchagent.orchestrator import ResearchAgent
from deepsearchagent.ui import ResearchHandler, ResearchServer
from deepsearchagent.tools.search import SearchError
from support_fixtures import AutoBackend, FakeAsyncSearch, FakeJudge

GATES = {}
GATE_LOCK = threading.Lock()


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, tzinfo=timezone.utc)


def wait_stage(question, stage):
    if f"[pause:{stage}]" not in question:
        return
    with GATE_LOCK:
        gate = GATES.setdefault(question, threading.Event())
    if not gate.wait(15):
        raise TimeoutError("Fixture gate was not released")


class MemoryHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.chats = {}

    def create(self, identifier, question):
        with self.lock:
            self.chats[identifier] = {
                "id": identifier,
                "question": question,
                "status": "running",
                "created_at": "2026-10-12T00:00:00+00:00",
                "updated_at": "2026-10-12T00:00:00+00:00",
                "messages": [{"role": "user", "content": question}],
                "events": [],
            }

    def save_event(self, identifier, event):
        with self.lock:
            chat = self.chats[identifier]
            chat["events"].append(event)
            if event["type"] == "result":
                chat["messages"].append(
                    {"role": "assistant", "content": event["report"]}
                )
                chat["status"] = (
                    "cancelled"
                    if event["stop_reason"] == "cancelled"
                    else "completed"
                    if event.get("status") == "COMPLETE"
                    else "failed"
                    if event.get("status") == "FAILED"
                    else "partial"
                )
            elif event["type"] == "error":
                chat["status"] = "failed"

    def list(self, offset=0):
        with self.lock:
            return {
                "chats": [
                    {k: v for k, v in c.items() if k not in ("events", "messages")}
                    for c in list(self.chats.values())[::-1][offset : offset + 50]
                ],
                "has_more": False,
            }

    def get(self, identifier):
        with self.lock:
            chat = self.chats.get(identifier)
            return json.loads(json.dumps(chat)) if chat else None


class BrowserBackend(AutoBackend):
    def __init__(self, *args):
        super().__init__()

    def structured(self, schema, instructions, payload, **options):
        question = json.loads(payload).get("question", "")
        wait_stage(
            question,
            {
                EvidencePlan: "plan",
                EvidenceExtraction: "extract",
                ReportDraft: "write",
            }.get(schema, "audit"),
        )
        if schema is EvidencePlan and "[failure]" in question:
            raise ResponseError(
                "CANARY-INTERNAL-SECRET", category="provider_unavailable"
            )
        result = super().structured(schema, instructions, payload, **options)
        if schema is EvidencePlan:
            for query in result.search_queries:
                query.query = question[:100] + " | " + query.query
        if schema is ReportDraft and "[long]" in question:
            from deepsearchagent.models import ReportSection

            paragraphs = result.sections[0].paragraphs
            result.sections = [
                ReportSection(
                    title=f"Evidence section {i + 1}",
                    paragraphs=[
                        p.model_copy(deep=True) for p in paragraphs for _ in range(1)
                    ],
                )
                for i in range(3)
            ]
        return result


class BrowserSearch(FakeAsyncSearch):
    def __init__(self, *args):
        super().__init__(delay=0)
        self.attempts = {}

    async def asearch(self, query, *, timeout):
        self.attempts[query] = self.attempts.get(query, 0) + 1
        if "[search-unavailable]" in query:
            raise SearchError("CANARY-INTERNAL-SECRET", category="provider_unavailable")
        if "[partial-search]" in query and query.endswith("variant 1"):
            raise SearchError("CANARY-INTERNAL-SECRET", category="provider_unavailable")
        if (
            "[retry]" in query
            and query.endswith("variant 1")
            and self.attempts[query] == 1
        ):
            raise SearchError("CANARY-INTERNAL-SECRET", category="rate_limit")
        if "[pause:search]" in query:
            question = query.split(" | ")[0]
            with GATE_LOCK:
                gate = GATES.setdefault(question, threading.Event())

            async def wait():
                while not gate.is_set():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait(), timeout=15)
        return await super().asearch(query, timeout=timeout)


class BrowserJudge(FakeJudge):
    def evaluate(self, question, *args):
        wait_stage(question, "verify")
        if "[refine]" in question:
            self.decisions = ["RESEARCH_MORE", "READY"]
        return super().evaluate(question, *args)


class FixtureServer(ResearchServer):
    def finish_request(self, request, client_address):
        FixtureHandler(request, client_address, self)


class FixtureHandler(ResearchHandler):
    def do_POST(self):
        if self.path == "/__qa__/reset" and self._local_request():
            if not self.server.run_lock.acquire(blocking=False):
                self._json_body(409, {"busy": True})
                return
            try:
                with GATE_LOCK:
                    GATES.clear()
                with self.server.store.lock:
                    self.server.store.chats.clear()
                self._json_body(200, {"reset": True})
            finally:
                self.server.run_lock.release()
            return
        if self.path == "/__qa__/release" and self._local_request():
            with GATE_LOCK:
                for gate in GATES.values():
                    gate.set()
            self._json_body(200, {"released": True})
            return
        super().do_POST()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with tempfile.TemporaryDirectory() as root, ExitStack() as patches:
        patches.enter_context(patch("deepsearchagent.models.datetime", FixtureDateTime))
        patches.enter_context(
            patch(
                "deepsearchagent.ui.Config.from_env",
                return_value=Config(session_dir=root, search_backoff=0.05),
            )
        )
        patches.enter_context(
            patch("deepsearchagent.ui.require_api_key", return_value="OFFLINE-CANARY")
        )
        patches.enter_context(
            patch(
                "deepsearchagent.ui.require_tavily_key", return_value="OFFLINE-CANARY"
            )
        )
        patches.enter_context(patch("deepsearchagent.ui.KimiBackend", BrowserBackend))
        patches.enter_context(patch("deepsearchagent.ui.AliyunBackend", BrowserBackend))
        patches.enter_context(
            patch("deepsearchagent.ui.TavilyMCPSearch", BrowserSearch)
        )
        patches.enter_context(
            patch("deepsearchagent.ui.list_models", return_value=["kimi-k3"])
        )

        def agent_factory(*args, **kwargs):
            return ResearchAgent(*args, judge=BrowserJudge(), **kwargs)

        patches.enter_context(
            patch("deepsearchagent.ui.ResearchAgent", side_effect=agent_factory)
        )
        server = FixtureServer(("127.0.0.1", port), store=MemoryHistory())
        print(f"Fixture UI: http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
